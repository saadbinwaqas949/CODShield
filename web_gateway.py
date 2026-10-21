"""HTTP demo channel. Event handlers remain in the separate worker process."""
from collections import defaultdict, deque
import hashlib
import json
import os
import re
import secrets
from decimal import Decimal
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Iterator
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from db import connect, save_event
import events as ev
from gateway import receive_message, start_conversation
from call_agent import finish_call, next_turn, opening, order_context
from call_providers.twilio import valid_signature, twiml_say
from psycopg2.extras import Json


app = FastAPI(title="CODShield demo chat")
CHAT_PAGE = Path(__file__).parent / "web" / "chat.html"
CALL_PAGE = Path(__file__).parent / "web" / "call.html"
RATE_WINDOW_SECONDS = 60
RATE_LIMIT = 10
PRODUCT_IMAGES_DIR = Path(__file__).parent / "uploads" / "products"
MAX_PRODUCT_IMAGE_BYTES = 5 * 1024 * 1024
_send_times: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = Lock()


class StartRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=200)


class SendRequest(BaseModel):
    conversation_id: UUID
    text: str = Field(min_length=1, max_length=500)


class StaffReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ProductUpdateRequest(BaseModel):
    description: str = Field(default="", max_length=3000)
    attributes: dict = Field(default_factory=dict)
    image_urls: list[str] = Field(default_factory=list, max_length=8)
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    stock: int | None = Field(default=None, ge=0)


class ProductCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    name_ur: str | None = Field(default=None, max_length=200)
    sku: str | None = Field(default=None, max_length=100)
    description: str = Field(default="", max_length=3000)
    attributes: dict = Field(default_factory=dict)
    image_urls: list[str] = Field(default_factory=list, max_length=8)
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    stock: int = Field(default=0, ge=0)


# Reject staff requests without the configured access token.
def require_staff(x_staff_token: str | None = Header(default=None, alias="X-Staff-Token")):
    expected = os.getenv("STAFF_ACCESS_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Staff access is not configured")
    if not x_staff_token or not secrets.compare_digest(x_staff_token, expected):
        raise HTTPException(status_code=401, detail="Staff sign-in required")


def get_db() -> Iterator:
    """Use one database connection per API request."""
    conn = connect()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Load a conversation and its seller from the database.
def _conversation(cur, conversation_id: UUID):
    cur.execute(
        "select c.seller_id,cu.phone_hash from conversations c "
        "join customers cu on cu.id=c.customer_id where c.id=%s",
        (conversation_id,),
    )
    return cur.fetchone()


# Apply the per-conversation message rate limit.
def _allow_send(conversation_id: UUID) -> bool:
    now = monotonic()
    key = str(conversation_id)
    with _rate_lock:
        times = _send_times[key]
        while times and now - times[0] >= RATE_WINDOW_SECONDS:
            times.popleft()
        if len(times) >= RATE_LIMIT:
            return False
        times.append(now)
        # Keep abandoned demo conversations from growing this process-local map forever.
        if len(_send_times) > 5000:
            for old_key in list(_send_times):
                old_times = _send_times[old_key]
                while old_times and now - old_times[0] >= RATE_WINDOW_SECONDS:
                    old_times.popleft()
                if not old_times:
                    _send_times.pop(old_key, None)
        return True


# Serve the customer chat page.
@app.get("/")
def index():
    return FileResponse(CHAT_PAGE, media_type="text/html; charset=utf-8")


# Serve the staff workspace.
@app.get("/staff")
def staff_page():
    return FileResponse(Path(__file__).parent / "web" / "staff.html", media_type="text/html; charset=utf-8")


# Resolve the seller assigned to the staff workspace.
def _staff_seller_id(cur):
    configured = os.getenv("STAFF_SELLER_ID")
    if configured:
        try:
            return UUID(configured)
        except ValueError:
            raise HTTPException(status_code=503, detail="STAFF_SELLER_ID must be a valid UUID")
    cur.execute("select id from sellers where name=%s order by created_at limit 1", ("Demo Store",))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=503, detail="Set up the shop or configure STAFF_SELLER_ID")
    return row[0]


@app.get("/api/staff/cockpit", dependencies=[Depends(require_staff)])
def staff_cockpit(conn=Depends(get_db)):
    """Return a seller-scoped snapshot for the staff overview."""
    cur = conn.cursor()
    try:
        seller_id = _staff_seller_id(cur)
        cur.execute(
            "select count(*) filter (where created_at >= date_trunc('day', now())),"
            "count(*) filter (where status in ('needs_human','awaiting_confirmation')) "
            "from orders where seller_id=%s", (seller_id,),
        )
        today_count, open_count = cur.fetchone()
        cur.execute(
            "select coalesce(sum(total_amount),0) from orders where seller_id=%s "
            "and status='delivered' and updated_at >= date_trunc('day', now())", (seller_id,),
        )
        delivered_value = cur.fetchone()[0]
        cur.execute(
            "select count(*) from conversations where seller_id=%s and handoff_mode in ('pending','human')",
            (seller_id,),
        )
        handoffs = cur.fetchone()[0]
        cur.execute(
            "select o.id,o.status,o.total_amount,o.risk_score,o.risk_level,o.created_at,"
            "cu.name,ra.reasons,cc.outcome,d.outcome,ar.tokens_in,ar.tokens_out,ar.latency_ms,ar.model "
            "from orders o join customers cu on cu.id=o.customer_id "
            "left join lateral (select reasons from risk_assessments where order_id=o.id "
            "order by created_at desc limit 1) ra on true "
            "left join lateral (select outcome from confirmation_calls where order_id=o.id "
            "order by created_at desc limit 1) cc on true "
            "left join deliveries d on d.order_id=o.id "
            "left join lateral (select sum(tokens_in)::int tokens_in,sum(tokens_out)::int tokens_out,"
            "round(avg(latency_ms))::int latency_ms,max(model) model from agent_runs "
            "where order_id=o.id) ar on true "
            "where o.seller_id=%s order by o.created_at desc limit 30", (seller_id,),
        )
        orders = []
        input_rate = os.getenv("GEMINI_INPUT_USD_PER_MILLION")
        output_rate = os.getenv("GEMINI_OUTPUT_USD_PER_MILLION")
        for row in cur.fetchall():
            tokens_in, tokens_out = row[10], row[11]
            cost = None
            if input_rate is not None and output_rate is not None and tokens_in is not None and tokens_out is not None:
                try:
                    cost = (tokens_in * float(input_rate) + tokens_out * float(output_rate)) / 1_000_000
                except ValueError:
                    cost = None
            orders.append({
                "id": str(row[0]), "status": row[1], "total_amount": str(row[2]),
                "risk_score": row[3], "risk_level": row[4],
                "created_at": row[5].isoformat() if row[5] else None,
                "customer": row[6] or "Customer", "risk_reasons": row[7] or [],
                "call_outcome": row[8], "delivery_outcome": row[9],
                "tokens_in": tokens_in, "tokens_out": tokens_out,
                "latency_ms": row[12], "model": row[13], "estimated_ai_cost_usd": cost,
            })
        cur.execute(
            "select type,correlation_id,created_at,processed_at,attempts,failed_at "
            "from events where seller_id=%s order by created_at desc limit 40", (seller_id,),
        )
        timeline = [{
            "type": row[0], "correlation_id": str(row[1]),
            "created_at": row[2].isoformat() if row[2] else None,
            "processed": row[3] is not None, "attempts": row[4], "failed": row[5] is not None,
        } for row in cur.fetchall()]
        return {
            "metrics": {"orders_today": today_count, "open_orders": open_count,
                        "staff_handoffs": handoffs, "delivered_value_today": str(delivered_value)},
            "briefing": f"Today: {today_count} orders; {handoffs} conversations need staff; "
                        f"delivered sales value Rs {delivered_value}.",
            "orders": orders, "timeline": timeline,
            "ai_cost_configured": input_rate is not None and output_rate is not None,
        }
    finally:
        cur.close()


# Create a customer conversation and return its identifier.
@app.post("/api/chat/start")
def start_chat(payload: StartRequest, conn=Depends(get_db)):
    phone = payload.phone.strip()
    if not phone:
        raise HTTPException(status_code=422, detail="Phone number is required")
    cur = conn.cursor()
    try:
        try:
            _seller_id, conversation_id, _phone_hash = start_conversation(
                cur, phone, name=payload.name.strip() if payload.name else None,
            )
        except (TypeError, LookupError):
            raise HTTPException(status_code=404, detail="Demo Store is not set up")
        conn.commit()
        return {"conversation_id": str(conversation_id)}
    finally:
        cur.close()


# Save a customer message and enqueue its event.
@app.post("/api/chat/send")
def send_chat(payload: SendRequest, conn=Depends(get_db)):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Message cannot be empty")
    cur = conn.cursor()
    try:
        conversation = _conversation(cur, payload.conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if not _allow_send(payload.conversation_id):
            raise HTTPException(status_code=429, detail="Please wait before sending another message")
        seller_id, phone_hash = conversation
        receive_message(cur, seller_id, payload.conversation_id, phone_hash, text)
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()


# Return conversation messages and optional order details.
@app.get("/api/chat/messages")
def chat_messages(
    conversation_id: UUID,
    after: UUID | None = None,
    debug: bool = Query(default=False),
    conn=Depends(get_db),
):
    cur = conn.cursor()
    try:
        if not _conversation(cur, conversation_id):
            raise HTTPException(status_code=404, detail="Conversation not found")

        if after is None:
            cur.execute(
                "select id,direction,body,created_at from messages "
                "where conversation_id=%s order by created_at,id",
                (conversation_id,),
            )
        else:
            cur.execute(
                "select created_at from messages where id=%s and conversation_id=%s",
                (after, conversation_id),
            )
            cursor_row = cur.fetchone()
            if not cursor_row:
                return {"messages": [], "order": _latest_order(cur, conversation_id, debug)}
            cur.execute(
                "select id,direction,body,created_at from messages where conversation_id=%s "
                "and (created_at>%s or (created_at=%s and id>%s)) order by created_at,id",
                (conversation_id, cursor_row[0], cursor_row[0], after),
            )
        messages = []
        for mid, direction, body, created_at in cur.fetchall():
            cur.execute("select media_urls from messages where id=%s and conversation_id=%s",
                        (mid, conversation_id))
            media_row = cur.fetchone()
            media_urls = media_row[0] if media_row else []
            messages.append({"id": str(mid), "direction": direction, "body": body or "",
                             "created_at": created_at.isoformat(), "media_urls": media_urls or []})
        cur.execute("select handoff_mode from conversations where id=%s", (conversation_id,))
        mode = cur.fetchone()
        return {"messages": messages, "order": _latest_order(cur, conversation_id, debug),
                "handoff_mode": mode[0] if mode else "ai"}
    finally:
        cur.close()


# Load the latest order summary for a conversation.
def _latest_order(cur, conversation_id: UUID, debug: bool):
    cur.execute(
        "select id,status,risk_level from orders where conversation_id=%s "
        "order by created_at desc limit 1",
        (conversation_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    order = {"status": row[1], "risk_level": row[2]}
    if debug:
        cur.execute(
            "select reasons from risk_assessments where order_id=%s "
            "order by created_at desc limit 1",
            (row[0],),
        )
        reasons = cur.fetchone()
        order["risk_reasons"] = reasons[0] if reasons else []
    return order


# Allow only supported HTTPS or local product-image URLs.
def _valid_product_image_url(url: str) -> bool:
    return url.startswith("https://") or bool(re.fullmatch(
        r"/product-images/[a-f0-9]{32}\.(?:jpg|png|webp)", url
    ))


# Persist a staff action as an event.
def _save_staff_event(cur, conversation_id, event_type, text=None):
    cur.execute("select seller_id,handoff_mode from conversations where id=%s", (conversation_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    seller_id = row[0]
    if event_type == "takeover" and row[1] not in ("pending", "human"):
        raise HTTPException(status_code=409, detail="This conversation has no active handoff")
    if event_type == "resume" and row[1] != "human":
        raise HTTPException(status_code=409, detail="This conversation is not being handled by staff")
    common = dict(seller_id=seller_id, correlation_id=conversation_id,
                  idempotency_key=f"staff-{event_type}-{conversation_id}-{uuid4()}")
    if event_type == "takeover":
        event = ev.HumanTakeover(**common, conversation_id=conversation_id)
    elif event_type == "reply":
        event = ev.HumanReply(**common, conversation_id=conversation_id, text=text)
    else:
        event = ev.HumanResume(**common, conversation_id=conversation_id)
    save_event(cur, event)


# List conversations waiting for or assigned to staff.
@app.get("/api/staff/conversations", dependencies=[Depends(require_staff)])
def staff_conversations(conn=Depends(get_db)):
    cur = conn.cursor()
    try:
        cur.execute(
            "select c.id,c.seller_id,s.name,cu.name,c.handoff_mode,c.handoff_reason,"
            "c.handoff_requested_at,max(m.created_at) "
            "from conversations c join sellers s on s.id=c.seller_id "
            "join customers cu on cu.id=c.customer_id left join messages m on m.conversation_id=c.id "
            "where c.handoff_mode in ('pending','human') "
            "group by c.id,s.name,cu.name order by c.handoff_requested_at desc nulls last,"
            "max(m.created_at) desc limit 100"
        )
        items = [
            {"conversation_id": str(row[0]), "seller": row[2], "customer": row[3] or "Customer",
             "mode": row[4], "reason": row[5] or "", "requested_at": row[6].isoformat() if row[6] else None,
             "latest_message_at": row[7].isoformat() if row[7] else None}
            for row in cur.fetchall()
        ]
        return {"conversations": items}
    finally:
        cur.close()


# Return the selected staff conversation thread.
@app.get("/api/staff/conversations/{conversation_id}/messages", dependencies=[Depends(require_staff)])
def staff_conversation_messages(conversation_id: UUID, conn=Depends(get_db)):
    cur = conn.cursor()
    try:
        cur.execute("select id from conversations where id=%s", (conversation_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Conversation not found")
        cur.execute(
            "select direction,body,created_at from messages where conversation_id=%s "
            "order by created_at,id limit 200", (conversation_id,),
        )
        return {"messages": [
            {"direction": row[0], "body": row[1] or "", "created_at": row[2].isoformat()}
            for row in cur.fetchall()
        ]}
    finally:
        cur.close()


# Queue an event that assigns a conversation to staff.
@app.post("/api/staff/conversations/{conversation_id}/takeover", dependencies=[Depends(require_staff)])
def staff_takeover(conversation_id: UUID, conn=Depends(get_db)):
    cur = conn.cursor()
    try:
        _save_staff_event(cur, conversation_id, "takeover")
        conn.commit()
        return {"ok": True, "mode": "takeover_pending"}
    finally:
        cur.close()


# Validate and queue a staff reply.
@app.post("/api/staff/conversations/{conversation_id}/reply", dependencies=[Depends(require_staff)])
def staff_reply(conversation_id: UUID, payload: StaffReplyRequest, conn=Depends(get_db)):
    cur = conn.cursor()
    try:
        cur.execute("select handoff_mode from conversations where id=%s", (conversation_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if row[0] != "human":
            raise HTTPException(status_code=409, detail="Take over the conversation before replying")
        _save_staff_event(cur, conversation_id, "reply", payload.text.strip())
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()


# Queue an event that returns a conversation to AI handling.
@app.post("/api/staff/conversations/{conversation_id}/resume", dependencies=[Depends(require_staff)])
def staff_resume(conversation_id: UUID, conn=Depends(get_db)):
    cur = conn.cursor()
    try:
        _save_staff_event(cur, conversation_id, "resume")
        conn.commit()
        return {"ok": True, "mode": "resume_pending"}
    finally:
        cur.close()


# List products for the staff seller.
@app.get("/api/staff/products", dependencies=[Depends(require_staff)])
def staff_products(conn=Depends(get_db)):
    cur = conn.cursor()
    try:
        cur.execute(
            "select id,seller_id,name,name_ur,price,stock,description,attributes,image_urls "
            "from products order by name"
        )
        return {"products": [
            {"id": str(row[0]), "seller_id": str(row[1]), "name": row[2], "name_ur": row[3],
             "price": str(row[4]), "stock": row[5], "description": row[6] or "",
             "attributes": row[7] or {}, "image_urls": row[8] or []}
            for row in cur.fetchall()
        ]}
    finally:
        cur.close()


# Update an existing seller product.
@app.put("/api/staff/products/{product_id}", dependencies=[Depends(require_staff)])
def update_staff_product(product_id: UUID, payload: ProductUpdateRequest, conn=Depends(get_db)):
    if any(not _valid_product_image_url(url) for url in payload.image_urls):
        raise HTTPException(status_code=422, detail="Product images must use HTTPS or a CODShield upload")
    if any(len(url) > 2048 for url in payload.image_urls):
        raise HTTPException(status_code=422, detail="Product image URL is too long")
    cur = conn.cursor()
    try:
        cur.execute(
            "update products set description=%s,attributes=%s,image_urls=%s,"
            "price=COALESCE(%s,price),stock=COALESCE(%s,stock) where id=%s",
            (payload.description.strip(), Json(payload.attributes), Json(payload.image_urls),
             payload.price, payload.stock, product_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Product not found")
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()


# Create a product for the staff seller.
@app.post("/api/staff/products", dependencies=[Depends(require_staff)])
def create_staff_product(payload: ProductCreateRequest, conn=Depends(get_db)):
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Product name cannot be empty")
    if any(not _valid_product_image_url(url) for url in payload.image_urls):
        raise HTTPException(status_code=422, detail="Product images must use HTTPS or a CODShield upload")
    if any(len(url) > 2048 for url in payload.image_urls):
        raise HTTPException(status_code=422, detail="Product image URL is too long")
    cur = conn.cursor()
    try:
        configured_seller = os.getenv("STAFF_SELLER_ID")
        if configured_seller:
            try:
                seller_id = UUID(configured_seller)
            except ValueError:
                raise HTTPException(status_code=503, detail="STAFF_SELLER_ID must be a valid UUID")
            cur.execute("select id from sellers where id=%s", (seller_id,))
        else:
            # The current single-shop demo uses Demo Store. Configure
            # STAFF_SELLER_ID when using a different shop.
            cur.execute("select id from sellers where name='Demo Store' order by created_at limit 1")
        seller = cur.fetchone()
        if not seller:
            raise HTTPException(status_code=503, detail="Set up the shop or configure STAFF_SELLER_ID")
        cur.execute(
            "insert into products (seller_id,sku,name,name_ur,price,stock,description,attributes,image_urls) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
            (seller[0], payload.sku.strip() or None if payload.sku else None,
             payload.name.strip(), payload.name_ur.strip() or None if payload.name_ur else None,
             payload.price, payload.stock, payload.description.strip(),
             Json(payload.attributes), Json(payload.image_urls)),
        )
        product_id = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": str(product_id)}
    finally:
        cur.close()


# Validate and store an uploaded product image.
@app.post("/api/staff/products/upload-image", dependencies=[Depends(require_staff)])
async def upload_staff_product_image(request: Request):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    formats = {"image/jpeg": (b"\xff\xd8\xff", ".jpg"),
               "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
               "image/webp": (b"RIFF", ".webp")}
    if content_type not in formats:
        raise HTTPException(status_code=415, detail="Use a JPG, PNG, or WebP image")
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_PRODUCT_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="Image must be 5 MB or smaller")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid content length")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_PRODUCT_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Image must be 5 MB or smaller")
        data.extend(chunk)
    signature, extension = formats[content_type]
    matches_type = data.startswith(signature)
    if content_type == "image/webp":
        matches_type = len(data) >= 12 and data[8:12] == b"WEBP"
    if not data or len(data) > MAX_PRODUCT_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image must be between 1 byte and 5 MB")
    if not matches_type:
        raise HTTPException(status_code=415, detail="Image data does not match its file type")
    PRODUCT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    filename = uuid4().hex + extension
    (PRODUCT_IMAGES_DIR / filename).write_bytes(bytes(data))
    return {"url": f"/product-images/{filename}"}


# Serve a validated product image from local storage.
@app.get("/product-images/{filename}")
def product_image(filename: str):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|png|webp)", filename):
        raise HTTPException(status_code=404, detail="Product image not found")
    path = PRODUCT_IMAGES_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Product image not found")
    return FileResponse(path)


# Load a browser-call session using its private token.
def _browser_session(cur, token, lock=False):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cur.execute(
        "select cs.id,cs.order_id,cs.attempt,cs.status,cs.transcript,o.seller_id "
        "from call_sessions cs join orders o on o.id=cs.order_id "
        "where cs.token_hash=%s and cs.provider='browser'" + (" for update" if lock else ""), (token_hash,),
    )
    row = cur.fetchone()
    if not row:
        return None
    data = row[4] if isinstance(row[4], dict) else json.loads(row[4] or "{}")
    return {"id": row[0], "order_id": row[1], "attempt": row[2], "status": row[3],
            "seller_id": row[5], **data}


# Serve the active browser confirmation-call page.
@app.get("/call/{token}")
def browser_call(token: str, conn=Depends(get_db)):
    if len(token) > 128:
        raise HTTPException(status_code=404, detail="Call link not found")
    cur = conn.cursor()
    try:
        session = _browser_session(cur, token)
        if not session or session["status"] != "active":
            raise HTTPException(status_code=404, detail="Call link is no longer active")
        return FileResponse(CALL_PAGE, media_type="text/html; charset=utf-8")
    finally:
        cur.close()


# Load the opening prompt for an active browser call.
@app.get("/api/calls/browser/{token}")
def browser_call_info(token: str, conn=Depends(get_db)):
    cur = conn.cursor()
    try:
        session = _browser_session(cur, token)
        if not session or session["status"] != "active":
            raise HTTPException(status_code=404, detail="Call link is no longer active")
        context = session.get("context", {})
        return {"say": opening(context), "items": context.get("items", []),
                "total": context.get("total"), "address": context.get("address")}
    finally:
        cur.close()


# Process a customer response in a browser call.
@app.post("/api/calls/browser/{token}/turn")
def browser_call_turn(token: str, payload: dict, conn=Depends(get_db)):
    text = payload.get("text")
    if text is not None and (not isinstance(text, str) or len(text) > 500):
        raise HTTPException(status_code=422, detail="Turn must be at most 500 characters")
    cur = conn.cursor()
    try:
        session = _browser_session(cur, token, lock=True)
        if not session or session["status"] != "active":
            raise HTTPException(status_code=404, detail="Call session is not active")
        result = next_turn(session, text)
        cur.execute("update call_sessions set transcript=%s where id=%s",
                    (Json({k: session[k] for k in ("context", "transcript", "turns", "silence_count", "recognition_failures")}), session["id"]))
        if result["done"]:
            finish_call(cur, session, result["outcome"], result.get("new_address"))
        conn.commit()
        return {"say": result["say"], "done": result["done"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


# Load a Twilio session and verify its related seller.
def _twilio_session(cur, session_id, lock=False):
    cur.execute(
        "select cs.id,cs.order_id,cs.attempt,cs.status,cs.transcript,o.seller_id "
        "from call_sessions cs join orders o on o.id=cs.order_id "
        "where cs.id=%s and cs.provider='twilio'" + (" for update" if lock else ""), (session_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    data = row[4] if isinstance(row[4], dict) else json.loads(row[4] or "{}")
    return {"id": row[0], "order_id": row[1], "attempt": row[2], "status": row[3],
            "seller_id": row[5], **data}


# Parse the form fields sent by Twilio.
async def _twilio_form(request: Request):
    raw = (await request.body()).decode("utf-8", "replace")
    from urllib.parse import parse_qs
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


# Verify that a Twilio webhook request is authentic.
def _twilio_signature_ok(request: Request, form):
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        return False
    url = base + request.url.path + ("?" + request.url.query if request.url.query else "")
    return valid_signature(url, form, request.headers.get("X-Twilio-Signature"), os.getenv("TWILIO_AUTH_TOKEN", ""))


# Return the opening TwiML for a valid call session.
@app.post("/api/calls/twilio/{session_id}")
async def twilio_call_start(session_id: UUID, request: Request, conn=Depends(get_db)):
    form = await _twilio_form(request)
    if not _twilio_signature_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    cur = conn.cursor()
    try:
        session = _twilio_session(cur, session_id)
        if not session or session["status"] != "active":
            raise HTTPException(status_code=404, detail="Call session not found")
        url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/") + f"/api/calls/twilio/{session_id}/turn"
        xml = twiml_say(opening(session.get("context", {})), url, language="en-IN",
                        trial=os.getenv("TWILIO_TRIAL", "false").lower() == "true")
        return Response(xml, media_type="application/xml")
    finally:
        cur.close()


# Process a signed Twilio speech or keypad response.
@app.post("/api/calls/twilio/{session_id}/turn")
async def twilio_call_turn(session_id: UUID, request: Request, conn=Depends(get_db)):
    form = await _twilio_form(request)
    if not _twilio_signature_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    cur = conn.cursor()
    try:
        session = _twilio_session(cur, session_id, lock=True)
        if not session or session["status"] != "active":
            return Response("<?xml version=\"1.0\"?><Response><Hangup/></Response>", media_type="application/xml")
        text = form.get("Digits") or form.get("SpeechResult") or None
        if text in ("1", "2"):
            text = "confirm" if text == "1" else "cancel"
        result = next_turn(session, text)
        cur.execute("update call_sessions set transcript=%s where id=%s",
                    (Json({k: session[k] for k in ("context", "transcript", "turns", "silence_count", "recognition_failures")}), session_id))
        if result["done"]:
            finish_call(cur, session, result["outcome"], result.get("new_address"))
            conn.commit()
            xml = "<?xml version=\"1.0\"?><Response><Say language=\"en-IN\">Thank you. Goodbye.</Say><Hangup/></Response>"
        else:
            conn.commit()
            action = os.getenv("PUBLIC_BASE_URL", "").rstrip("/") + f"/api/calls/twilio/{session_id}/turn"
            xml = twiml_say(result["say"], action, language="en-IN")
        return Response(xml, media_type="application/xml")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
