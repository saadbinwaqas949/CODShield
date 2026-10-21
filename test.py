from uuid import uuid4
from events import OrderScored, can_transition

e = OrderScored(
    seller_id=uuid4(), correlation_id=uuid4(),
    idempotency_key="test-1", order_id=uuid4(),
    score=0.82, level="high",
    reasons=["Address is unclear"], model_version="v0",
)
print(e.model_dump_json(indent=2))
print(can_transition("created", "scoring"))     # True
print(can_transition("created", "dispatched"))  # False