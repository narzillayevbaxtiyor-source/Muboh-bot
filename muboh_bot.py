import os
import re
import json
import time
from typing import Dict, Optional

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
DB_FILE = os.getenv("DB_FILE", "muboh_db.json")

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "60"))

TRUST_SOURCES = {
    "@hukmcrypto_bot",
    "@crypoislam"
}

# =========================
# DB
# =========================
def load_db() -> Dict:
    if not os.path.exists(DB_FILE):
        return {"hukm": {}, "cooldown": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db: Dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

# =========================
# HELPERS
# =========================
TICKER_RE = re.compile(r"\b[A-Z]{2,10}\b")
MUBOH_RE = re.compile(r"\bMUBOH\b", re.I)
NOMUBOH_RE = re.compile(r"\b(HAROM|NOMUBOH|SHUBHA)\b", re.I)

def extract_ticker(text: str) -> Optional[str]:
    m = TICKER_RE.search(text.upper())
    return m.group(0) if m else None

def extract_hukm(text: str) -> Optional[str]:
    if MUBOH_RE.search(text):
        return "MUBOH"
    if NOMUBOH_RE.search(text):
        return "NOMUBOH"
    return None

def get_source(update: Update) -> str:
    msg = update.effective_message
    if msg.forward_from and msg.forward_from.username:
        return "@" + msg.forward_from.username.lower()
    if msg.forward_from_chat and msg.forward_from_chat.username:
        return "@" + msg.forward_from_chat.username.lower()
    if msg.from_user and msg.from_user.username:
        return "@" + msg.from_user.username.lower()
    return ""

# =========================
# HANDLER
# =========================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    if GROUP_CHAT_ID and update.effective_chat.id != GROUP_CHAT_ID:
        return

    text = msg.text.strip()
    db = load_db()

    # 1️⃣ Hukm xabarni o‘rganish
    hukm = extract_hukm(text)
    ticker = extract_ticker(text)
    source = get_source(update)

    if hukm and ticker and source in TRUST_SOURCES:
        db["hukm"][ticker] = {
            "hukm": hukm,
            "source": source,
            "ts": time.time()
        }
        save_db(db)
        return

    # 2️⃣ Oddiy ticker keldi → javob berish
    if not ticker:
        return

    last = db["cooldown"].get(ticker, 0)
    if time.time() - last < COOLDOWN_SECONDS:
        return

    record = db["hukm"].get(ticker)
    if not record:
        return

    if record["hukm"] == "MUBOH":
        reply = f"{ticker}\n\nMUBOH 🟢"
    else:
        reply = f"{ticker}\n\nNOMUBOH 🔴"

    await msg.reply_text(reply)

    db["cooldown"][ticker] = time.time()
    save_db(db)

# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("✅ Muboh bot ishga tushdi", flush=True)
    app.run_polling()

if __name__ == "__main__":
    main()
