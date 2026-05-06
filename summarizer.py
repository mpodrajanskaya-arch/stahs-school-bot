"""
Summarizes school messages using Claude API.
Falls back to plain formatting if no API key is set.
"""

import os
import re
from datetime import datetime

import anthropic
from dotenv import load_dotenv

import portal as _portal

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


async def summarize(messages: list[dict]) -> str:
    """Return HTML-formatted Telegram message summarizing new school comms."""
    if _client:
        return await _summarize_with_claude(messages)
    return _format_plain(messages)


async def _summarize_with_claude(messages: list[dict]) -> str:
    # Fetch full bodies for first 5 messages (to keep prompt manageable)
    details = []
    for m in messages[:5]:
        try:
            thread = _portal.get_thread(m["id"])
            details.append(
                f"[{_portal.format_date(m['received'])}] {m['subject']}\n{thread['body'][:600]}"
            )
        except Exception:
            details.append(f"[{_portal.format_date(m['received'])}] {m['subject']}\n{m['summary']}")

    if len(messages) > 5:
        for m in messages[5:]:
            details.append(f"[{_portal.format_date(m['received'])}] {m['subject']}")

    prompt = f"""Ты помощник, который делает краткий дайджест сообщений из школы для родителя.

Вот {len(messages)} новых сообщений от школы STAHS:

{chr(10).join(f'{i+1}. {d}' for i, d in enumerate(details))}

Сделай краткое summary на русском языке:
1. Для каждого сообщения — 1-2 предложения о сути
2. Выдели отдельным блоком все СОБЫТИЯ С ДАТАМИ (мероприятия, дедлайны, важные даты)
3. Используй HTML теги: <b>жирный</b>, <i>курсив</i>

Формат ответа:
<b>📚 Дайджест школы — [дата сегодня]</b>
<b>{len(messages)} новых сообщений</b>

[краткое summary каждого]

<b>📅 События и важные даты:</b>
[список событий с датами или "Событий с конкретными датами не обнаружено"]"""

    response = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _format_plain(messages: list[dict]) -> str:
    """Simple formatting without Claude API."""
    today = datetime.now().strftime("%-d %b %Y")
    lines = [
        f"<b>📚 Дайджест школы — {today}</b>",
        f"<b>{len(messages)} новых сообщений</b>\n",
    ]
    for m in messages:
        date_str = _portal.format_date(m["received"])
        lines.append(f"📌 <b>{m['subject']}</b>")
        lines.append(f"<i>{date_str}</i>")
        if m["summary"]:
            lines.append(f"{m['summary'][:150]}...")
        lines.append(f"<code>/read {m['id']}</code>\n")

    return "\n".join(lines)
