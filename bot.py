import os
import time
import requests
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env yo'q")
if not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_CHAT_ID env yo'q")

TOP_N = int(os.getenv("TOP_N", "50"))                 # Top 50
NEAR_PCT = float(os.getenv("NEAR_PCT", "2.0"))        # 2%

REF_SYMBOL = (os.getenv("REF_SYMBOL", "BTCUSDT") or "BTCUSDT").strip().upper()

# pacing
LOOP_SLEEP_SEC = float(os.getenv("LOOP_SLEEP_SEC", "2"))
PRICE_REFRESH_SEC = float(os.getenv("PRICE_REFRESH_SEC", "10"))
CLOSE_CHECK_SEC = float(os.getenv("CLOSE_CHECK_SEC", "120"))
TOP_REFRESH_SEC = float(os.getenv("TOP_REFRESH_SEC", "300"))
HEARTBEAT_SEC = float(os.getenv("HEARTBEAT_SEC", "60"))

# batch
BATCH_1D = int(os.getenv("BATCH_1D", "12"))

# filters
BAD_PARTS = ("UPUSDT", "DOWNUSDT", "BULL", "BEAR", "3L", "3S", "5L", "5S")
STABLE_STABLE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT"}

# Binance base fallbacks (region/451 muammosi uchun)
BINANCE_BASES = [
    (os.getenv("BINANCE_BASE_URL") or "").strip() or "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "top50-1m1w-armed-1d-high-near/1.3"})


# =========================
# TELEGRAM
# =========================
def tg_send(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = SESSION.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=25,
        )
        if r.status_code != 200:
            print("[TG SEND ERROR]", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        print("[TG SEND EXC]", e)
        return False


# =========================
# BINANCE (fallback)
# =========================
def fetch_json(path: str, params=None):
    last_err = None
    for base in BINANCE_BASES:
        try:
            url = base.rstrip("/") + path
            r = SESSION.get(url, params=params, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            continue
    raise last_err

def fetch_klines(symbol: str, interval: str, limit: int = 2):
    return fetch_json(
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )

def kline_to_ohlc(k) -> Tuple[int, float, float, float, float, int]:
    # openTime, open, high, low, close, closeTime
    return int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), int(k[6])

def last_closed_close_time(symbol: str, interval: str) -> int:
    kl = fetch_klines(symbol, interval, 2)
    last_closed = kline_to_ohlc(kl[-2])
    return last_closed[5]

def fetch_all_prices() -> Dict[str, float]:
    arr = fetch_json("/api/v3/ticker/price")
    out = {}
    for x in arr:
        try:
            out[x["symbol"]] = float(x["price"])
        except:
            pass
    return out

def remaining_pct_to_level(price: float, level: float) -> float:
    # 0 demak levelga yetdi yoki tepada
    if level <= 0:
        return 999.0
    return ((level - price) / level) * 100.0


# =========================
# SPOT filter set
# =========================
SPOT_USDT: Set[str] = set()

def load_spot_usdt_set():
    info = fetch_json("/api/v3/exchangeInfo")
    ok = set()
    for s in info.get("symbols", []):
        sym = s.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if s.get("status") != "TRADING":
            continue
        if sym in STABLE_STABLE or any(x in sym for x in BAD_PARTS):
            continue
        if s.get("quoteAsset") != "USDT":
            continue

        perms = s.get("permissions", [])
        psets = s.get("permissionSets", [])
        is_spot = ("SPOT" in perms) or any(("SPOT" in ps) for ps in psets)
        if is_spot:
            ok.add(sym)
    return ok

def get_top_gainers_usdt(top_n: int) -> List[str]:
    data = fetch_json("/api/v3/ticker/24hr")
    items = []
    for d in data:
        sym = d.get("symbol", "")
        if sym not in SPOT_USDT:
            continue
        try:
            pct = float(d.get("priceChangePercent", "0"))
        except:
            continue
        items.append((pct, sym))
    items.sort(reverse=True, key=lambda x: x[0])
    return [s for _, s in items[:top_n]]


# =========================
# STATE
# =========================
@dataclass
class Watch:
    level: float
    sent: bool = False
    broken_up: bool = False  # price >= level bo'lib tepaga chiqdimi (retestda ham BUY yo'q)

@dataclass
class BotState:
    last_1m_close: Optional[int] = None
    last_1w_close: Optional[int] = None
    last_1d_close: Optional[int] = None

    month_armed: bool = False
    week_armed: bool = False

    top_symbols: List[str] = field(default_factory=list)
    last_top_refresh_ts: float = 0.0

    watch_1d_high: Dict[str, Watch] = field(default_factory=dict)

    need_load_1d: bool = False
    load_1d_index: int = 0

ST = BotState()


# =========================
# TOP50 refresh
# =========================
def refresh_top50(force: bool = False):
    now = time.time()
    if (not force) and ST.top_symbols and (now - ST.last_top_refresh_ts) < TOP_REFRESH_SEC:
        return
    ST.top_symbols = get_top_gainers_usdt(TOP_N)
    ST.last_top_refresh_ts = now


# =========================
# EVENTS
# =========================
def on_monthly_close():
    ST.month_armed = True
    print("[ARM] monthly armed")

def on_weekly_close():
    ST.week_armed = True
    print("[ARM] weekly armed")

def on_daily_close():
    # Har safar 1D yopilganda: Top50 yangilanadi va 1D high lar qayta yuklanadi
    refresh_top50(force=True)
    ST.watch_1d_high.clear()
    ST.need_load_1d = True
    ST.load_1d_index = 0
    print("[CYCLE] daily close -> reload 1d highs")


# =========================
# LOAD daily highs (batch)
# =========================
def load_batch_1d_highs():
    if not ST.need_load_1d:
        return
    if not ST.top_symbols:
        return

    end = min(ST.load_1d_index + BATCH_1D, len(ST.top_symbols))
    batch = ST.top_symbols[ST.load_1d_index:end]

    ok = 0
    for sym in batch:
        try:
            kl = fetch_klines(sym, "1d", 2)
            last_closed = kline_to_ohlc(kl[-2])
            _, _, high, _, _, _ = last_closed
            ST.watch_1d_high[sym] = Watch(level=high)
            ok += 1
        except Exception:
            pass

    ST.load_1d_index = end
    print(f"[LOAD] 1d highs batch {ST.load_1d_index}/{len(ST.top_symbols)} ok={ok}")

    if ST.load_1d_index >= len(ST.top_symbols):
        ST.need_load_1d = False
        print("[LOAD] 1d highs done")


# =========================
# SIGNALS
# =========================
def check_signals(prices: Dict[str, float]):
    # faqat 1M + 1W armed bo'lsa ishlasin
    if not (ST.month_armed and ST.week_armed):
        return

    for sym, w in ST.watch_1d_high.items():
        price = prices.get(sym)
        if price is None:
            continue

        # tepaga yorib o'tsa -> retestda ham signal yo'q
        if price >= w.level:
            w.broken_up = True
            continue

        if w.sent or w.broken_up:
            continue

        rem = remaining_pct_to_level(price, w.level)
        if 0.0 <= rem <= NEAR_PCT:
            w.sent = True
            tg_send(
                f"📈 <b>BUY</b> <b>{sym}</b>\n"
                f"<b>1d high near</b> ({NEAR_PCT:.2f}%)\n"
                f"Qolgan: <b>{rem:.4f}%</b>"
            )


# =========================
# MAIN
# =========================
def main():
    global SPOT_USDT

    print("[BOOT] loading SPOT universe...")
    SPOT_USDT = load_spot_usdt_set()
    print(f"[BOOT] spot_usdt={len(SPOT_USDT)}")

    ok = tg_send("✅ Top50: 1M+1W armed -> 1D high'ga 2% qolganda BUY bot start.")
    print("[BOOT] telegram start msg:", ok)

    # initial top + initial daily highs (hoziroq ishga tushsin)
    refresh_top50(force=True)

    # ---- STARTDA ARM QILIB OLISH (kutib o'tirmaslik uchun) ----
    try:
        ST.last_1m_close = last_closed_close_time(REF_SYMBOL, "1M")
        ST.last_1w_close = last_closed_close_time(REF_SYMBOL, "1w")
        ST.month_armed = True
        ST.week_armed = True
        print("[ARM] boot armed=True (1M+1W)")
    except Exception as e:
        print("[ARM] boot arm failed:", e)
    # ----------------------------------------------------------

    # startdayoq 1D cycle ni ishga tushirib, high larni yuklab olamiz
    try:
        ST.last_1d_close = last_closed_close_time(REF_SYMBOL, "1d")
        on_daily_close()
    except Exception as e:
        print("[BOOT] initial daily load failed:", e)

    last_prices_ts = 0.0
    last_close_ts = 0.0
    last_hb = 0.0
    backoff = 1.0

    while True:
        try:
            now = time.time()

            if now - last_hb >= HEARTBEAT_SEC:
                last_hb = now
                print(f"[HB] alive | armed={ST.month_armed and ST.week_armed} | top={len(ST.top_symbols)} | watch={len(ST.watch_1d_high)}")

            refresh_top50(force=False)

            # close checks (1M/1W/1D) — keyingi cycle lar uchun
            if (now - last_close_ts) >= CLOSE_CHECK_SEC:
                last_close_ts = now

                cur_1m = last_closed_close_time(REF_SYMBOL, "1M")
                if ST.last_1m_close is None:
                    ST.last_1m_close = cur_1m
                elif cur_1m != ST.last_1m_close:
                    ST.last_1m_close = cur_1m
                    on_monthly_close()

                cur_1w = last_closed_close_time(REF_SYMBOL, "1w")
                if ST.last_1w_close is None:
                    ST.last_1w_close = cur_1w
                elif cur_1w != ST.last_1w_close:
                    ST.last_1w_close = cur_1w
                    on_weekly_close()

                cur_1d = last_closed_close_time(REF_SYMBOL, "1d")
                if ST.last_1d_close is None:
                    ST.last_1d_close = cur_1d
                    on_daily_close()
                elif cur_1d != ST.last_1d_close:
                    ST.last_1d_close = cur_1d
                    on_daily_close()

            load_batch_1d_highs()

            if (now - last_prices_ts) >= PRICE_REFRESH_SEC:
                last_prices_ts = now
                prices = fetch_all_prices()
                check_signals(prices)

            backoff = 1.0

        except Exception as e:
            print("[ERROR]", e)
            time.sleep(min(60.0, backoff))
            backoff *= 2.0

        time.sleep(LOOP_SLEEP_SEC)


if __name__ == "__main__":
    main()
