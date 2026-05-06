"""
STAHS School Bot — daily digest of school communications via Telegram.
Runs a scheduled digest every evening and supports on-demand commands.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

import portal
import summarizer
import gcal

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📬 Новые сообщения"), KeyboardButton("📅 За 3 дня")],
        [KeyboardButton("📋 Последние 5"),     KeyboardButton("🔄 Сбросить")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
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


async def send_digest(app: Application, chat_id: int | None = None, force_days: int | None = None):
    """Fetch new messages, summarize, send to Telegram.

    force_days: if set, ignore last_seen and return messages from the last N days.
    """
    target = chat_id or CHAT_ID
    log.info("Running digest...")
    try:
        if force_days is not None:
            all_msgs = portal.get_communications(limit=50)
            cutoff = datetime.now(timezone.utc) - timedelta(days=force_days)
            messages = []
            for m in all_msgs:
                try:
                    received = datetime.fromisoformat(m["received"].replace("Z", "+00:00"))
                    if received >= cutoff:
                        messages.append(m)
                except Exception:
                    messages.append(m)
        else:
            last_seen = _load_last_seen()
            messages = portal.get_new_communications(last_seen)

        if not messages:
            await app.bot.send_message(
                chat_id=target,
                text="📭 Новых сообщений от школы нет.",
            )
            return

        if force_days is None:
            _save_last_seen(messages[0]["id"])

        chunks, events = await summarizer.summarize(messages)

        for chunk in chunks:
            await app.bot.send_message(
                chat_id=target,
                text=chunk,
                parse_mode=ParseMode.HTML,
                link_preview_options={"is_disabled": True},
            )

        # Handle calendar events
        if events:
            if gcal.is_authorized():
                # Auto-add to Google Calendar
                added, skipped = [], []
                for ev in events:
                    try:
                        link = gcal.create_event(
                            title=ev.get("title", ""),
                            date=ev.get("date", ""),
                            time_start=ev.get("time_start", ""),
                            time_end=ev.get("time_end", ""),
                            details=ev.get("details", "STAHS School"),
                        )
                        added.append(f"✅ {ev['title']} — {ev.get('date_label', ev.get('date',''))}")
                    except Exception as e:
                        log.warning(f"Calendar event failed: {e}")
                        skipped.append(ev.get("title", ""))
                if added:
                    await app.bot.send_message(
                        chat_id=target,
                        text="<b>📅 Добавлено в Google Calendar:</b>\n" + "\n".join(added),
                        parse_mode=ParseMode.HTML,
                    )
            else:
                # Not authorized yet — show buttons
                buttons = []
                for ev in events:
                    url = summarizer.gcal_url(
                        title=ev.get("title", ""),
                        date=ev.get("date", ""),
                        time_start=ev.get("time_start", ""),
                        time_end=ev.get("time_end", ""),
                        details=ev.get("details", "STAHS School"),
                    )
                    if url:
                        label = f"📆 {ev['title']} — {ev.get('date_label', ev.get('date',''))}"
                        buttons.append([InlineKeyboardButton(label, url=url)])
                if buttons:
                    await app.bot.send_message(
                        chat_id=target,
                        text="<b>📅 События — нажми чтобы добавить в календарь:</b>\n\n<i>Или используй /auth_calendar чтобы бот добавлял сам</i>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )

        log.info(f"Digest sent: {len(messages)} messages, {len(chunks)} chunk(s), {len(events)} events.")

    except Exception as e:
        log.error(f"Digest error: {e}", exc_info=True)
        await app.bot.send_message(
            chat_id=target,
            text=f"⚠️ Ошибка при получении дайджеста: {e}",
        )


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я буду присылать тебе дайджест сообщений от школы каждый вечер.\n\n"
        "Нажимай кнопки внизу 👇\n\n"
        "Или вручную:\n"
        "/digest — новые сообщения\n"
        "/today — за последние 3 дня\n"
        "/latest — последние 5 (заголовки)\n"
        "/read <id> — полное сообщение\n"
        "/reset — сбросить счётчик",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю новые сообщения...")
    await send_digest(ctx.application, chat_id=update.effective_chat.id)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю сообщения за последние 3 дня...")
    await send_digest(ctx.application, chat_id=update.effective_chat.id, force_days=3)


async def cmd_auth_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gcal.CLIENT_ID:
        await update.message.reply_text("⚠️ GCAL_CLIENT_ID не задан в настройках бота.")
        return
    if gcal.is_authorized():
        await update.message.reply_text("✅ Google Calendar уже подключён!")
        return
    url = gcal.get_auth_url()
    await update.message.reply_text(
        f"1️⃣ Открой ссылку и разреши доступ к Google Calendar:\n{url}\n\n"
        "2️⃣ После разрешения браузер откроет страницу «Сайт недоступен» — это нормально.\n\n"
        "3️⃣ Скопируй <b>полный URL</b> из адресной строки (он начинается с <code>http://localhost/?code=...</code>) "
        "и отправь его мне:\n<code>/gcal_code ВСТАВЬ_URL_СЮДА</code>",
        parse_mode=ParseMode.HTML,
        link_preview_options={"is_disabled": True},
    )


async def cmd_gcal_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: /gcal_code КОД")
        return
    code = ctx.args[0]
    try:
        gcal.exchange_code(code)
        await update.message.reply_text(
            "✅ Google Calendar подключён! Теперь бот будет автоматически добавлять события из школьных писем."
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    await update.message.reply_text(
        "✅ Сброшено. Теперь /digest покажет все непрочитанные сообщения.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📬 Новые сообщения":
        await update.message.reply_text("⏳ Загружаю новые сообщения...")
        await send_digest(ctx.application, chat_id=update.effective_chat.id)
    elif text == "📅 За 3 дня":
        await update.message.reply_text("⏳ Загружаю сообщения за последние 3 дня...")
        await send_digest(ctx.application, chat_id=update.effective_chat.id, force_days=3)
    elif text == "📋 Последние 5":
        await cmd_latest(update, ctx)
    elif text == "🔄 Сбросить":
        await cmd_reset(update, ctx)


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
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("auth_calendar", cmd_auth_calendar))
    app.add_handler(CommandHandler("gcal_code", cmd_gcal_code))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))

    log.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
