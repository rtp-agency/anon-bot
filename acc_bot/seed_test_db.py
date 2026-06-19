"""Создаёт пустую тестовую БД с минимальными таблицами `bots` и `pseudonyms`,
которых ожидает acc-бот (читает их только на чтение).

Запускать: python seed_test_db.py
"""
import sqlite3
import os

DB = os.environ.get("ACC_DB_PATH", "./data.db")

conn = sqlite3.connect(DB)
c = conn.cursor()

c.execute("""CREATE TABLE IF NOT EXISTS bots (
    token TEXT PRIMARY KEY,
    username TEXT,
    admin_user_id INTEGER,
    geo TEXT
)""")
c.execute("""CREATE TABLE IF NOT EXISTS pseudonyms (
    bot_token TEXT,
    user_id INTEGER,
    pseudonym TEXT,
    PRIMARY KEY (bot_token, user_id)
)""")

# Заглушечные данные для теста UI — две группы по разным гео.
# ВАЖНО: токены вымышленные. Реальная рассылка с них работать не будет
# (Telegram вернёт 401), но UI выбора ботов будет работать.
c.execute("INSERT OR IGNORE INTO bots VALUES (?, ?, ?, ?)",
          ("1111111111:AAFakeTokenMx", "AndreyMX1231_bot", 0, "mexico"))
c.execute("INSERT OR IGNORE INTO bots VALUES (?, ?, ?, ?)",
          ("2222222222:AAFakeTokenAr", "Avgpriem_bot", 0, "argentina"))
c.execute("INSERT OR IGNORE INTO bots VALUES (?, ?, ?, ?)",
          ("3333333333:AAFakeTokenCo", "AndreyColumb_bot", 0, "colombia"))

# Пара фейковых юзеров чтобы видеть «кому пошлёт»
c.execute("INSERT OR IGNORE INTO pseudonyms VALUES (?, ?, ?)",
          ("1111111111:AAFakeTokenMx", 5252506422, "TestMxUser"))
c.execute("INSERT OR IGNORE INTO pseudonyms VALUES (?, ?, ?)",
          ("2222222222:AAFakeTokenAr", 5252506422, "TestArUser"))

conn.commit()
conn.close()
print(f"Готово: {DB}")
