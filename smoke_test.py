"""Insert two demo orders and drive only their events through the worker handlers."""
import os
from uuid import uuid4

os.environ["CALL_PROVIDER"] = "fake"
os.environ["FAKE_CALL_OUTCOME"] = "confirmed"

import events as ev
from pydantic import TypeAdapter

from db import connect, mark_done, save_event
from gateway import hash_phone
from handlers import HANDLERS

adapter = TypeAdapter(ev.Event)


# Parse an event payload into its declared Pydantic event type.
def _parse_event(payload):
    return adapter.validate_json(payload) if isinstance(payload, str) else adapter.validate_python(payload)


def drain(cur, conn, order_id, limit=300):
    """Process only this order's event chain; leave unrelated queue events alone."""
    for _ in range(limit):
        cur.execute(
            "select id,payload from events where processed_at is null and correlation_id=%s "
            "order by created_at,id limit 1 for update skip locked",
            (order_id,),
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return
        event_id, payload = row
        event = _parse_event(payload)
        for handler in HANDLERS.get(event.type, []):
            handler(cur, event)
        mark_done(cur, event_id)
        conn.commit()
    raise RuntimeError("Smoke event loop reached its safety limit")


# Create a demo order directly for the smoke test.
def make_order(cur, conn, address):
    cur.execute("select id from sellers where name=%s", ("Demo Store",))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Demo Store is missing; run seed.py first")
    seller = row[0]
    cur.execute("select id,price from products where seller_id=%s limit 1", (seller,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Demo Store has no products; run seed.py first")
    product, price = row

    customer, addr, order = uuid4(), uuid4(), uuid4()
    phone = f"03{uuid4().int % 1_000_000_000:09d}"
    cur.execute(
        "insert into customers (id,seller_id,phone_hash,name) values (%s,%s,%s,%s)",
        (customer, seller, hash_phone(phone), "Smoke Test"),
    )
    cur.execute("insert into addresses (id,customer_id,raw_text) values (%s,%s,%s)",
                (addr, customer, address))
    cur.execute(
        "insert into orders (id,seller_id,customer_id,address_id,total_amount) "
        "values (%s,%s,%s,%s,%s)", (order, seller, customer, addr, price),
    )
    cur.execute("insert into order_items (order_id,product_id,qty,unit_price) values (%s,%s,1,%s)",
                (order, product, price))
    save_event(cur, ev.OrderCreated(
        seller_id=seller, correlation_id=order, idempotency_key=f"smoke-oc-{order}",
        order_id=order, customer_id=customer,
        items=[ev.OrderLine(product_id=product, qty=1, unit_price=float(price))],
        total_amount=float(price), raw_address=address,
    ))
    conn.commit()
    return seller, order


# Run the end-to-end database smoke-test scenarios.
def run():
    conn = connect()
    cur = conn.cursor()
    try:
        scenarios = ["House 5, DHA Phase 5, Lahore", "DHA"]
        for address in scenarios:
            seller, order = make_order(cur, conn, address)
            drain(cur, conn, order)
            cur.execute("select status,risk_level,risk_score from orders where id=%s", (order,))
            status, level, score = cur.fetchone()
            if status != "dispatched":
                raise AssertionError(f"Order pipeline stopped at {status!r}")
            if address == "DHA" and level != "medium":
                raise AssertionError(f"DHA address expected medium risk, got {level!r} ({score})")
            if address == "DHA":
                cur.execute("select reasons from risk_assessments where order_id=%s", (order,))
                reasons = cur.fetchone()[0]
                for flag in ("missing_house_number", "missing_city", "too_short"):
                    if not any(flag in reason for reason in reasons):
                        raise AssertionError(f"DHA risk reasons are missing {flag}")

            save_event(cur, ev.DeliveryOutcome(
                seller_id=seller, correlation_id=order, idempotency_key=f"smoke-do-{order}",
                order_id=order, outcome="delivered"))
            conn.commit()
            drain(cur, conn, order)
            cur.execute("select status from orders where id=%s", (order,))
            final_status = cur.fetchone()[0]
            if final_status != "delivered":
                raise AssertionError(f"Expected delivered, got {final_status!r}")
            print(f"PASS {address}: {level} risk, final status {final_status}")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    run()
