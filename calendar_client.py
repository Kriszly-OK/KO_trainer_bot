"""
calendar_client.py — Google Calendar integration
Fetches events across all calendars Krisz has access to.
"""

import os
import json
import pickle
from datetime import datetime, timedelta
from pathlib import Path
import pytz

# Google API imports — installed via requirements.txt
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
VIENNA_TZ = pytz.timezone("Europe/Vienna")
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "token.pickle")
CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials.json")


def get_calendar_service():
    """Authenticate and return a Google Calendar service object."""
    if not GOOGLE_AVAILABLE:
        return None

    creds = None

    # Load token from env var if set (for Railway deployment)
    token_env = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_env:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
        except Exception:
            pass

    # Otherwise load from file
    if not creds and Path(TOKEN_PATH).exists():
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save refreshed token
            with open(TOKEN_PATH, "wb") as f:
                pickle.dump(creds, f)
        except Exception as e:
            print(f"Token refresh failed: {e}")
            return None

    if not creds or not creds.valid:
        # This path is for local auth only — on Railway the token must be pre-set
        if Path(CREDENTIALS_PATH).exists():
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(TOKEN_PATH, "wb") as f:
                pickle.dump(creds, f)
        else:
            return None

    return build("calendar", "v3", credentials=creds)


def _fetch_events(start_dt: datetime, end_dt: datetime) -> list:
    """Fetch all events across all calendars in a time window."""
    service = get_calendar_service()
    if not service:
        return [{"summary": "Calendar unavailable — check token setup", "type": "error"}]

    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()

    try:
        # Get all calendars
        calendar_list = service.calendarList().list().execute()
        calendars = calendar_list.get("items", [])

        all_events = []
        for cal in calendars:
            cal_id = cal["id"]
            cal_name = cal.get("summary", cal_id)

            try:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=start_iso,
                    timeMax=end_iso,
                    singleEvents=True,
                    orderBy="startTime"
                ).execute()

                for event in result.get("items", []):
                    start = event.get("start", {})
                    end = event.get("end", {})
                    all_events.append({
                        "summary": event.get("summary", "Untitled"),
                        "start": start.get("dateTime", start.get("date", "")),
                        "end": end.get("dateTime", end.get("date", "")),
                        "calendar": cal_name,
                        "type": classify_event(event.get("summary", ""))
                    })
            except Exception:
                continue  # Skip calendars we can't read

        # Sort by start time
        all_events.sort(key=lambda x: x.get("start", ""))
        return all_events

    except Exception as e:
        return [{"summary": f"Calendar error: {str(e)}", "type": "error"}]


def classify_event(title: str) -> str:
    """Classify event as training, work, travel, or personal."""
    title_lower = title.lower()
    training_keywords = [
        "run", "yoga", "ashtanga", "workout", "gym", "race", "training",
        "swim", "cycle", "bike", "hike", "strength", "interval", "tempo",
        "easy run", "long run", "recovery run", "5k", "10k", "half marathon"
    ]
    work_keywords = [
        "standup", "meeting", "call", "sync", "review", "interview",
        "1:1", "1-1", "planning", "retro", "sprint", "all hands", "fiskaly"
    ]
    travel_keywords = ["flight", "train", "travel", "airport", "hotel"]

    for kw in training_keywords:
        if kw in title_lower:
            return "training"
    for kw in travel_keywords:
        if kw in title_lower:
            return "travel"
    for kw in work_keywords:
        if kw in title_lower:
            return "work"
    return "personal"


def get_todays_events() -> list:
    now = datetime.now(VIENNA_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return _fetch_events(start, end)


def get_tomorrows_events() -> list:
    now = datetime.now(VIENNA_TZ)
    tomorrow = now + timedelta(days=1)
    start = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    end = tomorrow.replace(hour=23, minute=59, second=59, microsecond=0)
    return _fetch_events(start, end)


def get_earliest_event_tomorrow() -> dict | None:
    """Returns the earliest event tomorrow — used for alarm calculation."""
    events = get_tomorrows_events()
    timed = [e for e in events if "T" in e.get("start", "")]
    if not timed:
        return None
    return timed[0]


def has_training_tomorrow() -> bool:
    events = get_tomorrows_events()
    return any(e.get("type") == "training" for e in events)


def has_training_today() -> bool:
    events = get_todays_events()
    return any(e.get("type") == "training" for e in events)
