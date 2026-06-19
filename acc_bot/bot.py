"""Accounting / reporting bot.

Shares the secret bots' SQLite DB at /home/anon-bot/data.db (read-only access to
`bots` and `pseudonyms`, full ownership of its own `acc_*` tables).

Sends сверки / rate messages by broadcasting through each attached secret
bot's own token, so to chat participants the message looks like an ordinary
post from their familiar secret bot.
"""

import os
import sys
import json
import time
import asyncio
import sqlite3
import logging
import socket
import html
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.request import HTTPXRequest
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


BOT_TOKEN = os.getenv("ACC_BOT_TOKEN", "8983545434:AAF165TzDfc8nrgWqzkLvJ2kLwCkYL-4xX4")
# Database is shared with the secret bots — we read their `bots` and
# `pseudonyms` tables; we write only to our `acc_*` tables.
DB_PATH = os.getenv("ACC_DB_PATH", "/home/anon-bot/data.db")
# First Owner — seeded at startup if no owners exist yet.
INITIAL_OWNER_ID = int(os.getenv("ACC_INITIAL_OWNER", "5252506422"))

MOSCOW_TZ = timezone(timedelta(hours=3))

# Geo display names and flag emojis, matching the secret bot module.
GEO_DISPLAY = {
    "argentina":  ("Аргентина",  "🇦🇷"),
    "bolivia":    ("Боливия",    "🇧🇴"),
    "chile":      ("Чили",       "🇨🇱"),
    "colombia":   ("Колумбия",   "🇨🇴"),
    "costa_rica": ("Коста-Рика", "🇨🇷"),
    "ecuador":    ("Эквадор",    "🇪🇨"),
    "indonesia":  ("Индонезия",  "🇮🇩"),
    "mexico":     ("Мексика",    "🇲🇽"),
    "morocco":    ("Марокко",    "🇲🇦"),
    "nigeria":    ("Нигерия",    "🇳🇬"),
    "peru":       ("Перу",       "🇵🇪"),
    "turkey":     ("Турция",     "🇹🇷"),
    "venezuela":  ("Венесуэла",  "🇻🇪"),
}
GEO_CURRENCIES = {
    "argentina": "ARS", "bolivia": "BOB", "chile": "CLP", "mexico": "MXN",
    "colombia": "COP", "peru": "PEN", "ecuador": "USD", "venezuela": "VES",
    "turkey": "TRY", "nigeria": "NGN", "morocco": "MAD", "costa_rica": "CRC",
    "indonesia": "IDR",
}
GEO_ORDER = list(GEO_DISPLAY.keys())


# ---------- DB layer ----------

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS acc_owners (
        user_id INTEGER PRIMARY KEY
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS acc_admins (
        user_id INTEGER PRIMARY KEY
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS acc_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS acc_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        geo TEXT NOT NULL,
        commission_pct REAL,           -- NULL means use default
        report_mode TEXT DEFAULT 'total',  -- 'total' or 'breakdown'
        enabled INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS acc_group_bots (
        group_id INTEGER,
        bot_token TEXT,
        PRIMARY KEY (group_id, bot_token)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS acc_group_geos (
        group_id INTEGER,
        geo TEXT,
        PRIMARY KEY (group_id, geo)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS acc_rates (
        geo TEXT PRIMARY KEY,
        rate REAL,
        updated_at REAL
    )""")
    # acc_daily_data is keyed by (group_id, geo, date). Older deployments had
    # PK (group_id, date) with no geo column — we migrate that below.
    c.execute("""CREATE TABLE IF NOT EXISTS acc_daily_data (
        group_id INTEGER,
        geo TEXT,
        date TEXT,                     -- YYYY-MM-DD (Moscow)
        items TEXT,                    -- JSON list of numbers
        updated_at REAL,
        PRIMARY KEY (group_id, geo, date)
    )""")
    # --- migrations ---
    # 1. If the old acc_daily_data had no `geo` column, rebuild it.
    cols = [r[1] for r in c.execute("PRAGMA table_info(acc_daily_data)").fetchall()]
    if "geo" not in cols:
        c.execute("ALTER TABLE acc_daily_data RENAME TO acc_daily_data_legacy")
        c.execute("""CREATE TABLE acc_daily_data (
            group_id INTEGER,
            geo TEXT,
            date TEXT,
            items TEXT,
            updated_at REAL,
            PRIMARY KEY (group_id, geo, date)
        )""")
        c.execute(
            "INSERT INTO acc_daily_data(group_id, geo, date, items, updated_at) "
            "SELECT old.group_id, g.geo, old.date, old.items, old.updated_at "
            "FROM acc_daily_data_legacy old JOIN acc_groups g ON g.id = old.group_id"
        )
        c.execute("DROP TABLE acc_daily_data_legacy")
        logger.info("Migrated acc_daily_data to per-geo schema")
    # 2. Backfill acc_group_geos from acc_groups.geo for any group that has
    #    no entries yet — keeps existing UX intact after upgrade.
    c.execute(
        "INSERT OR IGNORE INTO acc_group_geos(group_id, geo) "
        "SELECT id, geo FROM acc_groups WHERE geo IS NOT NULL AND geo != ''"
    )
    c.execute("""CREATE TABLE IF NOT EXISTS acc_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_date TEXT,              -- YYYY-MM-DD
        created_at REAL,
        kind TEXT,                     -- 'reconciliation' / 'rates' / 'auto_shift'
        payload TEXT,                  -- JSON: structured snapshot
        text_preview TEXT,             -- human-readable text
        sent_to TEXT,                  -- JSON list of bot_tokens / chat_ids that received it
        status TEXT DEFAULT 'sent',    -- 'pending_bookkeeper' / 'pending_owner' / 'sent' / 'cancelled' / 'expired'
        group_id INTEGER,              -- (auto-shift only) which anon-group it belongs to
        bot_token TEXT                 -- (auto-shift only) which child bot the shift came from
    )""")
    # Migration: add new columns to existing acc_reports.
    cols_reports = [r[1] for r in c.execute("PRAGMA table_info(acc_reports)").fetchall()]
    for new_col, ddl in [
        ("status", "TEXT DEFAULT 'sent'"),
        ("group_id", "INTEGER"),
        ("bot_token", "TEXT"),
    ]:
        if new_col not in cols_reports:
            c.execute(f"ALTER TABLE acc_reports ADD COLUMN {new_col} {ddl}")
    c.execute("""CREATE TABLE IF NOT EXISTS acc_target_chats (
        group_id INTEGER PRIMARY KEY,  -- 1 target chat per anon-group
        chat_id INTEGER NOT NULL UNIQUE,
        chat_title TEXT,
        bound_at REAL,
        bound_by INTEGER               -- owner user_id who confirmed
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS acc_pending_chats (
        chat_id INTEGER PRIMARY KEY,   -- chats where the bot is present but no group bound
        chat_title TEXT,
        added_at REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS acc_shift_state (
        bot_token TEXT PRIMARY KEY,
        last_shift_date TEXT,          -- last shift-date we already processed
        updated_at REAL
    )""")
    # Seed default commission if missing
    c.execute("INSERT OR IGNORE INTO acc_settings(key, value) VALUES (?, ?)",
              ("default_commission_pct", "15"))
    # Seed initial owner if no owners exist
    row = c.execute("SELECT COUNT(*) FROM acc_owners").fetchone()
    if row[0] == 0 and INITIAL_OWNER_ID:
        c.execute("INSERT OR IGNORE INTO acc_owners(user_id) VALUES (?)", (INITIAL_OWNER_ID,))
        logger.info(f"Seeded initial owner: {INITIAL_OWNER_ID}")
    conn.commit()
    # Purge reports older than 30 days
    cutoff = time.time() - 30 * 24 * 3600
    c.execute("DELETE FROM acc_reports WHERE created_at < ?", (cutoff,))
    conn.commit()
    conn.close()


# ---------- Role helpers ----------

def get_owners():
    conn = db_conn()
    rows = conn.execute("SELECT user_id FROM acc_owners").fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_admins():
    conn = db_conn()
    rows = conn.execute("SELECT user_id FROM acc_admins").fetchall()
    conn.close()
    return {r[0] for r in rows}


def is_acc_owner(user_id):
    return user_id in get_owners()


def is_acc_admin(user_id):
    return user_id in get_admins()


def is_acc_admin_or_owner(user_id):
    return user_id in get_owners() or user_id in get_admins()


# ---------- Settings ----------

def get_setting(key, default=None):
    conn = db_conn()
    row = conn.execute("SELECT value FROM acc_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key, value):
    conn = db_conn()
    conn.execute("INSERT OR REPLACE INTO acc_settings(key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def get_default_commission():
    return float(get_setting("default_commission_pct", "15"))


# ---------- Groups ----------

def list_groups(enabled_only=False):
    conn = db_conn()
    q = "SELECT id, name, geo, commission_pct, report_mode, enabled FROM acc_groups"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY name COLLATE NOCASE"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [
        {"id": r[0], "name": r[1], "geo": r[2], "commission_pct": r[3],
         "report_mode": r[4], "enabled": bool(r[5])}
        for r in rows
    ]


def get_group(group_id):
    conn = db_conn()
    r = conn.execute(
        "SELECT id, name, geo, commission_pct, report_mode, enabled FROM acc_groups WHERE id=?",
        (group_id,)
    ).fetchone()
    conn.close()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "geo": r[2], "commission_pct": r[3],
            "report_mode": r[4], "enabled": bool(r[5])}


def create_group(name, geo, commission_pct=None, report_mode="total"):
    conn = db_conn()
    cur = conn.execute(
        "INSERT INTO acc_groups(name, geo, commission_pct, report_mode, enabled) VALUES (?, ?, ?, ?, 1)",
        (name, geo, commission_pct, report_mode)
    )
    gid = cur.lastrowid
    conn.commit()
    conn.close()
    return gid


def update_group(group_id, **fields):
    if not fields:
        return
    allowed = {"name", "geo", "commission_pct", "report_mode", "enabled"}
    pairs = [(k, v) for k, v in fields.items() if k in allowed]
    if not pairs:
        return
    cols = ", ".join(f"{k}=?" for k, _ in pairs)
    vals = [v for _, v in pairs] + [group_id]
    conn = db_conn()
    conn.execute(f"UPDATE acc_groups SET {cols} WHERE id=?", vals)
    conn.commit()
    conn.close()


def delete_group(group_id):
    conn = db_conn()
    conn.execute("DELETE FROM acc_group_bots WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM acc_daily_data WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM acc_groups WHERE id=?", (group_id,))
    # acc_reports retained per spec — its payload still references the group by name
    conn.commit()
    conn.close()


def get_group_bots(group_id):
    conn = db_conn()
    rows = conn.execute(
        "SELECT bot_token FROM acc_group_bots WHERE group_id=?", (group_id,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def set_group_bots(group_id, tokens):
    conn = db_conn()
    conn.execute("DELETE FROM acc_group_bots WHERE group_id=?", (group_id,))
    for tok in tokens:
        conn.execute(
            "INSERT OR IGNORE INTO acc_group_bots(group_id, bot_token) VALUES (?, ?)",
            (group_id, tok)
        )
    conn.commit()
    conn.close()


def get_group_geos(group_id):
    """Return sorted list of geo codes attached to a group."""
    conn = db_conn()
    rows = conn.execute(
        "SELECT geo FROM acc_group_geos WHERE group_id=? ORDER BY geo", (group_id,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_group_geo(group_id, geo):
    conn = db_conn()
    conn.execute(
        "INSERT OR IGNORE INTO acc_group_geos(group_id, geo) VALUES (?, ?)",
        (group_id, geo)
    )
    conn.commit()
    conn.close()


def remove_group_geo(group_id, geo):
    conn = db_conn()
    conn.execute(
        "DELETE FROM acc_group_geos WHERE group_id=? AND geo=?",
        (group_id, geo)
    )
    # Also clear any orphan daily_data rows for that (group, geo)
    conn.execute(
        "DELETE FROM acc_daily_data WHERE group_id=? AND geo=?",
        (group_id, geo)
    )
    conn.commit()
    conn.close()


def get_bot_owning_group(bot_token):
    """Returns (group_id, group_name) if the bot is attached to any group, else None."""
    conn = db_conn()
    r = conn.execute(
        "SELECT g.id, g.name FROM acc_group_bots gb "
        "JOIN acc_groups g ON gb.group_id = g.id "
        "WHERE gb.bot_token = ? LIMIT 1",
        (bot_token,)
    ).fetchone()
    conn.close()
    return (r[0], r[1]) if r else None


def list_secret_bots():
    """Pull all secret bots from the shared DB."""
    conn = db_conn()
    rows = conn.execute(
        "SELECT token, username, COALESCE(geo, '') FROM bots ORDER BY username COLLATE NOCASE"
    ).fetchall()
    conn.close()
    return [{"token": r[0], "username": r[1], "geo": r[2]} for r in rows]


def bot_users(bot_token):
    """List user IDs registered in the given secret bot."""
    conn = db_conn()
    rows = conn.execute(
        "SELECT user_id FROM pseudonyms WHERE bot_token=?", (bot_token,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------- Rates ----------

def get_rates():
    conn = db_conn()
    rows = conn.execute("SELECT geo, rate, updated_at FROM acc_rates").fetchall()
    conn.close()
    return {r[0]: {"rate": r[1], "updated_at": r[2]} for r in rows}


def get_rate(geo):
    conn = db_conn()
    r = conn.execute("SELECT rate FROM acc_rates WHERE geo=?", (geo,)).fetchone()
    conn.close()
    return r[0] if r else None


def set_rate(geo, rate):
    conn = db_conn()
    conn.execute(
        "INSERT OR REPLACE INTO acc_rates(geo, rate, updated_at) VALUES (?, ?, ?)",
        (geo, rate, time.time())
    )
    conn.commit()
    conn.close()


def delete_rate(geo):
    conn = db_conn()
    cur = conn.execute("DELETE FROM acc_rates WHERE geo=?", (geo,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


# ---------- Daily data ----------

def today_str():
    return datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")


def set_daily_data(group_id, geo, items, date=None):
    if date is None:
        date = today_str()
    conn = db_conn()
    conn.execute(
        "INSERT OR REPLACE INTO acc_daily_data(group_id, geo, date, items, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (group_id, geo, date, json.dumps(items), time.time())
    )
    conn.commit()
    conn.close()


def get_daily_data(group_id, geo, date=None):
    if date is None:
        date = today_str()
    conn = db_conn()
    r = conn.execute(
        "SELECT items FROM acc_daily_data WHERE group_id=? AND geo=? AND date=?",
        (group_id, geo, date)
    ).fetchone()
    conn.close()
    return json.loads(r[0]) if r else None


def get_daily_data_all_geos(group_id, date=None):
    """Returns {geo: [numbers]} for all geos that have data for this date."""
    if date is None:
        date = today_str()
    conn = db_conn()
    rows = conn.execute(
        "SELECT geo, items FROM acc_daily_data WHERE group_id=? AND date=?",
        (group_id, date)
    ).fetchall()
    conn.close()
    return {r[0]: json.loads(r[1]) for r in rows}


# ---------- Reports ----------

def save_report(report_date, kind, payload, text_preview, sent_to_tokens,
                status="sent", group_id=None, bot_token=None):
    conn = db_conn()
    cur = conn.execute(
        "INSERT INTO acc_reports(report_date, created_at, kind, payload, text_preview, "
        "sent_to, status, group_id, bot_token) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (report_date, time.time(), kind,
         json.dumps(payload, ensure_ascii=False), text_preview,
         json.dumps(sent_to_tokens), status, group_id, bot_token)
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def update_report_status(report_id, status):
    conn = db_conn()
    conn.execute("UPDATE acc_reports SET status=? WHERE id=?", (status, report_id))
    conn.commit()
    conn.close()


def update_report_payload(report_id, payload, text_preview=None):
    conn = db_conn()
    if text_preview is None:
        conn.execute("UPDATE acc_reports SET payload=? WHERE id=?",
                     (json.dumps(payload, ensure_ascii=False), report_id))
    else:
        conn.execute("UPDATE acc_reports SET payload=?, text_preview=? WHERE id=?",
                     (json.dumps(payload, ensure_ascii=False), text_preview, report_id))
    conn.commit()
    conn.close()


def list_pending_reports(statuses):
    """Reports waiting for review at the given statuses (tuple of strings)."""
    placeholders = ",".join("?" * len(statuses))
    conn = db_conn()
    rows = conn.execute(
        f"SELECT id, report_date, created_at, kind, group_id, bot_token, text_preview, status, payload "
        f"FROM acc_reports WHERE status IN ({placeholders}) ORDER BY created_at ASC",
        tuple(statuses)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        bot_username = None
        try:
            if r[8]:
                pl = json.loads(r[8])
                bot_username = pl.get("bot_username")
        except Exception:
            pass
        out.append({
            "id": r[0], "date": r[1], "created_at": r[2], "kind": r[3],
            "group_id": r[4], "bot_token": r[5], "preview": r[6], "status": r[7],
            "bot_username": bot_username,
        })
    return out


def expire_previous_reports_for_bot(bot_token, except_report_id=None):
    """Mark any older pending (bookkeeper or owner) auto_shift reports for this
    bot as 'expired'. Called when a new auto_shift report arrives for the same
    bot — the new report supersedes the old ones.
    They stay in the DB so the History view still shows them.
    """
    if not bot_token:
        return 0
    conn = db_conn()
    if except_report_id is None:
        rows = conn.execute(
            "SELECT id FROM acc_reports "
            "WHERE kind='auto_shift' AND bot_token=? "
            "AND status IN ('pending_bookkeeper','pending_owner')",
            (bot_token,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM acc_reports "
            "WHERE kind='auto_shift' AND bot_token=? AND id<>? "
            "AND status IN ('pending_bookkeeper','pending_owner')",
            (bot_token, except_report_id)
        ).fetchall()
    to_expire = [r[0] for r in rows]
    if to_expire:
        placeholders = ",".join("?" * len(to_expire))
        conn.execute(
            f"UPDATE acc_reports SET status='expired' WHERE id IN ({placeholders})",
            tuple(to_expire)
        )
        conn.commit()
    conn.close()
    return len(to_expire)


def list_pending_reports_for_owner():
    return list_pending_reports(("pending_owner",))


def list_pending_reports_for_bookkeeper():
    return list_pending_reports(("pending_bookkeeper",))


def _pending_owner_reports_for_group(group_id):
    """All auto_shift reports for this group that are still pending_owner."""
    conn = db_conn()
    rows = conn.execute(
        "SELECT id, report_date, payload, text_preview, bot_token "
        "FROM acc_reports WHERE status='pending_owner' AND kind='auto_shift' "
        "AND group_id=? ORDER BY created_at",
        (group_id,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "date": r[1],
            "payload": json.loads(r[2]) if r[2] else {},
            "text_preview": r[3], "bot_token": r[4],
        })
    return out


# ---------- Target chats ----------

def list_target_chats():
    conn = db_conn()
    rows = conn.execute(
        "SELECT group_id, chat_id, chat_title, bound_at FROM acc_target_chats"
    ).fetchall()
    conn.close()
    return [{"group_id": r[0], "chat_id": r[1], "chat_title": r[2], "bound_at": r[3]} for r in rows]


def get_target_chat(group_id):
    conn = db_conn()
    r = conn.execute(
        "SELECT chat_id, chat_title FROM acc_target_chats WHERE group_id=?", (group_id,)
    ).fetchone()
    conn.close()
    return {"chat_id": r[0], "chat_title": r[1]} if r else None


def bind_target_chat(group_id, chat_id, chat_title, owner_user_id):
    """Bind a TG chat to an anon-group. One chat = one group. Replaces any
    previous binding for either side.
    """
    conn = db_conn()
    # Drop any prior binding for THIS chat (chats can only belong to one group)
    conn.execute("DELETE FROM acc_target_chats WHERE chat_id=?", (chat_id,))
    # And any prior binding for THIS group
    conn.execute("DELETE FROM acc_target_chats WHERE group_id=?", (group_id,))
    conn.execute(
        "INSERT INTO acc_target_chats(group_id, chat_id, chat_title, bound_at, bound_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (group_id, chat_id, chat_title, time.time(), owner_user_id)
    )
    conn.execute("DELETE FROM acc_pending_chats WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def unbind_target_chat(group_id):
    conn = db_conn()
    conn.execute("DELETE FROM acc_target_chats WHERE group_id=?", (group_id,))
    conn.commit()
    conn.close()


def add_pending_chat(chat_id, chat_title):
    conn = db_conn()
    conn.execute(
        "INSERT OR REPLACE INTO acc_pending_chats(chat_id, chat_title, added_at) "
        "VALUES (?, ?, ?)",
        (chat_id, chat_title, time.time())
    )
    conn.commit()
    conn.close()


def list_pending_chats():
    conn = db_conn()
    rows = conn.execute(
        "SELECT chat_id, chat_title, added_at FROM acc_pending_chats ORDER BY added_at"
    ).fetchall()
    conn.close()
    return [{"chat_id": r[0], "title": r[1], "added_at": r[2]} for r in rows]


def remove_pending_chat(chat_id):
    conn = db_conn()
    conn.execute("DELETE FROM acc_pending_chats WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


# ---------- Shift state ----------

def get_shift_state(bot_token):
    conn = db_conn()
    r = conn.execute(
        "SELECT last_shift_date FROM acc_shift_state WHERE bot_token=?", (bot_token,)
    ).fetchone()
    conn.close()
    return r[0] if r else None


def set_shift_state(bot_token, last_shift_date):
    conn = db_conn()
    conn.execute(
        "INSERT OR REPLACE INTO acc_shift_state(bot_token, last_shift_date, updated_at) "
        "VALUES (?, ?, ?)",
        (bot_token, last_shift_date, time.time())
    )
    conn.commit()
    conn.close()


def save_report_legacy_signature(report_date, kind, payload, text_preview, sent_to_tokens):
    """Backward-compat wrapper for old call sites."""
    return save_report(report_date, kind, payload, text_preview, sent_to_tokens)


# ---------- Receipts and shift logic (reads anon-bot's DB) ----------

def get_bot_shift(bot_token):
    """Return {'start': h, 'end': h, 'start_min': m, 'end_min': m} from
    anon-bot's `shifts` table. Default 0-23, minutes 0..59 if missing.
    """
    conn = db_conn()
    try:
        r = conn.execute(
            "SELECT shift_start, shift_end, "
            "COALESCE(shift_start_minute, 0), COALESCE(shift_end_minute, 59) "
            "FROM shifts WHERE bot_token=?", (bot_token,)
        ).fetchone()
    except Exception:
        # Older schema without minute columns
        r = conn.execute(
            "SELECT shift_start, shift_end FROM shifts WHERE bot_token=?", (bot_token,)
        ).fetchone()
        r = (r[0], r[1], 0, 59) if r else None
    conn.close()
    if not r:
        return {"start": 0, "end": 23, "start_min": 0, "end_min": 59}
    return {
        "start": int(r[0] or 0),
        "end": int(r[1] or 23),
        "start_min": int(r[2] or 0),
        "end_min": int(r[3] if r[3] is not None else 59),
    }


def current_working_day(bot_token, ref_dt=None):
    """Replicates anon-bot's get_working_day_date logic so we agree on which
    receipts belong to which shift. Returns YYYY-MM-DD (Moscow).
    """
    shift = get_bot_shift(bot_token)
    sm = shift.get("start_min", 0)
    em = shift.get("end_min", 59)
    start = shift["start"] * 60 + sm
    end = shift["end"] * 60 + em
    now = ref_dt or datetime.now(MOSCOW_TZ)
    cur = now.hour * 60 + now.minute
    if start > end and cur <= end:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def shift_window_for_date(bot_token, shift_date_str):
    """Translate a working-day date into a (start_ts, end_ts) UNIX-time window
    in Moscow tz. Used to filter receipts by their creation timestamp.
    """
    shift = get_bot_shift(bot_token)
    sh, sm = shift["start"], shift.get("start_min", 0)
    eh, em = shift["end"], shift.get("end_min", 59)
    base = datetime.strptime(shift_date_str, "%Y-%m-%d").replace(tzinfo=MOSCOW_TZ)
    start_dt = base.replace(hour=sh, minute=sm, second=0, microsecond=0)
    start_total = sh * 60 + sm
    end_total = eh * 60 + em
    if start_total <= end_total:
        end_dt = base.replace(hour=eh, minute=em, second=59, microsecond=999000)
    else:
        # Overnight: shift_date is the day when shift STARTED; ends next day
        end_dt = (base + timedelta(days=1)).replace(hour=eh, minute=em, second=59, microsecond=999000)
    return start_dt.timestamp(), end_dt.timestamp()


def receipts_for_bot_shift(bot_token, shift_date_str):
    """Return list of receipt dicts (parsed from receipts_persist.data) whose
    creation timestamp falls inside the given shift's window.

    Each item: {"receipt_id", "status", "amount", "currency", "pseudonym",
                "_ts" (creation ts), "created_at" (display), "owner_id"}
    """
    start_ts, end_ts = shift_window_for_date(bot_token, shift_date_str)
    conn = db_conn()
    rows = conn.execute(
        "SELECT receipt_id, data, ts FROM receipts_persist WHERE bot_token=? AND ts BETWEEN ? AND ?",
        (bot_token, start_ts, end_ts)
    ).fetchall()
    conn.close()
    out = []
    for receipt_id, data_json, ts in rows:
        try:
            d = json.loads(data_json)
        except Exception:
            continue
        d["receipt_id"] = receipt_id
        d.setdefault("_ts", ts)
        out.append(d)
    out.sort(key=lambda r: r.get("_ts", 0))
    return out


def _group_chat_label(group_id, fallback_group_name=""):
    """Return the human-readable name to show in report headers. Prefer the
    bound TG-chat title (what the bookkeeper sees in their chat list); fall
    back to the group's internal name when no chat is bound."""
    if group_id is not None:
        target = get_target_chat(group_id)
        if target and target.get("chat_title"):
            return target["chat_title"]
    return fallback_group_name or "(без группы)"


def render_auto_shift_report(bot_token, shift_date_str, show_money=True):
    """Build the auto-shift сверка text for one anon-bot.
    Returns (text, payload_dict, group_id) where group_id is the anon-group
    this bot belongs to (or None if the bot is unattached).
    """
    receipts = receipts_for_bot_shift(bot_token, shift_date_str)
    bot_info = next((b for b in list_secret_bots() if b["token"] == bot_token), None)
    bot_geo = bot_info["geo"] if bot_info else ""
    bot_username = bot_info["username"] if bot_info else bot_token[:12]

    # What group does this bot belong to?
    conn = db_conn()
    r = conn.execute(
        "SELECT g.id, g.name, g.commission_pct, g.report_mode FROM acc_groups g "
        "JOIN acc_group_bots gb ON g.id = gb.group_id WHERE gb.bot_token=?",
        (bot_token,)
    ).fetchone()
    conn.close()
    group_id = r[0] if r else None
    group_name = r[1] if r else "(нет группы)"
    pct = float(r[2]) if (r and r[2] is not None) else get_default_commission()
    report_mode = (r[3] if r else "total")

    approved = [r2 for r2 in receipts if r2.get("status") == "approved"]
    pending  = [r2 for r2 in receipts if r2.get("status") == "pending"]
    declined = [r2 for r2 in receipts if r2.get("status") == "declined"]

    total_approved = sum(float(r2.get("amount") or 0) for r2 in approved)
    currency = GEO_CURRENCIES.get(bot_geo, "")

    try:
        d = datetime.strptime(shift_date_str, "%Y-%m-%d")
        header_date = d.strftime("%d.%m.%Y")
    except ValueError:
        header_date = shift_date_str

    chat_label = _group_chat_label(group_id, group_name)
    lines = [
        f"📊 {chat_label} @{bot_username} за смену {header_date}",
        "",
    ]
    if approved:
        if report_mode == "breakdown":
            lines.append("Принятые чеки:")
            for r2 in approved:
                ts = datetime.fromtimestamp(r2.get("_ts", 0), MOSCOW_TZ).strftime("%H:%M")
                amt = fmt_amount(r2.get("amount") or 0)
                ps = r2.get("pseudonym", "?")
                lines.append(f"  {ts} — {ps}: {amt} {currency}".rstrip())
        lines.append(f"Сумма принятых: {fmt_amount(total_approved)} {currency}".rstrip())
    else:
        lines.append("Принятых чеков нет.")

    if pending:
        lines.append("")
        lines.append("⚠️ Неразобранные чеки (не вошли в сумму):")
        for r2 in pending:
            ts = datetime.fromtimestamp(r2.get("_ts", 0), MOSCOW_TZ).strftime("%H:%M")
            amt = fmt_amount(r2.get("amount") or 0)
            ps = r2.get("pseudonym", "?")
            lines.append(f"  {ts} — {ps}: {amt} {currency}".rstrip())

    if show_money:
        rate = get_rate(bot_geo)
        if rate and rate > 0 and total_approved > 0:
            usd = total_approved / rate
            mult = (100.0 - pct) / 100.0
            payout = usd * mult
            lines.append("")
            lines.append(f"Курс: {fmt_rate(rate)}")
            lines.append(f"В USD: {fmt_amount(total_approved)} ÷ {fmt_rate(rate)} = {fmt_usd(usd)} USD")
            lines.append(f"Итого выплата: {fmt_usd(usd)} × {mult:.2f} = {fmt_usd(payout)} USD (вычет {fmt_amount(pct)}%)")
        elif total_approved > 0:
            lines.append("")
            lines.append(f"⚠️ Курс для {GEO_DISPLAY.get(bot_geo, (bot_geo,))[0]} не задан — расчёт пропущен")

    text = "\n".join(lines)
    payload = {
        "kind": "auto_shift",
        "shift_date": shift_date_str,
        "bot_token": bot_token,
        "bot_username": bot_username,
        "bot_geo": bot_geo,
        "group_id": group_id,
        "group_name": group_name,
        "approved": [
            {"receipt_id": r2["receipt_id"], "amount": r2.get("amount"),
             "pseudonym": r2.get("pseudonym"), "ts": r2.get("_ts")}
            for r2 in approved
        ],
        "pending": [
            {"receipt_id": r2["receipt_id"], "amount": r2.get("amount"),
             "pseudonym": r2.get("pseudonym"), "ts": r2.get("_ts")}
            for r2 in pending
        ],
        "declined_count": len(declined),
        "total_approved": total_approved,
        "currency": currency,
        "commission_pct": pct,
    }
    return text, payload, group_id


def previous_shift_date(bot_token, ref_dt=None):
    """Previous CLOSED shift's date (the one we want to report on).
    If we're inside today's shift, that's yesterday. If today's shift just
    finished, today's working day is the closed one.
    """
    shift = get_bot_shift(bot_token)
    start = shift["start"]
    now = ref_dt or datetime.now(MOSCOW_TZ)
    # Working day in progress right now:
    todays_working = datetime.strptime(current_working_day(bot_token, now), "%Y-%m-%d")
    # The shift that just closed is one working-day before the current.
    prev = todays_working - timedelta(days=1)
    return prev.strftime("%Y-%m-%d")


HISTORY_RETENTION_DAYS = 5


def purge_old_history(retention_days=HISTORY_RETENTION_DAYS):
    """Hard-delete acc_reports rows older than retention_days. Pending
    (pending_bookkeeper / pending_owner) rows are never purged regardless of
    age — only finalized statuses (sent/cancelled/expired) get cleaned."""
    cutoff = time.time() - retention_days * 86400
    conn = db_conn()
    cur = conn.execute(
        "DELETE FROM acc_reports WHERE created_at < ? "
        "AND status IN ('sent','cancelled','expired')",
        (cutoff,)
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def list_recent_reports(limit=50):
    purge_old_history()
    cutoff = time.time() - HISTORY_RETENTION_DAYS * 86400
    conn = db_conn()
    rows = conn.execute(
        "SELECT id, report_date, created_at, kind, text_preview, bot_token, status, payload, group_id "
        "FROM acc_reports WHERE created_at >= ? "
        "ORDER BY created_at DESC LIMIT ?",
        (cutoff, limit)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        bot_username = None
        try:
            if r[7]:
                pl = json.loads(r[7])
                bot_username = pl.get("bot_username")
        except Exception:
            pass
        out.append({
            "id": r[0], "date": r[1], "created_at": r[2], "kind": r[3],
            "preview": r[4], "bot_token": r[5], "status": r[6],
            "bot_username": bot_username,
            "group_id": r[8],
        })
    return out


def list_recent_history_bundles(limit_rows=300):
    """Group `auto_shift` entries by (group_id, report_date) into bundles so
    Owner sees one entry per group per shift instead of one per bot. Non-
    auto_shift entries stay as standalone records.

    Returns a list ordered by latest activity, each item:
      kind='group_bundle': {kind, group_id, date, latest_created_at,
                            report_ids, statuses, bot_usernames}
      kind='single':       {kind, id, date, created_at, status, sub_kind, label}
    """
    rows = list_recent_reports(limit=limit_rows)
    bundles = {}
    singles = []
    for r in rows:
        if r["kind"] == "auto_shift" and r.get("group_id") is not None:
            key = (r["group_id"], r["date"])
            b = bundles.get(key)
            if b is None:
                b = {
                    "kind": "group_bundle",
                    "group_id": r["group_id"],
                    "date": r["date"],
                    "latest_created_at": r["created_at"],
                    "report_ids": [],
                    "statuses": [],
                    "bot_usernames": [],
                }
                bundles[key] = b
            b["report_ids"].append(r["id"])
            b["statuses"].append(r["status"])
            if r.get("bot_username"):
                b["bot_usernames"].append(r["bot_username"])
            if r["created_at"] > b["latest_created_at"]:
                b["latest_created_at"] = r["created_at"]
        else:
            singles.append({
                "kind": "single",
                "sub_kind": r["kind"],
                "id": r["id"],
                "date": r["date"],
                "created_at": r["created_at"],
                "status": r["status"],
                "bot_username": r.get("bot_username"),
            })
    out = list(bundles.values()) + singles
    out.sort(key=lambda x: x.get("latest_created_at") or x.get("created_at") or 0, reverse=True)
    return out


def get_report(report_id):
    conn = db_conn()
    r = conn.execute(
        "SELECT report_date, created_at, kind, payload, text_preview, sent_to, "
        "status, group_id, bot_token FROM acc_reports WHERE id=?", (report_id,)
    ).fetchone()
    conn.close()
    if not r:
        return None
    return {
        "date": r[0], "created_at": r[1], "kind": r[2],
        "payload": json.loads(r[3]) if r[3] else None,
        "text_preview": r[4],
        "sent_to": json.loads(r[5]) if r[5] else [],
        "status": r[6],
        "group_id": r[7],
        "bot_token": r[8],
    }


# ---------- Number formatting ----------

def _truncate_to_tenths(num):
    """Truncate (not round) a float to 1 decimal place.
    15.78 -> 15.7, -3.99 -> -3.9
    """
    import math
    if num >= 0:
        return math.floor(num * 10) / 10
    return math.ceil(num * 10) / 10


def fmt_amount(value):
    """Spanish/Argentinian-style numbers with thousands separator and at most
    1 decimal: 1.234.567,5 (or 1.234.567 if integer).
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    truncated = _truncate_to_tenths(num)
    if truncated == int(truncated):
        s = str(int(truncated))
        out = ""
        for i, ch in enumerate(reversed(s)):
            if i > 0 and i % 3 == 0:
                out = "." + out
            out = ch + out
        return out
    s = f"{truncated:.1f}"
    integer, decimal = s.split(".")
    sign = ""
    if integer.startswith("-"):
        sign = "-"
        integer = integer[1:]
    out = ""
    for i, ch in enumerate(reversed(integer)):
        if i > 0 and i % 3 == 0:
            out = "." + out
        out = ch + out
    return f"{sign}{out},{decimal}"


def fmt_usd(value):
    """Plain USD with at most 1 decimal, truncated."""
    try:
        num = _truncate_to_tenths(float(value))
    except (TypeError, ValueError):
        return str(value)
    if num == int(num):
        return str(int(num))
    return f"{num:.1f}"


def fmt_usd2(value):
    """USD with exactly 2 decimals (rounded). Used in totals where the
    finance team wants the cents to line up."""
    try:
        num = round(float(value), 2)
    except (TypeError, ValueError):
        return str(value)
    return f"{num:.2f}"


def fmt_rate(value):
    """Format an FX rate with up to 2 decimals (no truncation), thousands
    separator and comma decimal — e.g. 10.08 -> "10,08", 1495 -> "1.495",
    3690.5 -> "3.690,50".
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Round to 2 decimals to avoid float artifacts; do not truncate.
    rounded = round(num, 2)
    if rounded == int(rounded):
        s = str(int(rounded))
        out = ""
        for i, ch in enumerate(reversed(s)):
            if i > 0 and i % 3 == 0:
                out = "." + out
            out = ch + out
        return out
    s = f"{rounded:.2f}"
    integer, decimal = s.split(".")
    sign = ""
    if integer.startswith("-"):
        sign = "-"
        integer = integer[1:]
    out = ""
    for i, ch in enumerate(reversed(integer)):
        if i > 0 and i % 3 == 0:
            out = "." + out
        out = ch + out
    return f"{sign}{out},{decimal}"


# ---------- Broadcasting via secret-bot tokens ----------

_broadcast_sem = asyncio.Semaphore(8)


async def send_via_bot(client, bot_token, chat_id, text):
    async with _broadcast_sem:
        try:
            r = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=20.0,
            )
            data = r.json()
            return bool(data.get("ok"))
        except Exception as e:
            logger.error(f"send_via_bot failed for chat {chat_id}: {e}")
            return False


async def broadcast_text(bot_token, text):
    """Send `text` to every user registered in `bot_token`'s secret chat.

    Returns (sent, failed) counts.
    """
    user_ids = bot_users(bot_token)
    if not user_ids:
        return 0, 0
    sent = 0
    failed = 0
    async with httpx.AsyncClient(http2=False) as client:
        results = await asyncio.gather(
            *[send_via_bot(client, bot_token, uid, text) for uid in user_ids],
            return_exceptions=True
        )
    for r in results:
        if r is True:
            sent += 1
        else:
            failed += 1
    return sent, failed


# ---------- Reconciliation rendering ----------

def commission_for_group(group):
    pct = group.get("commission_pct")
    if pct is None:
        pct = get_default_commission()
    return float(pct)


def render_geo_section(group, geo, items, rate, show_money=True):
    """One (group, geo) sub-section.
    show_money=False hides commission/USD (used for Бухгалтер view).
    Returns (text, payout_usd_or_None, pct).
    """
    currency = GEO_CURRENCIES.get(geo, "")
    total = sum(items)
    pct = commission_for_group(group)
    multiplier = (100.0 - pct) / 100.0
    geo_name, flag = GEO_DISPLAY.get(geo, (geo, ""))

    lines = [f"— {flag} {geo_name} —".strip()]
    if group["report_mode"] == "breakdown" and len(items) > 1:
        lines.append("Чеки:")
        for x in items:
            lines.append(f"  {fmt_amount(x)} {currency}".rstrip())
    lines.append(f"Сумма: {fmt_amount(total)} {currency}".rstrip())
    if not show_money:
        # Bookkeeper view — no rate/USD/commission.
        return "\n".join(lines), None, pct
    if rate and rate > 0:
        usd = total / rate
        payout = usd * multiplier
        lines.append(f"Курс: {fmt_rate(rate)}")
        lines.append(f"В USD: {fmt_amount(total)} ÷ {fmt_rate(rate)} = {fmt_usd(usd)} USD")
        lines.append(f"Итого выплата: {fmt_usd(usd)} × {multiplier:.2f} = {fmt_usd(payout)} USD")
        return "\n".join(lines), payout, pct
    lines.append("⚠️ Курс не задан — расчёт пропущен")
    return "\n".join(lines), None, pct


def render_group_block(group, geo_data, rates, show_money=True):
    """Build a group's section spanning all its (geo, items) pairs."""
    lines = [group["name"]]
    payouts = []
    pct = commission_for_group(group)
    for geo in sorted(geo_data.keys()):
        items = geo_data[geo]
        rate = rates.get(geo)
        sec_text, sec_payout, _pct = render_geo_section(group, geo, items, rate, show_money=show_money)
        lines.append("")
        lines.append(sec_text)
        if sec_payout is not None:
            payouts.append(sec_payout)
    if show_money and len(payouts) > 1:
        parts = " + ".join(fmt_usd(p) for p in payouts)
        total = sum(payouts)
        lines.append("")
        lines.append(f"Итого по группе: {parts} = {fmt_usd(total)} USD")
    group_total = sum(payouts) if payouts else None
    return "\n".join(lines), group_total, pct


def render_reconciliation_for_bot(bot_token, date_str, show_money=True):
    """Build a сверка message text aggregating ALL groups attached to this bot.

    Returns (text, payout_total_usd, group_blocks_meta).
    """
    conn = db_conn()
    rows = conn.execute(
        "SELECT acc_groups.id FROM acc_groups "
        "JOIN acc_group_bots ON acc_groups.id = acc_group_bots.group_id "
        "WHERE acc_group_bots.bot_token = ? AND acc_groups.enabled = 1",
        (bot_token,)
    ).fetchall()
    conn.close()
    group_ids = [r[0] for r in rows]
    if not group_ids:
        return None, None, []

    blocks = []
    group_payouts = []
    meta = []
    distinct_commissions = set()
    for gid in group_ids:
        group = get_group(gid)
        if not group:
            continue
        geo_data = get_daily_data_all_geos(gid, date_str)
        if not geo_data:
            continue
        rates = {geo: get_rate(geo) for geo in geo_data.keys()}
        block, group_total_payout, pct = render_group_block(group, geo_data, rates, show_money=show_money)
        blocks.append(block)
        if group_total_payout is not None:
            group_payouts.append(group_total_payout)
        distinct_commissions.add(pct)
        meta.append({
            "group_id": gid,
            "group_name": group["name"],
            "geo_data": geo_data,
            "rates": rates,
            "payout_usd": group_total_payout,
            "commission_pct": pct,
        })

    if not blocks:
        return None, None, []

    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        header_date = d.strftime("%d.%m.%Y")
    except ValueError:
        header_date = date_str

    if show_money and len(distinct_commissions) == 1:
        single_pct = next(iter(distinct_commissions))
        header = f"Расчёт за {header_date} (вычет {fmt_amount(single_pct)}%)"
    else:
        header = f"Расчёт за {header_date}"

    text = header + "\n\n" + "\n\n".join(blocks)

    if show_money and len(group_payouts) > 1:
        total = sum(group_payouts)
        parts_str = " + ".join(fmt_usd(p) for p in group_payouts)
        text += f"\n\nИтого за {header_date}: {parts_str} = {fmt_usd(total)} USD"

    return text, (sum(group_payouts) if group_payouts else 0.0), meta


def render_rates_message():
    """Build the rates broadcast message."""
    rates = get_rates()
    today = datetime.now(MOSCOW_TZ).strftime("%d.%m")
    lines = [f"💹 Курсы {today}", ""]
    for geo in GEO_ORDER:
        if geo not in rates:
            continue
        name, flag = GEO_DISPLAY[geo]
        lines.append(f"{name} {flag} — {fmt_rate(rates[geo]['rate'])}")
    return "\n".join(lines)


# ---------- State machine for multi-step inputs ----------

# Per-user transient state: { user_id: {"mode": "...", "data": {...}} }
_user_state = {}

def set_state(user_id, state):
    if state is None:
        _user_state.pop(user_id, None)
    else:
        _user_state[user_id] = state


def get_state(user_id):
    return _user_state.get(user_id)


# ---------- UI helpers ----------

def access_text():
    return "⛔ Доступ запрещён"


def cancel_kb():
    """One-button inline keyboard with a cancel option, used on every text-input
    prompt so users don't have to type «отмена»."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel:state")]])


def main_menu(user_id):
    is_owner = is_acc_owner(user_id)
    rows = []
    if is_owner:
        owner_count = len(list_pending_reports_for_owner())
        oc_label = f"📋 Новые отчёты ({owner_count})" if owner_count else "📋 Новые отчёты"
        rows.append([InlineKeyboardButton(oc_label, callback_data="menu:pending_reviews")])
        rows.append([InlineKeyboardButton("💱 Ввести курсы", callback_data="menu:enter_rates")])
        rows.append([InlineKeyboardButton("📤 Отправить курсы", callback_data="menu:send_rates")])
        rows.append([InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")])
    else:
        # Бухгалтер: только своя работа
        bk_count = len(list_pending_reports_for_bookkeeper())
        bk_label = f"📥 Новые отчёты ({bk_count})" if bk_count else "📥 Новые отчёты"
        rows.append([InlineKeyboardButton(bk_label, callback_data="menu:pending_reviews")])
        rows.append([InlineKeyboardButton("💱 Ввести курсы", callback_data="menu:enter_rates")])
        submit_label = (
            f"📤 Передать на проверку ({bk_count})" if bk_count else "📤 Передать на проверку"
        )
        rows.append([InlineKeyboardButton(submit_label, callback_data="menu:submit_review")])
        rows.append([InlineKeyboardButton("📜 История", callback_data="menu:history")])
    return InlineKeyboardMarkup(rows)


def settings_menu():
    rows = [
        [InlineKeyboardButton("🛠 Список групп", callback_data="menu:groups")],
        [InlineKeyboardButton("💬 Привязка чатов", callback_data="menu:target_chats")],
        [InlineKeyboardButton("⚙️ Комиссия по умолчанию", callback_data="menu:default_pct")],
        [InlineKeyboardButton("📜 История", callback_data="menu:history")],
        [InlineKeyboardButton("👤 Роли", callback_data="menu:roles")],
        [InlineKeyboardButton("« Меню", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(rows)


def cancel_text():
    return "Отменено. /start для меню."


# ---------- Handlers ----------

def _is_private_chat(update: Update) -> bool:
    """Bot must only react in private DMs — never in groups/channels where it
    posts сверки and курсы. Treats anything that isn't a 1-on-1 chat as not-allowed.
    """
    chat = (update.effective_message and update.effective_message.chat) or (
        update.callback_query and update.callback_query.message and update.callback_query.message.chat
    )
    return bool(chat and chat.type == "private")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_chat(update):
        return  # silently ignore in groups
    user_id = update.effective_user.id
    if not is_acc_admin_or_owner(user_id):
        await update.message.reply_text(access_text())
        return
    set_state(user_id, None)
    role = "Owner" if is_acc_owner(user_id) else "Бухгалтер"
    await update.message.reply_text(
        f"👋 Бот сверки и курсов\n\nРоль: {role}\n\nВыберите действие:",
        reply_markup=main_menu(user_id),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_private_chat(update):
        await query.answer()
        return  # ignore callbacks from posted-into-group messages
    user_id = query.from_user.id
    if not is_acc_admin_or_owner(user_id):
        await query.answer(access_text(), show_alert=True)
        return
    data = query.data or ""
    try:
        await router(update, context, data)
    except Exception as e:
        logger.exception(f"Callback error: {e}")
        try:
            await query.answer(f"Ошибка: {e}", show_alert=True)
        except Exception:
            pass


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_chat(update):
        return  # ignore replies/text in groups
    user_id = update.effective_user.id
    if not is_acc_admin_or_owner(user_id):
        await update.message.reply_text(access_text())
        return
    text = (update.message.text or "").strip()
    if text.lower() in ("отмена", "cancel", "/cancel"):
        set_state(user_id, None)
        await update.message.reply_text(cancel_text(), reply_markup=main_menu(user_id))
        return
    state = get_state(user_id)
    if not state:
        await update.message.reply_text("Используйте /start", reply_markup=main_menu(user_id))
        return
    await state_router(update, context, state, text)


async def _start_data_entry(query, user_id, gid, geo):
    group = get_group(gid)
    if not group:
        await query.answer("Группа не найдена", show_alert=True)
        return
    set_state(user_id, {"mode": "enter_data", "group_id": gid, "geo": geo})
    mode_hint = "одной суммой" if group["report_mode"] == "total" else "построчно или одной суммой"
    existing = get_daily_data(gid, geo, today_str())
    existing_str = ""
    if existing:
        existing_str = "\n\nТекущие данные:\n" + "\n".join(f"  {fmt_amount(x)}" for x in existing)
    geo_name = GEO_DISPLAY.get(geo, (geo, ""))[0]
    await query.answer()
    await query.edit_message_text(
        f"📥 {group['name']} — {geo_name}\n"
        f"Формат: {mode_hint}.\n"
        f"Отправьте сумму (одну) или каждое число с новой строки.{existing_str}",
        reply_markup=cancel_kb()
    )


# ---------- Callback router (inline buttons) ----------

async def router(update, context, data):
    query = update.callback_query
    user_id = query.from_user.id

    if data == "menu:main":
        await query.answer()
        set_state(user_id, None)
        await query.edit_message_text("Меню:", reply_markup=main_menu(user_id))
        return

    if data == "menu:settings":
        if not is_acc_owner(user_id):
            await query.answer(access_text(), show_alert=True)
            return
        await query.answer()
        await query.edit_message_text("⚙️ Настройки:", reply_markup=settings_menu())
        return

    if data == "cancel:state":
        # Generic cancel for any text-input state. Resets state and goes to main menu.
        set_state(user_id, None)
        await query.answer("Отменено")
        try:
            await query.edit_message_text("Меню:", reply_markup=main_menu(user_id))
        except Exception:
            pass
        return

    if data == "menu:enter_data":
        await query.answer()
        groups = list_groups(enabled_only=True)
        if not groups:
            await query.edit_message_text(
                "Нет включённых групп. Создайте в «Список групп».",
                reply_markup=main_menu(user_id))
            return
        date = today_str()
        rows = []
        for g in groups:
            geos = get_group_geos(g["id"])
            all_data = get_daily_data_all_geos(g["id"], date)
            # Tick the group if EVERY attached geo has data
            mark = ""
            if geos:
                if all(geo in all_data for geo in geos):
                    mark = " ✓"
                elif any(geo in all_data for geo in geos):
                    mark = " ◐"
            geo_flags = " ".join(GEO_DISPLAY.get(geo, ("", ""))[1] for geo in geos) or "—"
            rows.append([InlineKeyboardButton(
                f"{g['name']} {geo_flags}{mark}",
                callback_data=f"data:enter:{g['id']}"
            )])
        rows.append([InlineKeyboardButton("« Меню", callback_data="menu:main")])
        await query.edit_message_text(
            f"📥 Ввод данных за {date}\n"
            f"✓ — все гео заполнены, ◐ — часть\n"
            f"Выберите группу:",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("data:enter:"):
        gid = int(data.split(":")[2])
        group = get_group(gid)
        if not group:
            await query.answer("Группа не найдена", show_alert=True)
            return
        geos = get_group_geos(gid)
        if not geos:
            await query.answer()
            await query.edit_message_text(
                f"В группе «{group['name']}» нет привязанных гео.\n"
                f"Добавьте гео через «🛠 Список групп» → «🌍 Гео».",
                reply_markup=main_menu(user_id))
            return
        if len(geos) == 1:
            # Single geo — go straight to entry
            return await _start_data_entry(query, user_id, gid, geos[0])
        # Multiple geos — show geo picker
        all_data = get_daily_data_all_geos(gid, today_str())
        rows = []
        for geo in geos:
            mark = " ✓" if geo in all_data else ""
            name, flag = GEO_DISPLAY.get(geo, (geo, ""))
            rows.append([InlineKeyboardButton(
                f"{flag} {name}{mark}",
                callback_data=f"data:enter_geo:{gid}:{geo}"
            )])
        rows.append([InlineKeyboardButton("« К списку групп", callback_data="menu:enter_data")])
        await query.answer()
        await query.edit_message_text(
            f"📥 {group['name']} — выберите гео:",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("data:enter_geo:"):
        _, _, gid_s, geo = data.split(":", 3)
        return await _start_data_entry(query, user_id, int(gid_s), geo)

    if data == "menu:enter_rates":
        await query.answer()
        await show_rates_editor(query, user_id)
        return

    if data.startswith("rate:set:"):
        geo = data.split(":")[2]
        set_state(user_id, {"mode": "enter_rate", "geo": geo})
        cur = get_rate(geo)
        await query.answer()
        cur_line = f"\nТекущий: {fmt_rate(cur)}" if cur else ""
        await query.edit_message_text(
            f"💱 Курс для {GEO_DISPLAY.get(geo, (geo,))[0]}\n"
            f"Отправьте число.{cur_line}",
            reply_markup=cancel_kb()
        )
        return

    if data.startswith("rate:delete:"):
        geo = data.split(":")[2]
        cur = get_rate(geo)
        if cur is None:
            await query.answer("Курс уже не задан", show_alert=True)
            await show_rates_editor(query, user_id)
            return
        name = GEO_DISPLAY.get(geo, (geo,))[0]
        await query.answer()
        rows = [
            [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"rate:delete_yes:{geo}")],
            [InlineKeyboardButton("« Назад", callback_data="menu:enter_rates")],
        ]
        await query.edit_message_text(
            f"Удалить курс {name}: {fmt_rate(cur)}?",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("rate:delete_yes:"):
        geo = data.split(":")[2]
        delete_rate(geo)
        await query.answer("Курс удалён")
        await show_rates_editor(query, user_id)
        return

    if data == "menu:make_recon":
        await query.answer()
        await build_recon_preview(query, user_id)
        return

    if data == "recon:confirm":
        await query.answer("Отправляю…")
        await send_recon(query, user_id)
        return

    if data == "menu:send_rates":
        await query.answer()
        await build_rates_preview(query, user_id)
        return

    if data == "rates:confirm":
        await query.answer("Отправляю…")
        await send_rates(query, user_id)
        return

    if data == "menu:groups":
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        await query.answer()
        await show_groups_list(query)
        return

    if data == "group:new":
        set_state(user_id, {"mode": "new_group_name"})
        await query.answer()
        await query.edit_message_text(
            "🆕 Новая группа.\nОтправьте название группы.",
            reply_markup=cancel_kb()
        )
        return

    if data.startswith("group:view:"):
        gid = int(data.split(":")[2])
        await query.answer()
        await show_group_view(query, gid)
        return

    if data.startswith("group:rename:"):
        gid = int(data.split(":")[2])
        set_state(user_id, {"mode": "rename_group", "group_id": gid})
        await query.answer()
        await query.edit_message_text(
            "Отправьте новое название группы.",
            reply_markup=cancel_kb()
        )
        return

    if data.startswith("group:setgeo:"):
        gid = int(data.split(":")[2])
        await query.answer()
        attached = set(get_group_geos(gid))
        rows = []
        for g in GEO_ORDER:
            name, flag = GEO_DISPLAY[g]
            mark = "✅" if g in attached else "▫️"
            rows.append([InlineKeyboardButton(
                f"{mark} {flag} {name}",
                callback_data=f"group:geo_toggle:{gid}:{g}"
            )])
        rows.append([InlineKeyboardButton("« Назад", callback_data=f"group:view:{gid}")])
        await query.edit_message_text(
            "🌍 Гео группы\nНажмите чтобы добавить/убрать:",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("group:geo_toggle:"):
        _, _, gid_s, geo = data.split(":", 3)
        gid = int(gid_s)
        attached = set(get_group_geos(gid))
        if geo in attached:
            remove_group_geo(gid, geo)
            await query.answer("Удалено (с данными)")
        else:
            add_group_geo(gid, geo)
            # Also update legacy groups.geo to keep backward-display consistent
            if not attached:
                update_group(gid, geo=geo)
            await query.answer("Добавлено")
        # Re-render the same picker so admin can keep toggling
        attached = set(get_group_geos(gid))
        rows = []
        for g in GEO_ORDER:
            name, flag = GEO_DISPLAY[g]
            mark = "✅" if g in attached else "▫️"
            rows.append([InlineKeyboardButton(
                f"{mark} {flag} {name}",
                callback_data=f"group:geo_toggle:{gid}:{g}"
            )])
        rows.append([InlineKeyboardButton("« Назад", callback_data=f"group:view:{gid}")])
        await query.edit_message_text(
            "🌍 Гео группы\nНажмите чтобы добавить/убрать:",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("group:setpct:"):
        gid = int(data.split(":")[2])
        set_state(user_id, {"mode": "set_group_pct", "group_id": gid})
        await query.answer()
        await query.edit_message_text(
            "Введите % комиссии для этой группы (число, например 15 или 12.5).\n"
            "Чтобы вернуться к дефолту — отправьте «дефолт».",
            reply_markup=cancel_kb()
        )
        return

    if data.startswith("group:setmode:"):
        gid = int(data.split(":")[2])
        rows = [
            [InlineKeyboardButton("Одна сумма (total)", callback_data=f"group:mode:{gid}:total")],
            [InlineKeyboardButton("Разбивка по чекам (breakdown)", callback_data=f"group:mode:{gid}:breakdown")],
            [InlineKeyboardButton("« Назад", callback_data=f"group:view:{gid}")],
        ]
        await query.answer()
        await query.edit_message_text(
            "Формат отчёта для группы:\n"
            "  • total — в сверке только итоговая сумма\n"
            "  • breakdown — в сверке отдельно каждый чек + сумма",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("group:mode:"):
        _, _, gid_s, mode = data.split(":", 3)
        update_group(int(gid_s), report_mode=mode)
        await query.answer("Формат обновлён")
        await show_group_view(query, int(gid_s))
        return

    if data.startswith("group:toggle:"):
        gid = int(data.split(":")[2])
        g = get_group(gid)
        if g:
            update_group(gid, enabled=0 if g["enabled"] else 1)
        await query.answer("Переключено")
        await show_group_view(query, gid)
        return

    if data.startswith("group:bots:"):
        gid = int(data.split(":")[2])
        await query.answer()
        await show_group_bots_editor(query, gid)
        return

    if data.startswith("gbot:toggle:"):
        _, _, gid_s, idx_s = data.split(":", 3)
        gid = int(gid_s)
        idx = int(idx_s)
        all_bots = list_secret_bots()
        if 0 <= idx < len(all_bots):
            target_tok = all_bots[idx]["token"]
            attached = set(get_group_bots(gid))
            if target_tok in attached:
                # Already in THIS group → detach
                attached.discard(target_tok)
                set_group_bots(gid, list(attached))
                await query.answer("Отвязан")
            else:
                # Trying to attach. Refuse if already owned by another group.
                owner = get_bot_owning_group(target_tok)
                if owner and owner[0] != gid:
                    await query.answer(
                        f"Уже в группе «{owner[1]}». Сначала отвяжите там.",
                        show_alert=True
                    )
                else:
                    attached.add(target_tok)
                    set_group_bots(gid, list(attached))
                    await query.answer("Привязан")
        else:
            await query.answer()
        await show_group_bots_editor(query, gid)
        return

    if data.startswith("group:delete:"):
        gid = int(data.split(":")[2])
        g = get_group(gid)
        rows = [
            [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"group:delete_yes:{gid}")],
            [InlineKeyboardButton("« Назад", callback_data=f"group:view:{gid}")],
        ]
        await query.answer()
        await query.edit_message_text(
            f"Удалить группу «{g['name']}»? История сверок останется.",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("group:delete_yes:"):
        gid = int(data.split(":")[2])
        delete_group(gid)
        await query.answer("Удалено")
        await show_groups_list(query)
        return

    if data == "menu:default_pct":
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        set_state(user_id, {"mode": "set_default_pct"})
        cur = get_default_commission()
        await query.answer()
        await query.edit_message_text(
            f"⚙️ Дефолтная комиссия\nТекущее значение: {fmt_amount(cur)}%\n"
            f"Введите новый %.",
            reply_markup=cancel_kb()
        )
        return

    if data == "menu:submit_review":
        await query.answer()
        await build_submit_review(query, user_id)
        return

    if data == "submit_review:confirm":
        await query.answer("Отправлено Owner-у на проверку")
        await submit_for_review(query, user_id)
        return

    if data == "menu:pending_reviews":
        await query.answer()
        await show_pending_reviews(query, user_id)
        return

    if data.startswith("review:view:"):
        rid = int(data.split(":")[2])
        await query.answer()
        await show_review_detail(query, rid, user_id)
        return

    if data.startswith("review:bookkeeper_done:"):
        # Bookkeeper hits «Готово» — show confirmation first.
        rid = int(data.split(":")[2])
        rep = get_report(rid)
        if not rep:
            await query.answer("Не найден", show_alert=True)
            return
        if rep["status"] != "pending_bookkeeper":
            await query.answer("Уже не у бухгалтера", show_alert=True)
            return
        await query.answer()
        rows = [
            [InlineKeyboardButton("✅ Да, передать", callback_data=f"review:bookkeeper_done_yes:{rid}")],
            [InlineKeyboardButton("« Назад", callback_data=f"review:view:{rid}")],
        ]
        await query.edit_message_text(
            f"Передать отчёт #{rid} на проверку?\n"
            f"После этого ты не сможешь его править.",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("review:bookkeeper_done_yes:"):
        rid = int(data.split(":")[2])
        rep = get_report(rid)
        if not rep:
            await query.answer("Не найден", show_alert=True)
            return
        if rep["status"] != "pending_bookkeeper":
            await query.answer("Уже передан", show_alert=True)
            await show_pending_reviews(query, user_id)
            return
        update_report_status(rid, "pending_owner")
        await query.answer("Отправлено Owner-у")
        bot_obj = query.get_bot()
        for owner_uid in get_owners():
            try:
                await bot_obj.send_message(
                    chat_id=owner_uid,
                    text=(f"📥 Бухгалтер передал отчёт #{rid} на проверку Owner.\n"
                          f"Откройте «📥 На проверке».")
                )
            except Exception:
                pass
        await show_pending_reviews(query, user_id)
        return

    if data.startswith("review:approve_send:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        rid = int(data.split(":")[2])
        await query.answer()
        rows = [
            [InlineKeyboardButton("✅ Да, отправить в чат", callback_data=f"review:approve_send_yes:{rid}")],
            [InlineKeyboardButton("« Назад", callback_data=f"review:view:{rid}")],
        ]
        await query.edit_message_text(
            f"Отправить отчёт #{rid} в TG-чат группы?\n"
            f"После отправки изменить уже нельзя.",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("review:approve_send_yes:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        rid = int(data.split(":")[2])
        await query.answer("Отправляю в чат…")
        await approve_and_send_review(query, rid, user_id)
        return

    if data.startswith("review:approve_group:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        gid = int(data.split(":")[2])
        siblings = _pending_owner_reports_for_group(gid)
        if not siblings:
            await query.answer("Нет готовых отчётов для этой группы", show_alert=True)
            return
        # Initialize selection: all selected by default
        excluded = context.user_data.setdefault("batch_excluded", {})
        excluded[gid] = set()
        await query.answer()
        await _show_batch_picker(query, context, gid)
        return

    if data.startswith("review:batch_toggle:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        _, _, gid_s, rid_s = data.split(":", 3)
        gid, rid = int(gid_s), int(rid_s)
        excluded = context.user_data.setdefault("batch_excluded", {}).setdefault(gid, set())
        if rid in excluded:
            excluded.discard(rid)
        else:
            excluded.add(rid)
        await query.answer()
        await _show_batch_picker(query, context, gid)
        return

    if data.startswith("review:batch_all:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        gid = int(data.split(":")[2])
        context.user_data.setdefault("batch_excluded", {})[gid] = set()
        await query.answer("Все выбраны")
        await _show_batch_picker(query, context, gid)
        return

    if data.startswith("review:batch_none:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        gid = int(data.split(":")[2])
        siblings = _pending_owner_reports_for_group(gid)
        context.user_data.setdefault("batch_excluded", {})[gid] = {r["id"] for r in siblings}
        await query.answer("Все сняты")
        await _show_batch_picker(query, context, gid)
        return

    if data.startswith("review:approve_group_yes:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        gid = int(data.split(":")[2])
        excluded = context.user_data.get("batch_excluded", {}).get(gid, set())
        await query.answer("Отправляю одним сообщением…")
        await approve_group_batch(query, gid, user_id, excluded_ids=excluded)
        # cleanup
        context.user_data.get("batch_excluded", {}).pop(gid, None)
        return

    if data == "review:approve_all":
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        pending = list_pending_reports_for_owner()
        if not pending:
            await query.answer("Нет отчётов на отправку", show_alert=True)
            return
        await query.answer()
        rows = [
            [InlineKeyboardButton("✅ Да, отправить всё", callback_data="review:approve_all_yes")],
            [InlineKeyboardButton("« Назад", callback_data="menu:pending_reviews")],
        ]
        await query.edit_message_text(
            f"Отправить ВСЕ {len(pending)} отчётов? "
            f"Каждой группе уйдёт одно сводное сообщение в её TG-чат с общей "
            f"выплатой по группе.",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "review:approve_all_yes":
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        await query.answer("Отправляю всё…")
        await approve_all_pending_batch(query, user_id)
        return

    if data.startswith("review:edit_list:"):
        rid = int(data.split(":")[2])
        rep = get_report(rid)
        if not rep:
            await query.answer("Не найден", show_alert=True)
            return
        if not _can_edit_report(rep, user_id):
            await query.answer("Нет прав на редактирование", show_alert=True)
            return
        await query.answer()
        await show_edit_list(query, rid, rep, user_id)
        return

    if data.startswith("review:edit_one:"):
        _, _, rid_s, receipt_id = data.split(":", 3)
        rid = int(rid_s)
        rep = get_report(rid)
        if not rep:
            await query.answer("Не найден", show_alert=True)
            return
        if not _can_edit_report(rep, user_id):
            await query.answer("Нет прав на редактирование", show_alert=True)
            return
        snap = rep["payload"] or {}
        overrides = snap.get("overrides") or {}
        cur_amount = None
        cur_currency = snap.get("currency", "")
        for lst in (snap.get("approved", []), snap.get("pending", []), snap.get("manual_added", [])):
            for r in lst:
                if str(r.get("receipt_id")) == receipt_id:
                    cur_amount = overrides.get(receipt_id, r.get("amount"))
                    break
            if cur_amount is not None:
                break
        if cur_amount is None:
            await query.answer("Чек не найден в отчёте", show_alert=True)
            return
        set_state(user_id, {
            "mode": "edit_receipt_amount",
            "report_id": rid,
            "receipt_id": receipt_id,
        })
        await query.answer()
        await query.edit_message_text(
            f"✏️ Изменение суммы чека\n\n"
            f"Текущая: {fmt_amount(cur_amount)} {cur_currency}\n\n"
            f"Отправьте новую сумму числом.",
            reply_markup=cancel_kb()
        )
        return

    if data.startswith("review:toggle_exclude:"):
        _, _, rid_s, receipt_id = data.split(":", 3)
        rid = int(rid_s)
        rep = get_report(rid)
        if not rep:
            await query.answer("Не найден", show_alert=True)
            return
        if not _can_edit_report(rep, user_id):
            await query.answer("Нет прав на редактирование", show_alert=True)
            return
        snap = rep["payload"] or {}
        excluded = set(snap.get("excluded") or [])
        if receipt_id in excluded:
            excluded.discard(receipt_id)
            note = "Чек возвращён"
        else:
            excluded.add(receipt_id)
            note = "Чек исключён"
        snap["excluded"] = sorted(excluded)
        _stamp_editor(snap, user_id)
        text, _p, _g = _rerender_auto_shift_from_snapshot(snap, show_money=is_acc_owner(user_id))
        update_report_payload(rid, snap, text_preview=text)
        await query.answer(note)
        await show_edit_list(query, rid, get_report(rid), user_id)
        return

    if data.startswith("review:add_manual:"):
        rid = int(data.split(":")[2])
        rep = get_report(rid)
        if not rep:
            await query.answer("Не найден", show_alert=True)
            return
        if not _can_edit_report(rep, user_id):
            await query.answer("Нет прав на редактирование", show_alert=True)
            return
        snap = rep["payload"] or {}
        currency = snap.get("currency", "")
        set_state(user_id, {"mode": "add_manual_receipt", "report_id": rid})
        await query.answer()
        await query.edit_message_text(
            f"➕ Добавить чек вручную\n\n"
            f"Отправь сообщение в формате:\n"
            f"  <сумма> <псевдоним>\n\n"
            f"Например: 1500 Damir\n"
            f"Валюта: {currency or '—'}",
            reply_markup=cancel_kb()
        )
        return

    if data.startswith("review:reset_overrides:"):
        rid = int(data.split(":")[2])
        rep = get_report(rid)
        if not rep:
            await query.answer("Не найден", show_alert=True)
            return
        if not _can_edit_report(rep, user_id):
            await query.answer("Нет прав на редактирование", show_alert=True)
            return
        snap = rep["payload"] or {}
        snap.pop("overrides", None)
        snap.pop("excluded", None)
        snap.pop("manual_added", None)
        snap.pop("edited_by", None)
        text, _p, _g = _rerender_auto_shift_from_snapshot(snap, show_money=is_acc_owner(user_id))
        update_report_payload(rid, snap, text_preview=text)
        await query.answer("Правки сброшены")
        await show_review_detail(query, rid, user_id)
        return

    if data.startswith("review:recount:"):
        # Both Бухгалтер and Owner can recount (they may see updates from anon-bot)
        rid = int(data.split(":")[2])
        rep = get_report(rid)
        if not rep:
            await query.answer("Не найден", show_alert=True)
            return
        snap = rep["payload"] or {}
        if snap.get("kind") == "auto_shift" and snap.get("bot_token") and snap.get("shift_date"):
            show_money = is_acc_owner(user_id)
            new_text, new_payload, _gid = render_auto_shift_report(
                snap["bot_token"], snap["shift_date"], show_money=show_money
            )
            # Compare counts to give Owner/bookkeeper a quick "what changed" hint.
            old_approved_ids = {str(r.get("receipt_id")) for r in snap.get("approved", [])}
            old_pending_ids  = {str(r.get("receipt_id")) for r in snap.get("pending", [])}
            new_approved_ids = {str(r.get("receipt_id")) for r in new_payload.get("approved", [])}
            new_pending_ids  = {str(r.get("receipt_id")) for r in new_payload.get("pending", [])}
            diff_summary = []
            newly_approved = (new_approved_ids - old_approved_ids) - old_pending_ids
            if newly_approved:
                diff_summary.append(f"+{len(newly_approved)} новых принятых")
            moved_pending_to_approved = new_approved_ids & old_pending_ids
            if moved_pending_to_approved:
                diff_summary.append(f"{len(moved_pending_to_approved)} стали принятыми")
            moved_approved_to_pending = new_pending_ids & old_approved_ids
            if moved_approved_to_pending:
                diff_summary.append(f"{len(moved_approved_to_pending)} вернулись в неразобранные")
            disappeared = (old_approved_ids | old_pending_ids) - (new_approved_ids | new_pending_ids)
            if disappeared:
                diff_summary.append(f"-{len(disappeared)} удалены")

            _carry_manual_edits(snap, new_payload)
            new_text, _p, _g = _rerender_auto_shift_from_snapshot(new_payload, show_money=show_money)
            update_report_payload(rid, new_payload, text_preview=new_text)
            alert = "Подтянул свежие чеки. " + (
                ", ".join(diff_summary) if diff_summary else "Без изменений."
            )
            await query.answer(alert, show_alert=True)
            await show_review_detail(query, rid, user_id)
        else:
            await query.answer("Этот тип отчёта нельзя пересчитать автоматически", show_alert=True)
        return

    if data.startswith("review:cancel:"):
        rid = int(data.split(":")[2])
        await query.answer()
        rows = [
            [InlineKeyboardButton("🗑 Да, отбросить", callback_data=f"review:cancel_yes:{rid}")],
            [InlineKeyboardButton("« Назад", callback_data=f"review:view:{rid}")],
        ]
        await query.edit_message_text(
            f"Отбросить отчёт #{rid}?\n"
            f"Он исчезнет из «Новые отчёты», но останется в Истории.",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("review:cancel_yes:"):
        rid = int(data.split(":")[2])
        update_report_status(rid, "cancelled")
        await query.answer("Отброшено")
        await show_pending_reviews(query, user_id)
        return

    if data == "menu:target_chats":
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        await query.answer()
        await show_target_chats(query)
        return

    if data.startswith("chat:bind:"):
        # data format: chat:bind:<chat_id>
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        chat_id = int(data.split(":")[2])
        await query.answer()
        await show_chat_bind_groups(query, chat_id)
        return

    if data.startswith("chat:bind_to:"):
        # data format: chat:bind_to:<chat_id>:<group_id>
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        _, _, chat_id_s, gid_s = data.split(":", 3)
        chat_id = int(chat_id_s)
        gid = int(gid_s)
        title = ""
        for pc in list_pending_chats():
            if pc["chat_id"] == chat_id:
                title = pc["title"]
                break
        if not title:
            tc = next((c for c in list_target_chats() if c["chat_id"] == chat_id), None)
            if tc:
                title = tc["chat_title"]
        bind_target_chat(gid, chat_id, title, user_id)
        await query.answer("Привязано")
        await show_target_chats(query)
        return

    if data.startswith("chat:unbind:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        gid = int(data.split(":")[2])
        unbind_target_chat(gid)
        await query.answer("Отвязано")
        await show_target_chats(query)
        return

    if data == "menu:history":
        await query.answer()
        await show_history(query)
        return

    if data.startswith("history:view:"):
        rid = int(data.split(":")[2])
        await query.answer()
        await show_report(query, rid, user_id)
        return

    if data.startswith("history:bundle:"):
        _, _, gid_s, date = data.split(":", 3)
        gid = int(gid_s)
        await query.answer()
        await show_history_bundle(query, gid, date, user_id)
        return

    if data.startswith("history:restore_bundle:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        _, _, gid_s, date = data.split(":", 3)
        gid = int(gid_s)
        conn = db_conn()
        rows = conn.execute(
            "SELECT id, status FROM acc_reports "
            "WHERE kind='auto_shift' AND group_id=? AND report_date=? "
            "AND status IN ('sent','cancelled','expired')",
            (gid, date)
        ).fetchall()
        conn.close()
        if not rows:
            await query.answer("Нет отчётов для восстановления", show_alert=True)
            return
        for rid, _status in rows:
            rep = get_report(rid)
            if not rep:
                continue
            snap = rep["payload"] or {}
            snap.pop("last_synced_at", None)
            update_report_payload(rid, snap)
            update_report_status(rid, "pending_owner")
        await query.answer(f"Восстановлено: {len(rows)}")
        await show_pending_reviews(query, user_id)
        return

    if data.startswith("history:restore:"):
        if not is_acc_owner(user_id):
            await query.answer("Только Owner", show_alert=True)
            return
        rid = int(data.split(":")[2])
        rep = get_report(rid)
        if not rep:
            await query.answer("Не найден", show_alert=True)
            return
        if (rep.get("payload") or {}).get("kind") != "auto_shift":
            await query.answer("Этот тип отчёта нельзя восстановить", show_alert=True)
            return
        if rep.get("status") not in ("sent", "cancelled", "expired"):
            await query.answer("Уже в очереди", show_alert=True)
            return
        # Reset last_synced_at so the next open re-fetches fresh receipts; that
        # way if Owner restored to apply a new rate, recount picks it up.
        snap = rep["payload"] or {}
        snap.pop("last_synced_at", None)
        update_report_payload(rid, snap)
        update_report_status(rid, "pending_owner")
        await query.answer("Восстановлено в очередь Owner")
        await show_review_detail(query, rid, user_id)
        return

    if data == "menu:roles":
        if not is_acc_owner(user_id):
            await query.answer(access_text(), show_alert=True)
            return
        await query.answer()
        await show_roles(query)
        return

    if data.startswith("roles:add:"):
        kind = data.split(":")[2]   # 'owner' or 'admin'
        if not is_acc_owner(user_id):
            await query.answer(access_text(), show_alert=True)
            return
        set_state(user_id, {"mode": "add_role", "kind": kind})
        await query.answer()
        role_label = "Owner" if kind == "owner" else "Бухгалтер"
        await query.edit_message_text(
            f"Отправьте Telegram ID юзера для роли {role_label}.",
            reply_markup=cancel_kb()
        )
        return

    if data.startswith("roles:remove:"):
        _, _, kind, uid_s = data.split(":", 3)
        if not is_acc_owner(user_id):
            await query.answer(access_text(), show_alert=True)
            return
        target_uid = int(uid_s)
        conn = db_conn()
        if kind == "owner":
            cnt = conn.execute("SELECT COUNT(*) FROM acc_owners").fetchone()[0]
            if cnt <= 1:
                conn.close()
                await query.answer("Нельзя удалить последнего Owner", show_alert=True)
                return
            conn.execute("DELETE FROM acc_owners WHERE user_id=?", (target_uid,))
        else:
            conn.execute("DELETE FROM acc_admins WHERE user_id=?", (target_uid,))
        conn.commit()
        conn.close()
        await query.answer("Удалено")
        await show_roles(query)
        return

    # Unknown
    await query.answer("?", show_alert=False)


# ---------- View helpers (called from router) ----------

async def show_groups_list(query):
    groups = list_groups()
    rows = [[InlineKeyboardButton("➕ Новая группа", callback_data="group:new")]]
    for g in groups:
        geos = get_group_geos(g["id"])
        flags = "".join(GEO_DISPLAY.get(geo, ("", ""))[1] for geo in geos) or "—"
        mark = "" if g["enabled"] else "  ⏸"
        rows.append([InlineKeyboardButton(
            f"{flags} {g['name']}{mark}", callback_data=f"group:view:{g['id']}"
        )])
    rows.append([InlineKeyboardButton("« Меню", callback_data="menu:main")])
    await query.edit_message_text(
        "🛠 Группы:" if groups else "🛠 Группы:\n(пусто)",
        reply_markup=InlineKeyboardMarkup(rows))


async def show_group_view(query, group_id):
    g = get_group(group_id)
    if not g:
        await query.edit_message_text("Группа не найдена", reply_markup=main_menu(query.from_user.id))
        return
    bots = get_group_bots(group_id)
    bot_names = []
    if bots:
        all_b = {b["token"]: b for b in list_secret_bots()}
        for tok in bots:
            info = all_b.get(tok)
            bot_names.append(f"@{info['username']}" if info else "(удалённый)")
    pct = g["commission_pct"]
    pct_str = f"{fmt_amount(pct)}%" if pct is not None else f"дефолт ({fmt_amount(get_default_commission())}%)"
    geos = get_group_geos(group_id)
    if geos:
        geo_str = ", ".join(
            f"{GEO_DISPLAY.get(geo, (geo, ''))[1]} {GEO_DISPLAY.get(geo, (geo, ''))[0]}".strip()
            for geo in geos
        )
    else:
        geo_str = "— не задано —"
    status = "🟢 ВКЛ" if g["enabled"] else "🔴 ВЫКЛ"
    body = (
        f"📁 {g['name']}\n\n"
        f"Гео: {geo_str}\n"
        f"Комиссия: {pct_str}\n"
        f"Формат: {g['report_mode']}\n"
        f"Статус: {status}\n"
        f"Боты: {', '.join(bot_names) if bot_names else '— не привязано —'}"
    )
    rows = [
        [InlineKeyboardButton("✏️ Имя", callback_data=f"group:rename:{group_id}"),
         InlineKeyboardButton("🌍 Гео", callback_data=f"group:setgeo:{group_id}")],
        [InlineKeyboardButton("📐 Формат", callback_data=f"group:setmode:{group_id}"),
         InlineKeyboardButton("% комиссия", callback_data=f"group:setpct:{group_id}")],
        [InlineKeyboardButton("🤖 Боты", callback_data=f"group:bots:{group_id}")],
        [InlineKeyboardButton("🔁 Вкл/выкл", callback_data=f"group:toggle:{group_id}"),
         InlineKeyboardButton("🗑 Удалить", callback_data=f"group:delete:{group_id}")],
        [InlineKeyboardButton("« К списку", callback_data="menu:groups")],
    ]
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup(rows))


async def show_group_bots_editor(query, group_id):
    all_bots = list_secret_bots()
    attached = set(get_group_bots(group_id))
    # Map of bot_token -> owning group_id (and name) for ALL bots,
    # so we can show "🔒 in <other group>" markers.
    conn = db_conn()
    rows_owned = conn.execute(
        "SELECT gb.bot_token, g.id, g.name FROM acc_group_bots gb "
        "JOIN acc_groups g ON gb.group_id = g.id"
    ).fetchall()
    conn.close()
    owners = {r[0]: (r[1], r[2]) for r in rows_owned}

    rows = []
    for i, b in enumerate(all_bots):
        tok = b["token"]
        geo_flag = GEO_DISPLAY.get(b["geo"], ("", ""))[1] if b["geo"] else ""
        if tok in attached:
            label = f"✅ {geo_flag} @{b['username']}".strip()
        elif tok in owners:
            other_gid, other_name = owners[tok]
            label = f"🔒 {geo_flag} @{b['username']} → {other_name}".strip()
        else:
            label = f"▫️ {geo_flag} @{b['username']}".strip()
        rows.append([InlineKeyboardButton(label, callback_data=f"gbot:toggle:{group_id}:{i}")])
    rows.append([InlineKeyboardButton("« Назад", callback_data=f"group:view:{group_id}")])
    await query.edit_message_text(
        "🤖 Привязка ботов\n"
        "✅ — в этой группе\n"
        "🔒 — в другой группе (один бот = одна группа)\n"
        "▫️ — свободен",
        reply_markup=InlineKeyboardMarkup(rows))


def _rates_editor_kb():
    rates = get_rates()
    rows = []
    for geo in GEO_ORDER:
        name, flag = GEO_DISPLAY[geo]
        cur = rates.get(geo)
        cur_str = fmt_rate(cur["rate"]) if cur else "—"
        row = [InlineKeyboardButton(
            f"{flag} {name}: {cur_str}", callback_data=f"rate:set:{geo}"
        )]
        if cur:
            row.append(InlineKeyboardButton("🗑", callback_data=f"rate:delete:{geo}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("« Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


async def show_rates_editor(query, user_id):
    await query.edit_message_text(
        "💱 Курсы (нажмите на гео чтобы изменить, 🗑 — удалить):",
        reply_markup=_rates_editor_kb())


async def build_recon_preview(query, user_id):
    """Compute сверки for each target bot, show preview and confirmation."""
    date = today_str()
    targets = {}  # bot_token -> (text, payout, meta)
    # All bots attached to enabled groups with data today
    conn = db_conn()
    rows = conn.execute(
        "SELECT DISTINCT acc_group_bots.bot_token FROM acc_group_bots "
        "JOIN acc_groups ON acc_group_bots.group_id = acc_groups.id "
        "WHERE acc_groups.enabled = 1"
    ).fetchall()
    conn.close()
    bot_tokens = [r[0] for r in rows]
    if not bot_tokens:
        await query.edit_message_text(
            "Нет групп с привязанными ботами.",
            reply_markup=main_menu(user_id))
        return
    for tok in bot_tokens:
        text, payout, meta = render_reconciliation_for_bot(tok, date)
        if text:
            targets[tok] = (text, payout, meta)

    if not targets:
        await query.edit_message_text(
            "Нет групп с введёнными данными за сегодня.",
            reply_markup=main_menu(user_id))
        return

    # Save preview state
    set_state(user_id, {
        "mode": "recon_preview",
        "date": date,
        "targets": targets,
    })

    # Build preview: show each chat target's message
    secret_bots = {b["token"]: b for b in list_secret_bots()}
    preview_chunks = []
    for tok, (text, payout, _meta) in targets.items():
        info = secret_bots.get(tok)
        botname = f"@{info['username']}" if info else tok[:12]
        preview_chunks.append(f"━━━ {botname} ━━━\n{text}")
    preview = "\n\n".join(preview_chunks)

    rows = [
        [InlineKeyboardButton("✅ Отправить", callback_data="recon:confirm")],
        [InlineKeyboardButton("« Меню", callback_data="menu:main")],
    ]
    # Telegram message max 4096 chars
    if len(preview) > 3900:
        preview = preview[:3900] + "\n\n…(обрезано в превью)"
    await query.edit_message_text(
        f"📊 Предпросмотр сверки за {date}\n\n{preview}",
        reply_markup=InlineKeyboardMarkup(rows))


async def send_recon(query, user_id):
    state = get_state(user_id)
    if not state or state.get("mode") != "recon_preview":
        await query.edit_message_text("Превью устарело. /start.", reply_markup=main_menu(user_id))
        return
    targets = state["targets"]
    date = state["date"]
    sent_to = []
    summary_lines = []
    secret_bots = {b["token"]: b for b in list_secret_bots()}
    for tok, (text, _payout, _meta) in targets.items():
        info = secret_bots.get(tok)
        botname = f"@{info['username']}" if info else tok[:12]
        sent, failed = await broadcast_text(tok, text)
        sent_to.append(tok)
        summary_lines.append(f"  • {botname}: {sent} ок / {failed} ошибок")

    # Save snapshot
    payload = {
        "date": date,
        "targets": {tok: {"text": text} for tok, (text, _p, _m) in targets.items()},
    }
    preview_text = "\n\n".join(text for _t, (text, _p, _m) in targets.items())
    save_report(date, "reconciliation", payload, preview_text, sent_to)

    # Clear daily data for this date so the next entry starts from scratch.
    # We only wipe groups that actually went into this batch — the ones whose
    # tokens are in `sent_to`. Other groups (e.g. disabled, or with no bots)
    # remain untouched.
    cleared_groups = set()
    conn = db_conn()
    for tok in sent_to:
        rows = conn.execute(
            "SELECT group_id FROM acc_group_bots WHERE bot_token=?", (tok,)
        ).fetchall()
        for (gid,) in rows:
            cleared_groups.add(gid)
    for gid in cleared_groups:
        conn.execute("DELETE FROM acc_daily_data WHERE group_id=? AND date=?", (gid, date))
    conn.commit()
    conn.close()

    set_state(user_id, None)
    cleared_str = f"\n\n🧹 Очищены данные за {date} ({len(cleared_groups)} групп)" if cleared_groups else ""
    await query.edit_message_text(
        "✅ Сверка отправлена.\n\n" + "\n".join(summary_lines) + cleared_str,
        reply_markup=main_menu(user_id))


async def build_rates_preview(query, user_id):
    rates = get_rates()
    if not rates:
        await query.edit_message_text(
            "Курсы не заданы.", reply_markup=main_menu(user_id))
        return
    text = render_rates_message()

    # Targets: target TG-chats of every ENABLED group that has a chat bound.
    conn = db_conn()
    rows = conn.execute(
        "SELECT g.id, g.name, tc.chat_id, tc.chat_title "
        "FROM acc_groups g JOIN acc_target_chats tc ON g.id = tc.group_id "
        "WHERE g.enabled = 1"
    ).fetchall()
    conn.close()
    targets = [{"group_id": r[0], "group_name": r[1], "chat_id": r[2], "chat_title": r[3]} for r in rows]

    if not targets:
        await query.edit_message_text(
            "Нет групп с привязанным TG-чатом.\n"
            "Привяжите чаты через «💬 Привязка чатов» сначала.",
            reply_markup=main_menu(user_id))
        return

    set_state(user_id, {"mode": "rates_preview", "text": text, "targets": targets})
    targets_str = "\n".join(f"  • {t['group_name']} → «{t['chat_title']}»" for t in targets)
    rows = [
        [InlineKeyboardButton("✅ Отправить", callback_data="rates:confirm")],
        [InlineKeyboardButton("« Меню", callback_data="menu:main")],
    ]
    await query.edit_message_text(
        f"📤 Предпросмотр курсов\n\n{text}\n\nЦели:\n{targets_str}",
        reply_markup=InlineKeyboardMarkup(rows))


async def send_rates(query, user_id):
    state = get_state(user_id)
    if not state or state.get("mode") != "rates_preview":
        await query.edit_message_text("Превью устарело. /start.", reply_markup=main_menu(user_id))
        return
    text = state["text"]
    targets = state["targets"]
    bot_obj = query.get_bot()
    summary = []
    delivered = []
    for t in targets:
        try:
            await bot_obj.send_message(chat_id=t["chat_id"], text=text)
            delivered.append(t["chat_id"])
            summary.append(f"  • {t['group_name']} → «{t['chat_title']}»: ✓")
        except Exception as e:
            summary.append(f"  • {t['group_name']} → «{t['chat_title']}»: ❌ {e}")

    save_report(today_str(), "rates", {"text": text, "targets": targets},
                text, delivered)
    set_state(user_id, None)
    await query.edit_message_text(
        "✅ Курсы отправлены.\n\n" + "\n".join(summary),
        reply_markup=main_menu(user_id))


def _format_report_date(date_str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return date_str or "?"


async def show_history(query):
    bundles = list_recent_history_bundles()
    rows = []
    for b in bundles:
        if b["kind"] == "group_bundle":
            g = get_group(b["group_id"])
            gname = g["name"] if g else f"группа {b['group_id']}"
            label = f"Отчёт «{gname}» — {_format_report_date(b['date'])}"
            cb = f"history:bundle:{b['group_id']}:{b['date']}"
        else:
            sub = b["sub_kind"]
            date_str = _format_report_date(b["date"])
            if sub == "reconciliation":
                label = f"Сверка — {date_str}"
            elif sub == "rates":
                label = f"Курсы — {date_str}"
            else:
                label = f"{sub} — {date_str}"
            cb = f"history:view:{b['id']}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    rows.append([InlineKeyboardButton("« Меню", callback_data="menu:main")])
    await query.edit_message_text(
        f"📜 История (последние {HISTORY_RETENTION_DAYS} дней):" if bundles else "📜 История пуста.",
        reply_markup=InlineKeyboardMarkup(rows))


async def show_history_bundle(query, group_id, date, user_id):
    """Aggregated view of all auto_shift reports for one (group, shift_date).
    Shows the same combined rendering used in the batch send, with bound
    chats listed at the top and a per-bundle restore button."""
    conn = db_conn()
    rows = conn.execute(
        "SELECT id, status, payload, created_at FROM acc_reports "
        "WHERE kind='auto_shift' AND group_id=? AND report_date=? "
        "ORDER BY created_at ASC",
        (group_id, date)
    ).fetchall()
    conn.close()
    if not rows:
        await query.edit_message_text("Бандл пуст.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("« История", callback_data="menu:history")]
        ]))
        return
    g = get_group(group_id)
    gname = g["name"] if g else f"группа {group_id}"
    # Bound chats: current binding (we don't snapshot it at send time)
    target = get_target_chat(group_id)
    chats_line = f"Чат: «{target['chat_title']}»" if target else "Чат: не привязан"

    parts = []
    statuses = set()
    grand_payout = 0.0
    payouts_list = []
    missing_rate = []
    report_ids = []
    shift_date_for_total = None
    for rid, status, payload_json, _ts in rows:
        report_ids.append(rid)
        statuses.add(status)
        try:
            snap = json.loads(payload_json) if payload_json else {}
        except Exception:
            snap = {}
        if _effective_total_approved(snap) <= 0:
            continue
        if shift_date_for_total is None:
            shift_date_for_total = snap.get("shift_date")
        text, _p, _g = _rerender_auto_shift_from_snapshot(snap, show_money=True)
        parts.append(text)
        payout, _total, _rate, _pct = _snapshot_payout_usd(snap)
        if payout is not None:
            payouts_list.append(payout)
            grand_payout += payout
        elif _total > 0:
            missing_rate.append(snap.get("bot_username") or "?")
    combined = ("\n\n" + "━" * 18 + "\n\n").join(parts) if parts else "(нет ботов с депозитами)"
    footer = ["", "━" * 18,
              _format_payout_summary(shift_date_for_total, payouts_list, grand_payout)]
    if missing_rate:
        footer.append(f"⚠️ Без курса (не учтены): {', '.join('@' + u for u in missing_rate)}")
    combined = combined + "\n" + "\n".join(footer)

    status_label = (
        "sent" if statuses == {"sent"} else
        "expired" if statuses == {"expired"} else
        "cancelled" if statuses == {"cancelled"} else
        "mixed"
    )
    head = (
        f"📊 Отчёт «{gname}» — {_format_report_date(date)}\n"
        f"{chats_line}\n"
        f"Ботов в бандле: {len(rows)}  ·  Статус: {status_label}\n"
    )

    body = head + "\n" + combined
    if len(body) > 3800:
        body = body[:3800] + "\n…(обрезано)"

    rows_kb = []
    # Restore: only if at least one of the reports is in a finalised state
    # AND the Owner is asking. Restores ALL finalised reports of the bundle.
    restorable = [
        rid for (rid, st, *_rest) in rows if st in ("sent", "cancelled", "expired")
    ]
    if restorable and is_acc_owner(user_id):
        rows_kb.append([InlineKeyboardButton(
            f"↩️ Восстановить все в «На проверке» ({len(restorable)})",
            callback_data=f"history:restore_bundle:{group_id}:{date}"
        )])
    rows_kb.append([InlineKeyboardButton("« История", callback_data="menu:history")])
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup(rows_kb))


async def show_report(query, report_id, user_id):
    r = get_report(report_id)
    if not r:
        await query.edit_message_text("Запись не найдена.")
        return
    ts = datetime.fromtimestamp(r["created_at"], MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    kind = r["kind"]
    snap = r.get("payload") or {}
    bot_username = snap.get("bot_username") if isinstance(snap, dict) else None
    title_bits = []
    if kind == "auto_shift":
        title_bits.append(f"📊 Авто-сверка @{bot_username or '?'}")
    elif kind == "reconciliation":
        title_bits.append("📋 Сверка")
    elif kind == "rates":
        title_bits.append("💱 Курсы")
    else:
        title_bits.append(kind)
    title_bits.append(f"за {_format_report_date(r['date'])}")
    title_bits.append(f"({r.get('status') or '?'})")
    title = " ".join(title_bits)
    text = r["text_preview"] or "(пусто)"
    if len(text) > 3500:
        text = text[:3500] + "\n…(обрезано)"
    rows = []
    if kind == "auto_shift" and r.get("status") in ("sent", "cancelled", "expired") \
            and is_acc_owner(user_id):
        rows.append([InlineKeyboardButton(
            "↩️ Восстановить в «На проверке»",
            callback_data=f"history:restore:{report_id}"
        )])
    rows.append([InlineKeyboardButton("« История", callback_data="menu:history")])
    await query.edit_message_text(
        f"{title} от {ts}\n\n{text}",
        reply_markup=InlineKeyboardMarkup(rows))


async def build_submit_review(query, user_id):
    """Smart submit button:
      - If there are pending_bookkeeper auto_shift reports, batch-forward them.
      - Else fall back to the manual data flow (data entered via «📥 Ввести данные»).
    """
    auto_pending = list_pending_reports_for_bookkeeper()
    if auto_pending:
        # Build a short summary listing each report
        lines = [f"📤 Готовы к передаче Owner-у ({len(auto_pending)}):", ""]
        for r in auto_pending:
            ts = datetime.fromtimestamp(r["created_at"], MOSCOW_TZ).strftime("%d.%m %H:%M")
            lines.append(f"  • #{r['id']} — смена {r['date']} ({ts})")
        text = "\n".join(lines)
        set_state(user_id, {
            "mode": "submit_review_preview_auto",
            "report_ids": [r["id"] for r in auto_pending],
        })
        rows = [
            [InlineKeyboardButton("✅ Передать на проверку", callback_data="submit_review:confirm")],
            [InlineKeyboardButton("« Меню", callback_data="menu:main")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        return

    # ----- legacy manual flow -----
    date = today_str()
    groups = list_groups(enabled_only=True)
    snippets = []
    snapshot = {"date": date, "groups": []}
    for g in groups:
        gd = get_daily_data_all_geos(g["id"], date)
        if not gd:
            continue
        block, _payout, _pct = render_group_block(
            g, gd, {geo: get_rate(geo) for geo in gd.keys()},
            show_money=False
        )
        snippets.append(block)
        snapshot["groups"].append({
            "group_id": g["id"],
            "name": g["name"],
            "geo_data": gd,
        })

    if not snippets:
        await query.edit_message_text(
            "Нет ни новых отчётов от смен, ни введённых вручную данных.",
            reply_markup=main_menu(user_id))
        return

    text = f"Данные за {date}\n\n" + "\n\n".join(snippets)
    set_state(user_id, {
        "mode": "submit_review_preview_manual",
        "snapshot": snapshot,
        "text": text,
        "date": date,
    })
    rows = [
        [InlineKeyboardButton("✅ Отправить Owner-у", callback_data="submit_review:confirm")],
        [InlineKeyboardButton("« Меню", callback_data="menu:main")],
    ]
    body = text
    if len(body) > 3800:
        body = body[:3800] + "\n…(обрезано)"
    await query.edit_message_text(
        f"📤 Превью для отправки на проверку:\n\n{body}",
        reply_markup=InlineKeyboardMarkup(rows))


async def submit_for_review(query, user_id):
    state = get_state(user_id)
    if not state:
        await query.edit_message_text("Превью устарело. /start", reply_markup=main_menu(user_id))
        return
    bot_obj = query.get_bot()
    mode = state.get("mode")

    if mode == "submit_review_preview_auto":
        ids = state.get("report_ids") or []
        forwarded = []
        for rid in ids:
            rep = get_report(rid)
            if rep and rep.get("status") == "pending_bookkeeper":
                update_report_status(rid, "pending_owner")
                forwarded.append(rid)
        set_state(user_id, None)
        # Notify Owners once with summary
        if forwarded:
            for owner_uid in get_owners():
                try:
                    await bot_obj.send_message(
                        chat_id=owner_uid,
                        text=(f"📥 Бухгалтер передал на проверку {len(forwarded)} отчёт(ов): "
                              f"{', '.join(f'#{r}' for r in forwarded)}.\n"
                              f"Откройте «📥 На проверке».")
                    )
                except Exception:
                    pass
        await query.edit_message_text(
            f"✅ Передано Owner-у: {len(forwarded)} отчётов.",
            reply_markup=main_menu(user_id))
        return

    if mode == "submit_review_preview_manual":
        snapshot = state["snapshot"]
        text = state["text"]
        date = state["date"]
        rid = save_report(
            report_date=date,
            kind="reconciliation_review",
            payload=snapshot,
            text_preview=text,
            sent_to_tokens=[],
            status="pending_owner",
        )
        set_state(user_id, None)
        for owner_uid in get_owners():
            try:
                await bot_obj.send_message(
                    chat_id=owner_uid,
                    text=f"📥 Бухгалтер прислал на проверку отчёт за {date}. См. меню «📥 На проверке»."
                )
            except Exception:
                pass
        await query.edit_message_text(
            f"✅ Отправлено Owner-у на проверку.\nID отчёта: {rid}",
            reply_markup=main_menu(user_id))
        return

    await query.edit_message_text("Превью устарело. /start", reply_markup=main_menu(user_id))


async def show_pending_reviews(query, user_id):
    is_owner = is_acc_owner(user_id)
    if is_owner:
        # Owner sees only reports that bookkeeper has forwarded
        pending = list_pending_reports_for_owner()
    else:
        # Бухгалтер sees only what's waiting on him
        pending = list_pending_reports_for_bookkeeper()
    rows = []
    if is_owner and pending:
        rows.append([InlineKeyboardButton(
            f"📤 Отправить ВСЕ ({len(pending)})",
            callback_data="review:approve_all"
        )])
    for r in pending:
        bot_username = r.get("bot_username")
        date = r.get("date") or "?"
        if bot_username:
            label = f"@{bot_username} — {_format_report_date(date)}"
        else:
            label = _format_report_date(date)
        rows.append([InlineKeyboardButton(
            label, callback_data=f"review:view:{r['id']}"
        )])
    rows.append([InlineKeyboardButton("« Меню", callback_data="menu:main")])
    header = "📥 На проверке:" if pending else "📥 Нет отчётов на проверке."
    await query.edit_message_text(header, reply_markup=InlineKeyboardMarkup(rows))


async def _send_report_card(message, rid, rep, user_id):
    """Send the report card as a NEW message after a text-input edit, so the
    user lands back on the editable card instead of the main menu."""
    snap = rep.get("payload") or {}
    kind = snap.get("kind") or rep.get("kind")
    status = rep.get("status")
    is_owner = is_acc_owner(user_id)
    if kind != "auto_shift":
        await message.reply_text("Готово.", reply_markup=main_menu(user_id))
        return
    body, _p, _g = _rerender_auto_shift_from_snapshot(snap, show_money=is_owner)
    rows = [[InlineKeyboardButton("🔄 Пересчитать", callback_data=f"review:recount:{rid}")]]
    if status == "pending_bookkeeper":
        rows.append([InlineKeyboardButton("✏️ Править суммы / исключить", callback_data=f"review:edit_list:{rid}")])
        rows.append([InlineKeyboardButton("📤 Передать на проверку", callback_data=f"review:bookkeeper_done:{rid}")])
    elif status == "pending_owner" and is_owner:
        rows.append([InlineKeyboardButton("✏️ Править суммы / исключить", callback_data=f"review:edit_list:{rid}")])
        if snap.get("overrides") or snap.get("excluded") or snap.get("manual_added"):
            rows.append([InlineKeyboardButton("🗑 Сбросить правки", callback_data=f"review:reset_overrides:{rid}")])
    rows.append([InlineKeyboardButton("🗑 Отбросить", callback_data=f"review:cancel:{rid}")])
    rows.append([InlineKeyboardButton("« К списку", callback_data="menu:pending_reviews")])
    status_label = {
        "pending_bookkeeper": "у бухгалтера",
        "pending_owner": "у Owner",
        "sent": "отправлено",
        "cancelled": "отброшено",
        "expired": "просрочено",
    }.get(status, status or "?")
    if len(body) > 3800:
        body = body[:3800] + "\n…(обрезано)"
    await message.reply_text(
        f"📋 Авто-сверка #{rid} ({status_label})\n\n{body}",
        reply_markup=InlineKeyboardMarkup(rows))


async def show_review_detail(query, report_id, user_id):
    rep = get_report(report_id)
    if not rep:
        await query.edit_message_text("Отчёт не найден.", reply_markup=main_menu(user_id))
        return
    snapshot = rep["payload"] or {}
    date = snapshot.get("date") or rep["date"]
    kind = snapshot.get("kind") or rep["kind"]

    if kind == "auto_shift":
        is_owner = is_acc_owner(user_id)
        status = rep.get("status")
        # Auto-resync from live anon-bot state when a still-pending auto_shift
        # report is opened. Throttled to once per 30s so navigating back-and-
        # forth between cards doesn't trigger heavy DB reads each time.
        AUTO_RESYNC_TTL = 30
        last_sync = float(snapshot.get("last_synced_at") or 0)
        if status in ("pending_bookkeeper", "pending_owner") \
                and snapshot.get("bot_token") and snapshot.get("shift_date") \
                and (time.time() - last_sync) > AUTO_RESYNC_TTL:
            try:
                _txt, fresh_payload, _g = render_auto_shift_report(
                    snapshot["bot_token"], snapshot["shift_date"], show_money=is_owner
                )
                _carry_manual_edits(snapshot, fresh_payload)
                fresh_payload["last_synced_at"] = time.time()
                fresh_text, _p, _g = _rerender_auto_shift_from_snapshot(fresh_payload, show_money=is_owner)
                update_report_payload(report_id, fresh_payload, text_preview=fresh_text)
                snapshot = fresh_payload
            except Exception:
                logger.exception("Auto-resync of auto_shift report failed")
        body, _payload, _gid = _rerender_auto_shift_from_snapshot(snapshot, show_money=is_owner)
        rows = [[InlineKeyboardButton("🔄 Пересчитать", callback_data=f"review:recount:{report_id}")]]
        if status == "pending_bookkeeper":
            rows.append([InlineKeyboardButton("✏️ Править суммы / исключить", callback_data=f"review:edit_list:{report_id}")])
            rows.append([InlineKeyboardButton("📤 Передать на проверку", callback_data=f"review:bookkeeper_done:{report_id}")])
        elif status == "pending_owner" and is_owner:
            rows.append([InlineKeyboardButton("✏️ Править суммы / исключить", callback_data=f"review:edit_list:{report_id}")])
            if snapshot.get("overrides") or snapshot.get("excluded") or snapshot.get("manual_added"):
                rows.append([InlineKeyboardButton("🗑 Сбросить правки", callback_data=f"review:reset_overrides:{report_id}")])
        rows.append([InlineKeyboardButton("🗑 Отбросить", callback_data=f"review:cancel:{report_id}")])
        rows.append([InlineKeyboardButton("« К списку", callback_data="menu:pending_reviews")])
        if len(body) > 3800:
            body = body[:3800] + "\n…(обрезано)"
        status_label = {
            "pending_bookkeeper": "у бухгалтера",
            "pending_owner": "у Owner",
            "sent": "отправлено",
            "cancelled": "отменено",
            "expired": "просрочено",
        }.get(status, status or "?")
        await query.edit_message_text(
            f"📋 Авто-сверка #{report_id} ({status_label})\n\n{body}",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    # ----- manual review report -----
    # Build Owner-side preview (with money) from the snapshot.
    chunks = []
    payouts = []
    distinct_pct = set()
    for entry in snapshot.get("groups", []):
        gid = entry["group_id"]
        g = get_group(gid)
        if not g:
            continue
        # Convert JSON keys to str then back when reading; geo names are plain.
        geo_data = entry.get("geo_data") or {}
        rates = {geo: get_rate(geo) for geo in geo_data.keys()}
        block, payout, pct = render_group_block(g, geo_data, rates, show_money=True)
        chunks.append(block)
        if payout is not None:
            payouts.append(payout)
        distinct_pct.add(pct)
    if not chunks:
        body = "(пусто)"
    else:
        try:
            d = datetime.strptime(date, "%Y-%m-%d")
            header_date = d.strftime("%d.%m.%Y")
        except Exception:
            header_date = date
        if len(distinct_pct) == 1:
            pct = next(iter(distinct_pct))
            header = f"Расчёт за {header_date} (вычет {fmt_amount(pct)}%)"
        else:
            header = f"Расчёт за {header_date}"
        body = header + "\n\n" + "\n\n".join(chunks)
        if len(payouts) > 1:
            total = sum(payouts)
            parts_str = " + ".join(fmt_usd(p) for p in payouts)
            body += f"\n\nИтого: {parts_str} = {fmt_usd(total)} USD"

    rows = []
    if is_acc_owner(user_id):
        rows.append([InlineKeyboardButton("✅ Утвердить и отправить", callback_data=f"review:approve_send:{report_id}")])
    rows.append([InlineKeyboardButton("🗑 Отбросить", callback_data=f"review:cancel:{report_id}")])
    rows.append([InlineKeyboardButton("« К списку", callback_data="menu:pending_reviews")])
    if len(body) > 3800:
        body = body[:3800] + "\n…(обрезано)"
    await query.edit_message_text(
        f"📋 Отчёт #{report_id}\n\n{body}",
        reply_markup=InlineKeyboardMarkup(rows))


async def approve_and_send_review(query, report_id, user_id):
    """Owner approves a review report — send final payload to its TG-chat."""
    rep = get_report(report_id)
    if not rep:
        await query.edit_message_text("Отчёт не найден.", reply_markup=main_menu(user_id))
        return
    snapshot = rep["payload"] or {}
    bot_obj = query.get_bot()
    kind = snapshot.get("kind") or rep["kind"]

    # ----- AUTO-SHIFT (per anon-bot, per shift) -----
    if kind == "auto_shift":
        gid = snapshot.get("group_id")
        if not gid:
            await query.edit_message_text(
                "Бот этой сверки не привязан к группе — отправлять некуда.",
                reply_markup=main_menu(user_id))
            return
        target = get_target_chat(gid)
        if not target:
            await query.edit_message_text(
                f"Группа «{snapshot.get('group_name')}» не привязана к TG-чату.\n"
                f"Привяжите чат через «💬 Привязка чатов».",
                reply_markup=main_menu(user_id))
            return
        # Re-render text from snapshot with money visible (Owner view).
        text, _payload, _ = _rerender_auto_shift_from_snapshot(snapshot, show_money=True)
        try:
            await bot_obj.send_message(chat_id=target["chat_id"], text=text)
            update_report_status(report_id, "sent")
            await query.edit_message_text(
                f"✅ Сверка отправлена в «{target['chat_title']}».",
                reply_markup=main_menu(user_id))
        except Exception as e:
            await query.edit_message_text(
                f"❌ Ошибка отправки: {e}",
                reply_markup=main_menu(user_id))
        return

    # ----- LEGACY MANUAL recon (multi-group snapshot) -----
    date = snapshot.get("date") or rep["date"]
    summary = []
    delivered_chats = []
    for entry in snapshot.get("groups", []):
        gid = entry["group_id"]
        g = get_group(gid)
        if not g:
            summary.append(f"  • {entry.get('name')}: группа удалена, пропуск")
            continue
        geo_data = entry.get("geo_data") or {}
        rates = {geo: get_rate(geo) for geo in geo_data.keys()}
        block, _payout, pct = render_group_block(g, geo_data, rates, show_money=True)
        try:
            d = datetime.strptime(date, "%Y-%m-%d")
            header_date = d.strftime("%d.%m.%Y")
        except Exception:
            header_date = date
        header = f"Расчёт за {header_date} (вычет {fmt_amount(pct)}%)"
        text = header + "\n\n" + block

        target = get_target_chat(gid)
        if not target:
            summary.append(f"  • {g['name']}: чат не привязан — пропуск")
            continue
        try:
            await bot_obj.send_message(chat_id=target["chat_id"], text=text)
            delivered_chats.append(target["chat_id"])
            summary.append(f"  • {g['name']} → «{target['chat_title']}»: ✓")
        except Exception as e:
            summary.append(f"  • {g['name']} → «{target['chat_title']}»: ошибка {e}")

    update_report_status(report_id, "sent")
    cleared = 0
    conn = db_conn()
    for entry in snapshot.get("groups", []):
        gid = entry["group_id"]
        target = get_target_chat(gid)
        if target and target["chat_id"] in delivered_chats:
            conn.execute(
                "DELETE FROM acc_daily_data WHERE group_id=? AND date=?",
                (gid, date)
            )
            cleared += 1
    conn.commit()
    conn.close()

    clear_note = f"\n🧹 Очищены данные у {cleared} групп" if cleared else ""
    await query.edit_message_text(
        "✅ Отчёт утверждён и отправлен.\n\n" + "\n".join(summary) + clear_note,
        reply_markup=main_menu(user_id))


async def _show_batch_picker(query, context, group_id):
    """Render the batch-send selection screen: each sibling has a toggle row,
    bottom row has Select-all / Clear / Confirm."""
    siblings = _pending_owner_reports_for_group(group_id)
    if not siblings:
        await query.edit_message_text(
            "Нет готовых отчётов для этой группы.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Меню", callback_data="menu:main")]
            ]))
        return
    excluded = context.user_data.get("batch_excluded", {}).get(group_id, set())
    selected_count = sum(1 for r in siblings if r["id"] not in excluded)
    g = get_group(group_id)
    group_label = g["name"] if g else f"группа {group_id}"
    rows = []
    for r in siblings:
        bot_username = (r["payload"] or {}).get("bot_username") or "?"
        date = r.get("date") or "?"
        mark = "✅" if r["id"] not in excluded else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} #{r['id']} @{bot_username} — {date}",
            callback_data=f"review:batch_toggle:{group_id}:{r['id']}"
        )])
    rows.append([
        InlineKeyboardButton("Все", callback_data=f"review:batch_all:{group_id}"),
        InlineKeyboardButton("Снять все", callback_data=f"review:batch_none:{group_id}"),
    ])
    if selected_count > 0:
        rows.append([InlineKeyboardButton(
            f"✅ Отправить выбранные ({selected_count})",
            callback_data=f"review:approve_group_yes:{group_id}"
        )])
    rows.append([InlineKeyboardButton("« Назад", callback_data="menu:pending_reviews")])
    await query.edit_message_text(
        f"📦 Выберите, какие отчёты отправить одним сообщением для «{group_label}»:\n"
        f"Выбрано: {selected_count}/{len(siblings)}",
        reply_markup=InlineKeyboardMarkup(rows))


async def approve_group_batch(query, group_id, user_id, excluded_ids=None):
    """Combine selected pending_owner auto_shift reports for a group into one TG message."""
    excluded_ids = set(excluded_ids or ())
    all_siblings = _pending_owner_reports_for_group(group_id)
    siblings = [r for r in all_siblings if r["id"] not in excluded_ids]
    if not siblings:
        await query.edit_message_text(
            "Нет выбранных отчётов для отправки.",
            reply_markup=main_menu(user_id))
        return
    target = get_target_chat(group_id)
    g = get_group(group_id)
    group_label = g["name"] if g else f"группа {group_id}"
    if not target:
        await query.edit_message_text(
            f"Группа «{group_label}» не привязана к TG-чату.\n"
            f"Привяжите чат через «💬 Привязка чатов».",
            reply_markup=main_menu(user_id))
        return

    # Render each sibling using its snapshot (Owner view), then concatenate.
    # Skip bots with zero deposits entirely — they clutter the report.
    parts = []
    payouts_for_total = []
    total_payout_usd = 0.0
    missing_rate_bots = []
    shift_date_for_total = None
    for r in siblings:
        snap = r["payload"] or {}
        if _effective_total_approved(snap) <= 0:
            continue
        if shift_date_for_total is None:
            shift_date_for_total = snap.get("shift_date")
        text, _p, _g = _rerender_auto_shift_from_snapshot(snap, show_money=True)
        parts.append(text)
        payout, _total, _rate, _pct = _snapshot_payout_usd(snap)
        if payout is not None:
            payouts_for_total.append(payout)
            total_payout_usd += payout
        elif _total > 0:
            missing_rate_bots.append((snap.get("bot_username") or "?", snap.get("bot_geo") or "?"))

    if not parts:
        await query.edit_message_text(
            "Нет ботов с депозитами в выбранных отчётах.",
            reply_markup=main_menu(user_id))
        return

    combined = ("\n\n" + "━" * 18 + "\n\n").join(parts)
    # Group-level total payout footer in the requested form:
    #   Итого за DD.MM.YYYY: A + B + C = TOTAL USD.
    footer_lines = ["", "━" * 18, _format_payout_summary(shift_date_for_total, payouts_for_total, total_payout_usd)]
    if missing_rate_bots:
        names = ", ".join(f"@{u}" for u, _ in missing_rate_bots)
        footer_lines.append(f"⚠️ Без курса (не учтены): {names}")
    combined = combined + "\n" + "\n".join(footer_lines)
    # Telegram message limit is 4096 chars — split if too long.
    chunks = []
    while combined:
        if len(combined) <= 4000:
            chunks.append(combined)
            break
        # Try to split at a separator boundary
        cut = combined.rfind("\n\n" + "━" * 18, 0, 4000)
        if cut <= 0:
            cut = 4000
        chunks.append(combined[:cut])
        combined = combined[cut:].lstrip()

    bot_obj = query.get_bot()
    delivered = False
    err_text = None
    try:
        for chunk in chunks:
            await bot_obj.send_message(chat_id=target["chat_id"], text=chunk)
        delivered = True
    except Exception as e:
        err_text = str(e)

    if delivered:
        for r in siblings:
            update_report_status(r["id"], "sent")
        await query.edit_message_text(
            f"✅ Отправлено в «{target['chat_title']}»: {len(siblings)} отчётов одним сообщением "
            f"({len(chunks)} part{'' if len(chunks) == 1 else 's'}).",
            reply_markup=main_menu(user_id))
    else:
        await query.edit_message_text(
            f"❌ Ошибка отправки: {err_text}",
            reply_markup=main_menu(user_id))


async def approve_all_pending_batch(query, user_id):
    """Send every pending_owner auto_shift report. Reports are grouped by
    group_id and each group gets one combined message in its bound TG-chat
    (same format as approve_group_batch). At the end we report a grand total
    across all groups so the Owner sees the day's full payout in one place."""
    pending = list_pending_reports_for_owner()
    auto_pending = [r for r in pending if r.get("kind") == "auto_shift" and r.get("group_id") is not None]
    if not auto_pending:
        await query.edit_message_text(
            "Нет авто-отчётов на отправку.",
            reply_markup=main_menu(user_id))
        return

    # Group by group_id
    by_group = {}
    for r in auto_pending:
        by_group.setdefault(r["group_id"], []).append(r["id"])

    bot_obj = query.get_bot()
    summary_lines = []
    grand_total_usd = 0.0
    grand_missing = []
    sent_count = 0
    error_count = 0

    for gid, rids in by_group.items():
        g = get_group(gid)
        group_label = g["name"] if g else f"группа {gid}"
        target = get_target_chat(gid)
        if not target:
            summary_lines.append(f"  • «{group_label}»: ❌ нет привязанного чата — пропуск")
            error_count += len(rids)
            continue

        # Re-fetch fresh payloads (auto-resync may have updated them).
        siblings = []
        for rid in rids:
            rep = get_report(rid)
            if rep and (rep.get("payload") or {}).get("kind") == "auto_shift":
                siblings.append({"id": rid, "payload": rep["payload"] or {}})
        if not siblings:
            continue

        parts = []
        group_payouts = []
        group_payout = 0.0
        group_missing = []
        shift_date_for_total = None
        for r in siblings:
            snap = r["payload"]
            if _effective_total_approved(snap) <= 0:
                continue
            if shift_date_for_total is None:
                shift_date_for_total = snap.get("shift_date")
            text, _p, _g = _rerender_auto_shift_from_snapshot(snap, show_money=True)
            parts.append(text)
            payout, _total, _rate, _pct = _snapshot_payout_usd(snap)
            if payout is not None:
                group_payouts.append(payout)
                group_payout += payout
            elif _total > 0:
                group_missing.append(snap.get("bot_username") or "?")

        if not parts:
            summary_lines.append(f"  • «{group_label}»: пропуск (нет депозитов)")
            continue

        combined = ("\n\n" + "━" * 18 + "\n\n").join(parts)
        footer = ["", "━" * 18,
                  _format_payout_summary(shift_date_for_total, group_payouts, group_payout)]
        if group_missing:
            footer.append(f"⚠️ Без курса (не учтены): {', '.join('@' + u for u in group_missing)}")
        combined = combined + "\n" + "\n".join(footer)

        # Split into <4000 char chunks at separator boundaries.
        chunks = []
        while combined:
            if len(combined) <= 4000:
                chunks.append(combined)
                break
            cut = combined.rfind("\n\n" + "━" * 18, 0, 4000)
            if cut <= 0:
                cut = 4000
            chunks.append(combined[:cut])
            combined = combined[cut:].lstrip()

        delivered = False
        err_text = None
        try:
            for chunk in chunks:
                await bot_obj.send_message(chat_id=target["chat_id"], text=chunk)
            delivered = True
        except Exception as e:
            err_text = str(e)

        if delivered:
            for r in siblings:
                update_report_status(r["id"], "sent")
            summary_lines.append(
                f"  • «{group_label}» → «{target['chat_title']}»: {len(parts)} шт., "
                f"{fmt_usd2(group_payout)} USD"
            )
            grand_total_usd += group_payout
            grand_missing.extend(group_missing)
            sent_count += len(parts)
        else:
            summary_lines.append(f"  • «{group_label}»: ❌ {err_text}")
            error_count += len(siblings)

    header_lines = [
        f"✅ Отправлено отчётов: {sent_count}" + (f"  ·  ❌ ошибок: {error_count}" if error_count else ""),
        "",
        f"💰 Итог по всем группам: {fmt_usd2(grand_total_usd)} USD",
    ]
    if grand_missing:
        header_lines.append(f"⚠️ Без курса (не учтены): {', '.join('@' + u for u in grand_missing)}")
    out = "\n".join(header_lines) + "\n\n" + "\n".join(summary_lines)
    if len(out) > 3800:
        out = out[:3800] + "\n…(обрезано)"
    await query.edit_message_text(out, reply_markup=main_menu(user_id))


def _rerender_auto_shift_from_snapshot(snapshot, show_money=True):
    """Rebuild text from a saved auto_shift snapshot.
    Manual edits (snapshot.overrides / .excluded / .manual_added) are applied
    on top of the original amounts so totals reflect them.
    """
    bot_username = snapshot.get("bot_username", "?")
    group_name = snapshot.get("group_name", "?")
    shift_date_str = snapshot.get("shift_date", "")
    currency = snapshot.get("currency", "")
    pct = float(snapshot.get("commission_pct", get_default_commission()))
    overrides = snapshot.get("overrides") or {}
    excluded = set(snapshot.get("excluded") or [])
    manual_added = snapshot.get("manual_added") or []
    approved = snapshot.get("approved", [])
    pending = snapshot.get("pending", [])

    def effective_amount(r):
        rid = str(r.get("receipt_id"))
        if rid in overrides:
            return float(overrides[rid])
        return float(r.get("amount") or 0)

    total_approved = sum(
        effective_amount(r) for r in approved
        if str(r.get("receipt_id")) not in excluded
    )
    total_approved += sum(
        effective_amount(r) for r in manual_added
        if str(r.get("receipt_id")) not in excluded
    )

    try:
        d = datetime.strptime(shift_date_str, "%Y-%m-%d")
        header_date = d.strftime("%d.%m.%Y")
    except ValueError:
        header_date = shift_date_str

    chat_label = _group_chat_label(snapshot.get("group_id") if isinstance(snapshot, dict) else group_id, group_name)
    lines = [
        f"📊 {chat_label} @{bot_username} за смену {header_date}",
        "",
    ]

    def fmt_line(r):
        ts = datetime.fromtimestamp(r.get("ts", 0), MOSCOW_TZ).strftime("%H:%M") if r.get("ts") else "??:??"
        ps = r.get("pseudonym", "?")
        rid = str(r.get("receipt_id"))
        is_excluded = rid in excluded
        if rid in overrides:
            new_amt = fmt_amount(overrides[rid])
            old_amt = fmt_amount(r.get("amount") or 0)
            base = f"  {ts} — {ps}: ✏️ {new_amt} {currency} (было {old_amt})".rstrip()
        else:
            base = f"  {ts} — {ps}: {fmt_amount(r.get('amount') or 0)} {currency}".rstrip()
        if is_excluded:
            base = f"  {ts} — {ps}: ❌ исключён ({fmt_amount(r.get('amount') or 0)} {currency})".rstrip()
        return base

    visible_approved = [r for r in approved if str(r.get("receipt_id")) not in excluded]
    visible_manual = [r for r in manual_added if str(r.get("receipt_id")) not in excluded]
    if visible_approved or visible_manual:
        lines.append("Принятые чеки:")
        for r in visible_approved:
            lines.append(fmt_line(r))
        for r in visible_manual:
            ts = datetime.fromtimestamp(r.get("ts", 0), MOSCOW_TZ).strftime("%H:%M") if r.get("ts") else "??:??"
            ps = r.get("pseudonym", "?")
            rid = str(r.get("receipt_id"))
            amt = fmt_amount(overrides.get(rid, r.get("amount") or 0))
            lines.append(f"  {ts} — {ps}: ➕ {amt} {currency}".rstrip())
        lines.append(f"Сумма принятых: {fmt_amount(total_approved)} {currency}".rstrip())
    else:
        lines.append("Принятых чеков нет.")

    excluded_originals = [r for r in approved if str(r.get("receipt_id")) in excluded]
    if excluded_originals:
        lines.append("")
        lines.append("❌ Исключены вручную (не в сумме):")
        for r in excluded_originals:
            lines.append(fmt_line(r))

    if pending:
        lines.append("")
        lines.append("⚠️ Неразобранные чеки (не вошли в сумму):")
        for r in pending:
            lines.append(fmt_line(r))

    editor = snapshot.get("edited_by")
    if editor:
        lines.append("")
        lines.append(f"📝 Правки внёс: {editor}")

    if show_money:
        rate = get_rate(snapshot.get("bot_geo", ""))
        if rate and rate > 0 and total_approved > 0:
            usd = total_approved / rate
            mult = (100.0 - pct) / 100.0
            payout = usd * mult
            lines.append("")
            lines.append(f"Курс: {fmt_rate(rate)}")
            lines.append(f"В USD: {fmt_amount(total_approved)} ÷ {fmt_rate(rate)} = {fmt_usd(usd)} USD")
            lines.append(f"Итого выплата: {fmt_usd(usd)} × {mult:.2f} = {fmt_usd(payout)} USD (вычет {fmt_amount(pct)}%)")
        elif total_approved > 0:
            lines.append("")
            lines.append("⚠️ Курс не задан — расчёт пропущен")
    return "\n".join(lines), snapshot, snapshot.get("group_id")


def _effective_total_approved(snapshot):
    """Sum approved + manual_added receipts with overrides applied, excluding
    those in `excluded`. Mirrors what `_rerender_auto_shift_from_snapshot`
    computes for display."""
    overrides = snapshot.get("overrides") or {}
    excluded = set(snapshot.get("excluded") or [])
    total = 0.0
    for r in snapshot.get("approved", []):
        rid = str(r.get("receipt_id"))
        if rid in excluded:
            continue
        total += float(overrides.get(rid, r.get("amount") or 0))
    for r in snapshot.get("manual_added") or []:
        rid = str(r.get("receipt_id"))
        if rid in excluded:
            continue
        total += float(overrides.get(rid, r.get("amount") or 0))
    return total


def _format_payout_summary(shift_date_str, parts, total):
    """Format the totals footer line as
        «Итого за 17.06.2026: 2569.50 + 150.79 + ... = 4513.79 USD.»
    Date defaults to today's MSK if missing. If only one part, omits the sum
    breakdown to avoid an ugly "X = X" line.
    """
    try:
        d = datetime.strptime(shift_date_str or "", "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        d = today_str()
        try:
            d = datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            pass
    if len(parts) > 1:
        breakdown = " + ".join(fmt_usd2(p) for p in parts)
        return f"Итого за {d}: {breakdown} = {fmt_usd2(total)} USD."
    return f"Итого за {d}: {fmt_usd2(total)} USD."


def _snapshot_payout_usd(snapshot):
    """Return (payout_usd, total_local, rate, pct) for an auto_shift snapshot.
    payout_usd is None if rate is missing/zero."""
    total = _effective_total_approved(snapshot)
    pct = float(snapshot.get("commission_pct", get_default_commission()))
    rate = get_rate(snapshot.get("bot_geo", ""))
    if not rate or rate <= 0 or total <= 0:
        return None, total, rate, pct
    usd = total / rate
    payout = usd * (100.0 - pct) / 100.0
    return payout, total, rate, pct


def _carry_manual_edits(old_snap, new_snap):
    """Copy bookkeeper/Owner edits (overrides/excluded/manual_added/edited_by)
    from old_snap into the freshly-recounted new_snap, dropping references to
    receipts that no longer exist."""
    new_ids = {str(r.get("receipt_id")) for r in new_snap.get("approved", [])} \
              | {str(r.get("receipt_id")) for r in new_snap.get("pending", [])}
    manual = old_snap.get("manual_added") or []
    if manual:
        new_snap["manual_added"] = manual
        new_ids |= {str(r.get("receipt_id")) for r in manual}
    overrides = old_snap.get("overrides") or {}
    kept_overrides = {k: v for k, v in overrides.items() if k in new_ids}
    if kept_overrides:
        new_snap["overrides"] = kept_overrides
    excluded = old_snap.get("excluded") or []
    kept_excluded = [rid for rid in excluded if rid in new_ids]
    if kept_excluded:
        new_snap["excluded"] = kept_excluded
    editor = old_snap.get("edited_by")
    if editor:
        new_snap["edited_by"] = editor


def _can_edit_report(rep, user_id):
    """Bookkeeper edits while status='pending_bookkeeper', Owner edits while
    status='pending_owner'. Once 'sent' nobody edits."""
    status = rep.get("status")
    if status == "pending_bookkeeper":
        return is_acc_admin(user_id) or is_acc_owner(user_id)
    if status == "pending_owner":
        return is_acc_owner(user_id)
    return False


def _stamp_editor(snap, user_id):
    """Record who last touched the snapshot — used for provenance line."""
    if is_acc_owner(user_id):
        snap["edited_by"] = "Owner"
    elif is_acc_admin(user_id):
        snap["edited_by"] = "бухгалтер"


async def show_edit_list(query, report_id, rep, user_id):
    """Show all receipts of an auto_shift report as editable rows."""
    snap = rep["payload"] or {}
    if snap.get("kind") != "auto_shift":
        await query.edit_message_text(
            "Этот тип отчёта нельзя редактировать.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Назад", callback_data=f"review:view:{report_id}")]
            ]))
        return
    overrides = snap.get("overrides") or {}
    excluded = set(snap.get("excluded") or [])
    currency = snap.get("currency", "")
    rows = []

    def add_rows(label_prefix, r):
        rid = str(r.get("receipt_id"))
        ts = datetime.fromtimestamp(r.get("ts", 0), MOSCOW_TZ).strftime("%H:%M") if r.get("ts") else "??:??"
        ps = r.get("pseudonym", "?")
        amount = overrides.get(rid, r.get("amount"))
        if rid in excluded:
            mark = "❌"
        elif rid in overrides:
            mark = "✏️"
        else:
            mark = label_prefix
        label = f"{mark} {ts} {ps}: {fmt_amount(amount)} {currency}".strip()
        toggle_text = "Вернуть" if rid in excluded else "Исключить"
        rows.append([
            InlineKeyboardButton(label[:48], callback_data=f"review:edit_one:{report_id}:{rid}"),
            InlineKeyboardButton(toggle_text, callback_data=f"review:toggle_exclude:{report_id}:{rid}"),
        ])

    for r in snap.get("approved", []):
        add_rows("✅", r)
    for r in snap.get("manual_added", []):
        add_rows("➕", r)
    for r in snap.get("pending", []):
        add_rows("⚠️", r)

    rows.append([InlineKeyboardButton("➕ Добавить чек вручную", callback_data=f"review:add_manual:{report_id}")])
    if snap.get("overrides") or snap.get("excluded") or snap.get("manual_added"):
        rows.append([InlineKeyboardButton("🗑 Сбросить мои правки", callback_data=f"review:reset_overrides:{report_id}")])
    rows.append([InlineKeyboardButton("« Назад", callback_data=f"review:view:{report_id}")])
    await query.edit_message_text(
        "✏️ Тапни сумму чтобы её изменить, «Исключить» — убрать из подсчёта.\n"
        "✅=принят · ⚠️=неразобран · ✏️=изменён · ❌=исключён · ➕=добавлен вручную",
        reply_markup=InlineKeyboardMarkup(rows))


async def show_target_chats(query):
    """Show currently bound chats + pending ones waiting for binding."""
    bound = list_target_chats()
    pending = list_pending_chats()

    lines = ["💬 Привязка чатов\n"]
    if bound:
        lines.append("🔗 Привязаны:")
        for b in bound:
            g = get_group(b["group_id"])
            gname = g["name"] if g else f"группа {b['group_id']} (удалена)"
            lines.append(f"  • «{b['chat_title']}» → {gname}")
        lines.append("")
    if pending:
        lines.append("⏳ Ожидают привязки:")
        for p in pending:
            lines.append(f"  • «{p['title']}» (id={p['chat_id']})")
        lines.append("")
    if not bound and not pending:
        lines.append("Пока ничего. Добавьте бот в TG-чат — он появится тут.")

    rows = []
    for p in pending:
        rows.append([InlineKeyboardButton(
            f"🔗 Привязать «{p['title']}»",
            callback_data=f"chat:bind:{p['chat_id']}"
        )])
    for b in bound:
        rows.append([InlineKeyboardButton(
            f"🗑 Отвязать «{b['chat_title']}»",
            callback_data=f"chat:unbind:{b['group_id']}"
        )])
    rows.append([InlineKeyboardButton("« Меню", callback_data="menu:main")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def show_chat_bind_groups(query, chat_id):
    groups = list_groups()
    title = ""
    for pc in list_pending_chats():
        if pc["chat_id"] == chat_id:
            title = pc["title"]
            break
    rows = []
    for g in groups:
        tc = get_target_chat(g["id"])
        suffix = f"  (уже привязан к «{tc['chat_title']}»)" if tc else ""
        rows.append([InlineKeyboardButton(
            f"{g['name']}{suffix}",
            callback_data=f"chat:bind_to:{chat_id}:{g['id']}"
        )])
    rows.append([InlineKeyboardButton("« Назад", callback_data="menu:target_chats")])
    await query.edit_message_text(
        f"Привязать чат «{title}» (id={chat_id}) к группе:",
        reply_markup=InlineKeyboardMarkup(rows))


async def show_roles(query):
    owners = sorted(get_owners())
    admins = sorted(get_admins())  # 'admin' role is now displayed as «Бухгалтер»
    owner_lines = "\n".join(f"  • <code>{u}</code>" for u in owners) or "  (никого)"
    admin_lines = "\n".join(f"  • <code>{u}</code>" for u in admins) or "  (никого)"
    rows = [
        [InlineKeyboardButton("➕ Добавить Owner", callback_data="roles:add:owner")],
        [InlineKeyboardButton("➕ Добавить Бухгалтера", callback_data="roles:add:admin")],
    ]
    for u in owners:
        rows.append([InlineKeyboardButton(f"🗑 Owner {u}", callback_data=f"roles:remove:owner:{u}")])
    for u in admins:
        rows.append([InlineKeyboardButton(f"🗑 Бухгалтер {u}", callback_data=f"roles:remove:admin:{u}")])
    rows.append([InlineKeyboardButton("« Меню", callback_data="menu:main")])
    await query.edit_message_text(
        f"👤 Роли\n\nOwners:\n{owner_lines}\n\nБухгалтеры:\n{admin_lines}",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML")


# ---------- Text input state machine ----------

async def state_router(update, context, state, text):
    user_id = update.effective_user.id
    mode = state.get("mode")

    if mode == "enter_data":
        gid = state["group_id"]
        geo = state["geo"]
        # Parse: lines of numbers (allow comma as decimal sep, spaces in numbers)
        nums = []
        for line in text.replace(",", ".").splitlines():
            s = line.strip()
            if not s:
                continue
            # strip currency suffix if any: "1500 MXN" -> "1500"
            tok = s.split()[0]
            try:
                nums.append(float(tok))
            except ValueError:
                await update.message.reply_text(
                    f"❌ Не распознал число в строке: «{line}»\nПопробуйте ещё раз или «отмена»."
                )
                return
        if not nums:
            await update.message.reply_text("❌ Не вижу ни одного числа. Попробуйте ещё раз.")
            return
        set_daily_data(gid, geo, nums)
        set_state(user_id, None)
        g = get_group(gid)
        total = sum(nums)
        cur = GEO_CURRENCIES.get(geo, "")
        geo_name = GEO_DISPLAY.get(geo, (geo, ""))[0]
        header = f"«{g['name']}» — {geo_name}"
        if len(nums) > 1:
            lines = "\n".join(f"  {fmt_amount(x)} {cur}".rstrip() for x in nums)
            msg = (f"✅ Сохранено для {header}:\n{lines}\n"
                   f"Сумма: {fmt_amount(total)} {cur}".rstrip())
        else:
            msg = f"✅ Сохранено для {header}: {fmt_amount(total)} {cur}".rstrip()
        await update.message.reply_text(msg, reply_markup=main_menu(user_id))
        return

    if mode == "enter_rate":
        geo = state["geo"]
        try:
            rate = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Введите число.")
            return
        if rate <= 0:
            await update.message.reply_text("❌ Курс должен быть положительным.")
            return
        set_rate(geo, rate)
        set_state(user_id, None)
        name = GEO_DISPLAY.get(geo, (geo,))[0]
        # Re-show the rates editor inline so the user can keep entering more
        # rates without navigating back to the main menu each time.
        await update.message.reply_text(
            f"✅ Курс {name}: {fmt_rate(rate)}",
            reply_markup=_rates_editor_kb())
        return

    if mode == "new_group_name":
        name = text.strip()
        if not name:
            await update.message.reply_text("❌ Название не может быть пустым.")
            return
        # Create group with empty geo list — admin attaches geos via 🌍 Гео.
        gid = create_group(name=name, geo="")
        set_state(user_id, None)
        await update.message.reply_text(
            f"✅ Группа «{name}» создана.\nДобавьте гео и привяжите ботов в карточке группы."
        )
        # Show group view
        class FakeQuery:
            def __init__(self, message, user):
                self.message = message
                self.from_user = user
            async def edit_message_text(self, text, **kwargs):
                await self.message.reply_text(text, **kwargs)
            async def answer(self, *args, **kwargs):
                pass
        await show_group_view(FakeQuery(update.message, update.effective_user), gid)
        return

    if mode == "rename_group":
        gid = state["group_id"]
        name = text.strip()
        if not name:
            await update.message.reply_text("❌ Название не может быть пустым.")
            return
        update_group(gid, name=name)
        set_state(user_id, None)
        await update.message.reply_text(
            f"✅ Переименовано в «{name}»",
            reply_markup=main_menu(user_id))
        return

    if mode == "set_group_pct":
        gid = state["group_id"]
        s = text.strip().lower()
        if s in ("дефолт", "default", "стандарт"):
            update_group(gid, commission_pct=None)
            set_state(user_id, None)
            await update.message.reply_text("✅ Установлен дефолтный %.", reply_markup=main_menu(user_id))
            return
        try:
            pct = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Введите число или «дефолт».")
            return
        if pct < 0 or pct > 100:
            await update.message.reply_text("❌ % должен быть от 0 до 100.")
            return
        update_group(gid, commission_pct=pct)
        set_state(user_id, None)
        await update.message.reply_text(
            f"✅ % комиссии: {fmt_amount(pct)}%",
            reply_markup=main_menu(user_id))
        return

    if mode == "set_default_pct":
        try:
            pct = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Введите число.")
            return
        if pct < 0 or pct > 100:
            await update.message.reply_text("❌ % должен быть от 0 до 100.")
            return
        set_setting("default_commission_pct", str(pct))
        set_state(user_id, None)
        await update.message.reply_text(
            f"✅ Дефолтный % обновлён: {fmt_amount(pct)}%",
            reply_markup=main_menu(user_id))
        return

    if mode == "add_role":
        kind = state["kind"]
        try:
            target = int(text)
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.")
            return
        conn = db_conn()
        table = "acc_owners" if kind == "owner" else "acc_admins"
        conn.execute(f"INSERT OR IGNORE INTO {table}(user_id) VALUES (?)", (target,))
        conn.commit()
        conn.close()
        set_state(user_id, None)
        role_label = "Owner" if kind == "owner" else "Бухгалтер"
        await update.message.reply_text(
            f"✅ Юзер {target} добавлен как {role_label}.",
            reply_markup=main_menu(user_id))
        return

    if mode == "edit_receipt_amount":
        try:
            new_amount = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Введите число (например 1234.5)", reply_markup=cancel_kb())
            return
        if new_amount < 0:
            await update.message.reply_text("❌ Сумма не может быть отрицательной.", reply_markup=cancel_kb())
            return
        rid = state["report_id"]
        receipt_id = state["receipt_id"]
        rep = get_report(rid)
        if not rep:
            set_state(user_id, None)
            await update.message.reply_text("❌ Отчёт не найден.", reply_markup=main_menu(user_id))
            return
        if not _can_edit_report(rep, user_id):
            set_state(user_id, None)
            await update.message.reply_text("❌ Нет прав на редактирование.", reply_markup=main_menu(user_id))
            return
        snap = rep["payload"] or {}
        # If editing a manually-added receipt — update it in place.
        manual_added = snap.get("manual_added") or []
        edited_manual = False
        for mr in manual_added:
            if str(mr.get("receipt_id")) == str(receipt_id):
                mr["amount"] = new_amount
                edited_manual = True
                break
        if not edited_manual:
            overrides = snap.get("overrides") or {}
            overrides[str(receipt_id)] = new_amount
            snap["overrides"] = overrides
        else:
            snap["manual_added"] = manual_added
        _stamp_editor(snap, user_id)
        is_owner = is_acc_owner(user_id)
        text_preview, _p, _g = _rerender_auto_shift_from_snapshot(snap, show_money=is_owner)
        update_report_payload(rid, snap, text_preview=text_preview)
        set_state(user_id, None)
        rep_fresh = get_report(rid)
        await update.message.reply_text(
            f"✅ Сумма изменена: {fmt_amount(new_amount)} {snap.get('currency', '')}".rstrip()
        )
        await _send_report_card(update.message, rid, rep_fresh, user_id)
        return

    if mode == "add_manual_receipt":
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Формат: <сумма> <псевдоним>\nНапример: 1500 Damir",
                reply_markup=cancel_kb())
            return
        try:
            amount = float(parts[0].replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Первая часть должна быть числом.", reply_markup=cancel_kb())
            return
        if amount <= 0:
            await update.message.reply_text("❌ Сумма должна быть > 0.", reply_markup=cancel_kb())
            return
        pseudonym = parts[1].strip()
        rid = state["report_id"]
        rep = get_report(rid)
        if not rep:
            set_state(user_id, None)
            await update.message.reply_text("❌ Отчёт не найден.", reply_markup=main_menu(user_id))
            return
        if not _can_edit_report(rep, user_id):
            set_state(user_id, None)
            await update.message.reply_text("❌ Нет прав на редактирование.", reply_markup=main_menu(user_id))
            return
        snap = rep["payload"] or {}
        manual_id = f"manual-{int(time.time()*1000)}-{user_id}"
        snap.setdefault("manual_added", []).append({
            "receipt_id": manual_id,
            "amount": amount,
            "pseudonym": pseudonym,
            "ts": time.time(),
        })
        _stamp_editor(snap, user_id)
        is_owner = is_acc_owner(user_id)
        text_preview, _p, _g = _rerender_auto_shift_from_snapshot(snap, show_money=is_owner)
        update_report_payload(rid, snap, text_preview=text_preview)
        set_state(user_id, None)
        await update.message.reply_text(
            f"✅ Добавлено: {pseudonym} — {fmt_amount(amount)} {snap.get('currency', '')}".rstrip()
        )
        await _send_report_card(update.message, get_report(rid), user_id)
        return

    # Unknown state — reset
    set_state(user_id, None)
    await update.message.reply_text(
        "Сбросил состояние. /start", reply_markup=main_menu(user_id))


# ---------- App entry ----------

def _make_request():
    sock_opts = (
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30),
        (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10),
        (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3),
    )
    return HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=10.0,
        connection_pool_size=16,
        socket_options=sock_opts,
    )


async def auto_shift_scheduler(bot_obj):
    """Background loop: every minute, walk all known anon-bots that are bound
    to an acc-group, check whether their previous shift has just become a
    closed shift we haven't reported yet, and if so generate a pending_owner
    report.
    """
    while True:
        try:
            await _shift_scan_once(bot_obj)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception(f"shift scan failed: {e}")
        await asyncio.sleep(60)


async def _shift_scan_once(bot_obj):
    # Walk only anon-bots that are members of some acc-group
    conn = db_conn()
    rows = conn.execute(
        "SELECT DISTINCT bot_token FROM acc_group_bots"
    ).fetchall()
    conn.close()
    for (token,) in rows:
        try:
            await _maybe_emit_shift_report(bot_obj, token)
        except Exception as e:
            logger.error(f"emit shift report failed for {token[:12]}…: {e}")


async def _maybe_emit_shift_report(bot_obj, bot_token):
    prev_shift = previous_shift_date(bot_token)
    already = get_shift_state(bot_token)
    if already == prev_shift:
        return  # already processed
    # FIRST EVER run for this bot: just mark current state, do not backfill.
    if already is None:
        set_shift_state(bot_token, prev_shift)
        logger.info(f"[shift] first-seen {bot_token[:12]}… set last_shift_date={prev_shift}")
        return
    # We have a previous state and it differs from prev_shift → there are one or
    # more closed shifts to report. Emit one report per missed shift, in order.
    cursor = already
    while cursor != prev_shift:
        cur_dt = datetime.strptime(cursor, "%Y-%m-%d") + timedelta(days=1)
        cursor = cur_dt.strftime("%Y-%m-%d")
        text, payload, group_id = render_auto_shift_report(bot_token, cursor, show_money=False)
        if group_id is None:
            # Bot has no group anymore — skip but advance state to not loop forever
            set_shift_state(bot_token, cursor)
            continue
        # Save as pending_bookkeeper. Bookkeeper reviews first — when he hits
        # «Готово», status moves to pending_owner. Owner then approves to send
        # to the bound TG-chat.
        rid = save_report(
            report_date=cursor,
            kind="auto_shift",
            payload=payload,
            text_preview=text,
            sent_to_tokens=[],
            status="pending_bookkeeper",
            group_id=group_id,
            bot_token=bot_token,
        )
        # New report supersedes any older pending reports for the same bot —
        # they get moved to «expired» so they disappear from queues but stay
        # visible in History.
        expire_previous_reports_for_bot(bot_token, except_report_id=rid)
        set_shift_state(bot_token, cursor)
        # Notify Бухгалтеры first; if none — fall back to Owners so the report
        # doesn't get stuck.
        bot_username = payload.get("bot_username", bot_token[:12])
        bookkeepers = list(get_admins())
        targets = bookkeepers if bookkeepers else list(get_owners())
        for uid in targets:
            try:
                await bot_obj.send_message(
                    chat_id=uid,
                    text=(f"📥 Авто-сверка по @{bot_username} за смену {cursor}.\n"
                          f"Откройте «📥 На проверке».")
                )
            except Exception:
                pass
        logger.info(f"[shift] emitted report id={rid} for {bot_token[:12]}… date={cursor}")


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track when the bot is added to / removed from a TG group/supergroup."""
    cm = update.my_chat_member
    if not cm:
        return
    chat = cm.chat
    if chat.type not in ("group", "supergroup", "channel"):
        return
    new_status = cm.new_chat_member.status
    old_status = cm.old_chat_member.status
    # Bot becomes a member (any of these counts as "now in chat")
    became_member = new_status in ("member", "administrator") and old_status in ("left", "kicked", "restricted")
    left = new_status in ("left", "kicked") and old_status in ("member", "administrator", "restricted")

    chat_id = chat.id
    chat_title = chat.title or f"chat {chat_id}"

    if became_member:
        # If this chat is already bound, nothing to do.
        bound = next((b for b in list_target_chats() if b["chat_id"] == chat_id), None)
        if bound:
            logger.info(f"Bot re-added to bound chat {chat_id} ({chat_title})")
            return
        add_pending_chat(chat_id, chat_title)
        # Notify ALL owners
        for owner_uid in get_owners():
            try:
                await context.bot.send_message(
                    chat_id=owner_uid,
                    text=(f"🆕 Бот добавлен в чат «{chat_title}» (id={chat_id}).\n"
                          f"Зайдите в «💬 Привязка чатов» в /start чтобы привязать его к группе.")
                )
            except Exception:
                pass
        logger.info(f"Bot added to chat {chat_id} ({chat_title}) — awaiting binding")
    elif left:
        # Remove from pending; keep target binding if any (admin may add bot back)
        remove_pending_chat(chat_id)
        logger.info(f"Bot left/kicked from chat {chat_id} ({chat_title})")


async def _post_init(application):
    # Kick off background tasks once the bot event loop is up.
    application.create_task(auto_shift_scheduler(application.bot))


def main():
    if not BOT_TOKEN:
        raise SystemExit("ACC_BOT_TOKEN is required")
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(_make_request())
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    from telegram.ext import ChatMemberHandler
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    logger.info("Accounting bot starting (polling)")
    app.run_polling(poll_interval=1.0, timeout=30, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
