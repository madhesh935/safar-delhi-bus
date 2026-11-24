import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute("select name, sql from sqlite_master").fetchall())
t = c.execute("select name from sqlite_master where type='table'").fetchone()[0]
print(c.execute(f"select * from {t} limit 5").fetchall())
print(c.execute(f"select count(*), count(distinct vehicle_id), count(distinct route_id), min(timestamp), max(timestamp) from {t}").fetchall())
print(c.execute(f"select route_id, count(*) from {t} group by route_id order by 2 desc limit 10").fetchall())
print(c.execute(f"select count(*) from {t} where route_id is null or route_id=''").fetchall())
v = c.execute(f"select vehicle_id from {t} limit 1").fetchone()[0]
rows = c.execute(f"select timestamp, lat, lng, route_id, trip_id from {t} where vehicle_id=? order by timestamp limit 12", (v,)).fetchall()
print(rows)
