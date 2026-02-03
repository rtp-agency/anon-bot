import os
import logging
import time
import random
import string
import sqlite3
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import requests

import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

google_sheets_client = None
spreadsheet = None


MOSCOW_TZ = timezone(timedelta(hours=3))


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


def is_working_hours(bot_token):
    shift = bot_shifts.get(bot_token, {"start": 0, "end": 23})
    start = shift["start"]
    end = shift["end"]
    now = get_moscow_now()
    hour = now.hour
    if start <= end:
        return start <= hour <= end
    else:
        return hour >= start or hour <= end


def get_working_day_date(bot_token):
    shift = bot_shifts.get(bot_token, {"start": 0, "end": 23})
    start = shift["start"]
    end = shift["end"]
    now = get_moscow_now()
    if start > end and now.hour <= end:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def get_bot_currency(bot_token):
    geo = bot_geos.get(bot_token, "argentina")
    return GEO_CURRENCIES.get(geo, "ARS")


def is_chat_admin(bot_token, user_id):
    if bot_token in bot_admins and bot_admins[bot_token] == user_id:
        return True
    return user_id in bot_chat_admins.get(bot_token, set())


def get_main_keyboard():
    keyboard = [["Отправить фото", "Реквизиты"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


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
        shift_end INTEGER DEFAULT 23
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS chat_admins (
        bot_token TEXT,
        user_id INTEGER,
        PRIMARY KEY (bot_token, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS requisites (
        bot_token TEXT PRIMARY KEY,
        text TEXT
    )""")
    conn.commit()
    conn.close()


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


def db_save_requisites(bot_token, text):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO requisites VALUES (?, ?)", (bot_token, text))
    conn.commit()
    conn.close()


def db_save_shift(bot_token, shift_start, shift_end):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO shifts VALUES (?, ?, ?)", (bot_token, shift_start, shift_end))
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

    shifts_list = c.execute("SELECT bot_token, shift_start, shift_end FROM shifts").fetchall()
    for bot_token, shift_start, shift_end in shifts_list:
        bot_shifts[bot_token] = {"start": shift_start, "end": shift_end}

    admins_list = c.execute("SELECT bot_token, user_id FROM chat_admins").fetchall()
    for bot_token, user_id in admins_list:
        if bot_token not in bot_chat_admins:
            bot_chat_admins[bot_token] = set()
        bot_chat_admins[bot_token].add(user_id)

    reqs_list = c.execute("SELECT bot_token, text FROM requisites").fetchall()
    for bot_token, text in reqs_list:
        bot_requisites[bot_token] = text

    conn.close()
    return bots_list


def setup_secret_bot_handlers(app):
    app.add_handler(CommandHandler("start", secret_chat_start))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("change_name", change_name_command))
    app.add_handler(CommandHandler("setshift", setshift_command))
    app.add_handler(CommandHandler("op", op_command))
    app.add_handler(CommandHandler("deop", deop_command))
    app.add_handler(CommandHandler("chrq", chrq_command))
    app.add_handler(MessageHandler(filters.PHOTO, secret_chat_photo))
    app.add_handler(CallbackQueryHandler(debug_callback_handler), group=0)
    app.add_handler(CallbackQueryHandler(receipt_callback), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, secret_chat_message))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.VOICE | filters.AUDIO | filters.Document.ALL, secret_chat_media))


async def restore_bots(app):
    bots_list = db_load_all()
    for token, username, admin_user_id, geo in bots_list:
        try:
            new_app = Application.builder().token(token).build()
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
            await new_app.updater.start_polling()
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
    if user_id not in WHITELIST:
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

    if not query.data.startswith("geo_"):
        return

    if user_id not in admin_pending_tokens:
        await query.answer("Нет ожидающего токена", show_alert=True)
        return

    geo = query.data.replace("geo_", "")
    pending = admin_pending_tokens.pop(user_id)
    token = pending["token"]
    bot_username = pending["username"]

    try:
        new_app = Application.builder().token(token).build()
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
        await new_app.updater.start_polling()

        currency = GEO_CURRENCIES.get(geo, "ARS")
        geo_name = {
            "argentina": "Аргентина", "bolivia": "Боливия", "chile": "Чили",
            "mexico": "Мексика", "colombia": "Колумбия", "peru": "Перу",
            "ecuador": "Эквадор", "venezuela": "Венесуэла",
            "turkey": "Турция", "nigeria": "Нигерия",
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

            tg_username = update.effective_user.username
            tg_display = f"@{tg_username}" if tg_username else "без username"
            admin_ids = set()
            if bot_token in bot_admins:
                admin_ids.add(bot_admins[bot_token])
            admin_ids.update(bot_chat_admins.get(bot_token, set()))
            for aid in admin_ids:
                try:
                    await context.bot.send_message(
                        chat_id=aid,
                        text=f"🔔 Новый участник присоединился:\n{tg_display} (ID: {user_id})"
                    )
                except Exception:
                    pass

            return
        else:
            await update.message.reply_text("❌ Недействительная ссылка-приглашение")
            return

    if bot_token in user_pseudonyms and user_id in user_pseudonyms[bot_token]:
        pseudonym = user_pseudonyms[bot_token][user_id]

        is_admin = is_chat_admin(bot_token, user_id)
        admin_text = ("\n\nКоманды админа:\n"
            "/invite [минуты] - Ссылка-приглашение\n"
            "/setshift - Настроить смену\n"
            "/chrq - Изменить реквизиты\n"
            "/op <id> - Назначить админа\n"
            "/deop <id> - Снять админа") if is_admin else ""

        await update.message.reply_text(
            f"👋 С возвращением!\n\n"
            f"Ваш псевдоним: {pseudonym}\n\n"
            f"Отправьте фото — оно будет определено как чек\n"
            f"/change_name <новое_имя> - Сменить псевдоним"
            f"{admin_text}",
            reply_markup=get_main_keyboard()
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
                "Выберите свой псевдоним — отправьте любое имя\n\n"
                "Команды:\n"
                "/invite [минуты] - Ссылка-приглашение\n"
                "/setshift - Настроить смену\n"
                "/chrq - Изменить реквизиты\n"
                "/op <id> - Назначить админа\n"
                "/deop <id> - Снять админа"
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

    if bot_token not in user_pseudonyms:
        user_pseudonyms[bot_token] = {}

    if user_id not in user_pseudonyms[bot_token]:
        user_pseudonyms[bot_token][user_id] = text
        db_add_pseudonym(bot_token, user_id, text)

        is_admin = is_chat_admin(bot_token, user_id)
        admin_text = "\n/invite [минуты] - Сгенерировать ссылку-приглашение" if is_admin else ""

        await update.message.reply_text(
            f"✅ Ваш псевдоним установлен: {text}\n\n"
            f"Теперь вы можете отправлять сообщения в секретный чат!\n\n"
            f"Отправьте фото — оно будет определено как чек\n"
            f"/change_name <новое_имя> - Сменить псевдоним"
            f"{admin_text}",
            reply_markup=get_main_keyboard()
        )
        return

    if text == "Отправить фото":
        set_user_state(bot_token, user_id, {"mode": "send_photo"})
        await update.message.reply_text(
            "📷 Отправьте фото, и оно будет переслано как обычное фото (не чек).",
            reply_markup=get_main_keyboard()
        )
        return

    if text == "Реквизиты":
        reqs = bot_requisites.get(bot_token)
        if reqs:
            await update.message.reply_text(f"📋 Актуальные реквизиты:\n\n{reqs}")
        else:
            await update.message.reply_text("📋 Реквизиты ещё не установлены")
        return

    state = get_user_state(bot_token, user_id)

    if state and state.get("mode") == "waiting_requisites":
        bot_requisites[bot_token] = text
        db_save_requisites(bot_token, text)
        set_user_state(bot_token, user_id, None)
        await update.message.reply_text("✅ Реквизиты обновлены!")
        for uid in user_pseudonyms[bot_token].keys():
            if uid != user_id:
                try:
                    await context.bot.send_message(chat_id=uid, text="📋 Реквизиты были обновлены")
                except Exception:
                    pass
        return

    if state and state.get("mode") in ("setshift_start", "setshift_end"):
        await handle_setshift_flow(update, context, bot_token, user_id, state, text)
        return

    if state and state.get("mode") == "waiting_edit_amount":
        clean_text = text.strip().replace(',', '.')
        try:
            new_amount = float(clean_text)
        except ValueError:
            await update.message.reply_text("❌ Введите только цифры!")
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

        update_receipt_in_sheet(bot_username, old_amount, new_amount, receipt_data["pseudonym"])

        if is_working_hours(bot_token):
            diff = new_amount - old_amount
            if diff > 0:
                db_add_daily_total(bot_token, diff)
            elif diff < 0:
                db_subtract_daily_total(bot_token, abs(diff))

        receipt_data["amount"] = new_amount
        receipt_data["text"] = f"{format_amount(new_amount)} {currency}"
        receipt_data["edited_by"] = editor_name

        if is_working_hours(bot_token):
            daily_total = db_get_daily_total(bot_token)
            daily_line = f"\nИтого за смену: {format_amount(daily_total)} {currency}"
        else:
            shift = bot_shifts.get(bot_token, {"start": 0, "end": 23})
            daily_line = f"\nНерабочее время (смена: {shift['start']}:00–{shift['end']}:00 МСК)"

        status_text = f"Статус: Принят ✅\nИзменён: {editor_name} ({format_amount(old_amount)} → {format_amount(new_amount)})"

        action_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить", callback_data=f"receipt_edit_{receipt_id}")]
        ])

        if "message_ids" in receipt_data:
            for uid, msg_id in receipt_data["message_ids"].items():
                try:
                    if "photo_id" in receipt_data or "document_id" in receipt_data:
                        new_caption = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}"
                        await bot_to_use.edit_message_caption(
                            chat_id=uid,
                            message_id=msg_id,
                            caption=new_caption,
                            reply_markup=action_markup
                        )
                    else:
                        new_text = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}"
                        await bot_to_use.edit_message_text(
                            chat_id=uid,
                            message_id=msg_id,
                            text=new_text,
                            reply_markup=action_markup
                        )
                except Exception as e:
                    logger.error(f"Error updating edited receipt for {uid}: {e}")

        set_user_state(bot_token, user_id, None)
        await update.message.reply_text(f"✅ Сумма чека изменена: {format_amount(old_amount)} → {format_amount(new_amount)} {currency}")
        logger.info(f"Receipt {receipt_id} edited by {editor_name}: {old_amount} -> {new_amount}")
        return

    if state and state.get("mode") == "waiting_amount":
        clean_text = text.strip().replace(',', '.')
        try:
            amount = float(clean_text)
        except ValueError:
            await update.message.reply_text("❌ Введите только цифры!")
            return

        currency = get_bot_currency(bot_token)
        photo_id = state.get("photo_id")
        document_id = state.get("document_id")
        pseudonym = user_pseudonyms[bot_token][user_id]
        receipt_text = f"{format_amount(amount)} {currency}"

        set_user_state(bot_token, user_id, None)

        receipt_id = ''.join(random.choices(string.ascii_letters + string.digits, k=12))

        keyboard = [
            [InlineKeyboardButton("✅ Принять", callback_data=f"receipt_approve_{receipt_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"receipt_decline_{receipt_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        receipt_data = {
            "text": receipt_text,
            "status": "pending",
            "pseudonym": pseudonym,
            "bot_token": bot_token,
            "amount": amount,
            "currency": currency,
        }
        if photo_id:
            receipt_data["photo_id"] = photo_id
        if document_id:
            receipt_data["document_id"] = document_id

        receipts[receipt_id] = receipt_data

        for uid in user_pseudonyms[bot_token].keys():
            try:
                caption = f"{pseudonym}: {receipt_text}\n\nНовый чек\nСтатус: Ожидание"
                if photo_id:
                    sent = await context.bot.send_photo(
                        chat_id=uid,
                        photo=photo_id,
                        caption=caption,
                        reply_markup=reply_markup
                    )
                elif document_id:
                    sent = await context.bot.send_document(
                        chat_id=uid,
                        document=document_id,
                        caption=caption,
                        reply_markup=reply_markup
                    )
                else:
                    continue
                if "message_ids" not in receipts[receipt_id]:
                    receipts[receipt_id]["message_ids"] = {}
                receipts[receipt_id]["message_ids"][uid] = sent.message_id
            except Exception as e:
                logger.error(f"Error sending receipt to {uid}: {e}")

        file_type = "PDF" if document_id else "photo"
        logger.info(f"Receipt created ({file_type}): {receipt_id} - {amount} {currency} by {pseudonym}")
        return

    pseudonym = user_pseudonyms[bot_token][user_id]

    reply_prefix = ""
    if update.message.reply_to_message:
        reply_msg_id = update.message.reply_to_message.message_id
        original = message_map.get(bot_token, {}).get((user_id, reply_msg_id))
        if original:
            orig_text = original["text"]
            if len(orig_text) > 50:
                orig_text = orig_text[:50] + "..."
            reply_prefix = f"┌ {original['pseudonym']}: {orig_text}\n└ "

    message_text = f"{reply_prefix}{pseudonym}: {text}"

    if bot_token not in message_map:
        message_map[bot_token] = {}
    message_map[bot_token][(user_id, update.message.message_id)] = {
        "pseudonym": pseudonym,
        "text": text
    }

    for uid in user_pseudonyms[bot_token].keys():
        if uid != user_id:
            try:
                sent = await context.bot.send_message(chat_id=uid, text=message_text)
                message_map[bot_token][(uid, sent.message_id)] = {
                    "pseudonym": pseudonym,
                    "text": text
                }
            except Exception as e:
                logger.error(f"Error sending to {uid}: {e}")


async def secret_chat_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if bot_token not in user_pseudonyms:
        user_pseudonyms[bot_token] = {}

    if user_id not in user_pseudonyms[bot_token]:
        await update.message.reply_text("⚠️ Сначала установите псевдоним — отправьте любое имя")
        return

    pseudonym = user_pseudonyms[bot_token][user_id]
    state = get_user_state(bot_token, user_id)

    if state and state.get("mode") == "send_photo":
        set_user_state(bot_token, user_id, None)
        for uid in user_pseudonyms[bot_token].keys():
            if uid != user_id:
                try:
                    await context.bot.send_message(chat_id=uid, text=f"{pseudonym}:")
                    await context.bot.send_photo(chat_id=uid, photo=update.message.photo[-1].file_id)
                except Exception as e:
                    logger.error(f"Error sending photo to {uid}: {e}")
        await update.message.reply_text("✅ Фото отправлено.", reply_markup=get_main_keyboard())
        return

    photo_id = update.message.photo[-1].file_id
    set_user_state(bot_token, user_id, {"mode": "waiting_amount", "photo_id": photo_id})
    await update.message.reply_text("Введите сумму чека (только цифры):")


async def secret_chat_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if bot_token not in user_pseudonyms:
        user_pseudonyms[bot_token] = {}

    if user_id not in user_pseudonyms[bot_token]:
        await update.message.reply_text("⚠️ Сначала установите псевдоним — отправьте любое имя")
        return

    pseudonym = user_pseudonyms[bot_token][user_id]

    if update.message.document and update.message.document.mime_type == "application/pdf":
        doc_id = update.message.document.file_id
        set_user_state(bot_token, user_id, {"mode": "waiting_amount", "document_id": doc_id})
        await update.message.reply_text("Введите сумму чека (только цифры):")
        return

    for uid in user_pseudonyms[bot_token].keys():
        if uid != user_id:
            try:
                await context.bot.send_message(chat_id=uid, text=f"{pseudonym}:")

                if update.message.video:
                    await context.bot.send_video(chat_id=uid, video=update.message.video.file_id)
                elif update.message.video_note:
                    await context.bot.send_video_note(chat_id=uid, video_note=update.message.video_note.file_id)
                elif update.message.voice:
                    await context.bot.send_voice(chat_id=uid, voice=update.message.voice.file_id)
                elif update.message.audio:
                    await context.bot.send_audio(chat_id=uid, audio=update.message.audio.file_id)
                elif update.message.document:
                    await context.bot.send_document(chat_id=uid, document=update.message.document.file_id)
            except Exception as e:
                logger.error(f"Error sending media to {uid}: {e}")


async def debug_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        logger.info(f"!!! DEBUG: Callback query received: {update.callback_query.data}")
        logger.info(f"!!! DEBUG: From user: {update.callback_query.from_user.id}")
        logger.info(f"!!! DEBUG: Bot: {context.bot.username}")


async def receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    logger.info(f"=== Receipt callback triggered ===")
    logger.info(f"Callback data: {query.data}")
    logger.info(f"User ID: {query.from_user.id}")

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

    if action == "approve":
        receipt_data["status"] = "approved"
        status_text = f"Статус: Принят ✅ ({approver_name})"
        await query.answer("Чек принят!")
    elif action == "decline":
        receipt_data["status"] = "declined"
        status_text = f"Статус: Отклонён ❌ ({approver_name})"
        await query.answer("Чек отклонён!")
    elif action == "edit":
        if receipt_data.get("status") != "approved":
            await query.answer("Этот чек не был принят", show_alert=True)
            return
        set_user_state(bot_token, approver_id, {"mode": "waiting_edit_amount", "receipt_id": receipt_id})
        await query.answer("Введите новую сумму чека", show_alert=True)
        return
    elif action == "undo":
        if receipt_data.get("status") != "declined":
            await query.answer("Этот чек не был отклонён", show_alert=True)
            return
        receipt_data["status"] = "pending"
        status_text = "Статус: Ожидание"
        await query.answer("Чек возвращён на рассмотрение!")
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

    if action == "approve":
        bot_username = bot_to_use.username if hasattr(bot_to_use, 'username') else "unknown"
        photo_url = None
        if "photo_id" in receipt_data:
            photo_url = f"https://t.me/c/{receipt_data['photo_id']}"

        amount = receipt_data.get("amount")
        currency = receipt_data.get("currency")

        if amount:
            add_receipt_to_sheet(
                bot_username=bot_username,
                amount=amount,
                currency=currency or get_bot_currency(bot_token),
                pseudonym=receipt_data["pseudonym"],
                photo_url=photo_url
            )
            if is_working_hours(bot_token):
                db_add_daily_total(bot_token, amount)
            logger.info(f"Added receipt to Google Sheets: {amount} {currency}")

    elif action == "cancel":
        bot_username = bot_to_use.username if hasattr(bot_to_use, 'username') else "unknown"
        amount = receipt_data.get("amount")
        currency = receipt_data.get("currency")

        if amount:
            remove_receipt_from_sheet(
                bot_username=bot_username,
                amount=amount,
                pseudonym=receipt_data["pseudonym"]
            )
            if is_working_hours(bot_token):
                db_subtract_daily_total(bot_token, amount)
            logger.info(f"Cancelled receipt: {amount} {currency} by {approver_name}")

    currency_for_total = receipt_data.get("currency") or get_bot_currency(bot_token)
    if is_working_hours(bot_token):
        daily_total = db_get_daily_total(bot_token)
        daily_line = f"\nИтого за смену: {format_amount(daily_total)} {currency_for_total}"
    else:
        shift = bot_shifts.get(bot_token, {"start": 0, "end": 23})
        daily_line = f"\nНерабочее время (смена: {shift['start']}:00–{shift['end']}:00 МСК)"

    if action == "approve":
        action_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить", callback_data=f"receipt_edit_{receipt_id}")]
        ])
    elif action == "decline":
        action_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад", callback_data=f"receipt_undo_{receipt_id}")]
        ])
    elif action == "undo":
        action_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Принять", callback_data=f"receipt_approve_{receipt_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"receipt_decline_{receipt_id}")]
        ])
    else:
        action_markup = None

    if "message_ids" in receipt_data:
        for uid, msg_id in receipt_data["message_ids"].items():
            try:
                if "photo_id" in receipt_data or "document_id" in receipt_data:
                    new_caption = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}"
                    await bot_to_use.edit_message_caption(
                        chat_id=uid,
                        message_id=msg_id,
                        caption=new_caption,
                        reply_markup=action_markup
                    )
                else:
                    new_text = f"{receipt_data['pseudonym']}: {receipt_data['text']}\n\nНовый чек\n{status_text}{daily_line}"
                    await bot_to_use.edit_message_text(
                        chat_id=uid,
                        message_id=msg_id,
                        text=new_text,
                        reply_markup=action_markup
                    )
            except Exception as e:
                logger.error(f"Error updating receipt for {uid}: {e}", exc_info=True)

    logger.info("=== Receipt callback finished ===")


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if not is_chat_admin(bot_token, user_id):
        await update.message.reply_text("❌ Только администратор чата может генерировать ссылки-приглашения")
        return

    expires_minutes = 0
    if context.args:
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


async def chrq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_token = context.application.bot.token

    if not is_chat_admin(bot_token, user_id):
        await update.message.reply_text("❌ Только админы могут менять реквизиты")
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
    current = bot_shifts.get(bot_token, {"start": 0, "end": 23})
    await update.message.reply_text(
        f"Текущая смена: с {current['start']}:00 до {current['end']}:00 МСК\n\n"
        f"Введите час начала смены (от 0 до 23):"
    )


async def handle_setshift_flow(update, context, bot_token, user_id, state, text):
    if state.get("mode") == "setshift_start":
        try:
            hour = int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ Введите число от 0 до 23")
            return True
        if hour < 0 or hour > 23:
            await update.message.reply_text("❌ Введите число от 0 до 23")
            return True
        set_user_state(bot_token, user_id, {"mode": "setshift_end", "start": hour})
        await update.message.reply_text(f"Начало смены: {hour}:00 МСК\n\nВведите час окончания смены (от 0 до 23):")
        return True

    if state.get("mode") == "setshift_end":
        try:
            hour = int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ Введите число от 0 до 23")
            return True
        if hour < 0 or hour > 23:
            await update.message.reply_text("❌ Введите число от 0 до 23")
            return True
        start = state["start"]
        bot_shifts[bot_token] = {"start": start, "end": hour}
        db_save_shift(bot_token, start, hour)
        set_user_state(bot_token, user_id, None)
        if start <= hour:
            desc = f"с {start}:00 до {hour}:00 МСК"
        else:
            desc = f"с {start}:00 до {hour}:00 МСК (через полночь)"
        await update.message.reply_text(
            f"✅ Смена установлена: {desc}\n\n"
            f"Чеки будут учитываться только в рабочее время.",
            reply_markup=get_main_keyboard()
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

    admin_app = Application.builder().token(ADMIN_BOT_TOKEN).post_init(restore_bots).build()

    admin_app.add_handler(CommandHandler("start", start_admin))
    admin_app.add_handler(CommandHandler("create_secret_chat", create_secret_chat))
    admin_app.add_handler(CommandHandler("add", add_to_whitelist))
    admin_app.add_handler(CommandHandler("msg", broadcast_message))
    admin_app.add_handler(CallbackQueryHandler(admin_geo_callback))
    admin_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_message))

    logger.info("Admin bot started")
    admin_app.run_polling()


if __name__ == "__main__":
    main()
