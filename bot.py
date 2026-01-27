import os
import json
import time
import asyncio
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# =======================
# ENV
# =======================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID = int((os.getenv("CHAT_ID") or "0").strip() or "0")

# =======================
# SETTINGS (sozlash mumkin)
# =======================
INTERVAL = os.getenv("INTERVAL", "3m")  # 3m
SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "10"))  # tez-tez tekshiradi (signal tez chiqishi uchun)
TOP_REFRESH_SEC = int(os.getenv("TOP_REFRESH_SEC", "180"))  # TOP 10 gainers ro'yxatini yangilash
KLINE_LIMIT = int(os.getenv("KLINE_LIMIT", "80"))

# Impulse sharti: (close-open)/open >= IMPULSE_PCT
IMPULSE_PCT = float(os.getenv("IMPULSE_PCT", "0.004"))  # 0.004 = 0.40% (3m uchun moslash)
# Pullback sharti: pullback candle bearish bo'lishi shart
REQUIRE_BEARISH_PULLBACK = (os.getenv("REQUIRE_BEARISH_PULLBACK", "1").strip() != "0")

STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Binance API fallback
BINANCE_BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",  # ko'p joyda ishlaydi
]


# =======================
# Helpers
# =======================
def now_ts() -> int:
    return int(time.time())


def http_get_json(path: str, params: Optional[dict] = None, timeout: int = 10) -> dict:
    last_err = None
    for base in BINANCE_BASES:
        url = base + path
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All Binance endpoints failed for {path}. Last error: {last_err}")


def safe_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"symbols": {}, "last_top_refresh": 0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"symbols": {}, "last_top_refresh": 0}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


@dataclass
class SymbolState:
    stage: str = "WAIT_IMPULSE"  # WAIT_IMPULSE -> WAIT_PULLBACK -> WAIT_BREAK_HIGH -> IN_POSITION
    impulse_candle_time: int = 0

    pullback_high: float = 0.0
    pullback_low: float = 0.0
    pullback_close_time: int = 0

    in_position: bool = False
    buy_time: int = 0
    buy_price: float = 0.0

    last_closed_time: int = 0
    last_closed_low: float = 0.0

    last_signal: str = ""  # to prevent duplicates


def parse_klines(klines: list) -> List[dict]:
    """
    Binance kline array:
    [
      [
        0 openTime,
        1 open,
        2 high,
        3 low,
        4 close,
        5 volume,
        6 closeTime,
        ...
      ],
      ...
    ]
    """
    out = []
    for k in klines:
        out.append({
            "open_time": int(k[0]),
            "open": safe_float(k[1]),
            "high": safe_float(k[2]),
            "low": safe_float(k[3]),
            "close": safe_float(k[4]),
            "close_time": int(k[6]),
        })
    return out


def is_bull_impulse(c: dict) -> bool:
    if c["open"] <= 0:
        return False
    bullish = c["close"] > c["open"]
    pct = (c["close"] - c["open"]) / c["open"]
    return bullish and pct >= IMPULSE_PCT


def is_bearish(c: dict) -> bool:
    return c["close"] < c["open"]


def format_price(p: float) -> str:
    if p >= 1:
        return f"{p:.6f}".rstrip("0").rstrip(".")
    return f"{p:.10f}".rstrip("0").rstrip(".")


# =======================
# Binance data functions
# =======================
def get_exchange_info_symbols_usdt() -> set:
    data = http_get_json("/api/v3/exchangeInfo")
    ok = set()
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        sym = s.get("symbol", "")
        if sym.endswith("USDT"):
            ok.add(sym)
    return ok


def get_top10_gainers_usdt(tradable_usdt: set) -> List[str]:
    tickers = http_get_json("/api/v3/ticker/24hr")
    # filter USDT & tradable
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if sym not in tradable_usdt:
            continue
        # exclude leveraged tokens / weird (xUP/xDOWN) - xohlasang olib tashla
        if sym.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue
        p = safe_float(t.get("priceChangePercent", 0))
        rows.append((sym, p))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:10]]


def get_klines(symbol: str, interval: str, limit: int) -> List[dict]:
    data = http_get_json("/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
    return parse_klines(data)


def get_mark_price(symbol: str) -> float:
    # spot uchun "ticker/price" yetarli
    data = http_get_json("/api/v3/ticker/price", params={"symbol": symbol})
    return safe_float(data.get("price", 0))


# =======================
# Telegram helpers
# =======================
async def tg_send(app: Application, text: str) -> None:
    if not BOT_TOKEN or CHAT_ID == 0:
        return
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception:
        pass


# =======================
# Core logic
# =======================
def ensure_symbol_state(state: dict, symbol: str) -> SymbolState:
    s = state["symbols"].get(symbol)
    if not s:
        st = SymbolState()
        state["symbols"][symbol] = asdict(st)
        return st
    # merge defaults
    st = SymbolState(**{**asdict(SymbolState()), **s})
    state["symbols"][symbol] = asdict(st)
    return st


def update_symbol_state_dict(state: dict, symbol: str, st: SymbolState) -> None:
    state["symbols"][symbol] = asdict(st)


def analyze_symbol(symbol: str, st: SymbolState, klines: List[dict], last_price: float) -> Tuple[SymbolState, Optional[str]]:
    """
    Qoidalar:
    1) WAIT_IMPULSE: oxirgi yopilgan sham impulse bo'lsa -> WAIT_PULLBACK
    2) WAIT_PULLBACK: keyingi yopilgan sham pullback (bearish) bo'lsa:
         pullback_high/low saqlanadi -> WAIT_BREAK_HIGH
       aks holda: (impulse ketidan pullback kelmasa) reset WAIT_IMPULSE
    3) WAIT_BREAK_HIGH: narx pullback_high dan yuqoriga chiqsa -> BUY va IN_POSITION
    4) IN_POSITION: har safar yangi CLOSED sham kelganda last_closed_low yangilanadi.
       Agar narx last_closed_low dan pastga tushsa -> SELL va reset WAIT_IMPULSE
    """
    if len(klines) < 5:
        return st, None

    # always track last closed candle
    last_closed = klines[-2]  # -1 open candle, -2 closed
    if last_closed["close_time"] != st.last_closed_time:
        st.last_closed_time = last_closed["close_time"]
        st.last_closed_low = last_closed["low"]

    signal = None

    # duplicate-signal guard key
    def sig_key(kind: str, t: int) -> str:
        return f"{kind}:{t}"

    if st.stage == "WAIT_IMPULSE":
        # detect impulse on last closed candle
        if is_bull_impulse(last_closed):
            st.stage = "WAIT_PULLBACK"
            st.impulse_candle_time = last_closed["close_time"]

    elif st.stage == "WAIT_PULLBACK":
        # need the candle right after impulse
        # Find impulse candle index by close_time
        idx = None
        for i in range(len(klines) - 1):  # ignore last open candle
            if klines[i]["close_time"] == st.impulse_candle_time:
                idx = i
                break

        if idx is None:
            st.stage = "WAIT_IMPULSE"
            st.impulse_candle_time = 0
        else:
            # pullback candle should be the next closed candle after impulse
            if idx + 1 <= len(klines) - 2:
                pb = klines[idx + 1]
                ok_pb = (is_bearish(pb) if REQUIRE_BEARISH_PULLBACK else True)
                if ok_pb:
                    st.pullback_high = pb["high"]
                    st.pullback_low = pb["low"]
                    st.pullback_close_time = pb["close_time"]
                    st.stage = "WAIT_BREAK_HIGH"
                else:
                    # impulse ketidan pullback chiqmasa reset
                    st.stage = "WAIT_IMPULSE"
                    st.impulse_candle_time = 0

    elif st.stage == "WAIT_BREAK_HIGH":
        if st.pullback_high > 0 and last_price > st.pullback_high:
            st.stage = "IN_POSITION"
            st.in_position = True
            st.buy_time = now_ts()
            st.buy_price = last_price

            key = sig_key("BUY", st.pullback_close_time)
            if st.last_signal != key:
                st.last_signal = key
                signal = (
                    f"{symbol} ✅ BUY\n"
                    f"Break pullback HIGH: {format_price(st.pullback_high)}\n"
                    f"Price: {format_price(last_price)}\n"
                    f"TF: {INTERVAL}"
                )

    elif st.stage == "IN_POSITION":
        # SELL: price breaks below last closed candle LOW (faqat BUYdan keyin)
        if st.last_closed_low > 0 and last_price < st.last_closed_low:
            key = sig_key("SELL", st.last_closed_time)
            if st.last_signal != key:
                st.last_signal = key
                signal = (
                    f"{symbol} 🟥 SELL\n"
                    f"Break last CLOSED LOW: {format_price(st.last_closed_low)}\n"
                    f"Price: {format_price(last_price)}\n"
                    f"TF: {INTERVAL}"
                )

            # reset after sell
            st = SymbolState()  # clean reset

    return st, signal


# =======================
# Background loops
# =======================
async def scanner_loop(app: Application) -> None:
    state = load_state()
    tradable_usdt = set()

    # exchangeInfo 1 marta olib qo'yamiz (ba'zida og'ir)
    try:
        tradable_usdt = get_exchange_info_symbols_usdt()
    except Exception as e:
        await tg_send(app, f"⚠️ exchangeInfo error: {e}")

    top10: List[str] = []

    while True:
        try:
            # refresh top10 periodically
            if now_ts() - int(state.get("last_top_refresh", 0)) >= TOP_REFRESH_SEC or not top10:
                top10 = get_top10_gainers_usdt(tradable_usdt)
                state["last_top_refresh"] = now_ts()

                # remove states not in top10 (to keep clean)
                for sym in list(state.get("symbols", {}).keys()):
                    if sym not in top10:
                        state["symbols"].pop(sym, None)

                save_state(state)

            # scan each symbol
            for sym in top10:
                st = ensure_symbol_state(state, sym)

                kl = get_klines(sym, INTERVAL, KLINE_LIMIT)
                price = get_mark_price(sym)

                st, sig = analyze_symbol(sym, st, kl, price)
                update_symbol_state_dict(state, sym, st)

                if sig:
                    await tg_send(app, sig)

            save_state(state)

        except Exception as e:
            # don't crash loop
            await tg_send(app, f"⚠️ Scan error: {e}")

        await asyncio.sleep(SCAN_EVERY_SEC)


# =======================
# Telegram commands
# =======================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "✅ Bot ishga tushdi.\n"
        f"TF: {INTERVAL}\n"
        "Qoidalar:\n"
        "- TOP 10 gainers (24h) USDT spot\n"
        "- Impulse (bullish %)\n"
        "- Pullback bearish candle\n"
        "- BUY: pullback HIGH break\n"
        "- SELL: BUYdan keyin last closed LOW break"
    )
    await update.message.reply_text(txt)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = load_state()
    syms = list(st.get("symbols", {}).keys())
    await update.message.reply_text(f"Tracking symbols: {len(syms)}\n" + ("\n".join(syms[:10]) if syms else "—"))


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env yo'q")
    if CHAT_ID == 0:
        raise RuntimeError("CHAT_ID env yo'q")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # background scanner
    app.job_queue.run_once(lambda *_: asyncio.create_task(scanner_loop(app)), when=1)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
