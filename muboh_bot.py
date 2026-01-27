import os
import re
import json
import time
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# =========================
# ENV
# =========================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
GROUP_CHAT_ID = int((os.getenv("GROUP_CHAT_ID") or "0").strip() or "0")  # -100...
ADMIN_DM_CHAT_ID = int((os.getenv("ADMIN_DM_CHAT_ID") or "0").strip() or "0")  # optional

DB_FILE = os.getenv("DB_FILE", "muboh_db.json").strip()
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "60"))  # tickerga javobni spam qilmaslik
ONLY_USDT_SPOT = os.getenv("ONLY_USDT_SPOT", "1") == "1"

# Kimlardan kelgan “hukm” xabarlarini ishonchli deb qabul qilish (optional)
# Masalan: @HukmCrypto_bot username
TRUST_SOURCE_USERNAMES = set(
    u.strip().lower()
    for u in (os.getenv("TRUST_SOURCE_USERNAMES", "@HukmCrypto_bot") or "").split(",")
    if u.strip()
)

# =========================
# DB
# =========================
@dataclass
class HukmRecord:
    ticker: str
    hukm: str            # "MUBOH" / "NOMUBOH" / "UNKNOWN"
    detail: str = ""     # original text snippet
    source: str = ""     # username/channel title if available
    ts: float = 0.0

def load_db() -> Dict[str, Dict]:
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"records": {}, "last_reply_ts": {}}

def save_db(db: Dict) -> None:
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_FILE)

def db_get_record(db: Dict, ticker: str) -> Optional[HukmRecord]:
    rec = db.get("records", {}).get(ticker.upper())
    if not rec:
        return None
    return HukmRecord(**rec)

def db_set_record(db: Dict, rec: HukmRecord) -> None:
    db.setdefault("records", {})[rec.ticker.upper()] = asdict(rec)

def cooldown_ok(db: Dict, ticker: str) -> bool:
    last = float(db.get("last_reply_ts", {}).get(ticker.upper(), 0))
    return (time.time() - last) >= COOLDOWN_SECONDS

def mark_replied(db: Dict, ticker: str) -> None:
    db.setdefault("last_reply_ts", {})[ticker.upper()] = time.time()

# =========================
# PARSING
# =========================

TICKER_RE = re.compile(r"^[A-Z0-9]{2,12}$")
# Hukm bot formatidan "MUBOH" topish:
MUBOH_RE = re.compile(r"\bMUBOH\b", re.IGNORECASE)
NOMUBOH_RE = re.compile(r"\b(NOMUBOH|HAROM|SHUBHA|MASHKUK)\b", re.IGNORECASE)

def extract_first_ticker(text: str) -> Optional[str]:
    """
    Guruhga kelgan xabar ichidan birinchi ticker ni oladi.
    Sizning format: "BTC" (USDTsiz)
    """
    if not text:
        return None
    # birinchi token
    token = text.strip().split()[0].strip().upper()
    if TICKER_RE.match(token):
        return token
    return None

def extract_hukm_from_text(text: str) -> Tuple[str, str]:
    """
    text ichidan hukmni ajratadi.
    return: (hukm, detail_snippet)
    """
    if not text:
        return ("UNKNOWN", "")
    t = text.strip()

    if NOMUBOH_RE.search(t):
        return ("NOMUBOH", t[:800])
    if MUBOH_RE.search(t):
        return ("MUBOH", t[:800])

    return ("UNKNOWN", t[:800])

def get_source_username(update: Update) -> str:
    # Forward yoki oddiy muallif
    msg = update.effective_message
    if not msg:
        return ""
    if msg.forward_from and msg.forward_from.username:
        return "@" + msg.forward_from.username.lower()
    if msg.forward_from_chat and msg.forward_from_chat.username:
        return "@" + msg.forward_from_chat.username.lower()
    if msg.from_user and msg.from_user.username:
        return "@" + msg.from_user.username.lower()
    return ""

def looks_like_hukm_message(text: str) -> bool:
    # Hukm bot natijasi odatda MUBOH/NOMUBOH so'zlarini o'z ichiga oladi
    if not text:
        return False
    return bool(MUBOH_RE.search(text) or NOMUBOH_RE.search(text))

# =========================
# BOT LOGIC
# =========================

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    # faqat kerakli guruhdan ishlasin
    if GROUP_CHAT_ID and update.effective_chat and update.effective_chat.id != GROUP_CHAT_ID:
        return

    text = msg.text.strip()

    db = load_db()

    # 1) Agar bu hukm natijasi bo'lsa -> bazaga yozib qo'yamiz
    if looks_like_hukm_message(text):
        # hukm xabar ichidan ticker topish (ko'pincha boshida bo'ladi yoki "LTC - Litecoin" kabi)
        # 1) birinchi token
        ticker = extract_first_ticker(text)

        # 2) "LTC - Litecoin" holati
        if not ticker:
            m = re.match(r"^([A-Z0-9]{2,12})\s*[-–]\s*", text.strip().upper())
            if m:
                ticker = m.group(1)

        hukm, detail = extract_hukm_from_text(text)
        source = get_source_username(update)

        # agar trust ro'yxati qo'yilgan bo'lsa, faqat shulardan kelganini baza qilamiz
        if TRUST_SOURCE_USERNAMES and source and (source.lower() not in TRUST_SOURCE_USERNAMES):
            # bu hukm xabar, lekin ishonchsiz manbadan - bazaga yozmaymiz
            # ammo xohlasangiz admin DMga xabar beramiz
            if ADMIN_DM_CHAT_ID:
                await context.bot.send_message(
                    chat_id=ADMIN_DM_CHAT_ID,
                    text=f"⚠️ Hukm xabar keldi, lekin TRUST ro'yxatida yo'q manba: {source}\n\n{text[:800]}"
                )
            return

        if ticker and hukm in ("MUBOH", "NOMUBOH"):
            rec = HukmRecord(
                ticker=ticker.upper(),
                hukm=hukm,
                detail=detail,
                source=source,
                ts=time.time(),
            )
            db_set_record(db, rec)
            save_db(db)

            # ixtiyoriy: bazaga yozilganini jim turib qo'yamiz (spam qilmasin)
            return

    # 2) Oddiy ticker bo'lsa -> bazadan javob beramiz
    ticker = extract_first_ticker(text)
    if not ticker:
        return

    # USDT spot pipeline bo'lgani uchun: faqat ticker bo'lsa ham qabul qilamiz
    # (xohlasangiz shu yerda black list qo'shish mumkin)
    rec = db_get_record(db, ticker)

    if not rec:
        # Bazada yo'q: jim turamiz (siz xohlasangiz "tekshirilmagan" deb yozib qo'yamiz)
        # Hozir jim: pipeline shovqinsiz bo'lsin
        return

    if not cooldown_ok(db, ticker):
        return

    if rec.hukm == "MUBOH":
        reply = f"{ticker}\n\nMUBOH 🟢"
    elif rec.hukm == "NOMUBOH":
        reply = f"{ticker}\n\nNOMUBOH 🔴"
    else:
        reply = f"{ticker}\n\nNOMA'LUM ⚪️"

    await msg.reply_text(reply)
    mark_replied(db, ticker)
    save_db(db)

# =========================
# MAIN
# =========================
async def post_startup_ping(app: Application):
    if GROUP_CHAT_ID:
        try:
            await app.bot.send_message(chat_id=GROUP_CHAT_ID, text="✅ Muboh bot ishga tushdi (cache baza).")
        except Exception:
            pass

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env required")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_group_message))
    app.post_init = post_startup_ping
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
