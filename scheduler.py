"""
scheduler.py — Proactive messages: morning check-in, reminders, weekly prompts
"""

import os
import logging
import json
from datetime import datetime, date, timedelta
import pytz

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from calendar_client import (
    get_todays_events, get_tomorrows_events,
    get_earliest_event_tomorrow, has_training_tomorrow, has_training_today
)
from database import get_daily_totals, get_fridge, get_last_checkin
from ai_client import ask_claude

logger = logging.getLogger(__name__)
VIENNA_TZ = pytz.timezone("Europe/Vienna")

# Krisz's Telegram chat ID — resolved from username on first /start
# Stored as env var or in DB after first message
KRISZ_CHAT_ID = os.environ.get("KRISZ_CHAT_ID", "")


def get_chat_id(app) -> str:
    """Get Krisz's chat ID — from env or bot_data cache."""
    return KRISZ_CHAT_ID or app.bot_data.get("krisz_chat_id", "")


async def send_message(app, text: str, parse_mode: str = "Markdown"):
    chat_id = get_chat_id(app)
    if not chat_id:
        logger.warning("No chat ID set — can't send proactive message")
        return
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")


# ── Morning check-in — 7:00am weekdays, 9:00am weekends ─────────────────────
async def morning_checkin(app):
    now = datetime.now(VIENNA_TZ)
    today_events = get_todays_events()
    training_events = [e for e in today_events if e.get("type") == "training"]
    training_str = ""
    if training_events:
        names = [e["summary"] for e in training_events]
        training_str = f"\n\n📍 Training on calendar: {', '.join(names)}"

    msg = (
        f"Good morning ☀️ Quick check-in:\n"
        f"1️⃣ WFH or office today?\n"
        f"2️⃣ Anything not on the calendar I should know about?"
        f"{training_str}"
    )
    await send_message(app, msg)
    # Mark that we're awaiting WFH response — handled in bot.py message handler


# ── Pack bag reminder — 9:00pm if training tomorrow ─────────────────────────
async def evening_reminder(app):
    tomorrow_events = get_tomorrows_events()
    training_tomorrow = [e for e in tomorrow_events if e.get("type") == "training"]
    earliest = get_earliest_event_tomorrow()

    messages = []

    if training_tomorrow:
        names = [e["summary"] for e in training_tomorrow]
        messages.append(f"🎒 Pack your kit tonight — you have {', '.join(names)} tomorrow.")

    if earliest and "T" in earliest.get("start", ""):
        try:
            start_str = earliest["start"]
            # Parse and calculate alarm time (90 min before for training, 30 min otherwise)
            from dateutil import parser as dateparser
            event_time = dateparser.parse(start_str).astimezone(VIENNA_TZ)
            buffer = 90 if earliest.get("type") == "training" else 30
            alarm_time = event_time - timedelta(minutes=buffer)
            messages.append(
                f"⏰ Set your alarm for *{alarm_time.strftime('%H:%M')}* "
                f"— {buffer} min before your {event_time.strftime('%H:%M')} {earliest['summary']}."
            )
        except Exception:
            pass

    # Sleep reminder — target 8h, first event or 7:30am default
    try:
        if earliest and "T" in earliest.get("start", ""):
            from dateutil import parser as dateparser
            wake = dateparser.parse(earliest["start"]).astimezone(VIENNA_TZ)
        else:
            wake = datetime.now(VIENNA_TZ).replace(hour=7, minute=30)
        sleep_time = wake - timedelta(hours=8)
        messages.append(f"😴 Aim to be asleep by *{sleep_time.strftime('%H:%M')}* for 8h before tomorrow.")
    except Exception:
        pass

    if messages:
        await send_message(app, "\n\n".join(messages))


# ── Pre-workout fuel reminder — 90 min before training events ────────────────
async def pre_workout_check(app):
    """Runs every 30 min, checks if a training event starts in ~90 minutes."""
    now = datetime.now(VIENNA_TZ)
    today_events = get_todays_events()

    for event in today_events:
        if event.get("type") != "training":
            continue
        start_str = event.get("start", "")
        if "T" not in start_str:
            continue
        try:
            from dateutil import parser as dateparser
            event_time = dateparser.parse(start_str).astimezone(VIENNA_TZ)
            mins_until = (event_time - now).total_seconds() / 60
            if 80 <= mins_until <= 100:  # 80–100 min window
                await send_message(
                    app,
                    f"🍌 *Pre-workout fuel reminder*\n"
                    f"{event['summary']} starts at {event_time.strftime('%H:%M')} "
                    f"— time to think about fuelling.\n\n"
                    f"Suggestion: banana + handful of nuts, or rice cakes with nut butter."
                )
        except Exception:
            continue


# ── End of day nutrition check — 8:30pm ─────────────────────────────────────
async def end_of_day_check(app):
    totals = get_daily_totals(date.today().isoformat())
    cal = totals.get("calories", 0)
    prot = totals.get("protein", 0)
    training_today = has_training_today()
    prot_target = 135 if training_today else 124
    cal_target = 2155 if training_today else 1900

    # Only message if significantly off target
    if prot < prot_target * 0.7:
        deficit = prot_target - prot
        await send_message(
            app,
            f"⚠️ Protein check: {prot}g logged today, target is {prot_target}g.\n"
            f"{deficit}g short — have a protein-heavy snack before bed if you haven't eaten yet.\n"
            f"_(Greek yoghurt, cottage cheese, or a protein shake)_"
        )
    elif cal > cal_target * 1.15:
        over = cal - cal_target
        await send_message(
            app,
            f"📊 You're {over} kcal over target today ({cal} logged). "
            f"Worth noting for tomorrow — no drama."
        )


# ── Friday 12pm weekly check-in prompt ──────────────────────────────────────
async def weekly_checkin_prompt(app):
    last = get_last_checkin()
    last_str = f"Last logged: {last.get('weight')}kg on {last.get('date')}" if last else "No previous data"
    await send_message(
        app,
        f"📊 *Weekly check-in time*\n\n"
        f"{last_str}\n\n"
        f"Send your numbers:\n"
        f"weight: [kg]  muscle: [%]  fat: [%]\n\n"
        f"_(muscle and fat optional — weight is enough)_"
    )


# ── Friday 7pm fridge/meal plan prompt ──────────────────────────────────────
async def weekly_fridge_prompt(app):
    await send_message(
        app,
        f"🧊 *New week starting — what's in your fridge?*\n\n"
        f"List whatever you have at home and I'll build your meal suggestions around it.\n\n"
        f"e.g. eggs, chicken breast, lentils, spinach, greek yoghurt, rice..."
    )


# ── Setup ────────────────────────────────────────────────────────────────────
def setup_scheduler(app):
    scheduler = AsyncIOScheduler(timezone=VIENNA_TZ)

    # Morning check-in: 7:00am Mon–Fri
    scheduler.add_job(
        morning_checkin, CronTrigger(day_of_week="mon-fri", hour=7, minute=0, timezone=VIENNA_TZ),
        args=[app], id="morning_weekday"
    )
    # Morning check-in: 9:00am Sat–Sun
    scheduler.add_job(
        morning_checkin, CronTrigger(day_of_week="sat,sun", hour=9, minute=0, timezone=VIENNA_TZ),
        args=[app], id="morning_weekend"
    )
    # Evening reminders: 9:00pm daily
    scheduler.add_job(
        evening_reminder, CronTrigger(hour=21, minute=0, timezone=VIENNA_TZ),
        args=[app], id="evening_reminder"
    )
    # Pre-workout check: every 30 min
    scheduler.add_job(
        pre_workout_check, CronTrigger(minute="0,30", timezone=VIENNA_TZ),
        args=[app], id="pre_workout"
    )
    # End of day check: 8:30pm
    scheduler.add_job(
        end_of_day_check, CronTrigger(hour=20, minute=30, timezone=VIENNA_TZ),
        args=[app], id="eod_check"
    )
    # Weekly check-in prompt: Friday 12:00pm
    scheduler.add_job(
        weekly_checkin_prompt, CronTrigger(day_of_week="fri", hour=12, minute=0, timezone=VIENNA_TZ),
        args=[app], id="weekly_checkin"
    )
    # Weekly fridge prompt: Friday 7:00pm
    scheduler.add_job(
        weekly_fridge_prompt, CronTrigger(day_of_week="fri", hour=19, minute=0, timezone=VIENNA_TZ),
        args=[app], id="weekly_fridge"
    )

    scheduler.start()
    logger.info("Scheduler started ✅")
    return scheduler
