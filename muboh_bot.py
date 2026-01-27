import os
import re
import asyncio
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

# =====================
# CONFIG
# =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Halol / Muboh coinlar (keyinchalik kengaytirasan)
MUBOH_COINS = {
    "BTC", "ETH", "BNB", "SOL", "ADA",
    "XRP", "LTC", "DOT", "TRX", "AVAX",
}

TICKER_RE = re.compile(r"^[A-Z0-9]{2,10}$")

# =====================
# HANDLER
# =====================
async def check_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip().upper()

    # faqat ticker bo‘lsa (BTC, ETH, SOL)
    if not TICKER_RE.match(text):
        return

    if text in MUBOH_COINS:
        reply = (
            f"{text}\n\n"
            "MUBOH 🟢\n"
            "Manba: CrypoIslam / HukmCrypto"
        )
    else:
        reply = (
            f"{text}\n\n"
            "NOMUBOH 🔴\n"
            "Manba: CrypoIslam / HukmCrypto"
        )

    await update.message.reply_text(reply)

# =====================
# MAIN
# =====================
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, check_coin)
    )

    print("✅ Muboh bot ishga tushdi")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
