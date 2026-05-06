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


def gcal_url(title: str, date: str, time_start: str = "", time_end: str = "", details: str = "") -> str:
    try:
        if time_start:
            d = datetime.strptime(f"{date} {time_start}", "%Y-%m-%d %H:%M")
            d_end = datetime.strptime(f"{date} {time_end}", "%Y-%m-%d %H:%M") if time_end else d + timedelta(hours=1)
            dates = f"{d.strftime('%Y%m%dT%H%M%S')}/{d_end.strftime('%Y%m%dT%H%M%S')}"
        else:
            d = datetime.strptime(date, "%Y-%m-%d")
            dates = f"{d.strftime('%Y%m%d')}/{(d + timedelta(days=1)).strftime('%Y%m%d')}"
    except ValueError:
        return ""
    params = f"action=TEMPLATE&text={quote(title)}&dates={dates}"
    if details:
        params += f"&details={quote(details[:200])}"
    return f"https://calendar.google.com/calendar/render?{params}"


async def summarize(messages: list[dict]) -> tuple[list[str], list[dict]]:
    """Return (text_chunks, events_list).

    text_chunks: HTML-formatted Telegram messages
    events_list: [{title, date, date_label, time_start, time_end, details}]
    """
    if _client:
        summary, events = await _summarize_with_claude(messages)
    else:
        summary = _format_plain(messages)
        events = []
    return chunk_message(summary), events


async def _summarize_with_claude(messages: list[dict]) -> tuple[str, list[dict]]:
    # Use subject + built-in summary only — no extra portal requests
    details = []
    for m in messages:
        summary = m.get("summary", "")
        line = f"[{_portal.format_date(m['received'])}] {m['subject']}"
        if summary:
            line += f"\n{summary[:300]}"
        details.append(line)

    today = datetime.now().strftime("%-d %b %Y")
    current_year = datetime.now().year
    messages_text = chr(10).join(f"{i+1}. {d}" for i, d in enumerate(details))

    summary_prompt = f"""Ты помогаешь маме ученицы 5-го класса (Year 5) британской школы STAHS.

Вот {len(messages)} новых сообщений:
{messages_text}

Сделай дайджест на русском. Используй только HTML-теги <b> и <i>.

Формат строго такой — три блока:

<b>📚 Дайджест школы — {today}</b>
<b>{len(messages)} новых сообщений</b>

<b>🔴 Требует действия:</b>
[Только то, что нужно сделать маме: заплатить, подписать, ответить, зарегистрироваться, дать разрешение. Каждый пункт — новая строка, начиная с •. Если ничего — напиши "Нет".]

<b>📌 Важно для Year 5:</b>
[Информация, которая касается именно Year 5 или всей Prep school: мероприятия, поездки, изменения расписания. Одно предложение на пункт, начиная с •. Если ничего — "Нет".]

<b>ℹ️ Остальное:</b>
[Общие письма без действий: советы по здоровью, новости школы, общая информация. Одно предложение на пункт, начиная с •. Не больше 3 пунктов. Если ничего — "Нет".]"""

    r1 = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": summary_prompt}],
    )
    summary = r1.content[0].text.strip()

    # Use only subjects for date extraction — dates are almost always in the subject line
    subjects_text = chr(10).join(
        f"{i+1}. {m['subject']} [{m['received'][:10]}]"
        for i, m in enumerate(messages)
    )
    events_prompt = f"""Extract all events with specific dates from these school message subjects.
Return ONLY a valid JSON array, no markdown, no explanation.
Each object: {{"title":"event name in Russian","date":"YYYY-MM-DD","time_start":"HH:MM or empty","time_end":"HH:MM or empty","date_label":"21 мая","details":"brief note"}}
Only events with a specific date. If none, return [].
Current year: {current_year}.

{subjects_text}"""

    r2 = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": events_prompt}],
    )
    events = []
    try:
        raw = r2.content[0].text.strip()
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            events = json.loads(m.group())
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
        lines.append(f"📌 <b>{m['subject']}</b>")
        lines.append(f"<i>{_portal.format_date(m['received'])}</i>")
        if m.get("summary"):
            lines.append(m["summary"][:200])
        lines.append(f"<code>/read {m['id']}</code>\n")
    if len(messages) > 10:
        lines.append(f"<i>...и ещё {len(messages) - 10} сообщений</i>")
    return "\n".join(lines)
