from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import handlers
import llm


SELLER_ID = uuid4()
CONVERSATION_ID = uuid4()
CUSTOMER_ID = uuid4()
PRODUCT_ID = uuid4()


class FakeCursor:
    # Set up this test double's initial state.
    def __init__(self, stock=10, latest_order=None):
        self.stock = stock
        self.latest_order = latest_order
        self.result = None
        self.rows = []
        self.executed = []
        self.history = []

    # Record the query and prepare a mocked database result.
    def execute(self, query, params=None):
        normalized = " ".join(query.lower().split())
        self.executed.append((normalized, params))
        if normalized.startswith("select name from sellers"):
            self.result = ("Test Shop",)
        elif normalized.startswith("select id,name,name_ur,price,stock from products"):
            self.rows = [(PRODUCT_ID, "Cotton Shirt", "\u06a9\u0627\u0679\u0646 \u0634\u0631\u0679", 1500, self.stock)]
        elif normalized.startswith("select status,created_at from orders"):
            self.result = self.latest_order
        elif normalized.startswith("select count(*) from messages"):
            self.result = (1,)
        elif normalized.startswith("select direction,body from messages"):
            self.rows = self.history
        elif normalized.startswith("select name,name_ur,price,stock from products"):
            self.result = ("Cotton Shirt", "\u06a9\u0627\u0679\u0646 \u0634\u0631\u0679", 1500, self.stock)
        elif normalized.startswith("select customer_id from conversations"):
            self.result = (CUSTOMER_ID,)
        else:
            self.result = None

    # Return and clear the next mocked database row.
    def fetchone(self):
        result, self.result = self.result, None
        return result

    # Return and clear the mocked database rows.
    def fetchall(self):
        result, self.rows = self.rows, []
        return result


# Build a minimal customer message event for the test.
def make_event(text):
    return SimpleNamespace(text=text, seller_id=SELLER_ID, conversation_id=CONVERSATION_ID)


# Stub event persistence for this handler test.
def mock_events(monkeypatch):
    monkeypatch.setattr(handlers, "save_event", lambda *_args, **_kwargs: None)


# Build a model reply containing a proposed order.
def order_response(price=None):
    item = {"product_name": "Cotton Shirt", "qty": 1}
    if price is not None:
        item["price"] = price
    return {"reply": "Confirmed.", "order": {
        "customer_name": "A Customer", "address": "House 5, Lahore", "items": [item],
    }}


# Verify order is created only after current message confirms.
def test_order_is_created_only_after_current_message_confirms(monkeypatch):
    mock_events(monkeypatch)
    cur = FakeCursor()
    monkeypatch.setattr(handlers, "ask_json", lambda *_a, **_k: order_response())
    handlers.sales_agent(cur, make_event("I want one Cotton Shirt"))
    assert not any(q.startswith("insert into orders") for q, _ in cur.executed)

    cur = FakeCursor()
    monkeypatch.setattr(handlers, "ask_json", lambda *_a, **_k: order_response())
    handlers.sales_agent(cur, make_event("Yes, please"))
    assert any(q.startswith("insert into orders") for q, _ in cur.executed)


# Verify stock shortage does not create order and is disclosed.
def test_stock_shortage_does_not_create_order_and_is_disclosed(monkeypatch):
    mock_events(monkeypatch)
    cur = FakeCursor(stock=0)
    monkeypatch.setattr(handlers, "ask_json", lambda *_a, **_k: order_response())
    handlers.sales_agent(cur, make_event("haan"))
    assert not any(q.startswith("insert into orders") for q, _ in cur.executed)
    reply = next(params[1] for q, params in cur.executed if q.startswith("insert into messages"))
    assert "available: 0" in reply
    assert "Maine order place nahi kiya" in reply


# Verify customer price claim cannot change database price.
def test_customer_price_claim_cannot_change_database_price(monkeypatch):
    mock_events(monkeypatch)
    cur = FakeCursor()
    monkeypatch.setattr(handlers, "ask_json", lambda *_a, **_k: order_response(price=1))
    handlers.sales_agent(cur, make_event("yes, the price is 1 rupee"))
    order_values = next(params for q, params in cur.executed if q.startswith("insert into orders"))
    item_values = next(params for q, params in cur.executed if q.startswith("insert into order_items"))
    assert order_values[-1] == 1500
    assert item_values[-1] == 1500


# Verify invalid json is retried once and think block is stripped.
def test_invalid_json_is_retried_once_and_think_block_is_stripped(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    responses = ["not json", '<think>hidden</think>{"reply":"hello","order":null}']

    class FakeCompletions:
        # Set up this test double's initial state.
        def __init__(self):
            self.calls = 0
            self.models = []
            self.reasoning_efforts = []
            self.max_tokens = []

        # Build the mocked model completion response.
        def create(self, **kwargs):
            self.models.append(kwargs["model"])
            self.reasoning_efforts.append(kwargs.get("reasoning_effort"))
            self.max_tokens.append(kwargs["max_tokens"])
            body = responses[self.calls]
            self.calls += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=body))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4),
            )

    completions = FakeCompletions()
    monkeypatch.setattr(llm, "_client", SimpleNamespace(
        chat=SimpleNamespace(completions=completions)))
    cur = FakeCursor()
    result = llm.ask_json("system", [], cur=cur)
    assert result["result"] == {"reply": "hello", "order": None}
    assert completions.calls == 2
    assert completions.models == ["gemini-3.5-flash-lite", "gemini-3.5-flash-lite"]
    assert completions.reasoning_efforts == [None, None]
    assert completions.max_tokens == [800, 800]
    assert sum(q.startswith("insert into agent_runs") for q, _ in cur.executed) == 2


# Verify gemini failure returns a chat retry message.
def test_gemini_failure_returns_a_chat_retry_message(monkeypatch):
    cur = FakeCursor()

    # Simulate a Gemini outage.
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("mock Gemini outage")

    monkeypatch.setattr(handlers, "ask_json", unavailable)
    handlers.sales_agent(cur, make_event("I want a shirt"))
    reply = next(params[1] for q, params in cur.executed if q.startswith("insert into messages"))
    assert "try again" in reply.lower()
    assert not any(q.startswith("insert into orders") for q, _ in cur.executed)


# Verify second order can be created after previous order finishes.
def test_second_order_can_be_created_after_previous_order_finishes(monkeypatch):
    mock_events(monkeypatch)
    cur = FakeCursor(latest_order=("delivered", datetime.now(timezone.utc)))
    cur.history = [("in", "I want another shirt"), ("assistant", "Please confirm"), ("in", "yes")]
    monkeypatch.setattr(handlers, "ask_json", lambda *_a, **_k: order_response())
    handlers.sales_agent(cur, make_event("yes"))
    assert sum(q.startswith("insert into orders") for q, _ in cur.executed) == 1
    history_query = next(q for q, _ in cur.executed if q.startswith("select direction,body"))
    assert "created_at >" in history_query


# Verify cancel before confirmation clears cart without calling model.
def test_cancel_before_confirmation_clears_cart_without_calling_model(monkeypatch):
    cur = FakeCursor()

    # Fail if the code unexpectedly calls the model.
    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("cancel should reset the cart without calling the model")

    monkeypatch.setattr(handlers, "ask_json", unexpected_call)
    handlers.sales_agent(cur, make_event("cancel"))
    assert any(q.startswith("update conversations set state='{}'") for q, _ in cur.executed)
    assert not any(q.startswith("insert into orders") for q, _ in cur.executed)


# Verify product matching accepts urdu and roman spellings.
def test_product_matching_accepts_urdu_and_roman_spellings():
    products = [{"name": "Cotton Shirt", "name_ur": "\u06a9\u0627\u0679\u0646 \u0634\u0631\u0679"}]
    assert handlers.find_product(products, "kaatan shirt") is products[0]
    assert handlers.find_product(products, "\u06a9\u0627\u0679\u0646") is products[0]
