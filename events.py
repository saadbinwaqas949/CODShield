"""CODShield event contracts (Pydantic v2).

Modules communicate by saving these versioned event payloads to the event table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# Return the current UTC time for a new event.
def _now() -> datetime:
    return datetime.now(timezone.utc)


class Envelope(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    version: int = 1
    occurred_at: datetime = Field(default_factory=_now)
    seller_id: UUID
    correlation_id: UUID
    idempotency_key: str


class OrderLine(BaseModel):
    product_id: UUID
    qty: int = Field(gt=0)
    unit_price: float


class MessageReceived(Envelope):
    type: Literal["message.received"] = "message.received"
    conversation_id: UUID
    customer_phone_hash: str
    kind: Literal["text", "voice"]
    text: str | None = None
    audio_url: str | None = None


class HumanHandoffRequested(Envelope):
    type: Literal["human.handoff_requested"] = "human.handoff_requested"
    conversation_id: UUID
    reason: str


class HumanTakeover(Envelope):
    type: Literal["human.takeover"] = "human.takeover"
    conversation_id: UUID


class HumanReply(Envelope):
    type: Literal["human.reply"] = "human.reply"
    conversation_id: UUID
    text: str = Field(min_length=1, max_length=500)


class HumanResume(Envelope):
    type: Literal["human.resume"] = "human.resume"
    conversation_id: UUID


class OrderCreated(Envelope):
    type: Literal["order.created"] = "order.created"
    order_id: UUID
    customer_id: UUID
    items: list[OrderLine]
    total_amount: float
    raw_address: str


class AddressNormalized(Envelope):
    type: Literal["address.normalized"] = "address.normalized"
    order_id: UUID
    address_id: UUID
    city: str | None = None
    area: str | None = None
    street: str | None = None
    house_no: str | None = None
    block_phase_sector: str | None = None
    landmark: str | None = None
    flags: list[str] = Field(default_factory=list)
    normalized_text: str | None = None
    addr_hash: str | None = None
    lat: float | None = None
    lng: float | None = None
    quality_score: float = Field(ge=0, le=1)


class OrderScored(Envelope):
    type: Literal["order.scored"] = "order.scored"
    order_id: UUID
    score: float = Field(ge=0, le=1)
    level: Literal["low", "medium", "high"]
    reasons: list[str]
    model_version: str


class ConfirmationRequested(Envelope):
    type: Literal["confirmation.requested"] = "confirmation.requested"
    order_id: UUID
    reason: str
    attempt: int = 1


class ConfirmationCompleted(Envelope):
    type: Literal["confirmation.completed"] = "confirmation.completed"
    order_id: UUID
    outcome: Literal["confirmed", "cancelled", "changed_address", "no_answer", "unclear"]
    new_address: str | None = None
    transcript: str | None = None


OrderStatus = Literal[
    "created", "scoring", "cleared", "awaiting_confirmation", "needs_human",
    "confirmed", "cancelled", "dispatched", "delivered", "refused", "returned",
]


class OrderStatusChanged(Envelope):
    type: Literal["order.status_changed"] = "order.status_changed"
    order_id: UUID
    from_status: OrderStatus
    to_status: OrderStatus


class DeliveryOutcome(Envelope):
    type: Literal["delivery.outcome"] = "delivery.outcome"
    order_id: UUID
    outcome: Literal["delivered", "refused", "returned", "unreachable"]
    reason: str | None = None


class EntityFlagged(Envelope):
    type: Literal["entity.flagged"] = "entity.flagged"
    entity_type: Literal["phone", "address", "device"]
    entity_hash: str
    reason: str


Event = Annotated[
    Union[
        MessageReceived, HumanHandoffRequested, HumanTakeover, HumanReply, HumanResume,
        OrderCreated, AddressNormalized, OrderScored, ConfirmationRequested,
        ConfirmationCompleted, OrderStatusChanged, DeliveryOutcome, EntityFlagged,
    ],
    Field(discriminator="type"),
]

TRANSITIONS: dict[str, set[str]] = {
    "created": {"scoring"},
    "scoring": {"cleared", "awaiting_confirmation", "needs_human"},
    "cleared": {"dispatched"},
    "awaiting_confirmation": {"confirmed", "cancelled", "needs_human"},
    "needs_human": {"confirmed", "cancelled"},
    "confirmed": {"dispatched"},
    "dispatched": {"delivered", "refused", "returned"},
    "cancelled": set(), "delivered": set(), "refused": set(), "returned": set(),
}


# Check whether an order status change is allowed.
def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())
