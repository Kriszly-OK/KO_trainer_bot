"""
K_O_trainer_bot — Personal Trainer & Nutritionist Telegram Bot
Powered by Claude (Anthropic API) + Google Calendar
"""

import os
import logging
import json
from datetime import datetime, date
import pytz

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

from scheduler import setup_scheduler
from calendar_client import get_todays_events, get_tomorrows_events
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

# ── Authorised user ──────────────────────────────────────────────────────────
AUTHORISED_USERNAME = "kriszly"  # without @

def is_authorised(update: Update) -> bool:
    return update.effective_user.username == AUTHORISED_USERNAME


# ── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return

    existing = get_config("setup_complete")
    if existing:
        await update.message.reply_text(
            "Hey Krisz 👋 You're already set up. Send /today for your daily plan or /help for all commands."
        )
        return

    await update.message.reply_text(
        "Hey Krisz! Let's get you set up. I've got your core targets pre-loaded:\n\n"
        "📊 *Targets*\n"
        "• Calories: 2,155 (rest days ~1,900)\n"
        "• Protein: 124g baseline, 135g+ on training days\n\n"
        "These are already saved. A couple of quick questions to finish setup:\n\n"
        "1️⃣ What's your current weight in kg? (I'll use this as your baseline)",
        parse_mode="Markdown"
    )
    context.user_data["setup_step"] = 1


# ── /help ────────────────────────────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text(
        "🤖 *K_O Trainer Bot — Commands*\n\n"
        "/today — Today's training + nutrition plan\n"
        "/log [food] — Log a meal\n"
        "/totals — Today's calorie & protein totals\n"
        "/fridge — Update your fridge/food inventory\n"
        "/checkin — Log weekly weight & body comp\n"
        "/week — This week's training & meal overview\n"
        "/ask [question] — Ask your trainer anything\n"
        "/help — This message",
        parse_mode="Markdown"
    )


# ── /today ───────────────────────────────────────────────────────────────────
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text("Fetching your calendar and building your plan... ⏳")

    today_events = get_todays_events()
    tomorrow_events = get_tomorrows_events()
    totals = get_daily_totals(date.today().isoformat())
    fridge = get_fridge()
    last_checkin = get_last_checkin()
    wfh = context.user_data.get("wfh_today")

    prompt = build_daily_prompt(
        today_events, tomorrow_events, totals, fridge,
        last_checkin, wfh, mode="full_day"
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

    totals_before = get_daily_totals(date.today().isoformat())
    prompt = (
        f"The user just logged this meal: '{text}'\n"
        f"Today's running totals so far: {json.dumps(totals_before)}\n"
        f"Daily targets: 2155 kcal, 124g protein (135g on training days).\n\n"
        f"Estimate the calories and protein in this meal. Be realistic — don't low-ball.\n"
        f"Reply in this exact format:\n"
        f"CALORIES: [number]\n"
        f"PROTEIN: [number]\n"
        f"COMMENT: [one short sentence — running total context, flag if getting close to limit, "
        f"or note if protein is low for the meal]"
    )
    response = await ask_claude(prompt)

    # Parse structured response
    lines = response.strip().split("\n")
    cal = prot = 0
    comment = ""
    for line in lines:
        if line.startswith("CALORIES:"):
            try: cal = int(line.split(":")[1].strip())
            except: pass
        elif line.startswith("PROTEIN:"):
            try: prot = int(line.split(":")[1].strip())
            except: pass
        elif line.startswith("COMMENT:"):
            comment = line.split(":", 1)[1].strip()

    if cal > 0:
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
            f"Logged '{text}' — couldn't parse exact numbers but noted it.\n\n_{response}_",
            parse_mode="Markdown"
        )


# ── /totals ──────────────────────────────────────────────────────────────────
async def totals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    t = get_daily_totals(date.today().isoformat())
    cal = t.get("calories", 0)
    prot = t.get("protein", 0)
    cal_target = 2155
    prot_target = 124

    cal_pct = round((cal / cal_target) * 100)
    prot_pct = round((prot / prot_target) * 100)
    cal_remain = cal_target - cal
    prot_remain = prot_target - prot

    now_hour = datetime.now(VIENNA_TZ).hour
    time_context = "morning" if now_hour < 12 else "afternoon" if now_hour < 17 else "evening"

    await update.message.reply_text(
        f"📊 *Today's totals*\n\n"
        f"🔥 Calories: {cal} / {cal_target} kcal ({cal_pct}%)\n"
        f"   {'▓' * min(cal_pct//10, 10)}{'░' * max(0, 10 - cal_pct//10)}\n"
        f"   {cal_remain} remaining\n\n"
        f"💪 Protein: {prot} / {prot_target}g ({prot_pct}%)\n"
        f"   {'▓' * min(prot_pct//10, 10)}{'░' * max(0, 10 - prot_pct//10)}\n"
        f"   {prot_remain}g remaining\n\n"
        f"_{get_totals_comment(cal, prot, cal_target, prot_target, time_context)}_",
        parse_mode="Markdown"
    )


def get_totals_comment(cal, prot, cal_target, prot_target, time_context):
    if time_context == "morning" and cal < 300:
        return "Early days — plenty of room to build on."
    if time_context == "evening" and cal > cal_target * 0.95:
        return "On target for the day. Good."
    if prot < prot_target * 0.5 and time_context == "evening":
        return "Protein is low for this time of day — prioritise it in your next meal."
    if cal > cal_target * 1.1:
        return "Over target today — keep dinner light."
    if time_context == "afternoon" and prot < prot_target * 0.4:
        return "Protein is lagging — make your next meal protein-heavy."
    return "Tracking well."


# ── /fridge ──────────────────────────────────────────────────────────────────
async def fridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    text = " ".join(context.args) if context.args else ""
    if not text:
        current = get_fridge()
        if current:
            await update.message.reply_text(
                f"🧊 *Current fridge inventory:*\n{current}\n\n"
                f"To update, send: /fridge [what you have]",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "No fridge inventory set yet.\nSend: /fridge eggs, chicken, lentils, spinach..."
            )
        return

    set_fridge(text)

    # Ask Claude for a quick meal plan based on contents
    prompt = (
        f"Krisz has the following food at home: {text}\n"
        f"Her targets: ~2155 kcal/day, 124g protein. She likes simple meals — salads, beans, "
        f"lentils, a protein source. She gets bored of meal prep. Prefers variety.\n"
        f"Suggest 3 simple meals she could make this week from these ingredients. "
        f"Keep each suggestion to one line. Be practical, not aspirational."
    )
    suggestions = await ask_claude(prompt)
    await update.message.reply_text(
        f"✅ Fridge updated.\n\n🍽 *Quick meal ideas from what you have:*\n{suggestions}",
        parse_mode="Markdown"
    )


# ── /checkin ─────────────────────────────────────────────────────────────────
async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text(
        "Weekly check-in 📊\n\n"
        "Send me your numbers in this format:\n"
        "*weight: [kg]  muscle: [%]  fat: [%]*\n\n"
        "e.g. weight: 68.2  muscle: 34  fat: 28\n\n"
        "Muscle and fat are optional — weight is enough if that's all you have.",
        parse_mode="Markdown"
    )
    context.user_data["awaiting_checkin"] = True


# ── /week ────────────────────────────────────────────────────────────────────
async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    await update.message.reply_text("Pulling your week together... ⏳")

    today_events = get_todays_events()
    fridge = get_fridge()
    last_checkin = get_last_checkin()

    prompt = (
        f"Give Krisz a weekly overview. Today is {date.today().strftime('%A %d %B')}.\n"
        f"Calendar events visible today: {json.dumps(today_events)}\n"
        f"Food at home: {fridge or 'not set'}\n"
        f"Last check-in data: {json.dumps(last_checkin)}\n\n"
        f"Provide:\n"
        f"1. Training recommendation for the week (she's in a half marathon Runna block, "
        f"Ashtanga is strength training, not light)\n"
        f"2. Nutrition focus for the week in 2–3 sentences\n"
        f"3. One thing to watch or improve this week\n\n"
        f"Keep it tight — no fluff."
    )
    response = await ask_claude(prompt)
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
    last_checkin = get_last_checkin()

    prompt = build_coach_prompt(question, today_events, totals_today, last_checkin)
    response = await ask_claude(prompt)
    await update.message.reply_text(response, parse_mode="Markdown")


# ── Free text message handler ────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update): return
    text = update.message.text.strip()

    # Handle setup flow
    if context.user_data.get("setup_step") == 1:
        try:
            weight = float(text.replace("kg", "").strip())
            log_checkin(date.today().isoformat(), weight, None, None)
            set_config("setup_complete", "true")
            set_config("baseline_weight", str(weight))
            await update.message.reply_text(
                f"✅ Got it — {weight}kg logged as your starting point.\n\n"
                f"You're all set up. Send /today to get your first daily plan, "
                f"or /help to see all commands."
            )
            context.user_data.pop("setup_step", None)
        except:
            await update.message.reply_text("Just send a number, e.g. 68.5")
        return

    # Handle check-in data
    if context.user_data.get("awaiting_checkin"):
        await handle_checkin_data(update, context, text)
        return

    # Handle WFH/office morning response
    if context.user_data.get("awaiting_wfh"):
        wfh = "home" in text.lower() or "wfh" in text.lower() or "home" in text.lower()
        context.user_data["wfh_today"] = wfh
        context.user_data.pop("awaiting_wfh", None)

        today_events = get_todays_events()
        tomorrow_events = get_tomorrows_events()
        totals_today = get_daily_totals(date.today().isoformat())
        fridge = get_fridge()
        last_checkin_data = get_last_checkin()

        prompt = build_daily_prompt(
            today_events, tomorrow_events, totals_today,
            fridge, last_checkin_data, wfh, mode="full_day"
        )
        response = await ask_claude(prompt)
        await update.message.reply_text(response, parse_mode="Markdown")
        return

    # General free-form — treat as /ask
    today_events = get_todays_events()
    totals_today = get_daily_totals(date.today().isoformat())
    last_checkin_data = get_last_checkin()
    prompt = build_coach_prompt(text, today_events, totals_today, last_checkin_data)
    response = await ask_claude(prompt)
    await update.message.reply_text(response, parse_mode="Markdown")


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
        await update.message.reply_text(
            "Couldn't parse that. Try: weight: 68.2  muscle: 34  fat: 28"
        )
        return

    log_checkin(date.today().isoformat(), weight, muscle, fat)
    context.user_data.pop("awaiting_checkin", None)

    last = get_last_checkin(skip_latest=True)
    trend = ""
    if last and last.get("weight"):
        diff = round(weight - last["weight"], 1)
        if diff < 0: trend = f" (↓ {abs(diff)}kg from last week)"
        elif diff > 0: trend = f" (↑ {diff}kg from last week)"
        else: trend = " (no change)"

    details = f"{weight}kg{trend}"
    if muscle: details += f" · {muscle}% muscle"
    if fat: details += f" · {fat}% fat"

    prompt = (
        f"Krisz just logged her weekly check-in: {details}\n"
        f"Previous data: {json.dumps(last)}\n"
        f"Her goal: body recomposition + marathon training. Targets: 2155 kcal, 124g protein.\n"
        f"Give her a 2–3 sentence honest assessment of her progress. "
        f"Flag anything worth adjusting. No cheerleading."
    )
    assessment = await ask_claude(prompt)
    await update.message.reply_text(
        f"✅ Check-in logged: {details}\n\n{assessment}",
        parse_mode="Markdown"
    )


# ── Prompt builders ──────────────────────────────────────────────────────────
def build_daily_prompt(today_events, tomorrow_events, totals, fridge, last_checkin, wfh, mode):
    today_str = date.today().strftime("%A %d %B")
    wfh_str = "working from home" if wfh else ("in the office (Mariahilfer Strasse 54)" if wfh is False else "unknown — assume WFH")

    return (
        f"You are Krisz's personal trainer and nutritionist. Today is {today_str}. "
        f"She is {wfh_str}.\n\n"
        f"CALENDAR TODAY: {json.dumps(today_events)}\n"
        f"CALENDAR TOMORROW: {json.dumps(tomorrow_events)}\n"
        f"FOOD AT HOME: {fridge or 'not updated this week'}\n"
        f"TODAY'S NUTRITION SO FAR: {json.dumps(totals)}\n"
        f"LAST BODY CHECK-IN: {json.dumps(last_checkin)}\n\n"
        f"KRISZ'S PROFILE:\n"
        f"- Training for a half marathon (Runna plan), Ashtanga yoga = strength training not light activity\n"
        f"- Targets: 2155 kcal/day, 124g protein (135g on training days), rest days ~1900 kcal\n"
        f"- Likes simple meals: salads, beans, lentils, a protein source. Gets bored of meal prep.\n"
        f"- Significant food noise and emotional eating — don't push extra food unnecessarily\n"
        f"- Gym: not yet, focus on running base and Ashtanga consistency first\n"
        f"- Tone: direct, no fluff, no cheerleading\n\n"
        f"Provide her daily brief covering:\n"
        f"1. Training today (from calendar or recommended — be specific)\n"
        f"2. Nutrition approach for today (meals from fridge if WFH, simple options if office)\n"
        f"3. Key reminders based on tomorrow's calendar (pack bag, alarm time, pre-run fuel)\n\n"
        f"Keep it concise. Use *bold* for section headers. No bullet soup."
    )


def build_coach_prompt(question, today_events, totals, last_checkin):
    today_str = date.today().strftime("%A %d %B")
    return (
        f"You are Krisz's personal trainer and nutritionist. Today is {today_str}.\n"
        f"CALENDAR TODAY: {json.dumps(today_events)}\n"
        f"TODAY'S NUTRITION: {json.dumps(totals)}\n"
        f"LAST CHECK-IN: {json.dumps(last_checkin)}\n\n"
        f"PROFILE: Training for half marathon + Ashtanga (strength). "
        f"Targets: 2155 kcal, 124g protein. Goal: recomposition. "
        f"Tone: direct, no fluff.\n\n"
        f"Her question: {question}\n\n"
        f"Answer directly and concisely."
    )


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    init_db()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("log", log_food))
    app.add_handler(CommandHandler("totals", totals))
    app.add_handler(CommandHandler("fridge", fridge_command))
    app.add_handler(CommandHandler("checkin", checkin))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    setup_scheduler(app)

    logger.info("K_O_trainer_bot started ✅")
    app.run_polling()


if __name__ == "__main__":
    main()
