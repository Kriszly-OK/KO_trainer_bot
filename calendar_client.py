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
    if not GOOGLE_AVAILABLE:
        return None

    creds = None

    token_env = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_env:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
        except Exception as e:
            print(f"Token parse error: {e}")

    if not creds and Path(TOKEN_PATH).exists():
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_PATH, "wb") as f:
                pickle.dump(creds, f)
        except Exception as e:
            print(f"Token refresh failed: {e}")
            return None

    if not creds or not creds.valid:
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
        return [{"summary": "Calendar unavailable — check GOOGLE_TOKEN_JSON in Railway variables", "type": "error"}]

    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()

    try:
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
                    summary = event.get("summary", "Busy")
                    is_all_day = "date" in start and "dateTime" not in start

                    all_events.append({
                        "summary": summary,
                        "start": start.get("dateTime", start.get("date", "")),
                        "end": end.get("dateTime", end.get("date", "")),
                        "calendar": cal_name,
                        "type": classify_event(summary),
                        "all_day": is_all_day,
                        "timed": not is_all_day
                    })
            except Exception:
                continue

        all_events.sort(key=lambda x: x.get("start", ""))
        return all_events

    except Exception as e:
        return [{"summary": f"Calendar error: {str(e)}", "type": "error"}]


def classify_event(title: str) -> str:
    title_lower = title.lower()
    training_keywords = [
        "run", "yoga", "ashtanga", "workout", "gym", "race", "training",
        "swim", "cycle", "bike", "hike", "strength", "interval", "tempo",
        "easy run", "long run", "recovery run", "5k", "10k", "half marathon",
        "rolling", "threshold", "strides", "fartlek", "progression"
    ]
    work_keywords = [
        "standup", "meeting", "call", "sync", "review", "interview",
        "1:1", "1-1", "planning", "retro", "sprint", "all hands", "fiskaly",
        "busy", "therapie", "therapy"
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


def _make_day_range(day_offset: int):
    """Return start/end datetime for a day relative to today."""
    now = datetime.now(VIENNA_TZ)
    target = now + timedelta(days=day_offset)
    start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    end = target.replace(hour=23, minute=59, second=59, microsecond=0)
    return start, end


def get_todays_events() -> list:
    start, end = _make_day_range(0)
    return _fetch_events(start, end)


def get_tomorrows_events() -> list:
    start, end = _make_day_range(1)
    return _fetch_events(start, end)


def get_week_events() -> dict:
    """
    Fetch events for the next 7 days, grouped by day.
    Returns a dict: { "Monday 8 June": [events], "Tuesday 9 June": [events], ... }
    """
    now = datetime.now(VIENNA_TZ)
    week = {}

    for i in range(7):
        target = now + timedelta(days=i)
        start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        end = target.replace(hour=23, minute=59, second=59, microsecond=0)
        day_label = target.strftime("%A %d %B")
        events = _fetch_events(start, end)
        week[day_label] = events

    return week


def get_free_windows(events: list, day_offset: int = 0) -> list:
    """
    Given a list of events for a day, return free time windows of 45+ minutes.
    Used to suggest when to run or do yoga.
    """
    now = datetime.now(VIENNA_TZ)
    target = now + timedelta(days=day_offset)

    # Working hours to consider: 6am to 10pm
    day_start = target.replace(hour=6, minute=0, second=0, microsecond=0)
    day_end = target.replace(hour=22, minute=0, second=0, microsecond=0)

    # Collect timed busy blocks
    busy = []
    for e in events:
        if not e.get("timed"):
            continue
        try:
            from dateutil import parser as dateparser
            s = dateparser.parse(e["start"]).astimezone(VIENNA_TZ)
            en = dateparser.parse(e["end"]).astimezone(VIENNA_TZ)
            busy.append((s, en, e.get("summary", "Busy")))
        except Exception:
            continue

    busy.sort(key=lambda x: x[0])

    # Find gaps
    free = []
    cursor = day_start

    for s, en, label in busy:
        if s > cursor:
            gap_mins = (s - cursor).total_seconds() / 60
            if gap_mins >= 45:
                free.append({
                    "start": cursor.strftime("%H:%M"),
                    "end": s.strftime("%H:%M"),
                    "minutes": int(gap_mins)
                })
        cursor = max(cursor, en)

    # Check after last event
    if cursor < day_end:
        gap_mins = (day_end - cursor).total_seconds() / 60
        if gap_mins >= 45:
            free.append({
                "start": cursor.strftime("%H:%M"),
                "end": day_end.strftime("%H:%M"),
                "minutes": int(gap_mins)
            })

    return free


def get_earliest_event_tomorrow() -> dict | None:
    events = get_tomorrows_events()
    timed = [e for e in events if e.get("timed")]
    if not timed:
        return None
    return timed[0]


def has_training_today() -> bool:
    return any(e.get("type") == "training" for e in get_todays_events())


def has_training_tomorrow() -> bool:
    return any(e.get("type") == "training" for e in get_tomorrows_events())
