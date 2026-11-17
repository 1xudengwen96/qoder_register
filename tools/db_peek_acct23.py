"""查最近账号状态 + 密码（供 claim 补跑）。"""
import sqlite3

con = sqlite3.connect("data/qoder.db")
rows = con.execute(
    "select id, email, state, password, credits, plan from account "
    "order by id desc limit 3").fetchall()
for r in rows:
    print(r[0], r[1], r[2], "credits=", r[4], "plan=", r[5])
    print("  password:", r[3])
con.close()
