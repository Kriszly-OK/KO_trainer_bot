"""
database.py — Persistent storage using PostgreSQL (Supabase)
Falls back to SQLite if DATABASE_URL is not set (local dev only)
"""

import os
import logging
from datetime import date

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)


def get_conn():
    if USE_POSTGRES:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    else:
        import sqlite3
        conn = sqlite3.connect(os.environ.get("DB_PATH", "trainer_bot.db"))
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS meals (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    description TEXT,
                    calories INTEGER DEFAULT 0,
                    protein INTEGER DEFAULT 0,
                    fiber INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Add fiber column if table already existed without it
            cur.execute("ALTER TABLE meals ADD COLUMN IF NOT EXISTS fiber INTEGER DEFAULT 0")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS checkins (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    weight REAL,
                    muscle_pct REAL,
                    fat_pct REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fridge (
                    id INTEGER PRIMARY KEY,
                    contents TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_log (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    entry TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS meals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    description TEXT,
                    calories INTEGER DEFAULT 0,
                    protein INTEGER DEFAULT 0,
                    fiber INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            try:
                cur.execute("ALTER TABLE meals ADD COLUMN fiber INTEGER DEFAULT 0")
            except Exception:
                pass  # column already exists

            cur.execute("""
                CREATE TABLE IF NOT EXISTS checkins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    weight REAL,
                    muscle_pct REAL,
                    fat_pct REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fridge (
                    id INTEGER PRIMARY KEY,
                    contents TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    entry TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

        conn.commit()
        logger.info(f"Database initialised ({'PostgreSQL' if USE_POSTGRES else 'SQLite'})")
    finally:
        conn.close()


# ── Meals ────────────────────────────────────────────────────────────────────
def log_meal(date_str: str, description: str, calories: int, protein: int, fiber: int = 0):
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO meals (date, description, calories, protein, fiber) VALUES (%s, %s, %s, %s, %s)",
                (date_str, description, calories, protein, fiber)
            )
        else:
            cur.execute(
                "INSERT INTO meals (date, description, calories, protein, fiber) VALUES (?, ?, ?, ?, ?)",
                (date_str, description, calories, protein, fiber)
            )
        conn.commit()
    finally:
        conn.close()


def get_daily_totals(date_str: str) -> dict:
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute(
                "SELECT COALESCE(SUM(calories),0), COALESCE(SUM(protein),0), COALESCE(SUM(fiber),0) "
                "FROM meals WHERE date = %s",
                (date_str,)
            )
            row = cur.fetchone()
            return {"calories": row[0], "protein": row[1], "fiber": row[2]}
        else:
            cur.execute(
                "SELECT COALESCE(SUM(calories),0) as calories, COALESCE(SUM(protein),0) as protein, "
                "COALESCE(SUM(fiber),0) as fiber FROM meals WHERE date = ?",
                (date_str,)
            )
            row = cur.fetchone()
            return {"calories": row["calories"], "protein": row["protein"], "fiber": row["fiber"]}
    finally:
        conn.close()


def get_daily_log(date_str: str) -> list:
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute(
                "SELECT description, calories, protein, fiber FROM meals WHERE date = %s ORDER BY created_at",
                (date_str,)
            )
            return [{"description": r[0], "calories": r[1], "protein": r[2], "fiber": r[3]} for r in cur.fetchall()]
        else:
            cur.execute(
                "SELECT description, calories, protein, fiber FROM meals WHERE date = ? ORDER BY created_at",
                (date_str,)
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_to_daily_log(date_str: str, entry: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute("INSERT INTO daily_log (date, entry) VALUES (%s, %s)", (date_str, entry))
        else:
            cur.execute("INSERT INTO daily_log (date, entry) VALUES (?, ?)", (date_str, entry))
        conn.commit()
    finally:
        conn.close()


# ── Check-ins ────────────────────────────────────────────────────────────────
def log_checkin(date_str: str, weight: float, muscle_pct: float = None, fat_pct: float = None):
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO checkins (date, weight, muscle_pct, fat_pct) VALUES (%s, %s, %s, %s)",
                (date_str, weight, muscle_pct, fat_pct)
            )
        else:
            cur.execute(
                "INSERT INTO checkins (date, weight, muscle_pct, fat_pct) VALUES (?, ?, ?, ?)",
                (date_str, weight, muscle_pct, fat_pct)
            )
        conn.commit()
    finally:
        conn.close()


def get_last_checkin(skip_latest: bool = False) -> dict:
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            if skip_latest:
                cur.execute("SELECT * FROM checkins ORDER BY created_at DESC LIMIT 1 OFFSET 1")
            else:
                cur.execute("SELECT * FROM checkins ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        else:
            if skip_latest:
                cur.execute("SELECT * FROM checkins ORDER BY created_at DESC LIMIT 1 OFFSET 1")
            else:
                cur.execute("SELECT * FROM checkins ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                return dict(row)
        return {}
    finally:
        conn.close()


# ── Fridge ───────────────────────────────────────────────────────────────────
def set_fridge(contents: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM fridge")
        if USE_POSTGRES:
            cur.execute("INSERT INTO fridge (id, contents) VALUES (%s, %s)", (1, contents))
        else:
            cur.execute("INSERT INTO fridge (id, contents) VALUES (1, ?)", (contents,))
        conn.commit()
    finally:
        conn.close()


def get_fridge() -> str:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT contents FROM fridge WHERE id = 1")
        row = cur.fetchone()
        if row:
            return row[0] if USE_POSTGRES else row["contents"]
        return ""
    finally:
        conn.close()


# ── Config (generic key/value store) ─────────────────────────────────────────
def set_config(key: str, value: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO config (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value)
            )
        else:
            cur.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, value)
            )
        conn.commit()
    finally:
        conn.close()


def get_config(key: str) -> str:
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        else:
            cur.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cur.fetchone()
        if row:
            return row[0] if USE_POSTGRES else row["value"]
        return None
    finally:
        conn.close()


# ── Schedule overrides (daily corrections to calendar) ───────────────────────
def set_today_override(date_str: str, text: str):
    """Store a correction Krisz made about today's schedule/training."""
    set_config(f"override_{date_str}", text)


def get_today_override(date_str: str) -> str:
    """Retrieve any correction Krisz made about today's schedule/training."""
    return get_config(f"override_{date_str}")


# ── Delete today's meals ─────────────────────────────────────────────────────
def delete_todays_meals():
    conn = get_conn()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute("DELETE FROM meals WHERE date = %s", (date.today().isoformat(),))
        else:
            cur.execute("DELETE FROM meals WHERE date = ?", (date.today().isoformat(),))
        conn.commit()
    finally:
        conn.close()
