"""
Summarizes school messages using Claude API.
Falls back to plain formatting if no API key is set.
"""

import os
from datetime import datetime

import anthropic
from dotenv import load_dotenv

import portal as _portal

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

TG_LIMIT = 4000  # Telegram's hard limit is 4096; leave headroom


def chunk_message(text: str) -> list[str]:
    """Split a message into Telegram-safe chunks."""
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


async def summarize(messages: list[dict]) -> list[str]:
    """Return list of HTML-formatted Telegram messages summarizing new school comms."""
    if _client:
        text = await _summarize_with_claude(messages)
    else:
        text = _format_plain(messages)
    return chunk_message(text)


async def _summarize_with_claude(messages: list[dict]) -> str:
    details = []
    for m in messages[:8]:
        try:
            thread = _portal.get_thread(m["id"])
            body = thread["body"][:800]
        except Exception:
            body = m.get("summary", "")
        details.append(
            f"[{_portal.format_date(m['received'])}] {m['subject']}\n{body}"
        )

    if len(messages) > 8:
        for m in messages[8:]:
            details.append(f"[{_portal.format_date(m['received'])}] {m['subject']}")

    today = datetime.now().strftime("%-d %b %Y")
    prompt = f"""Ты помогаешь маме ученицы 5-го класса (Year 5) британской школы STAHS разобраться в письмах от школы.

Вот {len(messages)} новых сообщений:

{chr(10).join(f'{i+1}. {d}' for i, d in enumerate(details))}

Сделай КРАТКИЙ дайджест на русском. Максимум 800 слов. Используй только HTML-теги: <b>жирный</b>, <i>курсив</i>.

Формат — строго такой:

<b>📚 Дайджест школы — {today}</b>
<b>{len(messages)} новых сообщений</b>

<b>Что важно:</b>
[Для каждого сообщения — одно предложение: суть + нужно ли что-то делать маме. Пропусти общую рассылку без действий.]

<b>✅ Что нужно сделать:</b>
[Список action items — формы заполнить, деньги принести, разрешения дать, RSVP и т.п. Если ничего — напиши "Ничего не требуется".]

<b>📅 Даты и события:</b>
[Конкретные даты мероприятий, дедлайны, поездки. Если нет — "Нет".]"""

    response = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


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
