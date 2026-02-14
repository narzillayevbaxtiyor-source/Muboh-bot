import os
import time
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ======================
# ENV
# ======================
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

TOP_N = int(os.getenv("TOP_N") or "50")

REFRESH_TOP_SEC = float(os.getenv("REFRESH_TOP_SEC") or "120")
REFRESH_KLINES_SEC = float(os.getenv("REFRESH_KLINES_SEC") or "90")
SCAN_PRICE_SEC = float(os.getenv("SCAN_PRICE_SEC") or "3")

BINANCE_BASE = (os.getenv("BINANCE_BASE") or "https://data-api.binance.vision").strip()

# 1D bullish yaqinlashish: close cloud_top'dan qancha past bo'lsa "near"
NEAR_D_PCT = float(os.getenv("NEAR_D_PCT") or "0.015")  # 1.5%

STATE_FILE = os.getenv("STATE_FILE") or "state_ichimoku.json"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID yo'q")

# ======================
# DATA
# ======================
@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

def now_ms() -> int:
    return int(time.time() * 1000)

# ======================
# STATE
# ======================
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"symbols": {}, "top_symbols": [], "last_top_refresh_ms": 0}

def save_state(st: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def sym_state(st: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    s = st["symbols"].get(symbol)
    if not s:
        s = {
            # cached last closed candle open_time for each TF
            "last_1d_closed_open": None,
            "last_4h_closed_open": None,
            "last_15m_closed_open": None,

            # stages
            "armed_1d": None,          # 1D ARM event id
            "buy_done_4h": None,       # 4H BUY event id
            "sell_done_15m": None,     # 15m SELL event id

            # position state
            "in_position": False,

            # for spam control (per setup cycle)
            "setup_id": None,          # string id for current setup cycle (based on 1D closed candle)
        }
        st["symbols"][symbol] = s
    return s

# ======================
# HTTP / TG
# ======================
async def http_get_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        return await r.json()

async def tg_send_text(session: aiohttp.ClientSession, text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=20)) as r:
        await r.text()

# ======================
# BINANCE
# ======================
async def get_top_gainers(session: aiohttp.ClientSession, top_n: int) -> List[str]:
    url = f"{BINANCE_BASE}/api/v3/ticker/24hr"
    data = await http_get_json(session, url)

    usdt = []
    for x in data:
        sym = x.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym.endswith("BUSDUSDT") or sym.endswith("USDCUSDT"):
            continue
        if "UPUSDT" in sym or "DOWNUSDT" in sym or "BULLUSDT" in sym or "BEARUSDT" in sym:
            continue
        try:
            pct = float(x.get("priceChangePercent", "0") or "0")
        except Exception:
            continue
        usdt.append((sym, pct))

    usdt.sort(key=lambda t: t[1], reverse=True)
    return [s for s, _ in usdt[:top_n]]

async def get_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int) -> List[Candle]:
    url = f"{BINANCE_BASE}/api/v3/klines"
    raw = await http_get_json(session, url, params={"symbol": symbol, "interval": interval, "limit": str(limit)})
    out: List[Candle] = []
    for k in raw:
        out.append(Candle(
            open_time=int(k[0]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
            close_time=int(k[6]),
        ))
    return out

async def get_price_map(session: aiohttp.ClientSession, symbols: List[str]) -> Dict[str, float]:
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    data = await http_get_json(session, url)
    wanted = set(symbols)
    mp: Dict[str, float] = {}
    for x in data:
        sym = x.get("symbol")
        if sym in wanted:
            mp[sym] = float(x["price"])
    return mp

def last_closed(candles: List[Candle]) -> Candle:
    if len(candles) < 2:
        raise ValueError("Not enough candles")
    return candles[-2]

# ======================
# ICHIMOKU
# ======================
def midpoint(highs: List[float], lows: List[float]) -> float:
    return (max(highs) + min(lows)) / 2.0

def ichimoku_lines(closed: List[Candle]) -> Optional[Dict[str, Any]]:
    """
    closed: ONLY closed candles (forming removed)
    returns dict with:
      tenkan, kijun, senkou_a, senkou_b (current computed from past),
      cloud_top, cloud_bottom,
      chikou_ok (lagging confirmation, optional)
    """
    # need at least 52 + 26 + a bit
    if len(closed) < 80:
        return None

    highs = [c.high for c in closed]
    lows  = [c.low  for c in closed]
    closes = [c.close for c in closed]

    # Tenkan (9)
    tenkan = midpoint(highs[-9:], lows[-9:])
    # Kijun (26)
    kijun = midpoint(highs[-26:], lows[-26:])

    # Senkou A = (Tenkan + Kijun)/2 shifted +26
    senkou_a = (tenkan + kijun) / 2.0

    # Senkou B = midpoint(52) shifted +26
    senkou_b = midpoint(highs[-52:], lows[-52:])

    cloud_top = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)

    # Chikou confirmation: current close > close[-26]
    chikou_ok = False
    if len(closes) >= 27:
        chikou_ok = closes[-1] > closes[-27]

    return {
        "tenkan": tenkan,
        "kijun": kijun,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "cloud_top": cloud_top,
        "cloud_bottom": cloud_bottom,
        "chikou_ok": chikou_ok,
    }

# ======================
# CORE LOGIC
# ======================
async def refresh_tf_cache(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str, cache: Dict[str, Any]) -> None:
    """
    Cache ichida 1d/4h/15m closed candles + ichimoku lines saqlaydi.
    """
    cache.setdefault(symbol, {})

    # intervals to refresh
    for tf, limit in [("1d", 200), ("4h", 200), ("15m", 200)]:
        # refresh cadence
        entry = cache[symbol].get(tf)
        tnow = now_ms()
        if entry and (tnow - entry["t"] < int(REFRESH_KLINES_SEC * 1000)):
            continue

        candles = await get_klines(session, symbol, tf, limit)
        closed = candles[:-1]  # forming removed
        if len(closed) < 80:
            cache[symbol][tf] = {"t": tnow, "closed": closed, "ichi": None}
            continue

        ichi = ichimoku_lines(closed)
        cache[symbol][tf] = {"t": tnow, "closed": closed, "ichi": ichi}

        ss = sym_state(st, symbol)
        lc = closed[-1]
        if tf == "1d":
            ss["last_1d_closed_open"] = lc.open_time
        elif tf == "4h":
            ss["last_4h_closed_open"] = lc.open_time
        elif tf == "15m":
            ss["last_15m_closed_open"] = lc.open_time

async def handle_signals(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str, price: float, cache: Dict[str, Any]) -> None:
    ss = sym_state(st, symbol)
    tf1 = cache.get(symbol, {}).get("1d", {})
    tf4 = cache.get(symbol, {}).get("4h", {})
    tf15 = cache.get(symbol, {}).get("15m", {})

    ichi1 = tf1.get("ichi")
    ichi4 = tf4.get("ichi")
    ichi15 = tf15.get("ichi")

    if not ichi1 or not ichi4 or not ichi15:
        return

    # -----------------------
    # 1D ARM (bullish yaqinlashish)
    # -----------------------
    closed1 = tf1["closed"]
    last1 = closed1[-1]
    setup_id = f"{symbol}:{last1.open_time}"  # current daily cycle

    tenkan1 = ichi1["tenkan"]
    kijun1 = ichi1["kijun"]
    cloud_top1 = ichi1["cloud_top"]
    cloud_green1 = ichi1["senkou_a"] > ichi1["senkou_b"]
    chikou_ok1 = ichi1["chikou_ok"]

    # "yaqinlashsa" = close cloud_top'ga yaqin + bullish structure
    near_level_1d = cloud_top1 * (1.0 - NEAR_D_PCT)
    near_cloud_1d = (last1.close >= near_level_1d) and (last1.close < cloud_top1)

    bullish_1d_near = (tenkan1 > kijun1) and cloud_green1 and chikou_ok1 and near_cloud_1d

    # ARM only once per daily cycle
    if bullish_1d_near and ss.get("setup_id") != setup_id and not ss.get("in_position"):
        ss["setup_id"] = setup_id
        ss["armed_1d"] = setup_id
        ss["buy_done_4h"] = None
        ss["sell_done_15m"] = None

        remain_pct = (cloud_top1 - last1.close) / cloud_top1 * 100.0
        await tg_send_text(
            session,
            f"🟨 ICHIMOKU ARM (1D bullish near)\n"
            f"{symbol}\n"
            f"1D close={last1.close}\n"
            f"cloud_top={cloud_top1:.6f} | remain={remain_pct:.2f}% (near {NEAR_D_PCT*100:.2f}%)\n"
            f"tenkan>kijun={tenkan1>kijun1} | cloud_green={cloud_green1} | chikou_ok={chikou_ok1}"
        )

    # -----------------------
    # 4H BUY (only if armed)
    # -----------------------
    armed = ss.get("armed_1d") == ss.get("setup_id") and ss.get("setup_id") is not None
    closed4 = tf4["closed"]
    last4 = closed4[-1]

    tenkan4 = ichi4["tenkan"]
    kijun4 = ichi4["kijun"]
    cloud_top4 = ichi4["cloud_top"]

    buy_cond_4h = armed and (tenkan4 > kijun4) and (last4.close > cloud_top4)

    if buy_cond_4h and not ss.get("in_position"):
        # buy only once per setup_id
        if ss.get("buy_done_4h") != ss.get("setup_id"):
            ss["buy_done_4h"] = ss.get("setup_id")
            ss["in_position"] = True
            await tg_send_text(
                session,
                f"✅ BUY (Ichimoku 4H)\n"
                f"{symbol}\n"
                f"4H close={last4.close}\n"
                f"cloud_top_4h={cloud_top4:.6f}\n"
                f"tenkan>kijun={tenkan4>kijun4}"
            )

    # -----------------------
    # 15m SELL (only after BUY / in_position)
    # -----------------------
    if ss.get("in_position"):
        closed15 = tf15["closed"]
        last15 = closed15[-1]

        tenkan15 = ichi15["tenkan"]
        kijun15 = ichi15["kijun"]

        sell_cond_15m = (tenkan15 < kijun15) or (last15.close < kijun15)

        if sell_cond_15m:
            # sell only once per setup_id
            if ss.get("sell_done_15m") != ss.get("setup_id"):
                ss["sell_done_15m"] = ss.get("setup_id")
                ss["in_position"] = False
                await tg_send_text(
                    session,
                    f"🟥 SELL (Ichimoku 15m)\n"
                    f"{symbol}\n"
                    f"15m close={last15.close}\n"
                    f"kijun_15m={kijun15:.6f}\n"
                    f"tenkan<kijun={tenkan15<kijun15}"
                )

# ======================
# LOOPS
# ======================
async def loop_refresh_top(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        try:
            syms = await get_top_gainers(session, TOP_N)
            st["top_symbols"] = syms
            st["last_top_refresh_ms"] = now_ms()
            print(f"✅ Top {TOP_N} gainers updated. Tracking: {len(syms)}")
            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Top refresh error: {type(e).__name__}: {e}")
        await asyncio.sleep(REFRESH_TOP_SEC)

async def loop_refresh_klines(session: aiohttp.ClientSession, st: Dict[str, Any], cache: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(2)
            continue
        for symbol in syms:
            try:
                await refresh_tf_cache(session, st, symbol, cache)
            except Exception as e:
                print("refresh_tf_cache error", symbol, e)
        save_state(st)
        await asyncio.sleep(REFRESH_KLINES_SEC)

async def loop_prices(session: aiohttp.ClientSession, st: Dict[str, Any], cache: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(1)
            continue
        try:
            price_map = await get_price_map(session, syms)
            for symbol, price in price_map.items():
                try:
                    await handle_signals(session, st, symbol, price, cache)
                except Exception as e:
                    print("signal error", symbol, e)
            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Price loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(SCAN_PRICE_SEC)

async def main():
    st = load_state()
    cache: Dict[str, Any] = {}

    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=60, ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        await tg_send_text(
            session,
            "🚀 Ichimoku bot started: Top50 gainers | 1D ARM (bullish near cloud) -> 4H BUY (close above cloud + tenkan>kijun) -> 15m SELL (tenkan<kijun or close<kijun)"
        )

        tasks = [
            asyncio.create_task(loop_refresh_top(session, st)),
            asyncio.create_task(loop_refresh_klines(session, st, cache)),
            asyncio.create_task(loop_prices(session, st, cache)),
        ]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
