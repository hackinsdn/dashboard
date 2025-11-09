import sqlite3
p = "apps/data/db.sqlite3"
con = sqlite3.connect(p)
cur = con.cursor()
cur.execute("update alembic_version set version_num='2.0.6'")
con.commit()
print("after=", cur.execute("select version_num from alembic_version").fetchall())
con.close()
