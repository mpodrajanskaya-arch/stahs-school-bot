"""
Google Calendar integration for STAHS bot.
One-time OAuth flow via /auth_calendar command, then auto-adds events.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = Path(__file__).parent / ".gcal_token.json"

CLIENT_ID = os.getenv("GCAL_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GCAL_CLIENT_SECRET", "")
CALENDAR_ID = os.getenv("GCAL_CALENDAR_ID", "primary")
REDIRECT_URI = "http://localhost"


def _client_config() -> dict:
    return {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }


def get_auth_url() -> str:
    """Return OAuth URL for user to visit."""
    flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return auth_url


def exchange_code(code_or_url: str) -> bool:
    """Exchange auth code (or redirect URL) for tokens. Returns True on success."""
    # Extract code from redirect URL if user pasted the full URL
    code = code_or_url.strip()
    if code.startswith("http"):
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(code).query)
        code = params.get("code", [code])[0]
    try:
        flow = Flow.from_client_config(
            _client_config(),
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials
        TOKEN_FILE.write_text(json.dumps({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
        }))
        return True
    except Exception as e:
        raise RuntimeError(f"Auth failed: {e}")


def _get_credentials() -> Credentials | None:
    if TOKEN_FILE.exists():
        data = json.loads(TOKEN_FILE.read_text())
    else:
        encoded = os.getenv("GCAL_TOKEN_JSON", "")
        if not encoded:
            return None
        import base64
        data = json.loads(base64.b64decode(encoded).decode())
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id", CLIENT_ID),
        client_secret=data.get("client_secret", CLIENT_SECRET),
        scopes=data.get("scopes", SCOPES),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(json.dumps({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
        }))
    return creds


def is_authorized() -> bool:
    return TOKEN_FILE.exists() or bool(os.getenv("GCAL_TOKEN_JSON", ""))


def create_event(title: str, date: str, time_start: str = "", time_end: str = "", details: str = "") -> str:
    """Create a Google Calendar event. Returns the event HTML link."""
    creds = _get_credentials()
    if not creds:
        raise RuntimeError("Not authorized. Use /auth_calendar first.")

    service = build("calendar", "v3", credentials=creds)

    if time_start:
        start_dt = datetime.strptime(f"{date} {time_start}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{date} {time_end}", "%Y-%m-%d %H:%M") if time_end else start_dt + timedelta(hours=1)
        start = {"dateTime": start_dt.isoformat(), "timeZone": "Europe/London"}
        end = {"dateTime": end_dt.isoformat(), "timeZone": "Europe/London"}
    else:
        d = datetime.strptime(date, "%Y-%m-%d")
        start = {"date": d.strftime("%Y-%m-%d")}
        end = {"date": (d + timedelta(days=1)).strftime("%Y-%m-%d")}

    event = {
        "summary": title,
        "description": details or "Добавлено STAHS School Bot",
        "start": start,
        "end": end,
    }

    result = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    return result.get("htmlLink", "")
