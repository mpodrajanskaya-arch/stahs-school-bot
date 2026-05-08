"""
STAHS School Bot — monitors school communications and sends Telegram digests.
- Checks for new messages every 30 minutes, sends immediately if found
- Daily digest at 19:00 (silent if nothing new)
- Morning reminders at 8:00 for events happening today or tomorrow
"""

import json
import logging
import os
from datetime import datetime, date, timedelta, timezone
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
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "19"))
DIGEST_MINUTE = int(os.getenv("DIGEST_MINUTE", "0"))
TZ = ZoneInfo(os.getenv("TZ", "Europe/London"))

STATE_FILE = Path(__file__).parent / ".last_seen.txt"
EVENTS_FILE = Path(__file__).parent / ".upcoming_events.json"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("1️⃣ Новые сообщения"), KeyboardButton("2️⃣ За 3 дня")],
        [KeyboardButton("3️⃣ Последние 5"),     KeyboardButton("4️⃣ Сбросить")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

MENU_TEXT = (
    "Выбери опцию:\n\n"
    "1️⃣ — новые сообщения (с прошлого раза)\n"
    "2️⃣ — дайджест за последние 3 дня\n"
    "3️⃣ — последние 5 сообщений (заголовки)\n"
    "4️⃣ — сбросить счётчик прочитанных\n\n"
    "💬 Или задай любой вопрос про школу:\n"
    "<i>«когда следующая поездка?»</i>\n"
    "<i>«что взять на garden party?»</i>"
)


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_last_seen() -> str | None:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip() or None
    return None


def _save_last_seen(thread_id: str):
    STATE_FILE.write_text(thread_id)


def _save_events(events: list[dict]):
    """Persist upcoming events so reminders can check them without re-fetching."""
    try:
        existing = _load_events()
        # Merge by (title, date) — avoid duplicates
        keys = {(e["title"], e["date"]) for e in existing}
        for ev in events:
            if (ev.get("title"), ev.get("date")) not in keys:
                existing.append(ev)
        EVENTS_FILE.write_text(json.dumps(existing))
    except Exception:
        EVENTS_FILE.write_text(json.dumps(events))


def _load_events() -> list[dict]:
    if EVENTS_FILE.exists():
        try:
            return json.loads(EVENTS_FILE.read_text())
        except Exception:
            pass
    return []


# ── Core digest logic ─────────────────────────────────────────────────────────

async def _send_messages_and_events(app, target: int, messages: list[dict]):
    """Summarize messages and send to target chat, including calendar handling."""
    chunks, events = await summarizer.summarize(messages)

    for chunk in chunks:
        await app.bot.send_message(
            chat_id=target,
            text=chunk,
            parse_mode=ParseMode.HTML,
            link_preview_options={"is_disabled": True},
        )

    if events:
        _save_events(events)

        if gcal.is_authorized():
            added = []
            for ev in events:
                try:
                    gcal.create_event(
                        title=ev.get("title", ""),
                        date=ev.get("date", ""),
                        time_start=ev.get("time_start", ""),
                        time_end=ev.get("time_end", ""),
                        details=ev.get("details", "STAHS School"),
                    )
                    added.append(f"✅ {ev['title']} — {ev.get('date_label', ev.get('date', ''))}")
                except Exception as e:
                    log.warning(f"Calendar event failed: {e}")
            if added:
                await app.bot.send_message(
                    chat_id=target,
                    text="<b>📅 Добавлено в Google Calendar:</b>\n" + "\n".join(added),
                    parse_mode=ParseMode.HTML,
                )
        else:
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
                    buttons.append([InlineKeyboardButton(
                        f"📆 {ev['title']} — {ev.get('date_label', ev.get('date', ''))}",
                        url=url,
                    )])
            if buttons:
                await app.bot.send_message(
                    chat_id=target,
                    text="<b>📅 События — нажми чтобы добавить в календарь:</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

    log.info(f"Sent digest: {len(messages)} messages, {len(chunks)} chunk(s), {len(events)} events.")


async def send_digest(app: Application, chat_id: int | None = None,
                      force_days: int | None = None, silent_if_empty: bool = False):
    """Fetch new messages and send digest. silent_if_empty=True skips 'no new messages'."""
    target = chat_id or CHAT_ID
    try:
        if force_days is not None:
            all_msgs = portal.get_communications(limit=50)
            cutoff = datetime.now(timezone.utc) - timedelta(days=force_days)
            messages = [
                m for m in all_msgs
                if _parse_received(m["received"]) >= cutoff
            ]
        else:
            messages = portal.get_new_communications(_load_last_seen())

        if not messages:
            if not silent_if_empty:
                await app.bot.send_message(chat_id=target, text="📭 Новых сообщений от школы нет.")
            return

        if force_days is None:
            _save_last_seen(messages[0]["id"])

        await _send_messages_and_events(app, target, messages)

    except Exception as e:
        log.error(f"Digest error: {e}", exc_info=True)
        await app.bot.send_message(chat_id=target, text=f"⚠️ Ошибка при получении дайджеста: {e}")


def _parse_received(iso: str) -> datetime:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def check_new_messages(app: Application):
    """Runs every 30 min — sends digest immediately if new messages arrived."""
    log.info("30-min check: looking for new messages...")
    await send_digest(app, silent_if_empty=True)


async def send_weekly_summary(app: Application):
    """Runs every Friday at 17:00 — this week recap + next week preview."""
    log.info("Sending weekly summary...")
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        all_msgs = portal.get_communications(limit=50)
        week_msgs = [m for m in all_msgs if _parse_received(m["received"]) >= cutoff]
        upcoming = _load_events()
        text = await summarizer.weekly_summary(week_msgs, upcoming)
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error(f"Weekly summary error: {e}", exc_info=True)


async def send_reminders(app: Application):
    """Runs at 8:00 — reminds about events happening today or tomorrow."""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    events = _load_events()

    reminders = []
    for ev in events:
        try:
            ev_date = date.fromisoformat(ev["date"])
        except Exception:
            continue
        if ev_date == today:
            reminders.append(f"📌 <b>Сегодня:</b> {ev['title']}")
        elif ev_date == tomorrow:
            reminders.append(f"⏰ <b>Завтра:</b> {ev['title']}")

    if reminders:
        text = "<b>🔔 Напоминание о событиях STAHS:</b>\n\n" + "\n".join(reminders)
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        log.info(f"Sent {len(reminders)} reminder(s).")


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я слежу за письмами школы STAHS.\n"
        "Проверяю каждые 30 минут — как только придёт что-то новое, сразу напишу.\n"
        "Каждое утро в 8:00 напоминаю о событиях на сегодня и завтра.\n\n"
        + MENU_TEXT,
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю новые сообщения...")
    await send_digest(ctx.application, chat_id=update.effective_chat.id)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю сообщения за последние 3 дня...")
    await send_digest(ctx.application, chat_id=update.effective_chat.id, force_days=3)


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    await update.message.reply_text(
        "✅ Сброшено. Следующая проверка покажет все непрочитанные.",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_latest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю...")
    try:
        messages = portal.get_communications(limit=5)
        lines = ["<b>📬 Последние 5 сообщений:</b>\n"]
        for m in messages:
            lines.append(f"📌 <b>{m['subject']}</b>")
            lines.append(f"   {portal.format_date(m['received'])}  |  id: <code>{m['id']}</code>")
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: /read <id>")
        return
    await update.message.reply_text("⏳ Загружаю сообщение...")
    try:
        thread = portal.get_thread(ctx.args[0])
        text = (
            f"<b>{thread['subject']}</b>\n"
            f"От: {thread['from']} | {portal.format_date(thread['received'])}\n\n"
            f"{thread['body'][:3000]}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_auth_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gcal.CLIENT_ID:
        await update.message.reply_text("⚠️ GCAL_CLIENT_ID не задан.")
        return
    if gcal.is_authorized():
        await update.message.reply_text("✅ Google Calendar уже подключён!")
        return
    try:
        url = gcal.get_auth_url()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка при создании ссылки: {e}")
        return
    await update.message.reply_text(
        f"1️⃣ Открой ссылку и разреши доступ:\n{url}\n\n"
        "2️⃣ Браузер откроет «Сайт недоступен» — это нормально.\n\n"
        "3️⃣ Скопируй <b>полный URL</b> из адресной строки и отправь:\n"
        "<code>/gcal_code ВСТАВЬ_URL_СЮДА</code>",
        parse_mode=ParseMode.HTML,
        link_preview_options={"is_disabled": True},
    )


async def cmd_gcal_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: /gcal_code КОД")
        return
    try:
        gcal.exchange_code(ctx.args[0])
        await update.message.reply_text(
            "✅ Google Calendar подключён! Бот будет автоматически добавлять события."
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def handle_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("1", "1️⃣ Новые сообщения"):
        await update.message.reply_text("⏳ Загружаю новые сообщения...")
        await send_digest(ctx.application, chat_id=update.effective_chat.id)
    elif text in ("2", "2️⃣ За 3 дня"):
        await update.message.reply_text("⏳ Загружаю сообщения за последние 3 дня...")
        await send_digest(ctx.application, chat_id=update.effective_chat.id, force_days=3)
    elif text in ("3", "3️⃣ Последние 5"):
        await cmd_latest(update, ctx)
    elif text in ("4", "4️⃣ Сбросить"):
        await cmd_reset(update, ctx)
    else:
        # Treat any other text as a question about school
        await update.message.reply_text("🔍 Ищу ответ в письмах школы...")
        try:
            messages = portal.get_communications(limit=50)
            answer = await summarizer.answer_question(text, messages)
            await update.message.reply_text(answer, parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Не смогла найти ответ: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=TZ)

    # New messages check every 30 minutes
    scheduler.add_job(check_new_messages, trigger="interval", minutes=30, args=[app])

    # Evening digest at 19:00 (silent if already sent during the day)
    scheduler.add_job(send_digest, trigger="cron",
                      hour=DIGEST_HOUR, minute=DIGEST_MINUTE, args=[app],
                      kwargs={"silent_if_empty": True})

    # Morning reminders at 8:00
    scheduler.add_job(send_reminders, trigger="cron", hour=8, minute=0, args=[app])

    # Weekly summary every Friday at 17:00
    scheduler.add_job(send_weekly_summary, trigger="cron",
                      day_of_week="fri", hour=17, minute=0, args=[app])

    scheduler.start()
    log.info(f"Scheduler started: 30-min checks, digest at {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}, reminders at 08:00 {TZ}")


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
