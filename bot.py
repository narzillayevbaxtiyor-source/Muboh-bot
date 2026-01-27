import os
import json
import time
import asyncio
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# =======================
# ENV
# =======================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID = int((os.getenv("CHAT_ID") or "0").strip() or "0")

# =======================
# SETTINGS
# =======================
INTERVAL = os.getenv("INTERVAL", "3m")
SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "10"))          # signal tezligi
TOP_REFRESH_SEC = int(os.getenv("TOP_REFRESH_SEC", "180"))       # top10 yangilash
KLINE_LIMIT = int(os.getenv("KLINE_LIMIT", "80"))

# Impulse: (close-open)/open >= IMPULSE_PCT
IMPULSE_PCT = float(os.getenv("IMPULSE_PCT", "0.004"))  # 0.40% default (3m uchun)
REQUIRE_BEARISH_PULLBACK = (os.getenv("REQUIRE_BEARISH_PULLBACK", "1").strip() != "0")

STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Binance API fallback (restricted location bo'lsa ham ko'p joyda ishlaydi)
BINANCE_BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]


# =======================
# Helpers
# =======================
def now_ts() -> int:
    return int(time.time())


def safe_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def http_get_json(path: str, params: Optional[dict] = None, timeout: int = 10):
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
    raise RuntimeError(f"All Binance endpoints failed: {path}. Last error: {last_err}")


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"symbols": {}, "last_top_refresh": 0, "top10": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"symbols": {}, "last_top_refresh": 0, "top10": []}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def format_price(p: float) -> str:
    if p >= 1:
        return f"{p:.6f}".rstrip("0").rstrip(".")
    return f"{p:.10f}".rstrip("0").rstrip(".")


def parse_klines(klines: list) -> List[dict]:
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


# =======================
# Binance data
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
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if sym not in tradable_usdt:
            continue
        if sym.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue
        p = safe_float(t.get("priceChangePercent", 0))
        rows.append((sym, p))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:10]]


def get_klines(symbol: str, interval: str, limit: int) -> List[dict]:
    data = http_get_json("/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
    return parse_klines(data)


def get_spot_price(symbol: str) -> float:
    data = http_get_json("/api/v3/ticker/price", params={"symbol": symbol})
    return safe_float(data.get("price", 0))


# =======================
# Telegram send
# =======================
async def tg_send(app: Application, text: str) -> None:
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception:
        pass


# =======================
# State per symbol
# =======================
@dataclass
class SymbolState:
    stage: str = "WAIT_IMPULSE"   # WAIT_IMPULSE -> WAIT_PULLBACK -> WAIT_BREAK_HIGH -> IN_POSITION
    impulse_candle_close_time: int = 0

    pullback_high: float = 0.0
    pullback_low: float = 0.0
    pullback_close_time: int = 0

    in_position: bool = False
    buy_price: float = 0.0
    buy_time: int = 0

    last_closed_time: int = 0
    last_closed_low: float = 0.0

    last_signal: str = ""


def ensure_symbol_state(state: dict, symbol: str) -> SymbolState:
    s = state["symbols"].get(symbol)
    if not s:
        st = SymbolState()
        state["symbols"][symbol] = asdict(st)
        return st
    # merge defaults safely
    st = SymbolState(**{**asdict(SymbolState()), **s})
    state["symbols"][symbol] = asdict(st)
    return st


def update_symbol_state_dict(state: dict, symbol: str, st: SymbolState) -> None:
    state["symbols"][symbol] = asdict(st)


# =======================
# Trading logic
# =======================
def analyze_symbol(symbol: str, st: SymbolState, klines: List[dict], last_price: float) -> Tuple[SymbolState, Optional[str]]:
    if len(klines) < 5:
        return st, None

    # -1 is current open candle, -2 is last closed candle
    last_closed = klines[-2]

    # Track last closed low (used for SELL)
    if last_closed["close_time"] != st.last_closed_time:
        st.last_closed_time = last_closed["close_time"]
        st.last_closed_low = last_closed["low"]

    signal = None

    def sig_key(kind: str, t: int) -> str:
        return f"{kind}:{t}"

    if st.stage == "WAIT_IMPULSE":
        if is_bull_impulse(last_closed):
            st.stage = "WAIT_PULLBACK"
            st.impulse_candle_close_time = last_closed["close_time"]

    elif st.stage == "WAIT_PULLBACK":
        # find impulse candle index by close_time
        idx = None
        for i in range(len(klines) - 1):  # ignore open candle
            if klines[i]["close_time"] == st.impulse_candle_close_time:
                idx = i
                break

        if idx is None:
            st = SymbolState()
        else:
            # pullback must be the next CLOSED candle after impulse
            if idx + 1 <= len(klines) - 2:
                pb = klines[idx + 1]
                ok_pb = is_bearish(pb) if REQUIRE_BEARISH_PULLBACK else True
                if ok_pb:
                    st.pullback_high = pb["high"]
                    st.pullback_low = pb["low"]
                    st.pullback_close_time = pb["close_time"]
                    st.stage = "WAIT_BREAK_HIGH"
                else:
                    # impulse ketidan pullback yo'q -> reset
                    st = SymbolState()

    elif st.stage == "WAIT_BREAK_HIGH":
        # BUY: price breaks pullback candle HIGH
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
                    f"Pullback HIGH break: {format_price(st.pullback_high)}\n"
                    f"Price: {format_price(last_price)}\n"
                    f"TF: {INTERVAL}"
                )

    elif st.stage == "IN_POSITION":
        # SELL: after BUY, if price breaks below last closed candle LOW
        if st.last_closed_low > 0 and last_price < st.last_closed_low:
            key = sig_key("SELL", st.last_closed_time)
            if st.last_signal != key:
                st.last_signal = key
                signal = (
                    f"{symbol} 🟥 SELL\n"
                    f"Last CLOSED LOW break: {format_price(st.last_closed_low)}\n"
                    f"Price: {format_price(last_price)}\n"
                    f"TF: {INTERVAL}"
                )
            # reset after sell
            st = SymbolState()

    return st, signal


# =======================
# Background scanner loop
# =======================
async def scanner_loop(app: Application) -> None:
    state = load_state()

    # Exchange info once
    tradable_usdt = set()
    try:
        tradable_usdt = get_exchange_info_symbols_usdt()
    except Exception as e:
        await tg_send(app, f"⚠️ exchangeInfo error: {e}")

    top10: List[str] = state.get("top10", []) or []

    while True:
        try:
            # refresh top10
            if now_ts() - int(state.get("last_top_refresh", 0)) >= TOP_REFRESH_SEC or not top10:
                top10 = get_top10_gainers_usdt(tradable_usdt)
                state["top10"] = top10
                state["last_top_refresh"] = now_ts()

                # clear states not in top10
                for sym in list(state.get("symbols", {}).keys()):
                    if sym not in top10:
                        state["symbols"].pop(sym, None)

                save_state(state)

            # scan each symbol
            for sym in top10:
                st = ensure_symbol_state(state, sym)

                kl = get_klines(sym, INTERVAL, KLINE_LIMIT)
                price = get_spot_price(sym)

                st, sig = analyze_symbol(sym, st, kl, price)
                update_symbol_state_dict(state, sym, st)

                if sig:
                    await tg_send(app, sig)

            save_state(state)

        except Exception as e:
            await tg_send(app, f"⚠️ Scan error: {e}")

        await asyncio.sleep(SCAN_EVERY_SEC)


# =======================
# Commands
# =======================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "✅ Bot ishga tushdi.\n"
        f"TF: {INTERVAL}\n"
        "Qoidalar:\n"
        "- Binance SPOT TOP 10 gainers (24h) USDT\n"
        "- Impulse bullish (foiz)\n"
        "- 1 ta bearish pullback candle\n"
        "- BUY: pullback HIGH break\n"
        "- SELL: BUYdan keyin last closed LOW break\n\n"
        "Buyruqlar:\n"
        "/status"
    )
    await update.message.reply_text(txt)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = load_state()
    top10 = st.get("top10", [])
    syms = list(st.get("symbols", {}).keys())
    msg = "📌 TOP10 (24h):\n" + ("\n".join(top10) if top10 else "—")
    msg += "\n\n🧠 Active states:\n" + ("\n".join(syms) if syms else "—")
    await update.message.reply_text(msg)


# =======================
# Main
# =======================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env yo'q")
    if CHAT_ID == 0:
        raise RuntimeError("CHAT_ID env yo'q")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    async def on_startup(app_: Application):
        asyncio.create_task(scanner_loop(app_))

    # PTB 21.x uchun to'g'ri startup hook
    app.post_init = on_startup

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
