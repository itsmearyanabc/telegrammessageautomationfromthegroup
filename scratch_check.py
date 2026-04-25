import sqlite3
import os

for f in os.listdir("sessions"):
    if f.endswith(".session"):
        path = os.path.join("sessions", f)
        print(f"--- {f} ---")
        try:
            with sqlite3.connect(path) as conn:
                c = conn.cursor()
                c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = c.fetchall()
                for table in tables:
                    tname = table[0]
                    c.execute(f"SELECT count(*) FROM {tname}")
                    count = c.fetchone()[0]
                    print(f"Table {tname}: {count} rows")
        except Exception as e:
            print(e)
