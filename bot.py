# bot.py — Expense Tracker Telegram Bot (Render + PostgreSQL version)

import logging, os, sys, json, asyncio
from decimal import Decimal, InvalidOperation
from datetime import datetime
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)
import psycopg

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === ENV VARIABLES ===
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
RUN_MODE = os.getenv("RUN_MODE", "polling")
PUBLIC_URL = os.getenv("PUBLIC_URL")

if not TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN is missing.")
    sys.exit(1)
if not DATABASE_URL:
    logger.error("❌ DATABASE_URL is missing.")
    sys.exit(1)

# === DB SETUP ===
async def init_db():
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT NOT NULL,
                    amount NUMERIC(10,2) NOT NULL,
                    label TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await conn.commit()
def init_db_sync():
    with psycopg.connect(os.getenv("DATABASE_URL"), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT NOT NULL,
                    amount NUMERIC(10,2) NOT NULL,
                    label TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
# === CONSTANTS ===
NEEDS_TARGET = Decimal("500")
WANTS_TARGET = Decimal("300")

NEEDS_SUBCATS = ["Food", "Subscription", "Transport", "Groceries", "Misc Needs"]
WANTS_SUBCATS = ["Dining Out", "Alcohol", "Dates", "Gifts", "Clothes", "Misc Wants"]

MENU_TEXT = (
    "Here’s what I can do:\n"
    "/start → log an expense\n"
    "/needs → see Needs totals\n"
    "/wants → see Wants totals\n"
    "/undo → undo last entry\n"
    "/restart → clear all your data"
)

AMOUNT, CATEGORY, SUBCATEGORY, LABEL = range(4)

# === HELPERS ===
def _fmt_money(x: Decimal) -> str:
    return f"${x.quantize(Decimal('0.01'))}"

async def db_exec(query, args=(), fetch=False):
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, args)
            if fetch:
                return await cur.fetchall()
            await conn.commit()

# === CONVERSATION FLOW ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How much did you spend? (e.g., 12.50)", reply_markup=ReplyKeyboardRemove()
    )
    return AMOUNT

async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = Decimal(update.message.text)
        if amt <= 0:
            raise InvalidOperation
    except Exception:
        await update.message.reply_text("Please enter a valid positive number.")
        return AMOUNT

    context.user_data["amount"] = amt
    kb = [[KeyboardButton("Needs")], [KeyboardButton("Wants")]]
    await update.message.reply_text(
        f"Amount noted: {_fmt_money(amt)}.\nIs this a Need or a Want?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return CATEGORY

async def get_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip().lower()
    if choice not in {"needs", "wants"}:
        await update.message.reply_text("Please choose Needs or Wants.")
        return CATEGORY

    context.user_data["cat"] = choice
    subcats = NEEDS_SUBCATS if choice == "needs" else WANTS_SUBCATS
    rows = [subcats[i:i+2] for i in range(0, len(subcats), 2)]
    kb = [[KeyboardButton(x) for x in row] for row in rows] + [[KeyboardButton("⬅ Back")]]
    await update.message.reply_text(
        "Pick a subcategory:", 
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
    )
    return SUBCATEGORY

async def get_subcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sub = update.message.text.strip()
    cat = context.user_data["cat"]
    amt: Decimal = context.user_data["amount"]

    if sub == "⬅ Back":
        kb = [[KeyboardButton("Needs")], [KeyboardButton("Wants")]]
        await update.message.reply_text(
            "Choose again: Needs or Wants?",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
        return CATEGORY

    valid = NEEDS_SUBCATS if cat == "needs" else WANTS_SUBCATS
    if sub not in valid:
        await update.message.reply_text("Please pick from the buttons.")
        return SUBCATEGORY

    context.user_data["subcat"] = sub

    if sub.startswith("Misc"):
        await update.message.reply_text("Enter a short label for this Misc item:")
        return LABEL

    await save_expense(update, context)
    return ConversationHandler.END

async def get_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = update.message.text.strip()
    context.user_data["label"] = label
    await save_expense(update, context)
    return ConversationHandler.END

async def save_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cat = context.user_data["cat"]
    subcat = context.user_data["subcat"]
    amt = context.user_data["amount"]
    label = context.user_data.get("label")

    await db_exec(
        "INSERT INTO expenses (user_id, category, subcategory, amount, label) VALUES (%s,%s,%s,%s,%s)",
        (uid, cat, subcat, amt, label),
    )

    await update.message.reply_text(
        f"Logged {_fmt_money(amt)} to {cat.title()} → {subcat}{' (' + label + ')' if label else ''}.",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data.clear()

# === COMMANDS ===
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE, cat: str):
    uid = update.effective_user.id
    rows = await db_exec(
        "SELECT subcategory, SUM(amount) FROM expenses WHERE user_id=%s AND category=%s GROUP BY subcategory",
        (uid, cat), fetch=True
    )
    if not rows:
        await update.message.reply_text("No expenses yet.")
        return

    total = sum(r[1] for r in rows)
    target = NEEDS_TARGET if cat == "needs" else WANTS_TARGET
    lines = [f"• {r[0]}: {_fmt_money(r[1])}" for r in rows]
    msg = f"📊 {cat.title()} summary\n" + "\n".join(lines) + f"\n\nTotal: {_fmt_money(total)} / {_fmt_money(target)}"
    await update.message.reply_text(msg)

async def needs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await summary(update, context, "needs")

async def wants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await summary(update, context, "wants")

async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = await db_exec(
        "SELECT id, category, subcategory, amount FROM expenses WHERE user_id=%s ORDER BY id DESC LIMIT 1",
        (uid,), fetch=True
    )
    if not rows:
        await update.message.reply_text("Nothing to undo.")
        return
    last = rows[0]
    await db_exec("DELETE FROM expenses WHERE id=%s", (last[0],))
    await update.message.reply_text(
        f"Removed last entry: {_fmt_money(last[3])} from {last[1].title()} → {last[2]}."
    )

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await db_exec("DELETE FROM expenses WHERE user_id=%s", (uid,))
    await update.message.reply_text("All your data cleared. ✅")

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT)

# === MAIN ===
async def main():
    await init_db()

    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_category)],
            SUBCATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_subcategory)],
            LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_label)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("needs", needs))
    app.add_handler(CommandHandler("wants", wants))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu))

    if RUN_MODE == "webhook":
        port = int(os.environ.get("PORT", "10000"))
        logger.info("Running in webhook mode on port %d", port)
        await app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TOKEN,
            webhook_url=f"{PUBLIC_URL}/{TOKEN}",
        )
    else:
        logger.info("Running in polling mode")
        await app.run_polling()

if __name__ == "__main__":
    # 1) Make sure the table exists (sync, safe to call once on boot)
    init_db_sync()

    # 2) Build the bot and handlers
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_category)],
            SUBCATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_subcategory)],
            LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_label)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("needs", needs))
    app.add_handler(CommandHandler("wants", wants))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu))

    # 3) Start the bot (blocking methods — DO NOT await)
    if RUN_MODE == "webhook" and PUBLIC_URL:
        port = int(os.environ.get("PORT", "10000"))
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TOKEN,
            webhook_url=f"{PUBLIC_URL}/{TOKEN}",
        )
    else:
        app.run_polling()

