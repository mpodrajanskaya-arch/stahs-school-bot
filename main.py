"""
STAHS School Bot — daily digest of school communications via Telegram.
Runs a scheduled digest every evening and supports on-demand commands.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

import portal
import summarizer

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "19"))
DIGEST_MINUTE = int(os.getenv("DIGEST_MINUTE", "0"))
TZ = ZoneInfo(os.getenv("TZ", "Europe/London"))

STATE_FILE = Path(__file__).parent / ".last_seen.txt"


def _load_last_seen() -> str | None:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip() or None
    return None


def _save_last_seen(thread_id: str):
    STATE_FILE.write_text(thread_id)


def _escape(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


async def send_digest(app: Application):
    """Fetch new messages, summarize, send to Telegram."""
    log.info("Running digest...")
    try:
        last_seen = _load_last_seen()
        messages = portal.get_new_communications(last_seen)

        if not messages:
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text="📭 Новых сообщений от школы нет.",
            )
            return

        # Save latest ID immediately
        _save_last_seen(messages[0]["id"])

        # Build summary via Claude (or plain format if no API key)
        summary = await summarizer.summarize(messages)

        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=summary,
            parse_mode=ParseMode.HTML,
        )
        log.info(f"Digest sent: {len(messages)} messages.")

    except Exception as e:
        log.error(f"Digest error: {e}", exc_info=True)
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⚠️ Ошибка при получении дайджеста: {e}",
        )


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я буду присылать тебе дайджест сообщений от школы каждый вечер.\n\n"
        "Команды:\n"
        "/digest — получить дайджест прямо сейчас\n"
        "/latest — последние 5 сообщений\n"
        "/read <id> — прочитать полное сообщение"
    )


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю новые сообщения...")
    await send_digest(ctx.application)


async def cmd_latest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю...")
    try:
        messages = portal.get_communications(limit=5)
        lines = ["<b>📬 Последние 5 сообщений:</b>\n"]
        for m in messages:
            date_str = portal.format_date(m["received"])
            lines.append(f"📌 <b>{m['subject']}</b>")
            lines.append(f"   {date_str}  |  id: <code>{m['id']}</code>")
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: /read <id>")
        return
    thread_id = ctx.args[0]
    await update.message.reply_text("⏳ Загружаю сообщение...")
    try:
        thread = portal.get_thread(thread_id)
        text = (
            f"<b>{thread['subject']}</b>\n"
            f"От: {thread['from']} | {portal.format_date(thread['received'])}\n\n"
            f"{thread['body'][:3000]}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        send_digest,
        trigger="cron",
        hour=DIGEST_HOUR,
        minute=DIGEST_MINUTE,
        args=[app],
    )
    scheduler.start()
    log.info(f"Digest scheduled at {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} {TZ}")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("read", cmd_read))

    log.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
