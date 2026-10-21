"""Pretend the courier delivered (or failed to deliver) every dispatched order.
Riskier orders are refused more often. Run it, then watch worker.py react."""
import random
from uuid import uuid4

import events as ev
from db import connect, save_event

REFUSE_CHANCE = {"low": 0.08, "medium": 0.25, "high": 0.50}

conn = connect()
cur = conn.cursor()
cur.execute("select o.id, o.seller_id, o.risk_level from orders o "
            "join deliveries d on d.order_id=o.id "
            "where o.status='dispatched' and d.outcome is null")
rows = cur.fetchall()
for oid, sid, level in rows:
    refused = random.random() < REFUSE_CHANCE.get(level or "medium", 0.25)
    outcome = "refused" if refused else "delivered"
    save_event(cur, ev.DeliveryOutcome(
        seller_id=sid, correlation_id=oid, idempotency_key=f"do-{oid}",
        order_id=oid, outcome=outcome,
        reason="Customer refused at the door" if refused else None))
    print(f"order {str(oid)[:8]} ({level}) -> {outcome}")
conn.commit()
print(f"{len(rows)} order(s) updated. Check worker.py.")