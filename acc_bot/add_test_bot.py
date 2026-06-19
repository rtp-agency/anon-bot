"""Добавляет тестовый бот в локальную data.db, чтобы можно было безопасно
тестировать рассылку через acc-бота.

Использование:
    python add_test_bot.py <token> [geo] [user_id_to_subscribe ...]

Примеры:
    # Добавить бота с гео Mexico, подписать себя как получателя
    python add_test_bot.py 123:AAFakeXYZ mexico 5252506422

    # Без подписчиков (просто в список доступных)
    python add_test_bot.py 123:AAFakeXYZ argentina

Что делает:
  1. Дёргает getMe для проверки токена и узнаёт username бота
  2. Добавляет строку в `bots` (token, username, admin_user_id=0, geo)
  3. Опционально добавляет user_id в `pseudonyms` для этого бота — тогда
     при рассылке через acc-бота сообщение придёт указанным юзерам
"""
import sys
import os
import sqlite3
import json
import urllib.request

DB = os.environ.get("ACC_DB_PATH", "./data.db")
ALLOWED_GEO = {
    "argentina", "bolivia", "chile", "colombia", "ecuador",
    "mexico", "morocco", "nigeria", "peru", "turkey", "venezuela",
}


def get_me(token):
    """Validate token via Telegram getMe."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.load(r)
    except Exception as e:
        print(f"❌ Ошибка запроса getMe: {e}")
        sys.exit(1)
    if not data.get("ok"):
        print(f"❌ Telegram отверг токен: {data}")
        sys.exit(1)
    return data["result"]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    token = sys.argv[1]
    geo = (sys.argv[2] if len(sys.argv) > 2 else "argentina").lower()
    user_ids = [int(u) for u in sys.argv[3:]]

    if geo not in ALLOWED_GEO:
        print(f"❌ Неизвестное гео '{geo}'. Доступно: {sorted(ALLOWED_GEO)}")
        sys.exit(1)

    info = get_me(token)
    username = info.get("username") or info.get("first_name")
    bot_id = info.get("id")
    print(f"✓ Токен валидный — @{username} (id={bot_id})")

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO bots(token, username, admin_user_id, geo) VALUES (?, ?, 0, ?)",
        (token, username, geo)
    )
    print(f"✓ Добавлен в `bots` ({geo})")

    for uid in user_ids:
        c.execute(
            "INSERT OR REPLACE INTO pseudonyms(bot_token, user_id, pseudonym) VALUES (?, ?, ?)",
            (token, uid, f"TestUser{uid}")
        )
        print(f"✓ Юзер {uid} подписан на @{username}")

    conn.commit()
    conn.close()
    print(f"\nГотово. Теперь @{username} появится в acc-боте в списке выбора ботов для группы.")
    print(f"Юзеров подписано: {len(user_ids)}")


if __name__ == "__main__":
    main()
