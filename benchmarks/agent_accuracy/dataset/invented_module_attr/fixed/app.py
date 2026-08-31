from lib.db import Connection

conn = Connection()
conn.execute("SELECT 1")
