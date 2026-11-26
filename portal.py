"""
STAHS Parent Portal client.
Pure httpx — no Playwright, no browser needed.
Login extracts stryver token from the login page HTML.
"""

import os
import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx
from dotenv import load_dotenv

load_dotenv()

PORTAL_URL = os.getenv("PORTAL_URL", "https://parentportal.stahs.org.uk")
EMAIL = os.getenv("PORTAL_EMAIL", "")
PASSWORD = os.getenv("PORTAL_PASSWORD", "")
SESSION_FILE = Path(__file__).parent / ".session.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": f"{PORTAL_URL}/",
}


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)

    def get_text(self):
        return " ".join(t.strip() for t in self.text if t.strip())


def _strip_html(html: str) -> str:
    s = _HTMLStripper()
    s.feed(html)
    return s.get_text()


def _login_httpx() -> dict:
    """Login via pure httpx — no browser needed."""
    client = httpx.Client(
        base_url=PORTAL_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        },
        follow_redirects=True,
        timeout=30,
    )

    # Step 1: GET login page — extract stryver token and target_once_logged_in
    resp = client.get("/login")
    resp.raise_for_status()

    stryver = re.search(r'name="stryver"\s+value="([^"]+)"', resp.text)
    target = re.search(r'name="target_once_logged_in"\s+value="([^"]+)"', resp.text)
    if not stryver:
        raise RuntimeError("Could not find stryver token on login page")

    stryver_val = stryver.group(1)
    target_val = target.group(1) if target else "aHR0cHM6Ly9wYXJlbnRwb3J0YWwuc3RhaHMub3JnLnVr"

    # Step 2: POST /api/v2/login-attempt (init)
    client.post(
        "/api/v2/login-attempt",
        content=json.dumps({
            "type": "login-attempt",
            "id": EMAIL,
            "attributes": {"action": "init", "provider": "local"},
        }),
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )

    # Step 3: POST /api/auth with credentials
    resp = client.post(
        "/api/auth",
        json={
            "stryver": stryver_val,
            "target_once_logged_in": target_val,
            "set_session_variables": "true",
            "client_id": EMAIL,
            "client_secret": PASSWORD,
            "stay_logged_in": False,
        },
        headers={"Content-Type": "application/json"},
    )

    resp.raise_for_status()

    # Extract session cookies
    cookies = {
        name: value
        for name, value in client.cookies.items()
        if name in ("MSP_ID", "MSP_TOKEN")
    }

    if not all(cookies.get(name) for name in ("MSP_ID", "MSP_TOKEN")):
        raise RuntimeError("Login failed — no session cookies received. Check credentials in .env")

    return cookies


def _load_session() -> dict | None:
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text())
            if isinstance(data.get("cookies"), dict):
                return data
        except Exception:
            pass
    return None


def _save_session(cookies: dict):
    SESSION_FILE.write_text(json.dumps({
        "cookies": cookies,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }))


_cached_client: httpx.Client | None = None


def _make_client(cookies: dict) -> httpx.Client:
    return httpx.Client(
        base_url=PORTAL_URL,
        cookies=cookies,
        headers=HEADERS,
        follow_redirects=True,
        timeout=60,
    )


def _get_client() -> httpx.Client:
    """Return a cached authenticated httpx client, refreshing only when needed."""
    global _cached_client

    if _cached_client is not None:
        return _cached_client

    session = _load_session()
    cookies = session.get("cookies") if session else None

    if not cookies:
        cookies = _login_httpx()
        _save_session(cookies)

    client = _make_client(cookies)

    # Validate session with a single lightweight check
    resp = _get_with_retry(client, "/api/message?date_unit=latest&view=all&page_size=1")
    if _needs_login(resp):
        client.close()
        cookies = _login_httpx()
        _save_session(cookies)
        client = _make_client(cookies)

    _cached_client = client
    return _cached_client


def invalidate_session():
    global _cached_client
    if _cached_client is not None:
        _cached_client.close()
    _cached_client = None
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()


def _needs_login(resp: httpx.Response) -> bool:
    return (resp.status_code in (401, 403)
            or "/login" in str(resp.url)
            or (resp.is_success and "text/html" in resp.headers.get("content-type", "")))


def _get_with_retry(client: httpx.Client, path: str) -> httpx.Response:
    """Retry temporary portal failures, with a bounded exponential delay."""
    for attempt in range(3):
        try:
            resp = client.get(path)
        except httpx.TransportError:
            if attempt == 2:
                raise
        else:
            if resp.status_code not in (502, 503, 504) or attempt == 2:
                return resp
            resp.close()
        time.sleep(2 ** attempt)


def _request(path: str) -> httpx.Response:
    resp = _get_with_retry(_get_client(), path)
    if _needs_login(resp):
        invalidate_session()
        resp = _get_with_retry(_get_client(), path)
    resp.raise_for_status()
    if _needs_login(resp):
        raise RuntimeError("School portal session could not be renewed")
    return resp


# ── Public API ────────────────────────────────────────────────────────────────

def get_communications(limit: int = 30) -> list[dict]:
    """Return list of recent messages [{id, subject, received, read, summary}]."""
    resp = _request(f"/api/message?date_unit=latest&view=all&page_size={limit}")
    result = []
    for item in resp.json().get("data", []):
        attrs = item.get("attributes", {})
        result.append({
            "id": str(item["id"]),
            "subject": attrs.get("subject", ""),
            "received": attrs.get("received", ""),
            "read": attrs.get("read", False),
            "summary": item.get("meta", {}).get("summary", ""),
        })
    return result


def get_thread(thread_id: str) -> dict:
    """Return full content of a message thread."""
    resp = _request(f"/api/message/{thread_id}")
    messages = resp.json().get("data", [])
    if not messages:
        return {"id": thread_id, "subject": "", "body": "", "from": "", "received": ""}

    msg = messages[0]
    attrs = msg.get("attributes", {})
    sender = (
        msg.get("relationships", {})
        .get("sender", {}).get("data", {})
        .get("meta", {}).get("email", "")
    )
    body_html = attrs.get("body", "")

    return {
        "id": thread_id,
        "subject": attrs.get("subject", ""),
        "from": sender,
        "received": attrs.get("received", ""),
        "body": _strip_html(body_html),
        "body_html": body_html,
    }


def get_new_communications(last_seen_id: str | None = None) -> list[dict]:
    """Return communications newer than last_seen_id."""
    all_msgs = get_communications(limit=50)
    if not last_seen_id:
        return all_msgs
    new = []
    for msg in all_msgs:
        if msg["id"] == last_seen_id:
            break
        new.append(msg)
    return new


def format_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%-d %b %Y, %H:%M")
    except Exception:
        return iso
