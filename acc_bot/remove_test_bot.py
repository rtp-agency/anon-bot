"""Удаляет тестовый бот из локальной data.db (со всеми его юзерами).

Использование:
    python remove_test_bot.py <token>
    python remove_test_bot.py --username <bot_username>
"""
import sys
import os
import sqlite3

DB = os.environ.get("ACC_DB_PATH", "./data.db")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    if sys.argv[1] == "--username":
        if len(sys.argv) < 3:
            print(__doc__)
            sys.exit(1)
        uname = sys.argv[2].lstrip("@")
        row = c.execute("SELECT token FROM bots WHERE username=?", (uname,)).fetchone()
        if not row:
            print(f"❌ Бот @{uname} не найден")
            sys.exit(1)
        token = row[0]
    else:
        token = sys.argv[1]

    # Also clean attached acc_group_bots (the group references would dangle otherwise)
    c.execute("DELETE FROM pseudonyms WHERE bot_token=?", (token,))
    c.execute("DELETE FROM acc_group_bots WHERE bot_token=?", (token,))
    c.execute("DELETE FROM bots WHERE token=?", (token,))
    conn.commit()
    conn.close()
    print(f"✓ Удалён бот с токеном {token[:20]}…")


if __name__ == "__main__":
    main()
