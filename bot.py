"""
K_O_trainer_bot — Personal Trainer & Nutritionist Telegram Bot
Powered by Claude (Anthropic API) + Google Calendar
"""

import os
import logging
import json
import tempfile
from datetime import datetime, date, timedelta
import pytz

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

from scheduler import setup_scheduler
from calendar_client import (
    get_todays_events, get_tomorrows_events,
    get_week_events, get_free_windows
)
from database import (
    init_db, log_meal, get_daily_totals, get_config,
    set_config, log_checkin, get_last_checkin,
    set_fridge, get_fridge, add_to_daily_log, get_daily_log,
    delete_todays_meals, set_today_override, get_today_override
)
from ai_client import ask_claude

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

VIENNA_TZ = pytz.timezone("Europe/Vienna")
AUTHORISED_USERNAME = "kriszly"

# WFH schedule — Mon=0, Fri=4 are WFH by default
WFH_DAYS = {0, 4}
OFFICE_DAYS = {1, 2, 3}

# Morning routine constants (used in prompts so Claude's timing advice is consistent
# with what the scheduler calculates)
COMMUTE_MINUTES = 45
MORNING_PREP_MINUTES = 40  # skincare, meds, breakfast
OFFICE_BUFFER_MINUTES = COMMUTE_MINUTES + MORNING_PREP_MINUTES  # 85
WFH_BUFFER_MINUTES = 15

EARLIEST_WAKE = "07:15"  # absolute earliest Krisz will wake up, and only for yoga
YOGA_MINUTES = 60

FIBER_TARGET = 30  # general daily guideline, not a strict target like protein

# Shared timing rules injected into every prompt that touches training scheduling
TIMING_RULES = (
    f"TIMING RULES (important, non-negotiable):\n"
    f"- Krisz does NOT want early-morning runs, ever. Runs fit into WFH daytime gaps or "
    f"evenings (after ~17:30) — never as a pre-work first-thing-in-the-morning session.\n"
    f"- The ONLY activity she's willing to wake up early for is Ashtanga yoga, and her "
    f"absolute earliest acceptable wake time is {EARLIEST_WAKE}. Never suggest waking earlier "
    f"than this, for anything.\n"
    f"- Morning yoga ({YOGA_MINUTES}min starting at {EARLIEST_WAKE}, finishing 08:15) "
    f"is only viable on office days if it still leaves {OFFICE_BUFFER_MINUTES}min before her first "
    f"commitment (i.e. first meeting at ~09:40 or later). On WFH days it just needs "
    f"{WFH_BUFFER_MINUTES}min after.\n"
    f"- If morning yoga doesn't fit on a given day, suggest evening yoga, a different day, or skip "
    f"it — do not suggest waking earlier than {EARLIEST_WAKE} under any circumstance."
)


def is_authorised(update: Update) -> bool:
    return update.effective_user.username == AUTHORISED_USERNAME

def is_wfh_today() -> bool:
    return datetime.now(VIENNA_TZ).weekday() in WFH_DAYS

def get_wfh_status(context_override=None) -> bool:
    if context_override is not None:
        return context_override
    return is_wfh_today()

def strip_heard_prefix(text: str) -> str:
    if text.startswith("Heard:"):
        text = text[6:].strip()
    return text


# ── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return

    existing = get_config("setup_complete")
    if existing:
        await update.message.reply_text(
            "Hey Krisz 👋 Already set up. Send /today for your daily plan or /help for all commands."
        )
        return

    await update.message.reply_text(
        "Hey Krisz! Targets pre-loaded:\n\n"
        "📊 *Targets*\n"
        "• Calories: 2,155 (rest days ~1,900)\n"
        "• Protein: 124g baseline, 135g+ on training days\n"
        f"• Fiber: ~{FIBER_TARGET}g/day (general guideline)\n\n"
        "One question to finish setup:\n\n"
        "What's your current weight in kg?",
        parse_mode="Markdown"
    )
    context.user_data["setup_step"] = 1


# ── /help ────────────────────────────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text(
        "🤖 *K O Trainer Bot — Commands*\n\n"
        "/today — Today's training + nutrition plan\n"
        "/log [food] — Log a meal\n"
        "/totals — Today's calorie, protein & fiber totals\n"
        "/meals — Show all meals logged today\n"
        "/reset — Clear today's food log\n"
        "/fridge — Update your fridge inventory\n"
        "/checkin — Log weekly weight & body comp\n"
        "/week — Full week training + schedule overview\n"
        "/ask [question] — Ask your trainer anything\n"
        "/help — This message\n\n"
        "_Tip: if I get something about today's schedule wrong, just tell me "
        "(e.g. \"I don't have training today\") and I'll adjust for the rest of the day._"
    )


# ── /today ───────────────────────────────────────────────────────────────────
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text("Fetching your calendar... ⏳")

    today_events = get_todays_events()
    tomorrow_events = get_tomorrows_events()
    free_windows = get_free_windows(today_events, day_offset=0)
    tomorrow_free_windows = get_free_windows(tomorrow_events, day_offset=1)
    totals = get_daily_totals(date.today().isoformat())
    meals = get_daily_log(date.today().isoformat())
    fridge = get_fridge()
    last_checkin = get_last_checkin()
    wfh = get_wfh_status(context.user_data.get("wfh_today"))

    prompt = build_daily_prompt(
        today_events, tomorrow_events, free_windows, tomorrow_free_windows,
        totals, meals, fridge, last_checkin, wfh
    )
    response = await ask_claude(prompt, max_tokens=800)
    await update.message.reply_text(response, parse_mode="Markdown")


# ── /log ─────────────────────────────────────────────────────────────────────
async def log_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Tell me what you ate, e.g. /log chicken salad with quinoa")
        return
    await process_food_log(update, text)


# ── /meals ───────────────────────────────────────────────────────────────────
async def meals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    meals = get_daily_log(date.today().isoformat())
    if not meals:
        await update.message.reply_text("Nothing logged yet today.")
        return
    lines = [
        f"• {m['description']} — {m['calories']} kcal · {m['protein']}g protein · {m.get('fiber', 0)}g fiber"
        for m in meals
    ]
    t = get_daily_totals(date.today().isoformat())
    lines.append(f"\n*Total: {t['calories']} kcal · {t['protein']}g protein · {t.get('fiber', 0)}g fiber*")
    await update.message.reply_text(
        "*Today's logged meals:*\n\n" + "\n".join(lines),
        parse_mode="Markdown"
    )


# ── /reset ───────────────────────────────────────────────────────────────────
async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    context.user_data["awaiting_reset_confirm"] = True
    await update.message.reply_text(
        "Delete all food logged today and start from zero?\n\nReply *yes* to confirm.",
        parse_mode="Markdown"
    )


# ── /totals ──────────────────────────────────────────────────────────────────
async def totals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    t = get_daily_totals(date.today().isoformat())
    cal = t.get("calories", 0)
    prot = t.get("protein", 0)
    fiber = t.get("fiber", 0)
    cal_target = 2155
    prot_target = 124
    cal_pct = round((cal / cal_target) * 100)
    prot_pct = round((prot / prot_target) * 100)
    fiber_pct = round((fiber / FIBER_TARGET) * 100)
    now_hour = datetime.now(VIENNA_TZ).hour
    time_context = "morning" if now_hour < 12 else "afternoon" if now_hour < 17 else "evening"

    await update.message.reply_text(
        f"📊 *Today's totals*\n\n"
        f"🔥 Calories: {cal} / {cal_target} kcal ({cal_pct}%)\n"
        f"   {'▓' * min(cal_pct//10, 10)}{'░' * max(0, 10 - cal_pct//10)}\n"
        f"   {cal_target - cal} remaining\n\n"
        f"💪 Protein: {prot} / {prot_target}g ({prot_pct}%)\n"
        f"   {'▓' * min(prot_pct//10, 10)}{'░' * max(0, 10 - prot_pct//10)}\n"
        f"   {prot_target - prot}g remaining\n\n"
        f"🌾 Fiber: {fiber} / ~{FIBER_TARGET}g ({fiber_pct}%)\n"
        f"   {'▓' * min(fiber_pct//10, 10)}{'░' * max(0, 10 - fiber_pct//10)}\n\n"
        f"_{get_totals_comment(cal, prot, cal_target, prot_target, time_context)}_",
        parse_mode="Markdown"
    )


def get_totals_comment(cal, prot, cal_target, prot_target, time_context):
    if time_context == "morning" and cal < 300:
        return "Early — plenty of room ahead."
    if time_context == "evening" and cal > cal_target * 0.95:
        return "On target for the day."
    if prot < prot_target * 0.5 and time_context == "evening":
        return "Protein critically low for this time — prioritise it now."
    if cal > cal_target * 1.1:
        return "Over target — keep dinner light."
    if time_context == "afternoon" and prot < prot_target * 0.4:
        return "Protein lagging — make your next meal protein-heavy."
    return "Tracking well."


# ── /fridge ──────────────────────────────────────────────────────────────────
async def fridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    text = " ".join(context.args) if context.args else ""
    if not text:
        current = get_fridge()
        if current:
            await update.message.reply_text(
                f"🧊 *Current fridge:*\n{current}\n\nTo update: /fridge [what you have]",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("No fridge inventory set. Send: /fridge eggs, chicken, lentils...")
        return
    await process_fridge_update(update, text)


# ── /checkin ─────────────────────────────────────────────────────────────────
async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text(
        "Weekly check-in 📊\n\n"
        "Send your numbers:\n"
        "*weight: [kg]  muscle: [%]  fat: [%]*\n\n"
        "e.g. weight: 68.2  muscle: 34  fat: 28\n\n"
        "Muscle and fat optional.",
        parse_mode="Markdown"
    )
    context.user_data["awaiting_checkin"] = True


# ── /week ────────────────────────────────────────────────────────────────────
async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text("Fetching your full week... ⏳")

    week_events = get_week_events()
    fridge = get_fridge()
    last_checkin = get_last_checkin()

    week_summary = []
    for day_label, events in week_events.items():
        training = [e for e in events if e.get("type") == "training"]
        busy = [e for e in events if e.get("type") in ("work",) or e.get("summary") == "Busy"]
        personal = [e for e in events if e.get("type") == "personal"]
        free = get_free_windows(events)

        day_str = f"{day_label}:"
        if training:
            names = [e["summary"] for e in training]
            day_str += f"\n  Training: {', '.join(names)}"
        if busy:
            timed_busy = [e for e in busy if e.get("timed")]
            if timed_busy:
                slots = []
                for e in timed_busy:
                    try:
                        from dateutil import parser as dp
                        s = dp.parse(e["start"]).astimezone(VIENNA_TZ).strftime("%H:%M")
                        en = dp.parse(e["end"]).astimezone(VIENNA_TZ).strftime("%H:%M")
                        slots.append(f"{s}–{en}")
                    except:
                        slots.append(e.get("summary", "Busy"))
                day_str += f"\n  Busy: {', '.join(slots)}"
        if personal:
            names = [e["summary"] for e in personal if e.get("summary") != "Busy"]
            if names:
                day_str += f"\n  Personal: {', '.join(names)}"
        if free:
            windows = [f"{w['start']}–{w['end']}" for w in free[:2]]
            day_str += f"\n  Free windows: {', '.join(windows)}"
        week_summary.append(day_str)

    wfh_note = "WFH: Monday, Friday. Office: Tuesday, Wednesday, Thursday."

    prompt = (
        f"You are Krisz's personal trainer. Give her a full week training and schedule overview.\n\n"
        f"TODAY: {date.today().strftime('%A %d %B')}\n"
        f"WFH PATTERN: {wfh_note}\n\n"
        f"WEEK CALENDAR:\n" + "\n".join(week_summary) + "\n\n"
        f"FOOD AT HOME: {fridge or 'not set'}\n"
        f"LAST CHECK-IN: {json.dumps(last_checkin)}\n\n"
        f"KRISZ'S PROFILE:\n"
        f"- Half marathon Runna training block. Ashtanga = strength training.\n"
        f"- Runna events in calendar are all-day events with no fixed time — suggest the best time slot.\n"
        f"- WFH days (Mon/Fri): training/yoga fit in calendar gaps. Office days (Tue/Wed/Thu): "
        f"runs go in the evening (after ~17:30).\n"
        f"- Busy blocks are work meetings — immovable.\n"
        f"{TIMING_RULES}\n"
        f"- On days with no run scheduled, suggest Ashtanga yoga (60min incl. setup) if it fits "
        f"per the timing rules above.\n\n"
        f"Provide:\n"
        f"1. For each day with a Runna event: the run details and best suggested time window\n"
        f"2. Best days for Ashtanga if not already covered above\n"
        f"3. Any nutrition notes for heavy training days\n"
        f"4. One thing to watch this week\n\n"
        f"Be specific. Use *bold* for day names only. No ## headers. No fluff. "
        f"Do NOT ask follow-up questions."
    )
    response = await ask_claude(prompt, max_tokens=900)
    await update.message.reply_text(response, parse_mode="Markdown")


# ── /ask ─────────────────────────────────────────────────────────────────────
async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    question = " ".join(context.args) if context.args else ""
    if not question:
        await update.message.reply_text("Ask me anything — e.g. /ask should I run today?")
        return
    today_events = get_todays_events()
    totals_today = get_daily_totals(date.today().isoformat())
    meals_today = get_daily_log(date.today().isoformat())
    last_checkin = get_last_checkin()
    prompt = build_coach_prompt(question, today_events, totals_today, meals_today, last_checkin)
    response = await ask_claude(prompt)
    await update.message.reply_text(response, parse_mode="Markdown")


# ── Free text message handler ────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    text = update.message.text.strip()

    if context.user_data.get("setup_step") == 1:
        try:
            weight = float(text.replace("kg", "").strip())
            log_checkin(date.today().isoformat(), weight, None, None)
            set_config("setup_complete", "true")
            set_config("baseline_weight", str(weight))
            await update.message.reply_text(
                f"✅ {weight}kg logged as your baseline.\n\n"
                f"All set. Send /today for your first daily plan."
            )
            context.user_data.pop("setup_step", None)
        except:
            await update.message.reply_text("Just send a number, e.g. 68.5")
        return

    if context.user_data.get("awaiting_reset_confirm"):
        context.user_data.pop("awaiting_reset_confirm", None)
        if text.lower().strip() == "yes":
            delete_todays_meals()
            await update.message.reply_text("✅ Today's food log cleared. Starting from zero.")
        else:
            await update.message.reply_text("Cancelled — log kept.")
        return

    if context.user_data.get("awaiting_checkin"):
        await handle_checkin_data(update, context, text)
        return

    if context.user_data.get("awaiting_wfh"):
        wfh = "home" in text.lower() or "wfh" in text.lower()
        context.user_data["wfh_today"] = wfh
        context.user_data.pop("awaiting_wfh", None)
        await send_daily_plan(update, context, wfh)
        return

    await handle_smart_message(update, context, text)


# ── Shared: build and send today's plan ──────────────────────────────────────
async def send_daily_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, wfh: bool):
    today_events = get_todays_events()
    tomorrow_events = get_tomorrows_events()
    free_windows = get_free_windows(today_events, day_offset=0)
    tomorrow_free_windows = get_free_windows(tomorrow_events, day_offset=1)
    totals_today = get_daily_totals(date.today().isoformat())
    meals_today = get_daily_log(date.today().isoformat())
    fridge = get_fridge()
    last_checkin_data = get_last_checkin()
    prompt = build_daily_prompt(
        today_events, tomorrow_events, free_windows, tomorrow_free_windows,
        totals_today, meals_today, fridge, last_checkin_data, wfh
    )
    response = await ask_claude(prompt, max_tokens=800)
    await update.message.reply_text(response, parse_mode="Markdown")


# ── Check-in data parser ─────────────────────────────────────────────────────
async def handle_checkin_data(update, context, text):
    import re
    weight = muscle = fat = None
    w = re.search(r'weight[:\s]+(\d+\.?\d*)', text, re.IGNORECASE)
    m = re.search(r'muscle[:\s]+(\d+\.?\d*)', text, re.IGNORECASE)
    f = re.search(r'fat[:\s]+(\d+\.?\d*)', text, re.IGNORECASE)
    if w: weight = float(w.group(1))
    if m: muscle = float(m.group(1))
    if f: fat = float(f.group(1))

    if not weight:
        await update.message.reply_text("Couldn't parse. Try: weight: 68.2  muscle: 34  fat: 28")
        return

    log_checkin(date.today().isoformat(), weight, muscle, fat)
    context.user_data.pop("awaiting_checkin", None)

    last = get_last_checkin(skip_latest=True)
    trend = ""
    if last and last.get("weight"):
        diff = round(weight - last["weight"], 1)
        if diff < 0: trend = f" (↓ {abs(diff)}kg)"
        elif diff > 0: trend = f" (↑ {diff}kg)"
        else: trend = " (no change)"

    details = f"{weight}kg{trend}"
    if muscle: details += f" · {muscle}% muscle"
    if fat: details += f" · {fat}% fat"

    prompt = (
        f"Krisz just logged: {details}. Previous: {json.dumps(last)}.\n"
        f"Goal: recomposition + marathon training. Targets: 2155 kcal, 124g protein.\n"
        f"2-3 sentence honest assessment. Flag anything worth adjusting. No cheerleading. No headers. "
        f"Do NOT ask follow-up questions."
    )
    assessment = await ask_claude(prompt)
    await update.message.reply_text(f"✅ Check-in logged: {details}\n\n{assessment}", parse_mode="Markdown")


# ── Voice message handler ────────────────────────────────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text("🎙 Got it, transcribing...")

    try:
        from faster_whisper import WhisperModel

        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        await file.download_to_drive(tmp_path)
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(tmp_path)
        text = " ".join([s.text for s in segments]).strip()
        os.unlink(tmp_path)

        if not text:
            await update.message.reply_text("Couldn't make that out — try again?")
            return

        await update.message.reply_text(f"_Heard: {text}_", parse_mode="Markdown")
        await handle_smart_message(update, context, text)

    except ImportError:
        await update.message.reply_text("⚠️ faster-whisper not installed.")
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("Transcription failed — try typing it.")


# ── Smart intent detection ───────────────────────────────────────────────────
async def handle_smart_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    clean_text = strip_heard_prefix(text)

    intent_prompt = (
        f"Classify this message into intent(s):\n\n"
        f"- food_log: reporting what she has eaten or is eating right now\n"
        f"- food_question: asking about calories, nutrition, or what to eat — NOT reporting eating\n"
        f"- delete_log: wants to delete, clear, reset logged food\n"
        f"- show_log: wants to see what she logged today\n"
        f"- weight_checkin: sharing weight, body fat, or muscle percentage numbers\n"
        f"- fridge_update: listing food she has at home\n"
        f"- wfh_response: saying whether working from home or office today\n"
        f"- schedule_correction: correcting or clarifying something about TODAY's schedule or "
        f"training that was WRONG on the calendar (e.g. 'I don't have training today', "
        f"'that run was yesterday', 'no training scheduled')\n"
        f"- missed_training: she SKIPPED or DIDN'T DO a training session that WAS correctly "
        f"scheduled (e.g. 'I missed my run today', 'didn't get to my run', 'skipped training', "
        f"'couldn't do the run', 'when should I reschedule my missed run')\n"
        f"- question: anything else\n\n"
        f"Message: '{clean_text}'\n\n"
        f"If the message contains MULTIPLE distinct things — e.g. she's reporting food AND "
        f"separately asking about a missed run, or asking two unrelated things — reply with ALL "
        f"relevant intents separated by commas (e.g. 'food_log,missed_training'). "
        f"Otherwise reply with just one intent word. No other text."
    )
    intent_response = (await ask_claude(intent_prompt, max_tokens=30)).strip().lower()
    intents = [i.strip() for i in intent_response.split(",") if i.strip()]
    logger.info(f"Intents: {intents} | Message: {clean_text[:60]}")

    handled = False

    if "food_log" in intents:
        await process_food_log(update, clean_text)
        handled = True

    if "fridge_update" in intents:
        await process_fridge_update(update, clean_text)
        handled = True

    if "weight_checkin" in intents:
        await handle_checkin_data(update, context, clean_text)
        handled = True

    if "missed_training" in intents:
        set_today_override(
            date.today().isoformat(),
            f"Missed/skipped a scheduled training session today: {clean_text}"
        )
        await handle_missed_training(update, clean_text)
        handled = True

    if "delete_log" in intents:
        context.user_data["awaiting_reset_confirm"] = True
        await update.message.reply_text(
            "Delete all food logged today?\n\nReply *yes* to confirm.",
            parse_mode="Markdown"
        )
        handled = True

    if "show_log" in intents:
        meals = get_daily_log(date.today().isoformat())
        if not meals:
            await update.message.reply_text("Nothing logged yet today.")
        else:
            lines = [
                f"• {m['description']} — {m['calories']} kcal · {m['protein']}g protein · {m.get('fiber', 0)}g fiber"
                for m in meals
            ]
            t = get_daily_totals(date.today().isoformat())
            lines.append(f"\n*Total: {t['calories']} kcal · {t['protein']}g protein · {t.get('fiber', 0)}g fiber*")
            await update.message.reply_text("*Today's logged meals:*\n\n" + "\n".join(lines), parse_mode="Markdown")
        handled = True

    # schedule_correction and wfh_response both produce a full daily plan —
    # only run one of them to avoid sending the plan twice
    if "schedule_correction" in intents and "missed_training" not in intents:
        set_today_override(date.today().isoformat(), clean_text)
        wfh = get_wfh_status(context.user_data.get("wfh_today"))
        await update.message.reply_text("Got it — noted for today. Updating your plan...")
        await send_daily_plan(update, context, wfh)
        handled = True
    elif "wfh_response" in intents:
        wfh = any(w in clean_text.lower() for w in ["home", "wfh", "remote"])
        context.user_data["wfh_today"] = wfh
        await send_daily_plan(update, context, wfh)
        handled = True

    if not handled:
        today_events = get_todays_events()
        totals_today = get_daily_totals(date.today().isoformat())
        meals_today = get_daily_log(date.today().isoformat())
        last_checkin_data = get_last_checkin()
        prompt = build_coach_prompt(clean_text, today_events, totals_today, meals_today, last_checkin_data)
        response = await ask_claude(prompt)
        await update.message.reply_text(response, parse_mode="Markdown")


# ── Food log processor ───────────────────────────────────────────────────────
async def process_food_log(update: Update, text: str):
    totals_before = get_daily_totals(date.today().isoformat())
    prompt = (
        f"Krisz just ate: '{text}'\n"
        f"Totals so far today: {json.dumps(totals_before)}\n"
        f"Targets: 2155 kcal, 124g protein (135g on training days), ~{FIBER_TARGET}g fiber (general guideline).\n\n"
        f"Estimate calories, protein, and fiber. Be accurate — include ALL items mentioned.\n"
        f"Reply in this EXACT format, nothing else:\n"
        f"CALORIES: [integer]\n"
        f"PROTEIN: [integer]\n"
        f"FIBER: [integer]\n"
        f"COMMENT: [ONE sentence about NUTRITION ONLY — running totals, protein/fiber gap. "
        f"If the message also mentions training, schedule, missed runs, or anything non-food, "
        f"IGNORE that completely here — it's handled in a separate message.]"
    )
    response = await ask_claude(prompt)
    lines = response.strip().split("\n")
    cal = prot = fiber = None
    comment = ""

    for line in lines:
        line = line.strip()
        if line.upper().startswith("CALORIES:"):
            try:
                val = line.split(":", 1)[1].strip().split()[0].replace(",", "")
                cal = int(float(val))
            except:
                pass
        elif line.upper().startswith("PROTEIN:"):
            try:
                val = line.split(":", 1)[1].strip().split()[0].replace(",", "")
                prot = int(float(val))
            except:
                pass
        elif line.upper().startswith("FIBER:"):
            try:
                val = line.split(":", 1)[1].strip().split()[0].replace(",", "")
                fiber = int(float(val))
            except:
                pass
        elif line.upper().startswith("COMMENT:"):
            comment = line.split(":", 1)[1].strip()

    import re
    if cal is None:
        m = re.search(r'CALORIES[:\s]+(\d+)', response, re.IGNORECASE)
        if m: cal = int(m.group(1))
    if prot is None:
        m = re.search(r'PROTEIN[:\s]+(\d+)', response, re.IGNORECASE)
        if m: prot = int(m.group(1))
    if fiber is None:
        m = re.search(r'FIBER[:\s]+(\d+)', response, re.IGNORECASE)
        if m: fiber = int(m.group(1))

    if cal and cal > 0:
        prot = prot or 0
        fiber = fiber or 0
        log_meal(date.today().isoformat(), text, cal, prot, fiber)
        new_totals = get_daily_totals(date.today().isoformat())
        await update.message.reply_text(
            f"✅ Logged: *{text}*\n"
            f"~{cal} kcal · {prot}g protein · {fiber}g fiber\n\n"
            f"📊 *Today so far:* {new_totals['calories']} kcal · {new_totals['protein']}g protein · "
            f"{new_totals.get('fiber', 0)}g fiber\n\n"
            f"_{comment}_",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"Couldn't estimate calories for that. Try being more specific, e.g. '150g chicken breast and salad'."
        )


# ── Fridge update processor ──────────────────────────────────────────────────
async def process_fridge_update(update: Update, text: str):
    clean = strip_heard_prefix(text)
    for prefix in ["this is what i have in my fridge", "i have in my fridge", "my fridge has", "in my fridge:"]:
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):].strip(":").strip()
            break

    set_fridge(clean)
    prompt = (
        f"Krisz has EXACTLY this and nothing else: {clean}\n"
        f"Targets: ~2155 kcal/day, 124g protein, ~{FIBER_TARGET}g fiber. Simple meals, variety, no long prep.\n"
        f"Suggest 3 meals for the week using ONLY ingredients from the list above — do not invent or assume "
        f"any ingredient not explicitly listed. One line each with rough kcal and protein. No headers."
    )
    suggestions = await ask_claude(prompt)
    await update.message.reply_text(
        f"✅ Fridge updated.\n\n🍽 *Meal ideas:*\n{suggestions}",
        parse_mode="Markdown"
    )


# ── Missed training handler ──────────────────────────────────────────────────
async def handle_missed_training(update: Update, text: str):
    """When Krisz reports a missed/skipped session, suggest where to fit it back in this week."""
    week_events = get_week_events()

    summary_lines = []
    for i, (day_label, events) in enumerate(week_events.items()):
        weekday = (datetime.now(VIENNA_TZ).weekday() + i) % 7
        is_wfh = weekday in WFH_DAYS
        training = [e for e in events if e.get("type") == "training"]
        free = get_free_windows(events)

        training_str = ", ".join([e["summary"] for e in training]) if training else "none"
        free_str = ", ".join([f"{w['start']}–{w['end']} ({w['minutes']}min)" for w in free[:3]]) if free else "none"
        tag = "TODAY" if i == 0 else ("WFH" if is_wfh else "office")
        summary_lines.append(f"{day_label} [{tag}]: training={training_str}; free windows={free_str}")

    prompt = (
        f"Krisz just told her trainer: '{text}'\n"
        f"This means she skipped/missed a training session that was scheduled for today.\n\n"
        f"Rest of week calendar:\n" + "\n".join(summary_lines) + "\n\n"
        f"PROFILE: Half marathon training block, Ashtanga = strength training. "
        f"{TIMING_RULES}\n\n"
        f"DECISION CRITERIA for skip vs reschedule:\n"
        f"- First, classify the missed session by its name (visible in today's training above): "
        f"'Easy run' = LOW priority, fine to drop entirely without makeup. 'Long run' or "
        f"'Progressive run' = HIGHER priority, worth fitting in once IF a genuinely free day "
        f"exists without compressing recovery. Speed/interval sessions (e.g. 'Rolling 400s', "
        f"'Tempo', 'Intervals', 'Fartlek') = MODERATE priority, usually fine to drop — one missed "
        f"speed session rarely affects a half marathon block. Ashtanga/yoga = flexible, move freely.\n"
        f"- NEVER place the makeup session on a day immediately before or after another hard "
        f"session already on the calendar this week — no back-to-back hard days.\n"
        f"- If no day satisfies the above without compressing recovery, choose to SKIP — say so "
        f"plainly with the reason.\n"
        f"- Default toward skipping LOW and MODERATE priority sessions rather than cramming. "
        f"Only fight to reschedule a LONG run, and only if it genuinely fits.\n\n"
        f"Give exactly ONE decisive recommendation — either:\n"
        f"(a) ONE specific day AND specific time window to do the makeup session, or\n"
        f"(b) a clear decision to skip it this week, with a one-line reason based on the criteria above.\n"
        f"Do NOT present multiple options or alternatives. Pick one and commit. "
        f"2-3 sentences max. No headers, no follow-up questions."
    )
    response = await ask_claude(prompt, max_tokens=300)
    await update.message.reply_text(f"No worries — noted.\n\n{response}", parse_mode="Markdown")


# ── Prompt builders ──────────────────────────────────────────────────────────
def build_daily_prompt(today_events, tomorrow_events, free_windows, tomorrow_free_windows,
                        totals, meals, fridge, last_checkin, wfh):
    today_str = date.today().strftime("%A %d %B")
    wfh_str = "working from home" if wfh else "in the office (Mariahilfer Strasse 54)"

    meals_str = "\n".join([
        f"- {m['description']}: {m['calories']} kcal, {m['protein']}g protein, {m.get('fiber', 0)}g fiber"
        for m in meals
    ]) if meals else "Nothing logged yet"

    training_today = [e for e in today_events if e.get("type") == "training"]
    training_str = ", ".join([e["summary"] for e in training_today]) if training_today else "None scheduled"

    training_tomorrow = [e for e in tomorrow_events if e.get("type") == "training"]
    training_tomorrow_str = ", ".join([e["summary"] for e in training_tomorrow]) if training_tomorrow else "None scheduled"

    free_str = ", ".join([f"{w['start']}–{w['end']} ({w['minutes']}min)" for w in free_windows]) if free_windows else "No clear gaps found"
    free_tomorrow_str = ", ".join([f"{w['start']}–{w['end']} ({w['minutes']}min)" for w in tomorrow_free_windows]) if tomorrow_free_windows else "No clear gaps found"

    override = get_today_override(date.today().isoformat())
    override_str = (
        f"\nSCHEDULE CORRECTION FROM KRISZ (this OVERRIDES any conflicting calendar training "
        f"event for today — if she said no training today, do NOT suggest training today): {override}\n"
        if override else ""
    )

    tomorrow_weekday = (datetime.now(VIENNA_TZ) + timedelta(days=1)).weekday()
    tomorrow_wfh = tomorrow_weekday in WFH_DAYS
    tomorrow_wfh_str = "WFH" if tomorrow_wfh else "office (Mariahilfer Strasse 54)"

    return (
        f"You are Krisz's personal trainer and nutritionist. Today is {today_str}.\n"
        f"She is {wfh_str}.\n"
        f"{override_str}\n"
        f"CALENDAR TODAY:\n{format_events(today_events)}\n"
        f"TRAINING TODAY: {training_str}\n"
        f"FREE TIME WINDOWS TODAY: {free_str}\n\n"
        f"CALENDAR TOMORROW ({tomorrow_wfh_str}):\n{format_events(tomorrow_events)}\n"
        f"TRAINING TOMORROW: {training_tomorrow_str}\n"
        f"FREE TIME WINDOWS TOMORROW: {free_tomorrow_str}\n\n"
        f"FOOD AT HOME: {fridge or 'not updated'}\n"
        f"TODAY NUTRITION TOTALS: {json.dumps(totals)}\n"
        f"TODAY LOGGED MEALS:\n{meals_str}\n"
        f"LAST CHECK-IN: {json.dumps(last_checkin)}\n\n"
        f"KRISZ'S PROFILE:\n"
        f"- Half marathon Runna block. Ashtanga = strength training.\n"
        f"- Runna events are all-day with no fixed time. Suggest the best time window from free windows.\n"
        f"- MORNING ROUTINE: needs {MORNING_PREP_MINUTES}min for wake-up/prep (skincare, meds, breakfast). "
        f"On OFFICE days additionally needs {COMMUTE_MINUTES}min commute — total {OFFICE_BUFFER_MINUTES}min "
        f"before she can leave home, AFTER any training+shower (~15min). On WFH days the buffer is "
        f"only ~{WFH_BUFFER_MINUTES}min.\n"
        f"- WFH Mon+Fri: training/yoga can fit during day gaps. Office Tue/Wed/Thu: runs go in "
        f"the evening (after ~17:30).\n"
        f"{TIMING_RULES}\n"
        f"- Targets: 2155 kcal/day, 124g protein (135g training days), rest days ~1900 kcal, "
        f"~{FIBER_TARGET}g fiber (general guideline, not strict).\n"
        f"- Simple meals, gets bored of meal prep. Food noise issues — don't suggest extra eating.\n"
        f"- Tone: direct, no fluff, no cheerleading, do NOT ask follow-up questions.\n\n"
        f"Give her daily brief:\n"
        f"1. *Training* — what and specifically when today (use free windows + morning routine constraints)\n"
        f"2. *Nutrition* — what to eat based on fridge (use ONLY listed ingredients) and WFH/office status. "
        f"Match meal type to time of day (breakfast in the morning, not lunch/dinner options).\n"
        f"3. *Tomorrow* — pack bag/alarm if training tomorrow. If NO training is scheduled tomorrow AND "
        f"there's a free window of 75+ minutes, suggest Ashtanga yoga (60min incl. setup) at the best time "
        f"given tomorrow's {tomorrow_wfh_str} status and free windows.\n\n"
        f"Use *bold* for section labels only. No ## headers."
    )


def build_coach_prompt(question, today_events, totals, meals, last_checkin):
    today_str = date.today().strftime("%A %d %B")
    meals_str = "\n".join([
        f"- {m['description']}: {m['calories']} kcal, {m['protein']}g protein, {m.get('fiber', 0)}g fiber"
        for m in meals
    ]) if meals else "Nothing logged yet"

    override = get_today_override(date.today().isoformat())
    override_str = f"\nSCHEDULE CORRECTION FROM KRISZ TODAY (overrides calendar): {override}\n" if override else ""

    now_hour = datetime.now(VIENNA_TZ).hour
    time_of_day = "morning" if now_hour < 11 else "midday" if now_hour < 15 else "afternoon" if now_hour < 18 else "evening"

    return (
        f"You are Krisz's personal trainer. Today is {today_str}, it's currently {time_of_day} "
        f"({datetime.now(VIENNA_TZ).strftime('%H:%M')}).\n"
        f"{override_str}"
        f"CALENDAR: {format_events(today_events)}\n"
        f"NUTRITION TOTALS: {json.dumps(totals)}\n"
        f"LOGGED MEALS:\n{meals_str}\n"
        f"LAST CHECK-IN: {json.dumps(last_checkin)}\n\n"
        f"PROFILE: Half marathon + Ashtanga (strength). Targets: 2155 kcal, 124g protein, "
        f"~{FIBER_TARGET}g fiber. WFH Mon+Fri, office Tue/Wed/Thu. "
        f"Morning routine needs {MORNING_PREP_MINUTES}min prep (+{COMMUTE_MINUTES}min commute on office days). "
        f"{TIMING_RULES}\n"
        f"Direct tone, no fluff, do NOT ask follow-up questions. "
        f"If asked about food/meals, match suggestions to the CURRENT time of day ({time_of_day}) — "
        f"don't give lunch/dinner options if it's morning, etc.\n\n"
        f"Question: {question}\n\nAnswer directly and concisely. No ## headers."
    )


def format_events(events: list) -> str:
    if not events:
        return "No events"
    lines = []
    for e in events:
        summary = e.get("summary", "Busy")
        cal = e.get("calendar", "")
        if e.get("all_day"):
            lines.append(f"  [All day] {summary} ({cal})")
        else:
            try:
                from dateutil import parser as dp
                s = dp.parse(e["start"]).astimezone(VIENNA_TZ).strftime("%H:%M")
                en = dp.parse(e["end"]).astimezone(VIENNA_TZ).strftime("%H:%M")
                lines.append(f"  {s}–{en} {summary} ({cal})")
            except:
                lines.append(f"  {summary} ({cal})")
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    init_db()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("log", log_food))
    app.add_handler(CommandHandler("totals", totals_command))
    app.add_handler(CommandHandler("meals", meals_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("fridge", fridge_command))
    app.add_handler(CommandHandler("checkin", checkin))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    setup_scheduler(app)

    logger.info("K_O_trainer_bot started ✅")
    app.run_polling()


if __name__ == "__main__":
    main()
