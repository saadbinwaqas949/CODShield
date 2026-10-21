"""Skip the AI: create one fake order directly, so you can watch worker.py handle it.
Run seed.py once first. Keep worker.py running in another terminal."""
from uuid import uuid4

import events as ev
from db import connect, save_event
from gateway import hash_phone

conn = connect()
cur = conn.cursor()

cur.execute("select id from sellers where name='Demo Store'")
row = cur.fetchone()
if not row:
    raise SystemExit("Run 'python seed.py' first.")
seller = row[0]
cur.execute("select id, price from products where seller_id=%s order by name limit 1", (seller,))
prod, price = cur.fetchone()
price = float(price)

address = input("Address (press Enter for a full address, or type DHA for a risky one): ").strip()
address = address or "House 5, DHA Phase 5, Lahore"

cust, addr, order = uuid4(), uuid4(), uuid4()
fake_phone = f"0300{uuid4().int % 10**7:07d}"  # valid-shaped Pakistani demo number
phone_hash = hash_phone(fake_phone)

cur.execute("insert into customers (id,seller_id,phone_hash,name) values (%s,%s,%s,%s)",
            (cust, seller, phone_hash, "Test Customer"))
cur.execute("insert into addresses (id,customer_id,raw_text) values (%s,%s,%s)",
            (addr, cust, address))
cur.execute("insert into orders (id,seller_id,customer_id,address_id,total_amount) "
            "values (%s,%s,%s,%s,%s)", (order, seller, cust, addr, price))
cur.execute("insert into order_items (order_id,product_id,qty,unit_price) values (%s,%s,1,%s)",
            (order, prod, price))
save_event(cur, ev.OrderCreated(
    seller_id=seller, correlation_id=order, idempotency_key=f"oc-{order}",
    order_id=order, customer_id=cust,
    items=[ev.OrderLine(product_id=prod, qty=1, unit_price=price)],
    total_amount=price, raw_address=address))
conn.commit()
print("Fake order created:", str(order)[:8], "- now look at the worker window.")
