"""
Summarizes school messages using Claude API.
Falls back to plain formatting if no API key is set.
"""

import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import quote

import anthropic
from dotenv import load_dotenv

import portal as _portal

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

TG_LIMIT = 4000


def chunk_message(text: str) -> list[str]:
    if len(text) <= TG_LIMIT:
        return [text]
    chunks = []
    while text:
        if len(text) <= TG_LIMIT:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, TG_LIMIT)
        if split_at == -1:
            split_at = TG_LIMIT
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _gcal_link(title: str, date: str, time_start: str = "", time_end: str = "", details: str = "") -> str:
    """Generate a Google Calendar quick-add URL."""
    try:
        if time_start:
            # With time: YYYYMMDDTHHmmss / YYYYMMDDTHHmmss
            d = datetime.strptime(f"{date} {time_start}", "%Y-%m-%d %H:%M")
            if time_end:
                d_end = datetime.strptime(f"{date} {time_end}", "%Y-%m-%d %H:%M")
            else:
                d_end = d + timedelta(hours=1)
            dates = f"{d.strftime('%Y%m%dT%H%M%S')}/{d_end.strftime('%Y%m%dT%H%M%S')}"
        else:
            # All-day: YYYYMMDD / YYYYMMDD (next day)
            d = datetime.strptime(date, "%Y-%m-%d")
            d_end = d + timedelta(days=1)
            dates = f"{d.strftime('%Y%m%d')}/{d_end.strftime('%Y%m%d')}"
    except ValueError:
        return ""

    params = f"action=TEMPLATE&text={quote(title)}&dates={dates}"
    if details:
        params += f"&details={quote(details[:200])}"
    return f"https://calendar.google.com/calendar/render?{params}"


def _build_calendar_block(events: list[dict]) -> str:
    """Turn a list of event dicts into a Telegram HTML block with calendar links."""
    if not events:
        return ""
    lines = ["\n<b>📅 Добавить в календарь:</b>"]
    for ev in events:
        link = _gcal_link(
            title=ev.get("title", ""),
            date=ev.get("date", ""),
            time_start=ev.get("time_start", ""),
            time_end=ev.get("time_end", ""),
            details=ev.get("details", "STAHS School"),
        )
        date_label = ev.get("date_label", ev.get("date", ""))
        if link:
            lines.append(f'📆 <a href="{link}">{ev["title"]} — {date_label}</a>')
        else:
            lines.append(f'📆 {ev["title"]} — {date_label}')
    return "\n".join(lines)


async def summarize(messages: list[dict]) -> list[str]:
    if _client:
        summary, events = await _summarize_with_claude(messages)
    else:
        summary = _format_plain(messages)
        events = []

    calendar_block = _build_calendar_block(events)
    full_text = summary + calendar_block if calendar_block else summary
    return chunk_message(full_text)


async def _summarize_with_claude(messages: list[dict]) -> tuple[str, list[dict]]:
    details = []
    for m in messages[:4]:
        try:
            thread = _portal.get_thread(m["id"])
            body = thread["body"][:800]
        except Exception:
            body = m.get("summary", "")
        details.append(f"[{_portal.format_date(m['received'])}] {m['subject']}\n{body}")

    if len(messages) > 4:
        for m in messages[4:]:
            details.append(f"[{_portal.format_date(m['received'])}] {m['subject']}")

    today = datetime.now().strftime("%-d %b %Y")
    current_year = datetime.now().year
    messages_text = chr(10).join(f"{i+1}. {d}" for i, d in enumerate(details))

    # Call 1: summary digest
    summary_prompt = f"""Ты помогаешь маме ученицы 5-го класса (Year 5) британской школы STAHS.

Вот {len(messages)} новых сообщений:
{messages_text}

Сделай краткий дайджест на русском. Используй только HTML-теги <b> и <i>.

<b>📚 Дайджест школы — {today}</b>
<b>{len(messages)} новых сообщений</b>

<b>Что важно:</b>
[Для каждого сообщения — одно предложение. Пропусти рассылки без действий.]

<b>✅ Что нужно сделать:</b>
[Action items: формы, деньги, RSVP. Если ничего — "Ничего не требуется".]"""

    r1 = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": summary_prompt}],
    )
    summary = r1.content[0].text.strip()

    # Call 2: extract events as JSON
    events_prompt = f"""Extract all events with specific dates from these school messages.
Return ONLY a valid JSON array. No explanation, no markdown, just JSON.
Format: [{{"title":"название на русском","date":"YYYY-MM-DD","time_start":"HH:MM or empty","time_end":"HH:MM or empty","date_label":"21 мая","details":"brief note"}}]
If no events with specific dates, return [].
Current year: {current_year}.

Messages:
{messages_text}"""

    r2 = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": events_prompt}],
    )
    events = []
    try:
        raw_events = r2.content[0].text.strip()
        json_match = re.search(r"\[.*\]", raw_events, re.DOTALL)
        if json_match:
            events = json.loads(json_match.group())
    except Exception:
        events = []

    return summary, events


def _format_plain(messages: list[dict]) -> str:
    today = datetime.now().strftime("%-d %b %Y")
    lines = [
        f"<b>📚 Дайджест школы — {today}</b>",
        f"<b>{len(messages)} новых сообщений</b>\n",
    ]
    for m in messages[:10]:
        date_str = _portal.format_date(m["received"])
        lines.append(f"📌 <b>{m['subject']}</b>")
        lines.append(f"<i>{date_str}</i>")
        summary = m.get("summary", "")
        if summary:
            lines.append(f"{summary[:200]}")
        lines.append(f"<code>/read {m['id']}</code>\n")
    if len(messages) > 10:
        lines.append(f"<i>...и ещё {len(messages) - 10} сообщений</i>")
    return "\n".join(lines)
