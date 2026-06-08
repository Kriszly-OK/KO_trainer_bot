# K_O_trainer_bot 🤖

Personal trainer and nutritionist Telegram bot for Krisz.
Powered by Claude (Anthropic) + Google Calendar.

---

## Files in this project

| File | What it does |
|------|-------------|
| `bot.py` | Main bot — handles all Telegram commands and messages |
| `ai_client.py` | Sends prompts to Claude API |
| `calendar_client.py` | Reads Google Calendar events |
| `database.py` | Stores meals, check-ins, fridge contents |
| `scheduler.py` | Sends proactive messages (morning brief, reminders, etc.) |
| `auth_google.py` | One-time script to authorise Google Calendar |
| `requirements.txt` | Python dependencies |
| `Procfile` | Tells Railway how to run the bot |
| `railway.json` | Railway deployment config |

---

## Deploy steps

### Step 1 — Get your Telegram chat ID

1. Send `/start` to `@K_O_trainer_bot` in Telegram
2. Go to Railway → your project → **Logs** tab
3. Look for a line like: `Chat ID: 123456789`
4. Copy that number — you'll need it in Step 3

### Step 2 — Run Google authorisation (one time, on your computer)

Make sure Python is installed, then:

```bash
pip install google-auth-oauthlib google-api-python-client
python auth_google.py
```

A browser opens → log in with your Google account → click Allow.

The script prints the full JSON token at the end. Copy all of it.

### Step 3 — Set environment variables in Railway

Go to Railway → your project → **Variables** tab. Add:

| Variable | Value |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `KRISZ_CHAT_ID` | The number from Step 1 |
| `GOOGLE_TOKEN_JSON` | The full JSON from Step 2 (paste as one line) |

### Step 4 — Deploy

Click **Redeploy** in Railway. Check the Logs tab — you should see:

```
K_O_trainer_bot started ✅
Scheduler started ✅
```

---

## Bot commands

| Command | What it does |
|---------|-------------|
| `/start` | First-time setup |
| `/today` | Today's training + nutrition plan |
| `/log [food]` | Log a meal |
| `/totals` | Today's calorie & protein totals |
| `/fridge` | Update fridge contents |
| `/checkin` | Log weekly weight/body comp |
| `/week` | Week overview |
| `/ask [question]` | Ask your trainer anything |

---

## Proactive messages schedule (Vienna time)

| Time | Message |
|------|---------|
| 7:00am Mon–Fri | Morning check-in (WFH or office?) |
| 9:00am Sat–Sun | Morning check-in |
| Every 30 min | Pre-workout fuel check (90 min before training) |
| 8:30pm daily | End-of-day protein/calorie check if off-target |
| 9:00pm daily | Evening reminder (pack bag, alarm time, sleep) |
| Friday 12:00pm | Weekly check-in prompt (weight/body comp) |
| Friday 7:00pm | Weekly fridge/meal plan prompt |

---

## Targets (pre-configured)

- Calories: 2,155 (rest days ~1,900)
- Protein: 124g baseline, 135g on training days
- Macro split: Moderate Carb (30/35/35)
