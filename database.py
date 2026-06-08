"""
database.py — SQLite storage for meals, check-ins, fridge, config
"""

import sqlite3
import os
from datetime import date

DB_PATH = os.environ.get("DB_PATH", "trainer_bot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            description TEXT,
            calories INTEGER DEFAULT 0,
            protein INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            weight REAL,
            muscle_pct REAL,
            fat_pct REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS fridge (
            id INTEGER PRIMARY KEY,
            contents TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            entry TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


# ── Meals ────────────────────────────────────────────────────────────────────
def log_meal(date_str: str, description: str, calories: int, protein: int):
    conn = get_conn()
    conn.execute(
        "INSERT INTO meals (date, description, calories, protein) VALUES (?, ?, ?, ?)",
        (date_str, description, calories, protein)
    )
    conn.commit()
    conn.close()


def get_daily_totals(date_str: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(calories),0) as calories, COALESCE(SUM(protein),0) as protein "
        "FROM meals WHERE date = ?",
        (date_str,)
    ).fetchone()
    conn.close()
    return {"calories": row["calories"], "protein": row["protein"]}


def get_daily_log(date_str: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT description, calories, protein FROM meals WHERE date = ? ORDER BY created_at",
        (date_str,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_to_daily_log(date_str: str, entry: str):
    conn = get_conn()
    conn.execute("INSERT INTO daily_log (date, entry) VALUES (?, ?)", (date_str, entry))
    conn.commit()
    conn.close()


# ── Check-ins ────────────────────────────────────────────────────────────────
def log_checkin(date_str: str, weight: float, muscle_pct: float = None, fat_pct: float = None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO checkins (date, weight, muscle_pct, fat_pct) VALUES (?, ?, ?, ?)",
        (date_str, weight, muscle_pct, fat_pct)
    )
    conn.commit()
    conn.close()


def get_last_checkin(skip_latest: bool = False) -> dict:
    conn = get_conn()
    if skip_latest:
        row = conn.execute(
            "SELECT * FROM checkins ORDER BY created_at DESC LIMIT 1 OFFSET 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM checkins ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    conn.close()
    if row:
        return {"date": row["date"], "weight": row["weight"],
                "muscle_pct": row["muscle_pct"], "fat_pct": row["fat_pct"]}
    return {}


# ── Fridge ───────────────────────────────────────────────────────────────────
def set_fridge(contents: str):
    conn = get_conn()
    conn.execute("DELETE FROM fridge")
    conn.execute("INSERT INTO fridge (id, contents) VALUES (1, ?)", (contents,))
    conn.commit()
    conn.close()


def get_fridge() -> str:
    conn = get_conn()
    row = conn.execute("SELECT contents FROM fridge WHERE id = 1").fetchone()
    conn.close()
    return row["contents"] if row else ""


# ── Config ───────────────────────────────────────────────────────────────────
def set_config(key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()
    conn.close()


def get_config(key: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None
