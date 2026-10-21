"""Run once: creates 'Demo Store' with 3 products."""
from uuid import uuid4

from db import connect

conn = connect()
cur = conn.cursor()
cur.execute("select id from sellers where name='Demo Store'")
if cur.fetchone():
    print("Demo Store already exists.")
else:
    sid = uuid4()
    cur.execute("insert into sellers (id,name) values (%s,'Demo Store')", (sid,))
    conn.commit()
    print("Created Demo Store.")

cur.execute("select id from sellers where name='Demo Store'")
sid = cur.fetchone()[0]
products = [
    ("SHIRT-01", "Cotton Shirt", "\u06a9\u0627\u0679\u0646 \u0634\u0631\u0679", 1500),
    ("SHOE-01", "Running Shoes", "\u0631\u0646\u0646\u06af \u0634\u0648\u0632", 4500),
    ("CAP-01", "Black Cap", "\u06a9\u0627\u0644\u06cc \u0679\u0648\u067e\u06cc", 800),
]
for sku, name, name_ur, price in products:
    cur.execute(
        "insert into products (seller_id,sku,name,name_ur,price,stock) "
        "values (%s,%s,%s,%s,%s,100) "
        "on conflict (seller_id,sku) do update set name_ur=excluded.name_ur",
        (sid, sku, name, name_ur, price),
    )
conn.commit()
print("Demo Store products are ready.")
