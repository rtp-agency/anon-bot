import os
import logging
import time
import random
import string
import sqlite3
import asyncio
import html
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import requests

import gspread
from google.oauth2.service_account import Credentials
from telegram.request import HTTPXRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Suppress noisy httpx polling logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
WHITELIST = [int(x) for x in os.getenv("WHITELIST", "").split(",") if x]
GOOGLE_SHEETS_CREDS = os.getenv("GOOGLE_SHEETS_CREDS")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

GEO_CURRENCIES = {
    "argentina": "ARS",
    "bolivia": "BOB",
    "chile": "CLP",
    "mexico": "MXN",
    "colombia": "COP",
    "peru": "PEN",
    "ecuador": "USD",
    "venezuela": "VES",
    "turkey": "TRY",
    "nigeria": "NGN",
    "morocco": "MAD",
    "costa_rica": "CRC",
    "indonesia": "IDR",
}

created_bots = {}
user_pseudonyms = {}
receipts = {}
bot_admins = {}
bot_chat_admins = {}
invite_links = {}
bot_geos = {}
bot_shifts = {}
user_states = {}
bot_requisites = {}
message_map = {}
message_map_timestamps = {}
banned_users = {}
receipt_watchers = {}
bot_owners = {}            # {bot_token: set(user_id)}
bot_handlers = {}          # {bot_token: set(user_id)}
ping_settings = {}         # {bot_token: {"interval_min": int, "enabled": bool, "task": asyncio.Task or None}}

MESSAGE_MAP_TTL = 48 * 3600  # 48 hours (Telegram delete limit)
_last_cleanup_time = 0

# Thread pool for blocking I/O (Google Sheets, SQLite)
_executor = ThreadPoolExecutor(max_workers=4)


async def run_in_thread(func, *args):
    """Run a blocking function in a thread pool to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, func, *args)


def cleanup_old_messages():
    global _last_cleanup_time
    now = time.time()
    if now - _last_cleanup_time < 3600:
        return
    _last_cleanup_time = now
    cutoff = now - MESSAGE_MAP_TTL
    total_deleted = 0
    for bot_token in list(message_map_timestamps.keys()):
        keys_to_delete = [
            key for key, ts in message_map_timestamps.get(bot_token, {}).items()
            if ts < cutoff
        ]
        for key in keys_to_delete:
            message_map.get(bot_token, {}).pop(key, None)
            message_map_timestamps[bot_token].pop(key, None)
        total_deleted += len(keys_to_delete)
        if not message_map_timestamps.get(bot_token):
            message_map_timestamps.pop(bot_token, None)
            message_map.pop(bot_token, None)
    # Clean old receipts too
    old_receipts = [
        rid for rid, r in receipts.items()
        if r.get("_ts", now) < cutoff
    ]
    for rid in old_receipts:
        del receipts[rid]
        db_delete_receipt(rid)
    if total_deleted or old_receipts:
        logger.info(f"Cleanup: removed {total_deleted} message_map entries, {len(old_receipts)} old receipts")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

google_sheets_client = None
spreadsheet = None


MOSCOW_TZ = timezone(timedelta(hours=3))


# Throttle concurrent send/edit calls to avoid bursting Telegram API
# and to prevent the event loop from getting blocked by many concurrent slow ops
_send_semaphore = asyncio.Semaphore(8)


def _purge_user_from_state(bot_token, uid, drop_pseudonym=True):
    """Remove a user from all in-memory and DB role/notification state.

    Used when a user is kicked, blocked, or deactivated. Pulls them from
    pseudonyms (optional), watchers, admins, owners, handlers in one shot
    so leftover references can't surface in approve notifications, ping lists,
    or member views.
    """
    removed = False
    if drop_pseudonym and bot_token in user_pseudonyms and uid in user_pseudonyms[bot_token]:
        del user_pseudonyms[bot_token][uid]
        removed = True
    if bot_token in receipt_watchers:
        receipt_watchers[bot_token].discard(uid)
    if bot_token in bot_chat_admins:
        bot_chat_admins[bot_token].discard(uid)
    if bot_token in bot_owners:
        bot_owners[bot_token].discard(uid)
    if bot_token in bot_handlers:
        bot_handlers[bot_token].discard(uid)
    try:
        conn = sqlite3.connect(DB_PATH)
        if drop_pseudonym:
            conn.execute("DELETE FROM pseudonyms WHERE bot_token = ? AND user_id = ?", (bot_token, uid))
        conn.execute("DELETE FROM receipt_watchers WHERE bot_token = ? AND user_id = ?", (bot_token, uid))
        conn.execute("DELETE FROM chat_admins WHERE bot_token = ? AND user_id = ?", (bot_token, uid))
        conn.execute("DELETE FROM chat_owners WHERE bot_token = ? AND user_id = ?", (bot_token, uid))
        conn.execute("DELETE FROM chat_handlers WHERE bot_token = ? AND user_id = ?", (bot_token, uid))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return removed


async def send_with_retry(coro_func, uid, bot_token, max_retries=2):
    async with _send_semaphore:
        for attempt in range(max_retries):
            try:
                return await coro_func()
            except Exception as e:
                err_str = str(e).lower()
                if "blocked by the user" in err_str or "user is deactivated" in err_str or "chat not found" in err_str:
                    if _purge_user_from_state(bot_token, uid):
                        logger.info(f"Auto-removed blocked/deactivated user {uid}")
                    raise
                if "message is not modified" in err_str:
                    return None
                if "message to edit not found" in err_str or "message to delete not found" in err_str:
                    return None
                if attempt < max_retries - 1 and (
                    "timed out" in err_str or "readerror" in err_str
                    or "connecterror" in err_str or "remoteprotocolerror" in err_str
                    or "poolerror" in err_str
                ):
                    await asyncio.sleep(0.5)
                    continue
                raise


async def gather_safe(*coros):
    """Like asyncio.gather but never propagates exceptions and never blocks indefinitely."""
    if not coros:
        return []
    return await asyncio.gather(*coros, return_exceptions=True)


async def _ping_pending_receipts_once(bot_token):
    """Send one round of reminder pings for all pending receipts of a bot."""
    bot_info = created_bots.get(bot_token)
    if not bot_info:
        return
    bot = bot_info["application"].bot

    pending = [
        (rid, rdata) for rid, rdata in receipts.items()
        if rdata.get("bot_token") == bot_token and rdata.get("status") == "pending"
    ]
    if not pending:
        return

    # Pings go only to people who were explicitly assigned: receipt watchers
    # plus handlers (since handlers run/configure the ping). Admins are NOT
    # notified automatically — they have to opt in via the watcher menu.
    notify_ids = set()
    notify_ids.update(receipt_watchers.get(bot_token, set()))
    notify_ids.update(bot_handlers.get(bot_token, set()))
    if not notify_ids:
        return

    sent_count = 0
    for rid, rdata in pending:
        msg_ids = rdata.get("message_ids", {})
        pseudonym = rdata.get("pseudonym", "?")
        text = rdata.get("text", "?")
        ping_text = f"⏰ Чек ожидает апрува\n{pseudonym}: {text}"
        for uid in notify_ids:
            target_msg = msg_ids.get(uid)
            if not target_msg:
                continue  # Admin never received this receipt's copy

            async def _do_send(uid=uid, target_msg=target_msg, ping_text=ping_text):
                return await bot.send_message(
                    chat_id=uid, text=ping_text,
                    reply_to_message_id=target_msg,
                    allow_sending_without_reply=True
                )
            try:
                await send_with_retry(_do_send, uid, bot_token)
                sent_count += 1
            except Exception as e:
                logger.error(f"Ping failed for {uid} receipt {rid}: {e}")
    if sent_count:
        logger.info(f"Ping: sent {sent_count} reminders for {len(pending)} pending receipts ({bot_info.get('username')})")


async def _ping_task(bot_token):
    """Loop: sleep N minutes, ping pending receipts, repeat. Stops if disabled."""
    try:
        while True:
            settings = ping_settings.get(bot_token)
            if not settings or not settings.get("enabled"):
                return
            interval_min = max(1, int(settings.get("interval_min", 30)))
            await asyncio.sleep(interval_min * 60)
            settings = ping_settings.get(bot_token)
            if not settings or not settings.get("enabled"):
                return
            try:
                await _ping_pending_receipts_once(bot_token)
            except Exception as e:
                logger.error(f"Ping iteration error for {bot_token[:20]}...: {e}")
    except asyncio.CancelledError:
        return


def start_ping_task(bot_token):
    """Start (or restart) the ping task for a bot if enabled in settings."""
    settings = ping_settings.get(bot_token)
    if not settings or not settings.get("enabled"):
        return
    existing = settings.get("task")
    if existing and not existing.done():
        existing.cancel()
    settings["task"] = asyncio.create_task(_ping_task(bot_token))


def stop_ping_task(bot_token):
    settings = ping_settings.get(bot_token)
    if not settings:
        return
    task = settings.get("task")
    if task and not task.done():
        task.cancel()
    settings["task"] = None


def resolve_reply_target(bot_token, user_id, reply_msg_id, target_uid):
    original = message_map.get(bot_token, {}).get((user_id, reply_msg_id))
    if not original:
        return None

    if "receipt_id" in original:
        receipt = receipts.get(original["receipt_id"])
        if receipt and "message_ids" in receipt:
            return receipt["message_ids"].get(target_uid)
        return None

    root_entry = None
    if "sent_to" in original:
        root_entry = original
    elif "sender_msg_id" in original:
        sender_entry = message_map.get(bot_token, {}).get(
            (original["sender_id"], original["sender_msg_id"])
        )
        if sender_entry:
            root_entry = sender_entry

    if not root_entry:
        return None

    if target_uid in root_entry.get("sent_to", {}):
        return root_entry["sent_to"][target_uid]
    elif root_entry.get("sender_id") == target_uid:
        return root_entry.get("sender_msg_id")

    return None


def format_amount(value):
    num = float(value)
    if num == int(num):
        s = str(int(num))
    else:
        s = f"{num:.2f}"
        integer_part, decimal_part = s.split(".")
        formatted = ""
        for i, ch in enumerate(reversed(integer_part)):
            if i > 0 and i % 3 == 0:
                formatted = "." + formatted
            formatted = ch + formatted
        return f"{formatted},{decimal_part}"
    formatted = ""
    for i, ch in enumerate(reversed(s)):
        if i > 0 and i % 3 == 0:
            formatted = "." + formatted
        formatted = ch + formatted
    return formatted


def get_moscow_now():
    return datetime.now(MOSCOW_TZ)


def _shift_bounds(bot_token):
    """Return ((start_h, start_m), (end_h, end_m)) for the bot. Both inclusive
    in the open-end style: end is the last minute that still belongs to the
    shift."""
    shift = bot_shifts.get(bot_token, {})
    sh = shift.get("start", 0)
    eh = shift.get("end", 23)
    sm = shift.get("start_min", 0)
    em = shift.get("end_min", 59)
    return (sh, sm), (eh, em)


def format_shift_window(bot_token):
    """Return "HH:MM–HH:MM" string for the bot's shift, used in user-facing
    messages."""
    (sh, sm), (eh, em) = _shift_bounds(bot_token)
    return f"{sh:02d}:{sm:02d}–{eh:02d}:{em:02d}"


def is_working_hours(bot_token):
    (sh, sm), (eh, em) = _shift_bounds(bot_token)
    now = get_moscow_now()
    cur = now.hour * 60 + now.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= cur <= end
    # Overnight
    return cur >= start or cur <= end


def get_working_day_date(bot_token):
    (sh, sm), (eh, em) = _shift_bounds(bot_token)
    start = sh * 60 + sm
    end = eh * 60 + em
    now = get_moscow_now()
    cur = now.hour * 60 + now.minute
    # Overnight shift: if we're still in the part that belongs to yesterday's
    # working day, roll back one day.
    if start > end and cur <= end:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def get_bot_currency(bot_token):
    geo = bot_geos.get(bot_token, "argentina")
    return GEO_CURRENCIES.get(geo, "ARS")


def is_chat_admin(bot_token, user_id):
    if bot_token in bot_admins and bot_admins[bot_token] == user_id:
        return True
    return user_id in bot_chat_admins.get(bot_token, set())


def is_chat_owner(bot_token, user_id):
    """Owner role: limited rights (permanent invites + manage watchers)."""
    return user_id in bot_owners.get(bot_token, set())


def is_chat_handler(bot_token, user_id):
    """Handler role: can manage receipt ping reminders."""
    return user_id in bot_handlers.get(bot_token, set())


def find_user_by_pseudonym(bot_token, pseudonym):
    """Look up a participant by their pseudonym (case-insensitive, exact match).

    Returns (user_id, original_pseudonym) on hit, (None, None) on miss.
    Pseudonyms are unique within a chat (enforced on set/change), so a hit
    is unambiguous.
    """
    target = (pseudonym or "").strip().lower()
    if not target:
        return None, None
    for uid, name in user_pseudonyms.get(bot_token, {}).items():
        if name.strip().lower() == target:
            return uid, name
    return None, None


async def notify_role_change(bot, target_id, role_label, granted=True):
    """DM the user whose role just changed and ask them to /start so the
    keyboard with updated access refreshes on their side. Best-effort: silently
    swallows any send errors (user might have blocked the bot)."""
    if granted:
        text = (
            f"🎉 Вам назначена роль: {role_label}\n\n"
            f"Нажмите /start чтобы обновить меню и применить новые права."
        )
    else:
        text = (
            f"ℹ️ С вас снята роль: {role_label}\n\n"
            f"Нажмите /start чтобы обновить меню."
        )
    try:
        await bot.send_message(chat_id=target_id, text=text)
    except Exception:
        pass


def format_pseudonym_picklist(bot_token, filter_fn=None):
    """Build a multi-line bullet list of pseudonyms for picker prompts.

    Pseudonyms are wrapped in <code> so a single tap copies them to clipboard
    in Telegram clients. Reply must be sent with parse_mode="HTML".

    `filter_fn(uid) -> bool` decides which participants to include.
    Empty result returns "(никого)" placeholder.
    """
    names = sorted(
        name for uid, name in user_pseudonyms.get(bot_token, {}).items()
        if (filter_fn is None or filter_fn(uid))
    )
    if not names:
        return "  (никого)"
    return "\n".join(f"  • <code>{html.escape(n)}</code>" for n in names)


def is_pseudonym_taken(bot_token, pseudonym, exclude_user_id=None):
    """True if the pseudonym is already in use by someone else in this chat."""
    target = (pseudonym or "").strip().lower()
    if not target:
        return False
    for uid, name in user_pseudonyms.get(bot_token, {}).items():
        if exclude_user_id is not None and uid == exclude_user_id:
            continue
        if name.strip().lower() == target:
            return True
    return False


def get_main_keyboard(is_admin=False, is_owner=False, is_handler=False):
    keyboard = [
        ["📷 Отправить фото", "📋 Реквизиты"],
        ["✏️ Сменить ник", "💰 Итог за смену"]
    ]
    if is_admin:
        keyboard.append(["🔗 Инвайт", "⏰ Смена", "📝 Изм. реквизиты"])
        keyboard.append(["👑 Назначить админа", "🚫 Снять админа"])
        keyboard.append(["👢 Кикнуть", "♻️ Разбан"])
        keyboard.append(["🛡 Назначить Owner", "🛡 Снять Owner"])
        keyboard.append(["🎯 Назначить Обработчика", "🎯 Снять Обработчика"])
        keyboard.append(["🔔 Уведомления чеков", "📋 Лист участников"])
        keyboard.append(["⏰ Пинг чеков"])
    else:
        # Non-admin roles get their own restricted buttons.
        if is_owner:
            keyboard.append(["🔗 Постоянный инвайт", "🔔 Уведомления чеков"])
            keyboard.append(["👢 Кикнуть"])
        if is_handler:
            keyboard.append(["⏰ Пинг чеков", "📝 Изм. реквизиты"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def keyboard_for_user(bot_token, user_id):
    """Build the right keyboard for a given user based on their roles."""
    return get_main_keyboard(
        is_admin=is_chat_admin(bot_token, user_id),
        is_owner=is_chat_owner(bot_token, user_id),
        is_handler=is_chat_handler(bot_token, user_id),
    )


def init_google_sheets():
    global google_sheets_client, spreadsheet
    try:
        if not GOOGLE_SHEETS_CREDS or not GOOGLE_SHEET_ID:
            logger.warning("Google Sheets credentials not configured")
            return False

        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDS, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEET_ID)

        try:
            spreadsheet.worksheet("Dashboard")
        except:
            worksheet = spreadsheet.add_worksheet(title="Dashboard", rows=100, cols=5)
            worksheet.update('A1:B1', [['Bot Name', 'Approved Transactions']])

        logger.info("Google Sheets initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize Google Sheets: {e}")
        return False


def create_bot_sheet(bot_username):
    try:
        if not spreadsheet:
            return False

        try:
            spreadsheet.worksheet(bot_username)
            logger.info(f"Sheet for {bot_username} already exists")
            return True
        except:
            pass

        worksheet = spreadsheet.add_worksheet(title=bot_username, rows=1000, cols=5)
        worksheet.update('A1:E1', [['Timestamp', 'Amount', 'Currency', 'Pseudonym', 'Photo URL']])

        update_dashboard_bot(bot_username, 0)

        logger.info(f"Created sheet for bot: {bot_username}")
        return True
    except Exception as e:
        logger.error(f"Failed to create bot sheet: {e}")
        return False


def add_receipt_to_sheet(bot_username, amount, currency, pseudonym, photo_url=None):
    try:
        if not spreadsheet:
            return False

        worksheet = spreadsheet.worksheet(bot_username)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        row = [timestamp, str(amount), currency, pseudonym, photo_url or ""]
        worksheet.append_row(row)

        update_dashboard_increment(bot_username)

        logger.info(f"Added receipt to {bot_username}: {amount} {currency}")
        return True
    except Exception as e:
        logger.error(f"Failed to add receipt to sheet: {e}")
        return False


def remove_receipt_from_sheet(bot_username, amount, pseudonym):
    try:
        if not spreadsheet:
            return False

        worksheet = spreadsheet.worksheet(bot_username)
        all_rows = worksheet.get_all_values()

        for i in range(len(all_rows) - 1, 0, -1):
            row = all_rows[i]
            if len(row) >= 4 and row[1] == str(amount) and row[3] == pseudonym:
                worksheet.delete_rows(i + 1)
                update_dashboard_decrement(bot_username)
                logger.info(f"Removed receipt from {bot_username}: {amount} by {pseudonym}")
                return True

        logger.warning(f"Receipt not found in sheet {bot_username}: {amount} by {pseudonym}")
        return False
    except Exception as e:
        logger.error(f"Failed to remove receipt from sheet: {e}")
        return False


def update_receipt_in_sheet(bot_username, old_amount, new_amount, pseudonym):
    try:
        if not spreadsheet:
            return False

        worksheet = spreadsheet.worksheet(bot_username)
        all_rows = worksheet.get_all_values()

        for i in range(len(all_rows) - 1, 0, -1):
            row = all_rows[i]
            if len(row) >= 4 and row[1] == str(old_amount) and row[3] == pseudonym:
                worksheet.update_cell(i + 1, 2, str(new_amount))
                logger.info(f"Updated receipt in {bot_username}: {old_amount} -> {new_amount} by {pseudonym}")
                return True

        logger.warning(f"Receipt not found for update in {bot_username}: {old_amount} by {pseudonym}")
        return False
    except Exception as e:
        logger.error(f"Failed to update receipt in sheet: {e}")
        return False


def update_dashboard_decrement(bot_username):
    try:
        if not spreadsheet:
            return False

        dashboard = spreadsheet.worksheet("Dashboard")
        cell = dashboard.find(bot_username)

        if cell:
            current = dashboard.cell(cell.row, 2).value
            new_count = max(0, int(current or 0) - 1)
            dashboard.update_cell(cell.row, 2, new_count)

        return True
    except Exception as e:
        logger.error(f"Failed to decrement dashboard: {e}")
        return False


def update_dashboard_bot(bot_username, count):
    try:
        if not spreadsheet:
            return False

        dashboard = spreadsheet.worksheet("Dashboard")
        cell = dashboard.find(bot_username)

        if cell:
            dashboard.update_cell(cell.row, 2, count)
        else:
            dashboard.append_row([bot_username, count])

        return True
    except Exception as e:
        logger.error(f"Failed to update dashboard: {e}")
        return False


def update_dashboard_increment(bot_username):
    try:
        if not spreadsheet:
            return False

        dashboard = spreadsheet.worksheet("Dashboard")
        cell = dashboard.find(bot_username)

        if cell:
            current = dashboard.cell(cell.row, 2).value
            new_count = int(current or 0) + 1
            dashboard.update_cell(cell.row, 2, new_count)
        else:
            dashboard.append_row([bot_username, 1])

        return True
    except Exception as e:
        logger.error(f"Failed to increment dashboard: {e}")
        return False


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS bots (
        token TEXT PRIMARY KEY,
        username TEXT,
        admin_user_id INTEGER,
        geo TEXT DEFAULT 'argentina'
    )""")
    try:
        c.execute("ALTER TABLE bots ADD COLUMN geo TEXT DEFAULT 'argentina'")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS pseudonyms (
        bot_token TEXT,
        user_id INTEGER,
        pseudonym TEXT,
        PRIMARY KEY (bot_token, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS invite_links_db (
        code TEXT PRIMARY KEY,
        bot_token TEXT,
        expires_at REAL,
        used INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_totals (
        bot_token TEXT,
        date TEXT,
        total REAL DEFAULT 0,
        PRIMARY KEY (bot_token, date)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS shifts (
        bot_token TEXT PRIMARY KEY,
        shift_start INTEGER DEFAULT 0,
        shift_end INTEGER DEFAULT 23,
        shift_start_minute INTEGER DEFAULT 0,
        shift_end_minute INTEGER DEFAULT 59
    )""")
    for col, default in (("shift_start_minute", 0), ("shift_end_minute", 59)):
        try:
            c.execute(f"ALTER TABLE shifts ADD COLUMN {col} INTEGER DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS chat_admins (
        bot_token TEXT,
        user_id INTEGER,
        PRIMARY KEY (bot_token, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS requisites (
        bot_token TEXT PRIMARY KEY,
        text TEXT,
        photo_id TEXT
    )""")
    try:
        c.execute("ALTER TABLE requisites ADD COLUMN photo_id TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS banned_users (
        bot_token TEXT,
        user_id INTEGER,
        PRIMARY KEY (bot_token, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS receipt_watchers (
        bot_token TEXT,
        user_id INTEGER,
        PRIMARY KEY (bot_token, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS chat_owners (
        bot_token TEXT,
        user_id INTEGER,
        PRIMARY KEY (bot_token, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS chat_handlers (
        bot_token TEXT,
        user_id INTEGER,
        PRIMARY KEY (bot_token, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS ping_settings (
        bot_token TEXT PRIMARY KEY,
        interval_min INTEGER DEFAULT 30,
        enabled INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS receipts_persist (
        receipt_id TEXT PRIMARY KEY,
        bot_token TEXT,
        data TEXT,
        ts REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS message_map_persist (
        bot_token TEXT,
        owner_id INTEGER,
        message_id INTEGER,
        data TEXT,
        ts REAL,
        PRIMARY KEY (bot_token, owner_id, message_id)
    )""")
    conn.commit()
    conn.close()


def db_save_receipt(receipt_id, receipt_data):
    try:
        import json
        # Make a JSON-serializable copy. message_ids has int keys (uid), keep as-is.
        serializable = {}
        for k, v in receipt_data.items():
            if k == "message_ids":
                serializable[k] = {str(uid): mid for uid, mid in v.items()}
            else:
                serializable[k] = v
        data_json = json.dumps(serializable, ensure_ascii=False)
        ts = receipt_data.get("_ts", time.time())
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO receipts_persist VALUES (?, ?, ?, ?)",
            (receipt_id, receipt_data.get("bot_token", ""), data_json, ts)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to persist receipt {receipt_id}: {e}")


def db_delete_receipt(receipt_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM receipts_persist WHERE receipt_id = ?", (receipt_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def db_load_receipts():
    """Load receipts from DB into memory. Skip those older than 48h."""
    import json
    try:
        cutoff = time.time() - MESSAGE_MAP_TTL
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT receipt_id, data, ts FROM receipts_persist WHERE ts > ?",
            (cutoff,)
        ).fetchall()
        # Also delete stale ones
        conn.execute("DELETE FROM receipts_persist WHERE ts <= ?", (cutoff,))
        conn.commit()
        conn.close()
        loaded = 0
        for receipt_id, data_json, ts in rows:
            try:
                data = json.loads(data_json)
                if "message_ids" in data:
                    data["message_ids"] = {int(k): v for k, v in data["message_ids"].items()}
                receipts[receipt_id] = data
                # Reconstruct message_map references for receipts
                bot_token = data.get("bot_token")
                if bot_token and "message_ids" in data:
                    if bot_token not in message_map:
                        message_map[bot_token] = {}
                    if bot_token not in message_map_timestamps:
                        message_map_timestamps[bot_token] = {}
                    for uid, msg_id in data["message_ids"].items():
                        message_map[bot_token][(uid, msg_id)] = {
                            "pseudonym": data.get("pseudonym", ""),
                            "text": f"Чек: {data.get('text', '')}",
                            "sender_id": data.get("owner_id"),
                            "receipt_id": receipt_id
                        }
                        message_map_timestamps[bot_token][(uid, msg_id)] = ts
                loaded += 1
            except Exception as e:
                logger.error(f"Failed to load receipt {receipt_id}: {e}")
        logger.info(f"Loaded {loaded} receipts from persistence")
    except Exception as e:
        logger.error(f"Failed to load receipts: {e}")


def db_add_bot(token, username, admin_user_id, geo="argentina"):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO bots VALUES (?, ?, ?, ?)", (token, username, admin_user_id, geo))
    conn.commit()
    conn.close()


def db_add_pseudonym(bot_token, user_id, pseudonym):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO pseudonyms VALUES (?, ?, ?)", (bot_token, user_id, pseudonym))
    conn.commit()
    conn.close()


def db_remove_pseudonym(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM pseudonyms WHERE bot_token = ? AND user_id = ?", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_ban_user(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO banned_users VALUES (?, ?)", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_unban_user(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM banned_users WHERE bot_token = ? AND user_id = ?", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_add_receipt_watcher(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO receipt_watchers VALUES (?, ?)", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_remove_receipt_watcher(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM receipt_watchers WHERE bot_token = ? AND user_id = ?", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_update_pseudonym(bot_token, user_id, pseudonym):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE pseudonyms SET pseudonym = ? WHERE bot_token = ? AND user_id = ?", (pseudonym, bot_token, user_id))
    conn.commit()
    conn.close()


def db_add_invite(code, bot_token, expires_at, used):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO invite_links_db VALUES (?, ?, ?, ?)", (code, bot_token, expires_at, int(used)))
    conn.commit()
    conn.close()


def db_mark_invite_used(code):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE invite_links_db SET used = 1 WHERE code = ?", (code,))
    conn.commit()
    conn.close()


def db_add_daily_total(bot_token, amount):
    date = get_working_day_date(bot_token)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO daily_totals (bot_token, date, total) VALUES (?, ?, ?) "
        "ON CONFLICT(bot_token, date) DO UPDATE SET total = total + ?",
        (bot_token, date, amount, amount)
    )
    conn.commit()
    conn.close()


def db_subtract_daily_total(bot_token, amount):
    date = get_working_day_date(bot_token)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE daily_totals SET total = total - ? WHERE bot_token = ? AND date = ?",
        (amount, bot_token, date)
    )
    conn.commit()
    conn.close()


def db_get_daily_total(bot_token):
    date = get_working_day_date(bot_token)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute("SELECT total FROM daily_totals WHERE bot_token = ? AND date = ?", (bot_token, date)).fetchone()
    conn.close()
    return row[0] if row else 0.0


def db_add_chat_admin(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO chat_admins VALUES (?, ?)", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_remove_chat_admin(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM chat_admins WHERE bot_token = ? AND user_id = ?", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_add_chat_owner(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO chat_owners VALUES (?, ?)", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_remove_chat_owner(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM chat_owners WHERE bot_token = ? AND user_id = ?", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_add_chat_handler(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO chat_handlers VALUES (?, ?)", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_remove_chat_handler(bot_token, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM chat_handlers WHERE bot_token = ? AND user_id = ?", (bot_token, user_id))
    conn.commit()
    conn.close()


def db_save_ping_settings(bot_token, interval_min, enabled):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO ping_settings VALUES (?, ?, ?)",
        (bot_token, int(interval_min), 1 if enabled else 0)
    )
    conn.commit()
    conn.close()


def db_save_requisites(bot_token, text, photo_id=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO requisites VALUES (?, ?, ?)", (bot_token, text, photo_id))
    conn.commit()
    conn.close()


def db_save_shift(bot_token, shift_start, shift_end, shift_start_minute=0, shift_end_minute=59):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO shifts(bot_token, shift_start, shift_end, "
        "shift_start_minute, shift_end_minute) VALUES (?, ?, ?, ?, ?)",
        (bot_token, shift_start, shift_end, shift_start_minute, shift_end_minute)
    )
    conn.commit()
    conn.close()


def db_load_all():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    bots_list = c.execute("SELECT token, username, admin_user_id, COALESCE(geo, 'argentina') FROM bots").fetchall()

    pseudonyms_list = c.execute("SELECT bot_token, user_id, pseudonym FROM pseudonyms").fetchall()
    for bot_token, user_id, pseudonym in pseudonyms_list:
        if bot_token not in user_pseudonyms:
            user_pseudonyms[bot_token] = {}
        user_pseudonyms[bot_token][user_id] = pseudonym

    invites_list = c.execute("SELECT code, bot_token, expires_at, used FROM invite_links_db").fetchall()
    for code, bot_token, expires_at, used in invites_list:
        invite_links[code] = {
            "bot_token": bot_token,
            "expires_at": expires_at,
            "used": bool(used)
        }

    shifts_list = c.execute(
        "SELECT bot_token, shift_start, shift_end, "
        "COALESCE(shift_start_minute, 0), COALESCE(shift_end_minute, 59) FROM shifts"
    ).fetchall()
    for bot_token, shift_start, shift_end, ssmin, semin in shifts_list:
        bot_shifts[bot_token] = {
            "start": shift_start, "end": shift_end,
            "start_min": ssmin, "end_min": semin,
        }

    admins_list = c.execute("SELECT bot_token, user_id FROM chat_admins").fetchall()
    for bot_token, user_id in admins_list:
        if bot_token not in bot_chat_admins:
            bot_chat_admins[bot_token] = set()
        bot_chat_admins[bot_token].add(user_id)

    reqs_list = c.execute("SELECT bot_token, text, photo_id FROM requisites").fetchall()
    for bot_token, text, photo_id in reqs_list:
        bot_requisites[bot_token] = {"text": text, "photo_id": photo_id}

    banned_list = c.execute("SELECT bot_token, user_id FROM banned_users").fetchall()
    for bot_token, user_id in banned_list:
        if bot_token not in banned_users:
            banned_users[bot_token] = set()
        banned_users[bot_token].add(user_id)

    watchers_list = c.execute("SELECT bot_token, user_id FROM receipt_watchers").fetchall()
    for bot_token, user_id in watchers_list:
        if bot_token not in receipt_watchers:
            receipt_watchers[bot_token] = set()
        receipt_watchers[bot_token].add(user_id)

    owners_list = c.execute("SELECT bot_token, user_id FROM chat_owners").fetchall()
    for bot_token, user_id in owners_list:
        if bot_token not in bot_owners:
            bot_owners[bot_token] = set()
        bot_owners[bot_token].add(user_id)

    handlers_list = c.execute("SELECT bot_token, user_id FROM chat_handlers").fetchall()
    for bot_token, user_id in handlers_list:
        if bot_token not in bot_handlers:
            bot_handlers[bot_token] = set()
        bot_handlers[bot_token].add(user_id)

    ping_list = c.execute("SELECT bot_token, interval_min, enabled FROM ping_settings").fetchall()
    for bot_token, interval_min, enabled in ping_list:
        ping_settings[bot_token] = {
            "interval_min": int(interval_min) if interval_min else 30,
            "enabled": bool(enabled),
            "task": None,
        }

    conn.close()

    # Load persisted receipts
    db_load_receipts()

    return bots_list


async def secret_chat_edited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mirror text/caption edits from the sender to all forwarded copies.

    Receipts are skipped — they have their own edit flow via the inline button.
    """
    msg = update.edited_message
    if not msg:
        return
    user_id = msg.from_user.id
    bot_token = context.application.bot.token

    if user_id in banned_users.get(bot_token, set()):
        return
    if user_id not in user_pseudonyms.get(bot_token, {}):
        return

    sender_key = (user_id, msg.message_id)
    original = message_map.get(bot_token, {}).get(sender_key)
    if not original:
        return  # not a tracked message
    if "receipt_id" in original:
        return  # receipt edits go through the dedicated UI
    sent_to = original.get("sent_to")
    if not sent_to:
        return

    pseudonym = user_pseudonyms[bot_token][user_id]
    new_text = msg.text or msg.caption or ""

    # Update sender-side cache so future replies see the new content.
    original["text"] = new_text

    is_text = bool(msg.text)
    formatted_text = f"{pseudonym}: {new_text}" if is_text else f"{pseudonym}: {new_text}"

    async def _edit_one(uid, target_msg_id):
        try:
            async def _do():
                if is_text:
                    return await context.bot.edit_message_text(
                        chat_id=uid, message_id=target_msg_id, text=formatted_text
                    )
                else:
                    return await context.bot.edit_message_caption(
                        chat_id=uid, message_id=target_msg_id, caption=formatted_text
                    )
            await send_with_retry(_do, uid, bot_token)
        except Exception as e:
            logger.error(f"Error mirroring edit to {uid}: {e}")

    await gather_safe(*[_edit_one(uid, mid) for uid, mid in sent_to.items()])


def setup_secret_bot_handlers(app):
    app.add_handler(CommandHandler("start", secret_chat_start))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("change_name", change_name_command))
    app.add_handler(CommandHandler("setshift", setshift_command))
    app.add_handler(CommandHandler("op", op_command))
    app.add_handler(CommandHandler("deop", deop_command))
    app.add_handler(CommandHandler("kick", kick_command))
    app.add_handler(CommandHandler("chrq", chrq_command))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.UpdateType.EDITED_MESSAGE, secret_chat_photo))
    app.add_handler(CallbackQueryHandler(debug_callback_handler), group=0)
    app.add_handler(CallbackQueryHandler(receipt_callback), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE, secret_chat_message))
    app.add_handler(MessageHandler(
        (filters.VIDEO | filters.VIDEO_NOTE | filters.VOICE | filters.AUDIO | filters.Document.ALL)
        & ~filters.UpdateType.EDITED_MESSAGE, secret_chat_media))
    # Propagate text/caption edits to all forwarded copies so everyone sees the latest.
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, secret_chat_edited))


def _make_request():
    import socket as _socket
    # Enable TCP keepalive at socket level so the kernel detects dead
    # connections and tears them down instead of leaving CLOSE-WAIT FDs.
    # Idle 30s -> probe every 10s -> close after 3 failed probes (~60s).
    socket_options = (
        (_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1),
        (_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE, 30),
        (_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL, 10),
        (_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT, 3),
    )
    return HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=10.0,
        connection_pool_size=16,
        socket_options=socket_options,
    )


async def restore_bots(app):
    bots_list = db_load_all()
    for token, username, admin_user_id, geo in bots_list:
        try:
            new_app = Application.builder().token(token).request(_make_request()).build()
            setup_secret_bot_handlers(new_app)

            bot_admins[token] = admin_user_id
            bot_geos[token] = geo
            if token not in user_pseudonyms:
                user_pseudonyms[token] = {}

            created_bots[token] = {
                "token": token,
                "application": new_app,
                "username": username
            }

            await new_app.initialize()
            await new_app.start()
            await new_app.updater.start_polling(poll_interval=2.0, timeout=30, allowed_updates=Update.ALL_TYPES)
            # Resume ping task if it was enabled at last shutdown.
            start_ping_task(token)
            logger.info(f"Restored bot @{username} (geo: {geo})")
        except Exception as e:
            logger.error(f"Failed to restore bot @{username}: {e}")


async def start_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in WHITELIST:
        await update.message.reply_text("⛔ Доступ запрещён")
        return

    await update.message.reply_text(
        "👋 Добро пожаловать в менеджер секретных чатов\n\n"
        "Команды:\n"
        "/create_secret_chat - Создать нового бота для секретного чата\n"
        "/add <user_id> - Добавить пользователя в whitelist\n"
        "/msg <текст> - Массовая рассылка по всем ботам\n"
        "Отправьте токен бота от @BotFather, чтобы создать нового бота"
    )


async def create_secret_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in WHITELIST:
        await update.message.reply_text("⛔ Доступ запрещён")
        return

    await update.message.reply_text(
        "📤 Отправьте мне токен бота от @BotFather\n"
        "Пример: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
    )


admin_pending_tokens = {}


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"[DEBUG] admin msg from {user_id} (whitelist={WHITELIST}): {update.message.text[:30] if update.message.text else 'N/A'}")
    if user_id not in WHITELIST:
        logger.info(f"[DEBUG] admin msg: {user_id} not in WHITELIST, skip")
        return

    text = update.message.text

    if ":" in text and len(text) > 20:
        try:
            response = requests.get(f"https://api.telegram.org/bot{text}/getMe")
            if response.status_code == 200:
                bot_info = response.json()["result"]
                bot_username = bot_info["username"]

                admin_pending_tokens[user_id] = {
                    "token": text,
                    "username": bot_username
                }

                keyboard = [
                    [InlineKeyboardButton("🇦🇷 Аргентина (ARS)", callback_data="geo_argentina")],
                    [InlineKeyboardButton("🇧🇴 Боливия (BOB)", callback_data="geo_bolivia")],
                    [InlineKeyboardButton("🇨🇱 Чили (CLP)", callback_data="geo_chile")],
                    [InlineKeyboardButton("🇲🇽 Мексика (MXN)", callback_data="geo_mexico")],
                    [InlineKeyboardButton("🇨🇴 Колумбия (COP)", callback_data="geo_colombia")],
                    [InlineKeyboardButton("🇵🇪 Перу (PEN)", callback_data="geo_peru")],
                    [InlineKeyboardButton("🇪🇨 Эквадор (USD)", callback_data="geo_ecuador")],
                    [InlineKeyboardButton("🇻🇪 Венесуэла (VES)", callback_data="geo_venezuela")],
                    [InlineKeyboardButton("🇹🇷 Турция (TRY)", callback_data="geo_turkey")],
                    [InlineKeyboardButton("🇳🇬 Нигерия (NGN)", callback_data="geo_nigeria")],
                    [InlineKeyboardButton("🇲🇦 Марокко (MAD)", callback_data="geo_morocco")],
                    [InlineKeyboardButton("🇨🇷 Коста-Рика (CRC)", callback_data="geo_costa_rica")],
                    [InlineKeyboardButton("🇮🇩 Индонезия (IDR)", callback_data="geo_indonesia")],
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await update.message.reply_text(
                    f"Бот @{bot_username} найден!\n\n"
                    f"Выберите гео для этого бота:",
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text("❌ Неверный токен")
        except Exception as e:
            logger.error(f"Error validating token: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")


async def admin_geo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    logger.info(f"[DEBUG] geo callback: data={query.data} user={user_id} pending={list(admin_pending_tokens.keys())}")

    if not query.data.startswith("geo_"):
        logger.info(f"[DEBUG] geo callback: data not geo_, skip")
        return

    if user_id not in admin_pending_tokens:
        logger.info(f"[DEBUG] geo callback: user not in pending, answering alert")
        await query.answer("Нет ожидающего токена", show_alert=True)
        return

    logger.info(f"[DEBUG] geo callback: starting bot creation flow")

    geo = query.data.replace("geo_", "")
    pending = admin_pending_tokens.pop(user_id)
    token = pending["token"]
    bot_username = pending["username"]

    try:
        new_app = Application.builder().token(token).request(_make_request()).build()
        setup_secret_bot_handlers(new_app)

        user_pseudonyms[token] = {}
        bot_admins[token] = user_id
        bot_geos[token] = geo

        created_bots[token] = {
            "token": token,
            "application": new_app,
            "username": bot_username
        }

        db_add_bot(token, bot_username, user_id, geo)
        create_bot_sheet(bot_username)

        await new_app.initialize()
        await new_app.start()
        await new_app.updater.start_polling(poll_interval=2.0, timeout=30, allowed_updates=Update.ALL_TYPES)
        start_ping_task(token)

        currency = GEO_CURRENCIES.get(geo, "ARS")
        geo_name = {
            "argentina": "Аргентина", "bolivia": "Боливия", "chile": "Чили",
            "mexico": "Мексика", "colombia": "Колумбия", "peru": "Перу",
            "ecuador": "Эквадор", "venezuela": "Венесуэла",
            "turkey": "Турция", "nigeria": "Нигерия",
            "morocco": "Марокко", "costa_rica": "Коста-Рика",
            "indonesia": "Индонезия",
        }.get(geo, geo)

        await query.edit_message_text(
            f"✅ Бот секретного чата создан!\n\n"
            f"Бот: @{bot_username}\n"
            f"Гео: {geo_name}\n"
            f"Валюта: {currency}\n\n"
            f"Пользователи теперь могут присоединиться и выбрать псевдоним"
        )
    except Exception as e:
        logger.error(f"Error creating bot: {e}")
        await query.edit_message_text(f"❌ Ошибка: {str(e)}")


async def secret_chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if user_id in banned_users.get(bot_token, set()):
        await update.message.reply_text("❌ Вы заблокированы в этом чате")
        return

    if context.args:
        invite_code = context.args[0]

        if invite_code in invite_links:
            invite_data = invite_links[invite_code]

            if invite_data["bot_token"] != bot_token:
                await update.message.reply_text("❌ Недействительная ссылка-приглашение")
                return

            if invite_data["used"]:
                await update.message.reply_text("❌ Эта ссылка-приглашение уже была использована")
                return

            if invite_data["expires_at"] < time.time():
                await update.message.reply_text("❌ Срок действия ссылки-приглашения истёк")
                return

            invite_links[invite_code]["used"] = True
            db_mark_invite_used(invite_code)

            await update.message.reply_text(
                "✅ Добро пожаловать в секретный чат!\n\n"
                "Выберите свой псевдоним — отправьте любое имя"
            )

            # Notification is deferred to the moment the user picks a pseudonym
            # (handled in secret_chat_message when a new pseudonym is set).
            return
        else:
            await update.message.reply_text("❌ Недействительная ссылка-приглашение")
            return

    if bot_token in user_pseudonyms and user_id in user_pseudonyms[bot_token]:
        pseudonym = user_pseudonyms[bot_token][user_id]

        await update.message.reply_text(
            f"👋 С возвращением!\n\n"
            f"Ваш псевдоним: {pseudonym}\n\n"
            f"Отправьте фото — оно будет определено как чек",
            reply_markup=keyboard_for_user(bot_token, user_id)
        )
    else:
        is_admin = is_chat_admin(bot_token, user_id)
        if not is_admin:
            await update.message.reply_text(
                "❌ Это приватный чат. Для входа нужна ссылка-приглашение.\n\n"
                "Попросите ссылку у администратора чата."
            )
        else:
            await update.message.reply_text(
                "👋 Добро пожаловать в секретный чат!\n\n"
                "Вы являетесь администратором этого чата.\n\n"
                "Выберите свой псевдоним — отправьте любое имя"
            )


def get_user_state(bot_token, user_id):
    key = f"{bot_token}_{user_id}"
    return user_states.get(key)


def set_user_state(bot_token, user_id, state):
    key = f"{bot_token}_{user_id}"
    if state is None:
        user_states.pop(key, None)
    else:
        user_states[key] = state


async def secret_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token
    text = update.message.text

    if user_id in banned_users.get(bot_token, set()):
        await update.message.reply_text("❌ Вы заблокированы в этом чате")
        return

    if bot_token not in user_pseudonyms:
        user_pseudonyms[bot_token] = {}

    if user_id not in user_pseudonyms[bot_token]:
        if is_pseudonym_taken(bot_token, text):
            await update.message.reply_text(
                "❌ Этот псевдоним уже занят. Выберите другой."
            )
            return
        user_pseudonyms[bot_token][user_id] = text
        db_add_pseudonym(bot_token, user_id, text)

        await update.message.reply_text(
            f"✅ Ваш псевдоним установлен: {text}\n\n"
            f"Теперь вы можете отправлять сообщения в секретный чат!",
            reply_markup=keyboard_for_user(bot_token, user_id)
        )

        # Notify everyone else in the chat that a new participant has joined.
        # Admins additionally see the numeric user ID; others see only the pseudonym
        # so non-admin roles can't learn participant identities.
        try:
            admin_ids = set()
            if bot_token in bot_admins:
                admin_ids.add(bot_admins[bot_token])
            admin_ids.update(bot_chat_admins.get(bot_token, set()))
            other_ids = set(user_pseudonyms.get(bot_token, {}).keys()) - admin_ids
            other_ids.discard(user_id)  # don't notify the joining user themselves
            admin_ids.discard(user_id)

            admin_text = f"🔔 Новый участник: {text}\nID: {user_id}"
            generic_text = f"🔔 Новый участник: {text}"

            async def _notify_admin(uid):
                async def _do():
                    return await context.bot.send_message(chat_id=uid, text=admin_text)
                await send_with_retry(_do, uid, bot_token)

            async def _notify_other(uid):
                async def _do():
                    return await context.bot.send_message(chat_id=uid, text=generic_text)
                await send_with_retry(_do, uid, bot_token)

            await gather_safe(*[_notify_admin(uid) for uid in admin_ids])
            await gather_safe(*[_notify_other(uid) for uid in other_ids])
        except Exception as e:
            logger.error(f"Failed to broadcast join notification: {e}")
        return

    is_admin = is_chat_admin(bot_token, user_id)
    is_owner = is_chat_owner(bot_token, user_id)
    is_handler = is_chat_handler(bot_token, user_id)

    if text == "📷 Отправить фото":
        set_user_state(bot_token, user_id, {"mode": "send_photo"})
        await update.message.reply_text(
            "📷 Отправьте фото, и оно будет переслано как обычное фото (не чек).\n"
            "Если передумали — напишите «отмена».",
            reply_markup=keyboard_for_user(bot_token, user_id)
        )
        return

    if text == "💰 Итог за смену":
        currency = get_bot_currency(bot_token)
        if is_working_hours(bot_token):
            daily_total = db_get_daily_total(bot_token)
            shift = bot_shifts.get(bot_token, {"start": 0, "end": 23})
            await update.message.reply_text(
                f"💰 Итого за смену: {format_amount(daily_total)} {currency}\n"
                f"Смена: {format_shift_window(bot_token)} МСК",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
        else:
            shift = bot_shifts.get(bot_token, {"start": 0, "end": 23})
            await update.message.reply_text(
                f"⏸ Сейчас нерабочее время\n"
                f"Смена: {format_shift_window(bot_token)} МСК",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
        return

    if text == "📋 Реквизиты":
        reqs = bot_requisites.get(bot_token)
        if reqs:
            req_text = reqs.get("text") or ""
            if reqs.get("photo_id"):
                try:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=reqs["photo_id"],
                        caption=f"📋 Актуальные реквизиты:\n\n{req_text}"
                    )
                except Exception as e:
                    logger.error(f"Error sending requisites photo: {e}")
                    if req_text:
                        await update.message.reply_text(f"📋 Актуальные реквизиты:\n\n{req_text}")
                    else:
                        await update.message.reply_text("📋 Ошибка при загрузке реквизитов. Попросите админа обновить их.")
            else:
                if req_text:
                    await update.message.reply_text(f"📋 Актуальные реквизиты:\n\n{req_text}")
                else:
                    await update.message.reply_text("📋 Реквизиты пустые. Попросите админа обновить их.")
        else:
            await update.message.reply_text("📋 Реквизиты ещё не установлены")
        return

    if text == "✏️ Сменить ник":
        set_user_state(bot_token, user_id, {"mode": "waiting_new_name"})
        await update.message.reply_text("Введите новый никнейм:")
        return

    if text == "🔗 Инвайт" and is_admin:
        set_user_state(bot_token, user_id, {"mode": "waiting_invite_minutes"})
        await update.message.reply_text("Введите время действия ссылки в минутах (или 0 для бессрочной):")
        return

    if text == "⏰ Смена" and is_admin:
        set_user_state(bot_token, user_id, {"mode": "setshift_start"})
        await update.message.reply_text("Введите час начала смены (0-23, МСК):")
        return

    if text == "📝 Изм. реквизиты" and (is_admin or is_handler):
        set_user_state(bot_token, user_id, {"mode": "waiting_requisites"})
        await update.message.reply_text("📋 Отправьте новые реквизиты (текст или фото с подписью):")
        return

    if text == "👑 Назначить админа" and is_admin:
        set_user_state(bot_token, user_id, {"mode": "waiting_op_id"})
        candidates = format_pseudonym_picklist(
            bot_token, lambda uid: not is_chat_admin(bot_token, uid)
        )
        await update.message.reply_text(
            f"Кандидаты:\n{candidates}\n\n"
            f"Введите псевдоним участника для назначения админом\n"
            f"или напишите «отмена» для выхода.",
            parse_mode="HTML"
        )
        return

    if text == "🚫 Снять админа" and is_admin:
        set_user_state(bot_token, user_id, {"mode": "waiting_deop_id"})
        creator_id = bot_admins.get(bot_token)
        candidates = format_pseudonym_picklist(
            bot_token,
            lambda uid: uid in bot_chat_admins.get(bot_token, set()) and uid != creator_id
        )
        await update.message.reply_text(
            f"Текущие админы (кроме создателя):\n{candidates}\n\n"
            f"Введите псевдоним участника для снятия прав админа\n"
            f"или напишите «отмена» для выхода.",
            parse_mode="HTML"
        )
        return

    if text == "👢 Кикнуть" and (is_admin or is_owner):
        set_user_state(bot_token, user_id, {"mode": "waiting_kick_id"})
        creator_id = bot_admins.get(bot_token)
        if is_admin:
            # Admin sees everyone except the creator.
            candidates = format_pseudonym_picklist(
                bot_token, lambda uid: uid != creator_id
            )
        else:
            # Owner cannot kick admins or other owners.
            admin_ids = bot_chat_admins.get(bot_token, set()) | ({creator_id} if creator_id else set())
            owner_ids = bot_owners.get(bot_token, set())
            protected = admin_ids | owner_ids
            candidates = format_pseudonym_picklist(
                bot_token, lambda uid: uid not in protected
            )
        await update.message.reply_text(
            f"Участники чата:\n{candidates}\n\n"
            f"Введите псевдоним участника для исключения\n"
            f"или напишите «отмена» для выхода.",
            parse_mode="HTML"
        )
        return

    if text == "♻️ Разбан" and is_admin:
        banned_set = banned_users.get(bot_token, set())
        if not banned_set:
            await update.message.reply_text(
                "📭 Список забаненных пуст",
                reply_markup=get_main_keyboard(is_admin)
            )
            return
        banned_lines = "\n".join(f"  • <code>{uid}</code>" for uid in sorted(banned_set))
        set_user_state(bot_token, user_id, {"mode": "waiting_unban_id"})
        await update.message.reply_text(
            f"♻️ Разбан\n\n"
            f"Забанены ({len(banned_set)}):\n"
            f"{banned_lines}\n\n"
            f"Отправьте ID пользователя для разбана,\n"
            f"или напишите «отмена» для выхода.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML"
        )
        return

    if text == "🔔 Уведомления чеков" and is_admin:
        watchers = receipt_watchers.get(bot_token, set())
        watcher_names = [
            user_pseudonyms.get(bot_token, {}).get(wid, "Без ника")
            for wid in watchers
        ]
        watcher_list = (
            "\n".join(f"  • <code>{html.escape(n)}</code>" for n in watcher_names)
            if watcher_names else "  Пока никого"
        )
        # Show handlers separately (they are notified automatically).
        handler_ids = bot_handlers.get(bot_token, set())
        handler_names = [
            user_pseudonyms.get(bot_token, {}).get(hid, "Без ника")
            for hid in handler_ids
        ]
        handler_list = (
            "\n".join(f"  • <code>{html.escape(n)}</code>" for n in handler_names)
            if handler_names else "  Никого"
        )
        members_list = format_pseudonym_picklist(bot_token)
        set_user_state(bot_token, user_id, {"mode": "waiting_watcher_action"})
        await update.message.reply_text(
            f"🔔 Уведомления о чеках\n\n"
            f"Доп. подписчики:\n"
            f"{watcher_list}\n\n"
            f"Обработчики (всегда получают):\n"
            f"{handler_list}\n\n"
            f"Все участники чата:\n"
            f"{members_list}\n\n"
            f"Отправьте псевдоним участника чтобы добавить/убрать его,\n"
            f"или напишите «отмена» для выхода.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML"
        )
        return

    # Owner version of the watchers menu — pseudonyms only, hide admin watchers,
    # owners cannot toggle anyone with admin role.
    if text == "🔔 Уведомления чеков" and is_owner and not is_admin:
        watchers = receipt_watchers.get(bot_token, set())
        # Strip admins from the visible list — owners must not see admin IDs/names here.
        visible = [
            user_pseudonyms.get(bot_token, {}).get(wid, "Без ника")
            for wid in watchers
            if not is_chat_admin(bot_token, wid)
        ]
        watcher_list = (
            "\n".join(f"  • <code>{html.escape(n)}</code>" for n in visible)
            if visible else "  Пока никого"
        )
        # Show available non-admin participants so owner knows whom to type.
        members = sorted(
            name for uid, name in user_pseudonyms.get(bot_token, {}).items()
            if not is_chat_admin(bot_token, uid)
        )
        members_list = (
            "\n".join(f"  • <code>{html.escape(n)}</code>" for n in members)
            if members else "  Никого"
        )
        set_user_state(bot_token, user_id, {"mode": "waiting_owner_watcher_action"})
        await update.message.reply_text(
            f"🔔 Уведомления о чеках\n\n"
            f"Доп. подписчики:\n"
            f"{watcher_list}\n\n"
            f"Доступные участники:\n"
            f"{members_list}\n\n"
            f"Отправьте псевдоним участника чтобы добавить/убрать его,\n"
            f"или напишите «отмена» для выхода.",
            reply_markup=keyboard_for_user(bot_token, user_id),
            parse_mode="HTML"
        )
        return

    if text == "🔗 Постоянный инвайт" and is_owner and not is_admin:
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        expires_at = time.time() + (365 * 24 * 60 * 60)
        invite_links[code] = {"bot_token": bot_token, "expires_at": expires_at, "used": False}
        db_add_invite(code, bot_token, expires_at, False)
        bot_username = context.bot.username
        link = f"https://t.me/{bot_username}?start={code}"
        await update.message.reply_text(
            f"🔗 Постоянная ссылка-приглашение:\n{link}",
            reply_markup=keyboard_for_user(bot_token, user_id)
        )
        return

    if text == "⏰ Пинг чеков" and (is_admin or is_handler):
        settings = ping_settings.get(bot_token) or {"interval_min": 30, "enabled": False, "task": None}
        ping_settings.setdefault(bot_token, settings)
        state_str = "🟢 ВКЛ" if settings.get("enabled") else "🔴 ВЫКЛ"
        await update.message.reply_text(
            f"⏰ Пинг неапрувнутых чеков\n\n"
            f"Состояние: {state_str}\n"
            f"Интервал: {settings.get('interval_min', 30)} мин.\n\n"
            f"Команды:\n"
            f"  • вкл — включить\n"
            f"  • выкл — выключить\n"
            f"  • интервал N — задать интервал N минут\n"
            f"  • отмена — выйти",
            reply_markup=keyboard_for_user(bot_token, user_id)
        )
        set_user_state(bot_token, user_id, {"mode": "waiting_ping_command"})
        return

    if text == "🛡 Назначить Owner" and is_admin:
        set_user_state(bot_token, user_id, {"mode": "waiting_owner_op_id"})
        candidates = format_pseudonym_picklist(
            bot_token, lambda uid: not is_chat_owner(bot_token, uid)
        )
        await update.message.reply_text(
            f"Кандидаты:\n{candidates}\n\n"
            f"Введите псевдоним участника для назначения роли Owner\n"
            f"или напишите «отмена» для выхода.",
            parse_mode="HTML"
        )
        return

    if text == "🛡 Снять Owner" and is_admin:
        set_user_state(bot_token, user_id, {"mode": "waiting_owner_deop_id"})
        candidates = format_pseudonym_picklist(
            bot_token, lambda uid: is_chat_owner(bot_token, uid)
        )
        await update.message.reply_text(
            f"Текущие Owners:\n{candidates}\n\n"
            f"Введите псевдоним участника для снятия роли Owner\n"
            f"или напишите «отмена» для выхода.",
            parse_mode="HTML"
        )
        return

    if text == "🎯 Назначить Обработчика" and is_admin:
        set_user_state(bot_token, user_id, {"mode": "waiting_handler_op_id"})
        candidates = format_pseudonym_picklist(
            bot_token, lambda uid: not is_chat_handler(bot_token, uid)
        )
        await update.message.reply_text(
            f"Кандидаты:\n{candidates}\n\n"
            f"Введите псевдоним участника для назначения роли Обработчика\n"
            f"или напишите «отмена» для выхода.",
            parse_mode="HTML"
        )
        return

    if text == "🎯 Снять Обработчика" and is_admin:
        set_user_state(bot_token, user_id, {"mode": "waiting_handler_deop_id"})
        candidates = format_pseudonym_picklist(
            bot_token, lambda uid: is_chat_handler(bot_token, uid)
        )
        await update.message.reply_text(
            f"Текущие Обработчики:\n{candidates}\n\n"
            f"Введите псевдоним участника для снятия роли Обработчика\n"
            f"или напишите «отмена» для выхода.",
            parse_mode="HTML"
        )
        return

    if text == "📋 Лист участников" and is_admin:
        users = user_pseudonyms.get(bot_token, {})
        if not users:
            await update.message.reply_text("📋 Участников пока нет", reply_markup=get_main_keyboard(is_admin))
            return
        lines = ["📋 Участники чата:\n"]
        for uid, pseudonym_name in users.items():
            admin_mark = " 👑" if is_chat_admin(bot_token, uid) else ""
            lines.append(f"  • <code>{html.escape(pseudonym_name)}</code> | ID: <code>{uid}</code>{admin_mark}")
        lines.append(f"\nВсего: {len(users)}")
        await update.message.reply_text("\n".join(lines), reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")
        return

    state = get_user_state(bot_token, user_id)

    if state and state.get("mode") == "waiting_new_name":
        if is_pseudonym_taken(bot_token, text, exclude_user_id=user_id):
            await update.message.reply_text("❌ Этот псевдоним уже занят. Выберите другой.")
            return
        old_pseudonym = user_pseudonyms[bot_token][user_id]
        user_pseudonyms[bot_token][user_id] = text
        db_update_pseudonym(bot_token, user_id, text)
        set_user_state(bot_token, user_id, None)
        await update.message.reply_text(f"✅ Никнейм изменён: {old_pseudonym} → {text}", reply_markup=keyboard_for_user(bot_token, user_id))
        return

    # User pressed "📷 Отправить фото" but typed something instead of sending a photo.
    # Treat "отмена" as cancellation; otherwise fall through to normal text forwarding.
    if state and state.get("mode") == "send_photo":
        if text.lower().strip() in ("отмена", "cancel", "отменить", "/cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text(
                "✅ Отправка фото отменена",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
            return
        # Any other text — clear stale state so the message gets forwarded normally
        # rather than silently waiting for a photo forever.
        set_user_state(bot_token, user_id, None)

    if state and state.get("mode") == "waiting_invite_minutes":
        try:
            minutes = int(text)
        except ValueError:
            await update.message.reply_text("❌ Введите число!")
            return
        set_user_state(bot_token, user_id, None)
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        if minutes > 0:
            expires_at = time.time() + (minutes * 60)
        else:
            expires_at = time.time() + (365 * 24 * 60 * 60)
        invite_links[code] = {"bot_token": bot_token, "expires_at": expires_at, "used": False}
        db_add_invite(code, bot_token, expires_at, False)
        bot_username = context.bot.username
        link = f"https://t.me/{bot_username}?start={code}"
        await update.message.reply_text(f"🔗 Ссылка-приглашение:\n{link}", reply_markup=get_main_keyboard(is_admin))
        return

    if state and state.get("mode") == "waiting_op_id":
        if text.lower().strip() in ("отмена", "cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text("✅ Отменено", reply_markup=get_main_keyboard(is_admin))
            return
        set_user_state(bot_token, user_id, None)
        target_id, target_name = find_user_by_pseudonym(bot_token, text)
        if target_id is None:
            await update.message.reply_text("❌ Участник с таким псевдонимом не найден", reply_markup=get_main_keyboard(is_admin))
            return
        if is_chat_admin(bot_token, target_id):
            await update.message.reply_text("ℹ️ Этот пользователь уже является админом", reply_markup=get_main_keyboard(is_admin))
            return
        if bot_token not in bot_chat_admins:
            bot_chat_admins[bot_token] = set()
        bot_chat_admins[bot_token].add(target_id)
        db_add_chat_admin(bot_token, target_id)
        await update.message.reply_text(f"✅ {target_name} назначен админом", reply_markup=get_main_keyboard(is_admin))
        await notify_role_change(context.bot, target_id, "Админ", granted=True)
        return

    if state and state.get("mode") == "waiting_deop_id":
        if text.lower().strip() in ("отмена", "cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text("✅ Отменено", reply_markup=get_main_keyboard(is_admin))
            return
        set_user_state(bot_token, user_id, None)
        target_id, target_name = find_user_by_pseudonym(bot_token, text)
        if target_id is None:
            await update.message.reply_text("❌ Участник с таким псевдонимом не найден", reply_markup=get_main_keyboard(is_admin))
            return
        if bot_token in bot_admins and bot_admins[bot_token] == target_id:
            await update.message.reply_text("❌ Нельзя снять права создателя чата", reply_markup=get_main_keyboard(is_admin))
            return
        if target_id not in bot_chat_admins.get(bot_token, set()):
            await update.message.reply_text("ℹ️ Этот пользователь не является админом", reply_markup=get_main_keyboard(is_admin))
            return
        bot_chat_admins[bot_token].discard(target_id)
        db_remove_chat_admin(bot_token, target_id)
        await update.message.reply_text(f"✅ {target_name} больше не админ", reply_markup=get_main_keyboard(is_admin))
        await notify_role_change(context.bot, target_id, "Админ", granted=False)
        return

    if state and state.get("mode") == "waiting_unban_id":
        if text.lower().strip() in ("отмена", "cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text("✅ Готово", reply_markup=get_main_keyboard(is_admin))
            return
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом")
            return
        set_user_state(bot_token, user_id, None)
        if target_id not in banned_users.get(bot_token, set()):
            await update.message.reply_text(
                "ℹ️ Этот пользователь не находится в бане",
                reply_markup=get_main_keyboard(is_admin)
            )
            return
        banned_users[bot_token].discard(target_id)
        db_unban_user(bot_token, target_id)
        await update.message.reply_text(
            f"♻️ Пользователь {target_id} разбанен.\n"
            f"Чтобы вернуться в чат, ему нужна новая инвайт-ссылка.",
            reply_markup=get_main_keyboard(is_admin)
        )
        return

    if state and state.get("mode") == "waiting_kick_id":
        kbd = keyboard_for_user(bot_token, user_id)
        if text.lower().strip() in ("отмена", "cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text("✅ Отменено", reply_markup=kbd)
            return
        set_user_state(bot_token, user_id, None)
        target_id, target_name = find_user_by_pseudonym(bot_token, text)
        if target_id is None:
            await update.message.reply_text("❌ Участник с таким псевдонимом не найден", reply_markup=kbd)
            return
        if bot_token in bot_admins and bot_admins[bot_token] == target_id:
            await update.message.reply_text("❌ Нельзя кикнуть создателя чата", reply_markup=kbd)
            return
        # Owner cannot kick admins / other owners.
        if is_owner and not is_admin:
            if is_chat_admin(bot_token, target_id) or is_chat_owner(bot_token, target_id):
                await update.message.reply_text("❌ Owner не может кикать админов или других Owner-ов", reply_markup=kbd)
                return
        # Full purge: pseudonym + admin/owner/handler roles + watcher entry, in memory and DB.
        _purge_user_from_state(bot_token, target_id, drop_pseudonym=True)
        if bot_token not in banned_users:
            banned_users[bot_token] = set()
        banned_users[bot_token].add(target_id)
        db_ban_user(bot_token, target_id)
        try:
            await context.bot.send_message(chat_id=target_id, text="❌ Вы были исключены из этого чата")
        except Exception:
            pass
        await update.message.reply_text(f"✅ {target_name} был исключён и заблокирован", reply_markup=kbd)
        return

    if state and state.get("mode") == "waiting_watcher_action":
        if text.lower() in ("отмена", "cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text("✅ Готово", reply_markup=get_main_keyboard(is_admin))
            return
        target_id, target_name = find_user_by_pseudonym(bot_token, text)
        if target_id is None:
            await update.message.reply_text(
                "❌ Участник с таким псевдонимом не найден",
                reply_markup=get_main_keyboard(is_admin)
            )
            set_user_state(bot_token, user_id, None)
            return
        if bot_token not in receipt_watchers:
            receipt_watchers[bot_token] = set()
        if target_id in receipt_watchers[bot_token]:
            receipt_watchers[bot_token].discard(target_id)
            db_remove_receipt_watcher(bot_token, target_id)
            await update.message.reply_text(f"🔕 {target_name} убран из уведомлений о чеках", reply_markup=get_main_keyboard(is_admin))
        else:
            receipt_watchers[bot_token].add(target_id)
            db_add_receipt_watcher(bot_token, target_id)
            await update.message.reply_text(f"🔔 {target_name} добавлен в уведомления о чеках", reply_markup=get_main_keyboard(is_admin))
        set_user_state(bot_token, user_id, None)
        return

    # Owner-restricted variant: pseudonym only, can't touch admins.
    if state and state.get("mode") == "waiting_owner_watcher_action":
        if text.lower() in ("отмена", "cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text("✅ Готово", reply_markup=keyboard_for_user(bot_token, user_id))
            return
        # Find user by case-insensitive pseudonym match. Skip admins so owners
        # cannot infer or alter their identity through this menu.
        target_pseudonym = text.strip()
        candidates = [
            (uid, name) for uid, name in user_pseudonyms.get(bot_token, {}).items()
            if name.lower() == target_pseudonym.lower() and not is_chat_admin(bot_token, uid)
        ]
        if not candidates:
            await update.message.reply_text(
                "❌ Участник с таким псевдонимом не найден",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
            set_user_state(bot_token, user_id, None)
            return
        if len(candidates) > 1:
            await update.message.reply_text(
                "⚠️ Несколько участников с таким псевдонимом — попросите админа сделать это вручную.",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
            set_user_state(bot_token, user_id, None)
            return
        target_id, target_name = candidates[0]
        if bot_token not in receipt_watchers:
            receipt_watchers[bot_token] = set()
        if target_id in receipt_watchers[bot_token]:
            receipt_watchers[bot_token].discard(target_id)
            db_remove_receipt_watcher(bot_token, target_id)
            await update.message.reply_text(
                f"🔕 {target_name} убран из уведомлений о чеках",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
        else:
            receipt_watchers[bot_token].add(target_id)
            db_add_receipt_watcher(bot_token, target_id)
            await update.message.reply_text(
                f"🔔 {target_name} добавлен в уведомления о чеках",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
        set_user_state(bot_token, user_id, None)
        return

    if state and state.get("mode") in ("waiting_owner_op_id", "waiting_owner_deop_id"):
        if text.lower().strip() in ("отмена", "cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text("✅ Отменено", reply_markup=get_main_keyboard(is_admin))
            return
        mode = state["mode"]
        set_user_state(bot_token, user_id, None)
        target_id, target_name = find_user_by_pseudonym(bot_token, text)
        if target_id is None:
            await update.message.reply_text("❌ Участник с таким псевдонимом не найден", reply_markup=get_main_keyboard(is_admin))
            return
        bot_owners.setdefault(bot_token, set())
        if mode == "waiting_owner_op_id":
            if target_id in bot_owners[bot_token]:
                await update.message.reply_text("ℹ️ Этот пользователь уже Owner", reply_markup=get_main_keyboard(is_admin))
                return
            bot_owners[bot_token].add(target_id)
            db_add_chat_owner(bot_token, target_id)
            await update.message.reply_text(f"🛡 {target_name} назначен Owner", reply_markup=get_main_keyboard(is_admin))
            await notify_role_change(context.bot, target_id, "Owner", granted=True)
        else:
            if target_id not in bot_owners[bot_token]:
                await update.message.reply_text("ℹ️ Этот пользователь не Owner", reply_markup=get_main_keyboard(is_admin))
                return
            bot_owners[bot_token].discard(target_id)
            db_remove_chat_owner(bot_token, target_id)
            await update.message.reply_text(f"🛡 {target_name} больше не Owner", reply_markup=get_main_keyboard(is_admin))
            await notify_role_change(context.bot, target_id, "Owner", granted=False)
        return

    if state and state.get("mode") in ("waiting_handler_op_id", "waiting_handler_deop_id"):
        if text.lower().strip() in ("отмена", "cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text("✅ Отменено", reply_markup=get_main_keyboard(is_admin))
            return
        mode = state["mode"]
        set_user_state(bot_token, user_id, None)
        target_id, target_name = find_user_by_pseudonym(bot_token, text)
        if target_id is None:
            await update.message.reply_text("❌ Участник с таким псевдонимом не найден", reply_markup=get_main_keyboard(is_admin))
            return
        bot_handlers.setdefault(bot_token, set())
        if mode == "waiting_handler_op_id":
            if target_id in bot_handlers[bot_token]:
                await update.message.reply_text("ℹ️ Этот пользователь уже Обработчик", reply_markup=get_main_keyboard(is_admin))
                return
            bot_handlers[bot_token].add(target_id)
            db_add_chat_handler(bot_token, target_id)
            await update.message.reply_text(f"🎯 {target_name} назначен Обработчиком", reply_markup=get_main_keyboard(is_admin))
            await notify_role_change(context.bot, target_id, "Обработчик", granted=True)
        else:
            if target_id not in bot_handlers[bot_token]:
                await update.message.reply_text("ℹ️ Этот пользователь не Обработчик", reply_markup=get_main_keyboard(is_admin))
                return
            bot_handlers[bot_token].discard(target_id)
            db_remove_chat_handler(bot_token, target_id)
            await update.message.reply_text(f"🎯 {target_name} больше не Обработчик", reply_markup=get_main_keyboard(is_admin))
            await notify_role_change(context.bot, target_id, "Обработчик", granted=False)
        return

    if state and state.get("mode") == "waiting_ping_command":
        cmd = text.lower().strip()
        settings = ping_settings.setdefault(bot_token, {"interval_min": 30, "enabled": False, "task": None})

        if cmd in ("отмена", "cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text("✅ Готово", reply_markup=keyboard_for_user(bot_token, user_id))
            return

        if cmd == "вкл":
            settings["enabled"] = True
            db_save_ping_settings(bot_token, settings["interval_min"], True)
            start_ping_task(bot_token)
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text(
                f"🟢 Пинг включён. Интервал: {settings['interval_min']} мин.",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
            return

        if cmd == "выкл":
            settings["enabled"] = False
            db_save_ping_settings(bot_token, settings["interval_min"], False)
            stop_ping_task(bot_token)
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text(
                "🔴 Пинг выключен.",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
            return

        if cmd.startswith("интервал"):
            parts = cmd.split()
            if len(parts) >= 2:
                try:
                    minutes = int(parts[1])
                except ValueError:
                    await update.message.reply_text("❌ Введите число минут, например: интервал 15")
                    return
                if minutes < 1 or minutes > 1440:
                    await update.message.reply_text("❌ Интервал должен быть от 1 до 1440 минут")
                    return
                settings["interval_min"] = minutes
                db_save_ping_settings(bot_token, minutes, settings["enabled"])
                if settings.get("enabled"):
                    # Restart task to apply new interval immediately
                    start_ping_task(bot_token)
                set_user_state(bot_token, user_id, None)
                await update.message.reply_text(
                    f"⏰ Интервал пинга: {minutes} мин.",
                    reply_markup=keyboard_for_user(bot_token, user_id)
                )
                return

        await update.message.reply_text(
            "❌ Не понял команду. Доступно: вкл / выкл / интервал N / отмена"
        )
        return

    if state and state.get("mode") == "waiting_requisites":
        bot_requisites[bot_token] = {"text": text, "photo_id": None}
        db_save_requisites(bot_token, text, None)
        set_user_state(bot_token, user_id, None)
        await update.message.reply_text("✅ Реквизиты обновлены!", reply_markup=get_main_keyboard(is_admin))

        async def _notify_req(uid):
            try:
                async def _do():
                    return await context.bot.send_message(chat_id=uid, text="📋 Реквизиты были обновлены")
                await send_with_retry(_do, uid, bot_token)
            except Exception:
                pass

        targets = [uid for uid in user_pseudonyms[bot_token].keys() if uid != user_id]
        await gather_safe(*[_notify_req(uid) for uid in targets])
        return

    if state and state.get("mode") in ("setshift_start", "setshift_end"):
        await handle_setshift_flow(update, context, bot_token, user_id, state, text)
        return

    if state and state.get("mode") == "waiting_edit_amount":
        clean_text = text.strip().replace(',', '.')
        try:
            new_amount = float(clean_text)
        except ValueError:
            await update.message.reply_text("❌ Неверный формат! Введите число (например 100 или 100.50)")
            return

        receipt_id = state.get("receipt_id")
        if receipt_id not in receipts:
            await update.message.reply_text("❌ Чек не найден")
            set_user_state(bot_token, user_id, None)
            return

        receipt_data = receipts[receipt_id]
        old_amount = receipt_data.get("amount")
        currency = receipt_data.get("currency") or get_bot_currency(bot_token)
        editor_name = user_pseudonyms.get(bot_token, {}).get(user_id, "Неизвестный")

        bot_app = None
        for cid, bot_info in created_bots.items():
            if bot_info["token"] == receipt_data.get("bot_token"):
                bot_app = bot_info["application"]
                break
        bot_to_use = bot_app.bot if bot_app else context.bot
        bot_username = bot_to_use.username if hasattr(bot_to_use, 'username') else "unknown"

        await run_in_thread(update_receipt_in_sheet, bot_username, old_amount, new_amount, receipt_data["pseudonym"])

        if is_working_hours(bot_token):
            diff = new_amount - old_amount
            if diff > 0:
                await run_in_thread(db_add_daily_total, bot_token, diff)
            elif diff < 0:
                await run_in_thread(db_subtract_daily_total, bot_token, abs(diff))

        receipt_data["amount"] = new_amount
        receipt_data["text"] = f"{format_amount(new_amount)} {currency}"
        receipt_data["edited_by"] = editor_name

        if is_working_hours(bot_token):
            daily_total = await run_in_thread(db_get_daily_total, bot_token)
            daily_line = f"\nИтого за смену: {format_amount(daily_total)} {currency}"
        else:
            shift = bot_shifts.get(bot_token, {"start": 0, "end": 23})
            daily_line = f"\nНерабочее время (смена: {format_shift_window(bot_token)} МСК)"

        status_text = f"Статус: Принят ✅\nИзменён: {editor_name} ({format_amount(old_amount)} → {format_amount(new_amount)})"

        action_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить", callback_data=f"receipt_edit_{receipt_id}")],
            [InlineKeyboardButton("💬 Комментарий", callback_data=f"receipt_comment_{receipt_id}")]
        ])

        comments_text = ""
        if receipt_data.get("comments"):
            comments_text = "\n\n💬 Комментарии:"
            for c in receipt_data["comments"]:
                comments_text += f"\n{c['pseudonym']}: {c['text']}"

        edit_has_media = "photo_id" in receipt_data or "document_id" in receipt_data

        async def _update_edit_msg(uid, msg_id):
            try:
                async def _do():
                    if edit_has_media:
                        new_caption = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}{comments_text}"
                        return await bot_to_use.edit_message_caption(
                            chat_id=uid, message_id=msg_id,
                            caption=new_caption, reply_markup=action_markup
                        )
                    else:
                        new_text = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}{comments_text}"
                        return await bot_to_use.edit_message_text(
                            chat_id=uid, message_id=msg_id,
                            text=new_text, reply_markup=action_markup
                        )
                await send_with_retry(_do, uid, bot_token)
            except Exception as e:
                logger.error(f"Error updating edited receipt for {uid}: {e}")

        if "message_ids" in receipt_data:
            await gather_safe(*[
                _update_edit_msg(uid, msg_id)
                for uid, msg_id in receipt_data["message_ids"].items()
            ])

        db_save_receipt(receipt_id, receipt_data)
        set_user_state(bot_token, user_id, None)
        await update.message.reply_text(f"✅ Сумма чека изменена: {format_amount(old_amount)} → {format_amount(new_amount)} {currency}")
        logger.info(f"Receipt {receipt_id} edited by {editor_name}: {old_amount} -> {new_amount}")
        return

    if state and state.get("mode") == "waiting_receipt_comment":
        receipt_id = state.get("receipt_id")
        if receipt_id not in receipts:
            await update.message.reply_text("❌ Чек не найден")
            set_user_state(bot_token, user_id, None)
            return

        receipt_data = receipts[receipt_id]
        commenter_name = user_pseudonyms.get(bot_token, {}).get(user_id, "Неизвестный")

        if "comments" not in receipt_data:
            receipt_data["comments"] = []
        receipt_data["comments"].append({"pseudonym": commenter_name, "text": text})

        bot_app = None
        for cid, bot_info in created_bots.items():
            if bot_info["token"] == receipt_data.get("bot_token"):
                bot_app = bot_info["application"]
                break
        bot_to_use = bot_app.bot if bot_app else context.bot

        status = receipt_data.get("status", "pending")
        status_map = {"pending": "Статус: Ожидание", "approved": "Статус: Принят ✅", "declined": "Статус: Отклонён ❌"}
        status_text = status_map.get(status, "Статус: Ожидание")

        if receipt_data.get("edited_by"):
            old_amount = receipt_data.get("amount")
            status_text += f"\nИзменён: {receipt_data['edited_by']}"

        currency = receipt_data.get("currency") or get_bot_currency(bot_token)
        if is_working_hours(bot_token):
            daily_total = db_get_daily_total(bot_token)
            daily_line = f"\nИтого за смену: {format_amount(daily_total)} {currency}"
        else:
            shift = bot_shifts.get(bot_token, {"start": 0, "end": 23})
            daily_line = f"\nНерабочее время (смена: {format_shift_window(bot_token)} МСК)"

        comments_text = "\n\n💬 Комментарии:"
        for c in receipt_data["comments"]:
            comments_text += f"\n{c['pseudonym']}: {c['text']}"

        comment_btn = [InlineKeyboardButton("💬 Комментарий", callback_data=f"receipt_comment_{receipt_id}")]
        if status == "pending":
            action_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Принять", callback_data=f"receipt_approve_{receipt_id}")],
                [InlineKeyboardButton("❌ Отклонить", callback_data=f"receipt_decline_{receipt_id}")],
                comment_btn
            ])
        elif status == "approved":
            action_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить", callback_data=f"receipt_edit_{receipt_id}")],
                comment_btn
            ])
        elif status == "declined":
            action_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Назад", callback_data=f"receipt_undo_{receipt_id}")],
                comment_btn
            ])
        else:
            action_markup = InlineKeyboardMarkup([comment_btn])

        comment_has_media = "photo_id" in receipt_data or "document_id" in receipt_data

        async def _update_comment_msg(uid, msg_id):
            try:
                async def _do():
                    if comment_has_media:
                        new_caption = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}{comments_text}"
                        return await bot_to_use.edit_message_caption(
                            chat_id=uid, message_id=msg_id,
                            caption=new_caption, reply_markup=action_markup
                        )
                    else:
                        new_text = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}{comments_text}"
                        return await bot_to_use.edit_message_text(
                            chat_id=uid, message_id=msg_id,
                            text=new_text, reply_markup=action_markup
                        )
                await send_with_retry(_do, uid, bot_token)
            except Exception as e:
                logger.error(f"Error updating receipt comment for {uid}: {e}")

        if "message_ids" in receipt_data:
            await gather_safe(*[
                _update_comment_msg(uid, msg_id)
                for uid, msg_id in receipt_data["message_ids"].items()
            ])

        db_save_receipt(receipt_id, receipt_data)
        set_user_state(bot_token, user_id, None)
        await update.message.reply_text("✅ Комментарий добавлен", reply_markup=get_main_keyboard(is_admin))
        return

    if state and state.get("mode") == "waiting_amount":
        if text.lower().strip() in ("отмена", "cancel", "отменить", "/cancel"):
            set_user_state(bot_token, user_id, None)
            await update.message.reply_text(
                "✅ Отправка чека отменена",
                reply_markup=keyboard_for_user(bot_token, user_id)
            )
            return
        clean_text = text.strip().replace(',', '.')
        try:
            amount = float(clean_text)
        except ValueError:
            await update.message.reply_text("❌ Неверный формат! Введите число (например 100 или 100.50) или «отмена»")
            return

        currency = get_bot_currency(bot_token)
        photo_id = state.get("photo_id")
        document_id = state.get("document_id")
        saved_reply_msg_id = state.get("reply_msg_id")
        pseudonym = user_pseudonyms[bot_token][user_id]
        receipt_text = f"{format_amount(amount)} {currency}"

        set_user_state(bot_token, user_id, None)

        receipt_id = ''.join(random.choices(string.ascii_letters + string.digits, k=12))

        keyboard = [
            [InlineKeyboardButton("✅ Принять", callback_data=f"receipt_approve_{receipt_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"receipt_decline_{receipt_id}")],
            [InlineKeyboardButton("💬 Комментарий", callback_data=f"receipt_comment_{receipt_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        receipt_data = {
            "text": receipt_text,
            "status": "pending",
            "pseudonym": pseudonym,
            "bot_token": bot_token,
            "amount": amount,
            "currency": currency,
            "owner_id": user_id,
            "created_at": get_moscow_now().strftime("%H:%M"),
        }
        if photo_id:
            receipt_data["photo_id"] = photo_id
        if document_id:
            receipt_data["document_id"] = document_id

        receipt_data["_ts"] = time.time()
        receipts[receipt_id] = receipt_data

        if bot_token not in message_map:
            message_map[bot_token] = {}
        if bot_token not in message_map_timestamps:
            message_map_timestamps[bot_token] = {}

        async def _send_receipt_to_user(uid):
            try:
                target_reply_id = resolve_reply_target(bot_token, user_id, saved_reply_msg_id, uid) if saved_reply_msg_id else None
                caption = f"{pseudonym}: {receipt_text}\n\nНовый чек\nСтатус: Ожидание"

                async def _do_send():
                    if photo_id:
                        return await context.bot.send_photo(
                            chat_id=uid, photo=photo_id, caption=caption,
                            reply_markup=reply_markup, reply_to_message_id=target_reply_id,
                            allow_sending_without_reply=True
                        )
                    elif document_id:
                        return await context.bot.send_document(
                            chat_id=uid, document=document_id, caption=caption,
                            reply_markup=reply_markup, reply_to_message_id=target_reply_id,
                            allow_sending_without_reply=True
                        )
                    return None

                sent = await send_with_retry(_do_send, uid, bot_token)
                if sent is None:
                    return
                if "message_ids" not in receipts[receipt_id]:
                    receipts[receipt_id]["message_ids"] = {}
                receipts[receipt_id]["message_ids"][uid] = sent.message_id
                message_map[bot_token][(uid, sent.message_id)] = {
                    "pseudonym": pseudonym,
                    "text": f"Чек: {receipt_text}",
                    "sender_id": user_id,
                    "receipt_id": receipt_id
                }
            except Exception as e:
                logger.error(f"Error sending receipt to {uid}: {e}")

        targets = list(user_pseudonyms[bot_token].keys())
        await gather_safe(*[_send_receipt_to_user(uid) for uid in targets])

        # Persist new receipt
        db_save_receipt(receipt_id, receipts[receipt_id])

        file_type = "PDF" if document_id else "photo"
        logger.info(f"Receipt created ({file_type}): {receipt_id} - {amount} {currency} by {pseudonym}")
        return

    pseudonym = user_pseudonyms[bot_token][user_id]

    if update.message.reply_to_message and text.lower() in ("удалить", "/удалить", "/delete", "delete"):
        reply_msg_id = update.message.reply_to_message.message_id
        original = message_map.get(bot_token, {}).get((user_id, reply_msg_id))
        if original:
            if original.get("sender_id") == user_id or is_chat_admin(bot_token, user_id):
                deleted_count = 0
                if "sent_to" in original:
                    for uid, msg_id in original["sent_to"].items():
                        try:
                            await context.bot.delete_message(chat_id=uid, message_id=msg_id)
                            deleted_count += 1
                        except Exception as e:
                            logger.error(f"Error deleting message for {uid}: {e}")
                    try:
                        await context.bot.delete_message(chat_id=user_id, message_id=reply_msg_id)
                        deleted_count += 1
                    except Exception:
                        pass
                elif "sender_msg_id" in original:
                    sender_id = original["sender_id"]
                    sender_msg_id = original["sender_msg_id"]
                    sender_original = message_map.get(bot_token, {}).get((sender_id, sender_msg_id))
                    if sender_original and "sent_to" in sender_original:
                        for uid, msg_id in sender_original["sent_to"].items():
                            try:
                                await context.bot.delete_message(chat_id=uid, message_id=msg_id)
                                deleted_count += 1
                            except Exception as e:
                                logger.error(f"Error deleting message for {uid}: {e}")
                        try:
                            await context.bot.delete_message(chat_id=sender_id, message_id=sender_msg_id)
                            deleted_count += 1
                        except Exception:
                            pass
                try:
                    await context.bot.delete_message(chat_id=user_id, message_id=update.message.message_id)
                except Exception:
                    pass
                if deleted_count > 0:
                    await update.message.reply_text(f"✅ Сообщение удалено у {deleted_count} участников")
                else:
                    await update.message.reply_text("❌ Не удалось удалить сообщение")
            else:
                await update.message.reply_text("❌ Вы можете удалять только свои сообщения")
            return
        else:
            await update.message.reply_text("❌ Сообщение не найдено или слишком старое")
            return

    reply_msg_id = None
    if update.message.reply_to_message:
        reply_msg_id = update.message.reply_to_message.message_id

    message_text = f"{pseudonym}: {text}"

    if bot_token not in message_map:
        message_map[bot_token] = {}

    cleanup_old_messages()

    sender_key = (user_id, update.message.message_id)
    message_map[bot_token][sender_key] = {
        "pseudonym": pseudonym,
        "text": text,
        "sender_id": user_id,
        "sender_msg_id": update.message.message_id,
        "sent_to": {}
    }
    if bot_token not in message_map_timestamps:
        message_map_timestamps[bot_token] = {}
    message_map_timestamps[bot_token][sender_key] = time.time()

    async def _send_text_to_user(uid):
        try:
            target_reply_id = resolve_reply_target(bot_token, user_id, reply_msg_id, uid) if reply_msg_id else None

            async def _do_send():
                return await context.bot.send_message(
                    chat_id=uid,
                    text=message_text,
                    reply_to_message_id=target_reply_id,
                    allow_sending_without_reply=True
                )

            sent = await send_with_retry(_do_send, uid, bot_token)
            message_map[bot_token][sender_key]["sent_to"][uid] = sent.message_id
            message_map[bot_token][(uid, sent.message_id)] = {
                "pseudonym": pseudonym,
                "text": text,
                "sender_id": user_id,
                "sender_msg_id": update.message.message_id
            }
            message_map_timestamps[bot_token][(uid, sent.message_id)] = time.time()
        except Exception as e:
            logger.error(f"Error sending to {uid}: {e}")

    targets = [uid for uid in user_pseudonyms[bot_token].keys() if uid != user_id]
    await gather_safe(*[_send_text_to_user(uid) for uid in targets])


async def secret_chat_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if user_id in banned_users.get(bot_token, set()):
        await update.message.reply_text("❌ Вы заблокированы в этом чате")
        return

    if bot_token not in user_pseudonyms:
        user_pseudonyms[bot_token] = {}

    if user_id not in user_pseudonyms[bot_token]:
        await update.message.reply_text("⚠️ Сначала установите псевдоним — отправьте любое имя")
        return

    pseudonym = user_pseudonyms[bot_token][user_id]
    state = get_user_state(bot_token, user_id)
    is_admin = is_chat_admin(bot_token, user_id)

    if state and state.get("mode") == "waiting_requisites":
        photo_id = update.message.photo[-1].file_id
        caption = update.message.caption or ""
        bot_requisites[bot_token] = {"text": caption, "photo_id": photo_id}
        db_save_requisites(bot_token, caption, photo_id)
        set_user_state(bot_token, user_id, None)
        await update.message.reply_text("✅ Реквизиты с фото обновлены!", reply_markup=get_main_keyboard(is_admin))
        for uid in user_pseudonyms[bot_token].keys():
            if uid != user_id:
                try:
                    await context.bot.send_message(chat_id=uid, text="📋 Реквизиты были обновлены")
                except Exception:
                    pass
        return

    if state and state.get("mode") == "send_photo":
        set_user_state(bot_token, user_id, None)
        if bot_token not in message_map:
            message_map[bot_token] = {}

        reply_msg_id = None
        if update.message.reply_to_message:
            reply_msg_id = update.message.reply_to_message.message_id

        message_map[bot_token][(user_id, update.message.message_id)] = {
            "pseudonym": pseudonym,
            "text": "[Фото]",
            "sender_id": user_id,
            "sender_msg_id": update.message.message_id,
            "sent_to": {}
        }
        photo_file_id = update.message.photo[-1].file_id
        msg_id = update.message.message_id

        async def _send_photo_to_user(uid):
            try:
                target_reply_id = resolve_reply_target(bot_token, user_id, reply_msg_id, uid) if reply_msg_id else None

                async def _do_send():
                    return await context.bot.send_photo(
                        chat_id=uid, photo=photo_file_id, caption=f"{pseudonym}:",
                        reply_to_message_id=target_reply_id, allow_sending_without_reply=True
                    )

                sent_photo = await send_with_retry(_do_send, uid, bot_token)
                message_map[bot_token][(user_id, msg_id)]["sent_to"][uid] = sent_photo.message_id
                message_map[bot_token][(uid, sent_photo.message_id)] = {
                    "pseudonym": pseudonym, "text": "[Фото]",
                    "sender_id": user_id, "sender_msg_id": msg_id
                }
            except Exception as e:
                logger.error(f"Error sending photo to {uid}: {e}")

        targets = [uid for uid in user_pseudonyms[bot_token].keys() if uid != user_id]
        await gather_safe(*[_send_photo_to_user(uid) for uid in targets])
        await update.message.reply_text("✅ Фото отправлено.", reply_markup=get_main_keyboard(is_admin))
        return

    photo_id = update.message.photo[-1].file_id
    state_data = {"mode": "waiting_amount", "photo_id": photo_id}
    if update.message.reply_to_message:
        state_data["reply_msg_id"] = update.message.reply_to_message.message_id
    set_user_state(bot_token, user_id, state_data)
    await update.message.reply_text("Введите сумму чека (например 100 или 100.50) или напишите «отмена» чтобы не отправлять:")


async def secret_chat_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if user_id in banned_users.get(bot_token, set()):
        await update.message.reply_text("❌ Вы заблокированы в этом чате")
        return

    if bot_token not in user_pseudonyms:
        user_pseudonyms[bot_token] = {}

    if user_id not in user_pseudonyms[bot_token]:
        await update.message.reply_text("⚠️ Сначала установите псевдоним — отправьте любое имя")
        return

    pseudonym = user_pseudonyms[bot_token][user_id]

    if update.message.document and update.message.document.mime_type:
        mime = update.message.document.mime_type
        if mime == "application/pdf" or mime.startswith("image/"):
            doc_id = update.message.document.file_id
            state_data = {"mode": "waiting_amount", "document_id": doc_id}
            if update.message.reply_to_message:
                state_data["reply_msg_id"] = update.message.reply_to_message.message_id
            set_user_state(bot_token, user_id, state_data)
            await update.message.reply_text("Введите сумму чека (например 100 или 100.50) или напишите «отмена» чтобы не отправлять:")
            return

    media_label = "[Медиа]"
    if update.message.video:
        media_label = "[Видео]"
    elif update.message.video_note:
        media_label = "[Видеосообщение]"
    elif update.message.voice:
        media_label = "[Голосовое]"
    elif update.message.audio:
        media_label = "[Аудио]"
    elif update.message.document:
        media_label = "[Файл]"

    reply_msg_id = None
    if update.message.reply_to_message:
        reply_msg_id = update.message.reply_to_message.message_id

    if bot_token not in message_map:
        message_map[bot_token] = {}
    message_map[bot_token][(user_id, update.message.message_id)] = {
        "pseudonym": pseudonym,
        "text": media_label,
        "sender_id": user_id,
        "sender_msg_id": update.message.message_id,
        "sent_to": {}
    }

    msg_id = update.message.message_id
    video_id = update.message.video.file_id if update.message.video else None
    video_note_id = update.message.video_note.file_id if update.message.video_note else None
    voice_id = update.message.voice.file_id if update.message.voice else None
    audio_id = update.message.audio.file_id if update.message.audio else None
    document_id = update.message.document.file_id if update.message.document else None

    async def _send_media_to_user(uid):
        try:
            target_reply_id = resolve_reply_target(bot_token, user_id, reply_msg_id, uid) if reply_msg_id else None

            async def _do_send():
                if video_id:
                    return await context.bot.send_video(
                        chat_id=uid, video=video_id, caption=f"{pseudonym}:",
                        reply_to_message_id=target_reply_id, allow_sending_without_reply=True)
                elif video_note_id:
                    return await context.bot.send_video_note(
                        chat_id=uid, video_note=video_note_id,
                        reply_to_message_id=target_reply_id, allow_sending_without_reply=True)
                elif voice_id:
                    return await context.bot.send_voice(
                        chat_id=uid, voice=voice_id, caption=f"{pseudonym}:",
                        reply_to_message_id=target_reply_id, allow_sending_without_reply=True)
                elif audio_id:
                    return await context.bot.send_audio(
                        chat_id=uid, audio=audio_id, caption=f"{pseudonym}:",
                        reply_to_message_id=target_reply_id, allow_sending_without_reply=True)
                elif document_id:
                    return await context.bot.send_document(
                        chat_id=uid, document=document_id, caption=f"{pseudonym}:",
                        reply_to_message_id=target_reply_id, allow_sending_without_reply=True)
                return None

            sent_media = await send_with_retry(_do_send, uid, bot_token)
            if sent_media:
                message_map[bot_token][(user_id, msg_id)]["sent_to"][uid] = sent_media.message_id
                message_map[bot_token][(uid, sent_media.message_id)] = {
                    "pseudonym": pseudonym, "text": media_label,
                    "sender_id": user_id, "sender_msg_id": msg_id
                }
        except Exception as e:
            logger.error(f"Error sending media to {uid}: {e}")

    targets = [uid for uid in user_pseudonyms[bot_token].keys() if uid != user_id]
    await gather_safe(*[_send_media_to_user(uid) for uid in targets])


async def debug_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass


async def receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query.data.startswith("receipt_"):
        return

    data_parts = query.data.split("_", 2)
    if len(data_parts) < 3:
        await query.answer("Неверные данные", show_alert=True)
        return

    action = data_parts[1]
    receipt_id = data_parts[2]

    if receipt_id not in receipts:
        await query.answer("Чек не найден", show_alert=True)
        return

    receipt_data = receipts[receipt_id]

    bot_token = receipt_data.get("bot_token")
    if not bot_token:
        logger.error("No bot_token in receipt_data!")
        return

    approver_id = query.from_user.id
    approver_name = user_pseudonyms.get(bot_token, {}).get(approver_id, "Неизвестный")

    if action in ("approve", "decline", "undo") and not (
        is_chat_admin(bot_token, approver_id) or is_chat_handler(bot_token, approver_id)
    ):
        await query.answer("❌ Только админы и обработчики могут управлять чеками", show_alert=True)
        return

    prev_status = receipt_data.get("status")

    # Helper: answer callback query, ignoring "too old" errors so the rest
    # of the handler (edit_message, sheets, persistence) still runs.
    async def _safe_answer(text, show_alert=False):
        try:
            await query.answer(text, show_alert=show_alert)
        except Exception:
            pass

    if action == "approve":
        if prev_status == "approved":
            await _safe_answer("Этот чек уже принят", show_alert=True)
            return
        receipt_data["status"] = "approved"
        status_text = f"Статус: Принят ✅ ({approver_name})"
        await _safe_answer("Чек принят!")
    elif action == "decline":
        if prev_status == "declined":
            await _safe_answer("Этот чек уже отклонён", show_alert=True)
            return
        receipt_data["status"] = "declined"
        status_text = f"Статус: Отклонён ❌ ({approver_name})"
        await _safe_answer("Чек отклонён!")
    elif action == "edit":
        if receipt_data.get("status") != "approved":
            await _safe_answer("Этот чек не был принят", show_alert=True)
            return
        set_user_state(bot_token, approver_id, {"mode": "waiting_edit_amount", "receipt_id": receipt_id})
        await _safe_answer("Введите новую сумму чека", show_alert=True)
        return
    elif action == "undo":
        if receipt_data.get("status") != "declined":
            await _safe_answer("Этот чек не был отклонён", show_alert=True)
            return
        receipt_data["status"] = "pending"
        status_text = "Статус: Ожидание"
        await _safe_answer("Чек возвращён на рассмотрение!")
    elif action == "comment":
        set_user_state(bot_token, approver_id, {"mode": "waiting_receipt_comment", "receipt_id": receipt_id})
        await _safe_answer("Введите текст комментария", show_alert=True)
        return
    else:
        return

    bot_app = None
    for chat_id, bot_info in created_bots.items():
        if bot_info["token"] == bot_token:
            bot_app = bot_info["application"]
            break

    if not bot_app:
        bot_to_use = context.bot
    else:
        bot_to_use = bot_app.bot

    bot_username = bot_to_use.username if hasattr(bot_to_use, 'username') else "unknown"
    amount = receipt_data.get("amount")
    currency = receipt_data.get("currency")

    if action == "approve":
        if amount:
            photo_url = None
            if "photo_id" in receipt_data:
                photo_url = f"https://t.me/c/{receipt_data['photo_id']}"
            await run_in_thread(
                add_receipt_to_sheet,
                bot_username, amount,
                currency or get_bot_currency(bot_token),
                receipt_data["pseudonym"], photo_url
            )
            if is_working_hours(bot_token):
                await run_in_thread(db_add_daily_total, bot_token, amount)
            logger.info(f"Added receipt to Google Sheets: {amount} {currency}")

    elif action == "decline":
        if prev_status == "approved" and amount:
            await run_in_thread(
                remove_receipt_from_sheet,
                bot_username, amount, receipt_data["pseudonym"]
            )
            if is_working_hours(bot_token):
                await run_in_thread(db_subtract_daily_total, bot_token, amount)
            logger.info(f"Declined previously approved receipt: {amount} {currency}")

    elif action == "cancel":
        if amount:
            await run_in_thread(
                remove_receipt_from_sheet,
                bot_username, amount, receipt_data["pseudonym"]
            )
            if is_working_hours(bot_token):
                await run_in_thread(db_subtract_daily_total, bot_token, amount)
            logger.info(f"Cancelled receipt: {amount} {currency} by {approver_name}")

    currency_for_total = receipt_data.get("currency") or get_bot_currency(bot_token)
    if is_working_hours(bot_token):
        daily_total = await run_in_thread(db_get_daily_total, bot_token)
        daily_line = f"\nИтого за смену: {format_amount(daily_total)} {currency_for_total}"
    else:
        shift = bot_shifts.get(bot_token, {"start": 0, "end": 23})
        daily_line = f"\nНерабочее время (смена: {format_shift_window(bot_token)} МСК)"

    comment_btn = [InlineKeyboardButton("💬 Комментарий", callback_data=f"receipt_comment_{receipt_id}")]
    if action == "approve":
        action_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить", callback_data=f"receipt_edit_{receipt_id}")],
            comment_btn
        ])
    elif action == "decline":
        action_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад", callback_data=f"receipt_undo_{receipt_id}")],
            comment_btn
        ])
    elif action == "undo":
        action_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Принять", callback_data=f"receipt_approve_{receipt_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"receipt_decline_{receipt_id}")],
            comment_btn
        ])
    else:
        action_markup = None

    comments_text = ""
    if receipt_data.get("comments"):
        comments_text = "\n\n💬 Комментарии:"
        for c in receipt_data["comments"]:
            comments_text += f"\n{c['pseudonym']}: {c['text']}"

    has_media = "photo_id" in receipt_data or "document_id" in receipt_data

    async def _update_receipt_msg(uid, msg_id):
        try:
            async def _do():
                if has_media:
                    new_caption = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}{comments_text}"
                    return await bot_to_use.edit_message_caption(
                        chat_id=uid, message_id=msg_id,
                        caption=new_caption, reply_markup=action_markup
                    )
                else:
                    new_text = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}{comments_text}"
                    return await bot_to_use.edit_message_text(
                        chat_id=uid, message_id=msg_id,
                        text=new_text, reply_markup=action_markup
                    )
            await send_with_retry(_do, uid, bot_token)
        except Exception as e:
            logger.error(f"Error updating receipt for {uid}: {e}")

    if "message_ids" in receipt_data:
        await gather_safe(*[
            _update_receipt_msg(uid, msg_id)
            for uid, msg_id in receipt_data["message_ids"].items()
        ])

    now_msk = get_moscow_now().strftime("%H:%M МСК")
    owner_id = receipt_data.get("owner_id")
    receipt_pseudonym = receipt_data.get("pseudonym", "?")
    amount_str = format_amount(amount) if amount else "?"
    currency_str = currency or currency_for_total

    # Author gets a personal "Ваш чек ..." message; watchers/handlers get a
    # third-person "Чек {псевдоним} ..." message about the same event.
    author_text = None
    others_text = None
    if action == "approve":
        author_text = f"✅ Ваш чек на {amount_str} {currency_str} принят ({approver_name}, {now_msk})"
        others_text = f"✅ Чек {receipt_pseudonym} на {amount_str} {currency_str} принят ({approver_name}, {now_msk})"
    elif action == "decline":
        author_text = f"❌ Ваш чек на {amount_str} {currency_str} отклонён ({approver_name}, {now_msk})"
        others_text = f"❌ Чек {receipt_pseudonym} на {amount_str} {currency_str} отклонён ({approver_name}, {now_msk})"
    elif action == "undo":
        author_text = f"🔄 Ваш чек на {amount_str} {currency_str} возвращён на рассмотрение ({approver_name}, {now_msk})"
        others_text = f"🔄 Чек {receipt_pseudonym} на {amount_str} {currency_str} возвращён на рассмотрение ({approver_name}, {now_msk})"

    if author_text:
        receipt_msg_ids = receipt_data.get("message_ids", {})

        async def _notify_one(uid, text_to_send):
            try:
                reply_to = receipt_msg_ids.get(uid)

                async def _do():
                    return await bot_to_use.send_message(
                        chat_id=uid, text=text_to_send,
                        reply_to_message_id=reply_to, allow_sending_without_reply=True
                    )
                await send_with_retry(_do, uid, bot_token)
            except Exception as e:
                logger.error(f"Error sending receipt notification to {uid}: {e}")

        # 1) Personal "Ваш чек" notification to the receipt author.
        coros = []
        if owner_id and owner_id != approver_id:
            coros.append(_notify_one(owner_id, author_text))

        # 2) Third-person notification to extra watchers + handlers (excluding
        #    the author and the approver to avoid duplicate / self-pings).
        others_ids = set()
        others_ids.update(receipt_watchers.get(bot_token, set()))
        others_ids.update(bot_handlers.get(bot_token, set()))
        others_ids.discard(approver_id)
        if owner_id:
            others_ids.discard(owner_id)
        coros.extend(_notify_one(uid, others_text) for uid in others_ids)

        await gather_safe(*coros)

    # Persist updated receipt status
    db_save_receipt(receipt_id, receipt_data)


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    is_admin_user = is_chat_admin(bot_token, user_id)
    is_owner_user = is_chat_owner(bot_token, user_id)
    if not (is_admin_user or is_owner_user):
        await update.message.reply_text("❌ Только администратор или Owner может генерировать ссылки-приглашения")
        return

    expires_minutes = 0
    if context.args:
        if not is_admin_user:
            await update.message.reply_text("❌ Owner может создавать только бессрочные ссылки. Используйте команду без аргументов.")
            return
        try:
            expires_minutes = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Неверный формат времени. Используйте: /invite или /invite 10 (на 10 минут)")
            return

    invite_code = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

    if expires_minutes > 0:
        expires_at = time.time() + (expires_minutes * 60)
        invite_links[invite_code] = {
            "bot_token": bot_token,
            "expires_at": expires_at,
            "used": False
        }
        db_add_invite(invite_code, bot_token, expires_at, False)
        bot_username = context.bot.username
        invite_link = f"https://t.me/{bot_username}?start={invite_code}"
        await update.message.reply_text(
            f"✅ Ссылка-приглашение создана!\n\n"
            f"Ссылка: {invite_link}\n\n"
            f"⏱ Истекает через {expires_minutes} мин.\n"
            f"👤 Одноразовая"
        )
    else:
        expires_at = time.time() + (365 * 24 * 60 * 60)
        invite_links[invite_code] = {
            "bot_token": bot_token,
            "expires_at": expires_at,
            "used": False
        }
        db_add_invite(invite_code, bot_token, expires_at, False)
        bot_username = context.bot.username
        invite_link = f"https://t.me/{bot_username}?start={invite_code}"
        await update.message.reply_text(
            f"✅ Ссылка-приглашение создана!\n\n"
            f"Ссылка: {invite_link}\n\n"
            f"👤 Одноразовая"
        )


async def op_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if not is_chat_admin(bot_token, user_id):
        await update.message.reply_text("❌ Только админы могут использовать эту команду")
        return

    if not context.args:
        await update.message.reply_text("❌ Использование: /op <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return

    if target_id not in user_pseudonyms.get(bot_token, {}):
        await update.message.reply_text("❌ Пользователь не найден в этом чате")
        return

    if is_chat_admin(bot_token, target_id):
        await update.message.reply_text("ℹ️ Этот пользователь уже является админом")
        return

    if bot_token not in bot_chat_admins:
        bot_chat_admins[bot_token] = set()
    bot_chat_admins[bot_token].add(target_id)
    db_add_chat_admin(bot_token, target_id)

    target_name = user_pseudonyms[bot_token].get(target_id, str(target_id))
    await update.message.reply_text(f"✅ {target_name} назначен админом")
    await notify_role_change(context.bot, target_id, "Админ", granted=True)


async def deop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if not is_chat_admin(bot_token, user_id):
        await update.message.reply_text("❌ Только админы могут использовать эту команду")
        return

    if not context.args:
        await update.message.reply_text("❌ Использование: /deop <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return

    if bot_token in bot_admins and bot_admins[bot_token] == target_id:
        await update.message.reply_text("❌ Нельзя снять права создателя чата")
        return

    if target_id not in bot_chat_admins.get(bot_token, set()):
        await update.message.reply_text("ℹ️ Этот пользователь не является админом")
        return

    bot_chat_admins[bot_token].discard(target_id)
    db_remove_chat_admin(bot_token, target_id)

    target_name = user_pseudonyms.get(bot_token, {}).get(target_id, str(target_id))
    await update.message.reply_text(f"✅ {target_name} больше не админ")
    await notify_role_change(context.bot, target_id, "Админ", granted=False)


async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if not is_chat_admin(bot_token, user_id):
        await update.message.reply_text("❌ Только админы могут использовать эту команду")
        return

    if not context.args:
        await update.message.reply_text("❌ Использование: /kick <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return

    if bot_token in bot_admins and bot_admins[bot_token] == target_id:
        await update.message.reply_text("❌ Нельзя кикнуть создателя чата")
        return

    if target_id not in user_pseudonyms.get(bot_token, {}):
        await update.message.reply_text("❌ Пользователь не найден в этом чате")
        return

    target_name = user_pseudonyms[bot_token].get(target_id, str(target_id))

    # Full purge: pseudonym + admin/owner/handler roles + watcher entry, in memory and DB.
    _purge_user_from_state(bot_token, target_id, drop_pseudonym=True)

    if bot_token not in banned_users:
        banned_users[bot_token] = set()
    banned_users[bot_token].add(target_id)
    db_ban_user(bot_token, target_id)

    try:
        await context.bot.send_message(chat_id=target_id, text="❌ Вы были исключены из этого чата")
    except Exception:
        pass

    await update.message.reply_text(f"✅ {target_name} был исключён и заблокирован")


async def chrq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if not (is_chat_admin(bot_token, user_id) or is_chat_handler(bot_token, user_id)):
        await update.message.reply_text("❌ Только админы и обработчики могут менять реквизиты")
        return

    set_user_state(bot_token, user_id, {"mode": "waiting_requisites"})
    await update.message.reply_text("📋 Отправьте новые реквизиты (в любом формате):")


async def change_name_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if bot_token not in user_pseudonyms:
        user_pseudonyms[bot_token] = {}

    if user_id not in user_pseudonyms[bot_token]:
        await update.message.reply_text("⚠️ Сначала нужно установить псевдоним")
        return

    if not context.args:
        await update.message.reply_text("❌ Использование: /change_name <новое_имя>")
        return

    old_pseudonym = user_pseudonyms[bot_token][user_id]
    new_pseudonym = " ".join(context.args)

    if is_pseudonym_taken(bot_token, new_pseudonym, exclude_user_id=user_id):
        await update.message.reply_text("❌ Этот псевдоним уже занят. Выберите другой.")
        return

    user_pseudonyms[bot_token][user_id] = new_pseudonym
    db_update_pseudonym(bot_token, user_id, new_pseudonym)

    await update.message.reply_text(
        f"✅ Псевдоним изменён!\n\n"
        f"Был: {old_pseudonym}\n"
        f"Стал: {new_pseudonym}"
    )


async def setshift_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if not is_chat_admin(bot_token, user_id):
        await update.message.reply_text("❌ Только администратор может настроить смену")
        return

    set_user_state(bot_token, user_id, {"mode": "setshift_start"})
    await update.message.reply_text(
        f"Текущая смена: {format_shift_window(bot_token)} МСК\n\n"
        f"Введите время начала смены в формате ЧЧ:ММ (например 16:30) "
        f"или просто часы (например 16):"
    )


def _parse_hh_mm(text):
    """Parse "HH:MM" or "HH" or "HH.MM" into (hour, minute) or None on error."""
    s = text.strip().replace(".", ":").replace("-", ":")
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        if len(parts) != 2:
            return None
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            return None
    else:
        try:
            h, m = int(s), 0
        except ValueError:
            return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


async def handle_setshift_flow(update, context, bot_token, user_id, state, text):
    if state.get("mode") == "setshift_start":
        parsed = _parse_hh_mm(text)
        if not parsed:
            await update.message.reply_text("❌ Введите время в формате ЧЧ:ММ (00:00–23:59)")
            return True
        h, m = parsed
        set_user_state(bot_token, user_id, {
            "mode": "setshift_end", "start_h": h, "start_m": m,
        })
        await update.message.reply_text(
            f"Начало смены: {h:02d}:{m:02d} МСК\n\n"
            f"Введите время окончания смены в формате ЧЧ:ММ:"
        )
        return True

    if state.get("mode") == "setshift_end":
        parsed = _parse_hh_mm(text)
        if not parsed:
            await update.message.reply_text("❌ Введите время в формате ЧЧ:ММ (00:00–23:59)")
            return True
        eh, em = parsed
        sh = state["start_h"]
        sm = state["start_m"]
        bot_shifts[bot_token] = {"start": sh, "end": eh, "start_min": sm, "end_min": em}
        db_save_shift(bot_token, sh, eh, sm, em)
        set_user_state(bot_token, user_id, None)
        start_total = sh * 60 + sm
        end_total = eh * 60 + em
        if start_total <= end_total:
            desc = f"с {sh:02d}:{sm:02d} до {eh:02d}:{em:02d} МСК"
        else:
            desc = f"с {sh:02d}:{sm:02d} до {eh:02d}:{em:02d} МСК (через полночь)"
        is_admin = is_chat_admin(bot_token, user_id)
        await update.message.reply_text(
            f"✅ Смена установлена: {desc}\n\n"
            f"Чеки будут учитываться только в рабочее время.",
            reply_markup=get_main_keyboard(is_admin)
        )
        return True

    return False


async def add_to_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in WHITELIST:
        await update.message.reply_text("⛔ Доступ запрещён")
        return

    if not context.args:
        await update.message.reply_text("❌ Использование: /add <user_id>")
        return

    try:
        new_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return

    if new_id in WHITELIST:
        await update.message.reply_text("ℹ️ Этот пользователь уже в whitelist")
        return

    WHITELIST.append(new_id)
    await update.message.reply_text(f"✅ Пользователь {new_id} добавлен в whitelist")


async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in WHITELIST:
        await update.message.reply_text("⛔ Доступ запрещён")
        return

    if not context.args:
        await update.message.reply_text("❌ Использование: /msg <текст сообщения>")
        return

    message_text = " ".join(context.args)

    total_users = 0
    total_sent = 0
    total_bots = 0

    for bot_token, bot_info in created_bots.items():
        bot_app = bot_info.get("application")
        if not bot_app:
            continue

        total_bots += 1
        users = user_pseudonyms.get(bot_token, {})

        for uid in users.keys():
            total_users += 1
            try:
                await bot_app.bot.send_message(chat_id=uid, text=f"📢 Рассылка:\n\n{message_text}")
                total_sent += 1
            except Exception as e:
                logger.error(f"Failed to send broadcast to {uid}: {e}")

    await update.message.reply_text(
        f"✅ Рассылка завершена\n\n"
        f"Ботов: {total_bots}\n"
        f"Отправлено: {total_sent}/{total_users}"
    )


def main():
    if not ADMIN_BOT_TOKEN:
        raise ValueError("ADMIN_BOT_TOKEN environment variable is required")

    if not WHITELIST:
        raise ValueError("WHITELIST environment variable is required")

    init_db()
    init_google_sheets()

    admin_app = Application.builder().token(ADMIN_BOT_TOKEN).request(_make_request()).post_init(restore_bots).build()

    admin_app.add_handler(CommandHandler("start", start_admin))
    admin_app.add_handler(CommandHandler("create_secret_chat", create_secret_chat))
    admin_app.add_handler(CommandHandler("add", add_to_whitelist))
    admin_app.add_handler(CommandHandler("msg", broadcast_message))
    admin_app.add_handler(CallbackQueryHandler(admin_geo_callback))
    admin_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_message))

    logger.info("Admin bot started")
    admin_app.run_polling(poll_interval=1.0, timeout=30, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
