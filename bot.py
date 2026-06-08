"""
K_O_trainer_bot — Personal Trainer & Nutritionist Telegram Bot
Powered by Claude (Anthropic API) + Google Calendar
"""

import os
import logging
import json
import tempfile
from datetime import datetime, date
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
    set_fridge, get_fridge, add_to_daily_log, get_daily_log
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
WFH_DAYS = {0, 4}  # Monday, Friday
OFFICE_DAYS = {1, 2, 3}  # Tuesday, Wednesday, Thursday

def is_authorised(update: Update) -> bool:
    return update.effective_user.username == AUTHORISED_USERNAME

def is_wfh_today() -> bool:
    return datetime.now(VIENNA_TZ).weekday() in WFH_DAYS

def get_wfh_status(context_override=None) -> bool:
    """Return WFH status — context override wins, otherwise use weekly default."""
    if context_override is not None:
        return context_override
    return is_wfh_today()

def strip_heard_prefix(text: str) -> str:
    """Remove 'Heard: ' prefix that appears in voice-transcribed messages."""
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
        "• Protein: 124g baseline, 135g+ on training days\n\n"
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
        "/totals — Today's calorie & protein totals\n"
        "/meals — Show all meals logged today\n"
        "/reset — Clear today's food log\n"
        "/fridge — Update your fridge inventory\n"
        "/checkin — Log weekly weight & body comp\n"
        "/week — Full week training + schedule overview\n"
        "/ask [question] — Ask your trainer anything\n"
        "/help — This message",
        parse_mode="Markdown"
    )


# ── /today ───────────────────────────────────────────────────────────────────
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text("Fetching your calendar... ⏳")

    today_events = get_todays_events()
    tomorrow_events = get_tomorrows_events()
    free_windows = get_free_windows(today_events, day_offset=0)
    totals = get_daily_totals(date.today().isoformat())
    meals = get_daily_log(date.today().isoformat())
    fridge = get_fridge()
    last_checkin = get_last_checkin()
    wfh = get_wfh_status(context.user_data.get("wfh_today"))

    prompt = build_daily_prompt(
        today_events, tomorrow_events, free_windows,
        totals, meals, fridge, last_checkin, wfh
    )
    response = await ask_claude(prompt)
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
    lines = [f"• {m['description']} — {m['calories']} kcal · {m['protein']}g protein" for m in meals]
    t = get_daily_totals(date.today().isoformat())
    lines.append(f"\n*Total: {t['calories']} kcal · {t['protein']}g protein*")
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
    cal_target = 2155
    prot_target = 124
    cal_pct = round((cal / cal_target) * 100)
    prot_pct = round((prot / prot_target) * 100)
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

    # Build a readable week summary
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

    # Determine WFH pattern for the week
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
        f"- Runna events in calendar are all-day events with no fixed time — suggest the best time slot based on her day.\n"
        f"- On WFH days: runs can fit between calendar gaps during the day.\n"
        f"- On office days: runs only before 8am or after 18:30.\n"
        f"- Busy blocks are work meetings — treat as immovable.\n\n"
        f"Provide:\n"
        f"1. For each day with a Runna event: the run details and the best suggested time window\n"
        f"2. Best days for Ashtanga if not already scheduled\n"
        f"3. Any nutrition notes for heavy training days\n"
        f"4. One thing to watch this week\n\n"
        f"Be specific and practical. Use *bold* for day names only. No ## headers. No fluff."
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

    # Setup flow
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

    # Reset confirmation
    if context.user_data.get("awaiting_reset_confirm"):
        context.user_data.pop("awaiting_reset_confirm", None)
        if text.lower().strip() == "yes":
            delete_todays_meals()
            await update.message.reply_text("✅ Today's food log cleared. Starting from zero.")
        else:
            await update.message.reply_text("Cancelled — log kept.")
        return

    # Check-in data
    if context.user_data.get("awaiting_checkin"):
        await handle_checkin_data(update, context, text)
        return

    # WFH override (only needed if changing from default)
    if context.user_data.get("awaiting_wfh"):
        wfh = "home" in text.lower() or "wfh" in text.lower()
        context.user_data["wfh_today"] = wfh
        context.user_data.pop("awaiting_wfh", None)
        today_events = get_todays_events()
        tomorrow_events = get_tomorrows_events()
        free_windows = get_free_windows(today_events)
        totals_today = get_daily_totals(date.today().isoformat())
        meals_today = get_daily_log(date.today().isoformat())
        fridge = get_fridge()
        last_checkin_data = get_last_checkin()
        prompt = build_daily_prompt(
            today_events, tomorrow_events, free_windows,
            totals_today, meals_today, fridge, last_checkin_data, wfh
        )
        response = await ask_claude(prompt)
        await update.message.reply_text(response, parse_mode="Markdown")
        return

    await handle_smart_message(update, context, text)


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
        f"2-3 sentence honest assessment. Flag anything worth adjusting. No cheerleading. No headers."
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
    # Strip voice transcription prefix before intent detection
    clean_text = strip_heard_prefix(text)

    intent_prompt = (
        f"Classify this message into exactly one intent:\n\n"
        f"- food_log: reporting what she has eaten or is eating right now\n"
        f"- food_question: asking about calories, nutrition, or what to eat — NOT reporting eating\n"
        f"- delete_log: wants to delete, clear, reset logged food\n"
        f"- show_log: wants to see what she logged today\n"
        f"- weight_checkin: sharing weight, body fat, or muscle percentage numbers\n"
        f"- fridge_update: listing food she has at home\n"
        f"- wfh_response: saying whether working from home or office today\n"
        f"- question: anything else\n\n"
        f"Message: '{clean_text}'\n\n"
        f"Reply with ONLY the intent word."
    )
    intent = (await ask_claude(intent_prompt, max_tokens=20)).strip().lower()
    logger.info(f"Intent: {intent} | Message: {clean_text[:60]}")

    if "food_log" in intent:
        await process_food_log(update, clean_text)

    elif "delete_log" in intent:
        context.user_data["awaiting_reset_confirm"] = True
        await update.message.reply_text(
            "Delete all food logged today?\n\nReply *yes* to confirm.",
            parse_mode="Markdown"
        )

    elif "show_log" in intent:
        meals = get_daily_log(date.today().isoformat())
        if not meals:
            await update.message.reply_text("Nothing logged yet today.")
            return
        lines = [f"• {m['description']} — {m['calories']} kcal · {m['protein']}g protein" for m in meals]
        t = get_daily_totals(date.today().isoformat())
        lines.append(f"\n*Total: {t['calories']} kcal · {t['protein']}g protein*")
        await update.message.reply_text("*Today's logged meals:*\n\n" + "\n".join(lines), parse_mode="Markdown")

    elif "weight_checkin" in intent:
        await handle_checkin_data(update, context, clean_text)

    elif "fridge_update" in intent:
        await process_fridge_update(update, clean_text)

    elif "wfh_response" in intent:
        wfh = any(w in clean_text.lower() for w in ["home", "wfh", "remote"])
        context.user_data["wfh_today"] = wfh
        today_events = get_todays_events()
        tomorrow_events = get_tomorrows_events()
        free_windows = get_free_windows(today_events)
        totals_today = get_daily_totals(date.today().isoformat())
        meals_today = get_daily_log(date.today().isoformat())
        fridge = get_fridge()
        last_checkin_data = get_last_checkin()
        prompt = build_daily_prompt(
            today_events, tomorrow_events, free_windows,
            totals_today, meals_today, fridge, last_checkin_data, wfh
        )
        response = await ask_claude(prompt)
        await update.message.reply_text(response, parse_mode="Markdown")

    else:
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
        f"Targets: 2155 kcal, 124g protein (135g on training days).\n\n"
        f"Estimate calories and protein. Be accurate — include ALL items mentioned.\n"
        f"Reply in this EXACT format, nothing else:\n"
        f"CALORIES: [integer]\n"
        f"PROTEIN: [integer]\n"
        f"COMMENT: [one sentence on running totals or protein gap]"
    )
    response = await ask_claude(prompt)
    lines = response.strip().split("\n")
    cal = prot = None
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
        elif line.upper().startswith("COMMENT:"):
            comment = line.split(":", 1)[1].strip()

    # Fallback: if still None, try finding any number after the keyword
    if cal is None or prot is None:
        import re
        if cal is None:
            m = re.search(r'CALORIES[:\s]+(\d+)', response, re.IGNORECASE)
            if m: cal = int(m.group(1))
        if prot is None:
            m = re.search(r'PROTEIN[:\s]+(\d+)', response, re.IGNORECASE)
            if m: prot = int(m.group(1))

    if cal and cal > 0:
        prot = prot or 0
        log_meal(date.today().isoformat(), text, cal, prot)
        new_totals = get_daily_totals(date.today().isoformat())
        await update.message.reply_text(
            f"✅ Logged: *{text}*\n"
            f"~{cal} kcal · {prot}g protein\n\n"
            f"📊 *Today so far:* {new_totals['calories']} kcal · {new_totals['protein']}g protein\n\n"
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
    # Also strip common phrases before the actual food list
    for prefix in ["this is what i have in my fridge", "i have in my fridge", "my fridge has", "in my fridge:"]:
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):].strip(":").strip()
            break

    set_fridge(clean)
    prompt = (
        f"Krisz has: {clean}\n"
        f"Targets: ~2155 kcal/day, 124g protein. Simple meals, variety, no long prep.\n"
        f"Suggest 3 meals for the week. One line each with rough kcal and protein. No headers."
    )
    suggestions = await ask_claude(prompt)
    await update.message.reply_text(
        f"✅ Fridge updated.\n\n🍽 *Meal ideas:*\n{suggestions}",
        parse_mode="Markdown"
    )


# ── Delete today's meals ─────────────────────────────────────────────────────
def delete_todays_meals():
    from database import get_conn
    conn = get_conn()
    conn.execute("DELETE FROM meals WHERE date = ?", (date.today().isoformat(),))
    conn.commit()
    conn.close()


# ── Prompt builders ──────────────────────────────────────────────────────────
def build_daily_prompt(today_events, tomorrow_events, free_windows, totals, meals, fridge, last_checkin, wfh):
    today_str = date.today().strftime("%A %d %B")
    weekday = datetime.now(VIENNA_TZ).weekday()
    wfh_str = "working from home" if wfh else "in the office (Mariahilfer Strasse 54)"

    meals_str = "\n".join([
        f"- {m['description']}: {m['calories']} kcal, {m['protein']}g protein"
        for m in meals
    ]) if meals else "Nothing logged yet"

    training_today = [e for e in today_events if e.get("type") == "training"]
    training_str = ", ".join([e["summary"] for e in training_today]) if training_today else "None scheduled"

    free_str = ", ".join([f"{w['start']}–{w['end']} ({w['minutes']}min)" for w in free_windows]) if free_windows else "No clear gaps found"

    return (
        f"You are Krisz's personal trainer and nutritionist. Today is {today_str}.\n"
        f"She is {wfh_str}.\n\n"
        f"CALENDAR TODAY:\n{format_events(today_events)}\n"
        f"TRAINING TODAY: {training_str}\n"
        f"FREE TIME WINDOWS TODAY: {free_str}\n"
        f"CALENDAR TOMORROW:\n{format_events(tomorrow_events)}\n"
        f"FOOD AT HOME: {fridge or 'not updated'}\n"
        f"TODAY NUTRITION TOTALS: {json.dumps(totals)}\n"
        f"TODAY LOGGED MEALS:\n{meals_str}\n"
        f"LAST CHECK-IN: {json.dumps(last_checkin)}\n\n"
        f"KRISZ'S PROFILE:\n"
        f"- Half marathon Runna block. Ashtanga = strength training.\n"
        f"- Runna events are all-day with no fixed time. Suggest the best time window from free windows above.\n"
        f"- WFH Mon+Fri: runs can fit during day gaps. Office Tue/Wed/Thu: runs only before 8am or after 18:30.\n"
        f"- Targets: 2155 kcal/day, 124g protein (135g training days), rest days ~1900 kcal.\n"
        f"- Simple meals, gets bored of meal prep. Food noise issues — don't suggest extra eating.\n"
        f"- Tone: direct, no fluff, no cheerleading, do NOT ask follow-up questions.\n\n"
        f"Give her daily brief:\n"
        f"1. *Training* — what and specifically when (use free windows)\n"
        f"2. *Nutrition* — what to eat based on fridge and WFH/office status\n"
        f"3. *Tomorrow* — pack bag or alarm if needed\n\n"
        f"Use *bold* for section labels only. No ## headers."
    )


def build_coach_prompt(question, today_events, totals, meals, last_checkin):
    today_str = date.today().strftime("%A %d %B")
    meals_str = "\n".join([
        f"- {m['description']}: {m['calories']} kcal, {m['protein']}g protein"
        for m in meals
    ]) if meals else "Nothing logged yet"

    return (
        f"You are Krisz's personal trainer. Today is {today_str}.\n"
        f"CALENDAR: {format_events(today_events)}\n"
        f"NUTRITION TOTALS: {json.dumps(totals)}\n"
        f"LOGGED MEALS:\n{meals_str}\n"
        f"LAST CHECK-IN: {json.dumps(last_checkin)}\n\n"
        f"PROFILE: Half marathon + Ashtanga (strength). Targets: 2155 kcal, 124g protein. "
        f"WFH Mon+Fri, office Tue/Wed/Thu. Direct tone, no fluff, do NOT ask follow-up questions.\n\n"
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
