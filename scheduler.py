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
    get_earliest_event_tomorrow, has_training_tomorrow, has_training_today,
    get_free_windows
)
from database import get_daily_totals, get_fridge, get_last_checkin
from ai_client import ask_claude

logger = logging.getLogger(__name__)
VIENNA_TZ = pytz.timezone("Europe/Vienna")

KRISZ_CHAT_ID = os.environ.get("KRISZ_CHAT_ID", "")

# WFH schedule — Monday=0, Friday=4
WFH_DAYS = {0, 4}

# Morning routine constants
COMMUTE_MINUTES = 45
MORNING_PREP_MINUTES = 40  # skincare, meds, breakfast
OFFICE_BUFFER_MINUTES = COMMUTE_MINUTES + MORNING_PREP_MINUTES  # 85
WFH_BUFFER_MINUTES = 15  # minimal wake-up buffer on WFH days
TRAINING_RECOVERY_MINUTES = 60  # time needed for run + shower/change before leaving


def get_chat_id(app) -> str:
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


# ── Evening reminder — 9:00pm: pack bag / alarm / yoga suggestion ───────────
async def evening_reminder(app):
    tomorrow_events = get_tomorrows_events()
    training_tomorrow = [e for e in tomorrow_events if e.get("type") == "training"]
    earliest = get_earliest_event_tomorrow()

    tomorrow_weekday = (datetime.now(VIENNA_TZ) + timedelta(days=1)).weekday()
    tomorrow_is_wfh = tomorrow_weekday in WFH_DAYS

    messages = []

    # ── Training tomorrow: pack bag ──
    if training_tomorrow:
        names = [e["summary"] for e in training_tomorrow]
        messages.append(f"🎒 Pack your kit tonight — you have {', '.join(names)} tomorrow.")
    else:
        # No run tomorrow — check if there's a good window for Ashtanga yoga
        free_tomorrow = get_free_windows(tomorrow_events, day_offset=1)
        good_windows = [w for w in free_tomorrow if w["minutes"] >= 75]

        if good_windows:
            # Prefer a morning window
            morning_windows = [w for w in good_windows if w["start"] < "12:00"]
            chosen = morning_windows[0] if morning_windows else good_windows[0]
            messages.append(
                f"🧘 No run scheduled tomorrow — good opportunity for Ashtanga. "
                f"You have a window {chosen['start']}–{chosen['end']}. "
                f"Block at least 60 minutes (practice + setup)."
            )

    # ── Alarm time calculation ──
    if earliest and "T" in earliest.get("start", ""):
        try:
            from dateutil import parser as dateparser
            event_time = dateparser.parse(earliest["start"]).astimezone(VIENNA_TZ)

            if tomorrow_is_wfh:
                base_buffer = WFH_BUFFER_MINUTES
            else:
                base_buffer = OFFICE_BUFFER_MINUTES  # 85 min: commute + prep

            training_buffer = TRAINING_RECOVERY_MINUTES if training_tomorrow else 0
            total_buffer = base_buffer + training_buffer

            alarm_time = event_time - timedelta(minutes=total_buffer)

            buffer_note = ""
            if not tomorrow_is_wfh:
                buffer_note = f" (covers {MORNING_PREP_MINUTES}min prep + {COMMUTE_MINUTES}min commute"
                if training_tomorrow:
                    buffer_note += f" + {TRAINING_RECOVERY_MINUTES}min for your run/shower"
                buffer_note += ")"

            messages.append(
                f"⏰ Set your alarm for *{alarm_time.strftime('%H:%M')}*"
                f"{buffer_note} — first commitment at {event_time.strftime('%H:%M')} "
                f"({earliest['summary']})."
            )
        except Exception as e:
            logger.error(f"Alarm calc error: {e}")

    if messages:
        await send_message(app, "\n\n".join(messages))


# ── Pre-workout fuel reminder — checks every 30 min ──────────────────────────
async def pre_workout_check(app):
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
            if 80 <= mins_until <= 100:
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
    fiber = totals.get("fiber", 0)
    training_today = has_training_today()
    prot_target = 135 if training_today else 124
    cal_target = 2155 if training_today else 1900

    if prot < prot_target * 0.7:
        deficit = prot_target - prot
        await send_message(
            app,
            f"⚠️ Protein check: {prot}g logged today, target {prot_target}g.\n"
            f"{deficit}g short — protein-heavy snack before bed if you haven't eaten yet.\n"
            f"_(Greek yoghurt, cottage cheese, or a protein shake)_"
        )
    elif cal > cal_target * 1.15:
        over = cal - cal_target
        await send_message(
            app,
            f"📊 You're {over} kcal over target today ({cal} logged). Worth noting for tomorrow."
        )


# ── Mindful eating check-in — 9:00pm ─────────────────────────────────────────
async def mindful_eating_check(app):
    await send_message(
        app,
        f"🌙 Evening pause — how's the day feeling food-wise?\n\n"
        f"If you're thinking about eating something now, a quick check: hungry, or something else "
        f"going on? Either answer is fine — just worth a moment of awareness before deciding."
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

    scheduler.add_job(
        morning_checkin, CronTrigger(day_of_week="mon-fri", hour=7, minute=0, timezone=VIENNA_TZ),
        args=[app], id="morning_weekday"
    )
    scheduler.add_job(
        morning_checkin, CronTrigger(day_of_week="sat,sun", hour=9, minute=0, timezone=VIENNA_TZ),
        args=[app], id="morning_weekend"
    )
    scheduler.add_job(
        evening_reminder, CronTrigger(hour=21, minute=0, timezone=VIENNA_TZ),
        args=[app], id="evening_reminder"
    )
    scheduler.add_job(
        pre_workout_check, CronTrigger(minute="0,30", timezone=VIENNA_TZ),
        args=[app], id="pre_workout"
    )
    scheduler.add_job(
        end_of_day_check, CronTrigger(hour=20, minute=30, timezone=VIENNA_TZ),
        args=[app], id="eod_check"
    )
    scheduler.add_job(
        mindful_eating_check, CronTrigger(hour=21, minute=0, timezone=VIENNA_TZ),
        args=[app], id="mindful_eating"
    )
    scheduler.add_job(
        weekly_checkin_prompt, CronTrigger(day_of_week="fri", hour=12, minute=0, timezone=VIENNA_TZ),
        args=[app], id="weekly_checkin"
    )
    scheduler.add_job(
        weekly_fridge_prompt, CronTrigger(day_of_week="fri", hour=19, minute=0, timezone=VIENNA_TZ),
        args=[app], id="weekly_fridge"
    )

    scheduler.start()
    logger.info("Scheduler started ✅")
    return scheduler
