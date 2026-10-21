import unittest
import unittest.mock
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import handlers
import events as ev
import llm
import os


SELLER_ID = uuid4()
CONVERSATION_ID = uuid4()
CUSTOMER_ID = uuid4()
PRODUCT_ID = uuid4()


class FakeCursor:
    """Small in-memory SQL fake for sales and status transition behavior."""
    # Set up this test double's initial state.
    def __init__(self, stock=10, status="dispatched", state=None, image_urls=None):
        self.stock = stock
        self.status = status
        self.state = state or {}
        self.handoff_mode = "ai"
        self.image_urls = image_urls or []
        self.one = None
        self.many = []
        self.executed = []
        self.orders = []
        self.order_items = []
        self.outgoing = []

    # Record the query and prepare a mocked database result.
    def execute(self, query, params=None):
        sql = " ".join(query.lower().split())
        params = params or ()
        self.executed.append((sql, params))
        self.one, self.many = None, []
        if sql.startswith("select name from sellers"):
            self.one = ("Demo Store",)
        elif sql.startswith("select id,name,name_ur,price,stock from products"):
            self.many = [(PRODUCT_ID, "Cotton Shirt", "\u06a9\u0627\u0679\u0646 \u0634\u0631\u0679", 1500, self.stock)]
        elif sql.startswith("select description,attributes,image_urls from products"):
            self.one = ("", {}, self.image_urls)
        elif sql.startswith("select handoff_mode from conversations"):
            self.one = (self.handoff_mode,)
        elif sql.startswith("select status,created_at from orders"):
            self.one = (self.status, datetime(2026, 1, 1, tzinfo=timezone.utc))
        elif sql.startswith("select state from conversations"):
            self.one = (dict(self.state),)
        elif sql.startswith("select count(*) from messages"):
            self.one = (3,)
        elif sql.startswith("select direction,body from messages"):
            self.many = [("in", "I want one Cotton Shirt")]
        elif sql.startswith("select name,name_ur,price,stock from products"):
            self.one = ("Cotton Shirt", "\u06a9\u0627\u0679\u0646 \u0634\u0631\u0679", 1500, self.stock)
        elif sql.startswith("select customer_id from conversations"):
            self.one = (CUSTOMER_ID,)
        elif sql.startswith("update products set stock=stock-%s"):
            qty, product_id, old_stock = params
            if product_id == PRODUCT_ID and self.stock == old_stock:
                self.stock -= qty
        elif sql.startswith("insert into orders"):
            self.orders.append(params)
        elif sql.startswith("insert into order_items"):
            self.order_items.append(params)
        elif sql.startswith("update conversations set state="):
            if "state = (state - 'pending_order')" in sql:
                self.state.pop("pending_order", None)
                self.state.update(params[0].adapted)
            elif len(params) == 2 and hasattr(params[0], "adapted"):
                self.state = params[0].adapted
            elif "state='{}'" in sql:
                self.state = {}
        elif sql.startswith("update conversations set handoff_mode='pending'"):
            self.handoff_mode = "pending"
        elif sql.startswith("update conversations set handoff_mode='human'"):
            self.handoff_mode = "human"
        elif sql.startswith("update conversations set handoff_mode='ai'"):
            self.handoff_mode = "ai"
        elif sql.startswith("insert into messages"):
            self.outgoing.append(params)
        elif sql.startswith("select status from orders"):
            self.one = (self.status,)
        elif sql.startswith("update orders set status="):
            self.status = params[0]
        elif sql.startswith("select product_id,qty from order_items"):
            self.many = [(PRODUCT_ID, 2)]
        elif sql.startswith("update products set stock=stock+%s"):
            qty, product_id = params
            if product_id == PRODUCT_ID:
                self.stock += qty

    # Return and clear the next mocked database row.
    def fetchone(self):
        row, self.one = self.one, None
        return row

    # Return and clear the mocked database rows.
    def fetchall(self):
        rows, self.many = self.many, []
        return rows


# Build a sample message event for the test.
def event(text):
    return SimpleNamespace(
        event_id=uuid4(), seller_id=SELLER_ID, conversation_id=CONVERSATION_ID, text=text,
    )


# Build a proposed order response for the test.
def proposed_order(price=1):
    return {"reply": "Sure", "order": {
        "customer_name": "Customer", "address": "House 5, Lahore",
        "items": [{"product_name": "Cotton Shirt", "qty": 1, "price": price}],
        "total": price,
    }}


class SalesOrderFlowTests(unittest.TestCase):
    # Create fresh fixtures for this test.
    def setUp(self):
        self.event_patch = unittest.mock.patch("handlers.save_event", return_value=None)
        self.event_patch.start()

    # Restore temporary test state after the test.
    def tearDown(self):
        self.event_patch.stop()

    # Verify order waits for confirmation of code computed readback.
    def test_order_waits_for_confirmation_of_code_computed_readback(self):
        cursor = FakeCursor()
        with unittest.mock.patch("handlers.ask_json", return_value=proposed_order()):
            handlers.sales_agent(cursor, event("I want one shirt"))
        self.assertEqual(cursor.orders, [])
        self.assertIn("Rs 1500.00", cursor.outgoing[-1][1])
        with unittest.mock.patch("handlers.ask_json", side_effect=AssertionError("confirmation should use saved proposal")):
            handlers.sales_agent(cursor, event("yes"))
        self.assertEqual(len(cursor.orders), 1)
        self.assertEqual(cursor.stock, 9)

    # Verify dispatched order does not block second order in same conversation.
    def test_dispatched_order_does_not_block_second_order_in_same_conversation(self):
        cursor = FakeCursor(status="dispatched", state={"order_id": str(uuid4()), "stock_reserved": True})
        with unittest.mock.patch("handlers.ask_json", return_value=proposed_order()):
            handlers.sales_agent(cursor, event("I want another shirt"))
        self.assertEqual(cursor.orders, [])
        with unittest.mock.patch("handlers.ask_json", side_effect=AssertionError("should confirm saved second cart")):
            handlers.sales_agent(cursor, event("yes please"))
        self.assertEqual(len(cursor.orders), 1)
        history_query = next(sql for sql, _ in cursor.executed if sql.startswith("select direction,body from messages"))
        self.assertIn("created_at >", history_query)

    # Verify stock shortage discloses available stock and does not order.
    def test_stock_shortage_discloses_available_stock_and_does_not_order(self):
        cursor = FakeCursor(stock=0)
        with unittest.mock.patch("handlers.ask_json", return_value=proposed_order()):
            handlers.sales_agent(cursor, event("I want one shirt"))
        self.assertEqual(cursor.orders, [])
        self.assertEqual(cursor.stock, 0)
        self.assertIn("available: 0", cursor.outgoing[-1][1])
        self.assertIn("haven't placed the order", cursor.outgoing[-1][1])

    # Verify ai price and total are ignored for database readback.
    def test_ai_price_and_total_are_ignored_for_database_readback(self):
        cursor = FakeCursor()
        with unittest.mock.patch("handlers.ask_json", return_value=proposed_order(price=1)):
            handlers.sales_agent(cursor, event("one shirt please"))
        self.assertEqual(cursor.orders, [])
        self.assertIn("Rs 1500.00", cursor.outgoing[-1][1])
        self.assertNotIn("Rs 1.00", cursor.outgoing[-1][1])
        with unittest.mock.patch("handlers.ask_json", side_effect=AssertionError("stored quote should be confirmed directly")):
            handlers.sales_agent(cursor, event("yes"))
        self.assertEqual(cursor.orders[0][-1], 1500)
        self.assertEqual(cursor.order_items[0][-1], 1500)

    # Verify refused delivery restores stock once after status transition.
    def test_refused_delivery_restores_stock_once_after_status_transition(self):
        cursor = FakeCursor(stock=4, status="dispatched")
        delivery = ev.DeliveryOutcome(
            seller_id=SELLER_ID, correlation_id=uuid4(), idempotency_key="delivery-test",
            order_id=uuid4(), outcome="refused",
        )
        handlers.orchestrator_delivery_outcome(cursor, delivery)
        handlers.orchestrator_delivery_outcome(cursor, delivery)
        self.assertEqual(cursor.status, "refused")
        self.assertEqual(cursor.stock, 6)
        restores = [sql for sql, _ in cursor.executed if sql.startswith("update products set stock=stock+")]
        self.assertEqual(len(restores), 1)

    # Verify pending handoff keeps ai available without repeating notice.
    def test_pending_handoff_keeps_ai_available_without_repeating_notice(self):
        cursor = FakeCursor()
        duplicate_notice = (
            "I don't have that detail. A shop employee has been notified and will join shortly. "
            "A shop employee has been notified and will join this chat shortly."
        )
        with unittest.mock.patch("handlers.ask_json", return_value={
            "reply": duplicate_notice, "order": None, "handoff": True,
            "handoff_reason": "Customer asked about a missing size", "show_product_images": [],
        }) as ask:
            handlers.sales_agent(cursor, event("Do you have this size?"))
            ask.assert_called_once()
        sent = cursor.outgoing[-1][1]
        self.assertNotIn("employee has been notified", sent)
        self.assertIn("staff inbox", sent)
        self.assertEqual(cursor.handoff_mode, "pending")
        self.assertEqual(handlers.save_event.call_count, 1)
        self.assertTrue(any("update conversations set handoff_mode='pending'" in sql
                            for sql, _ in cursor.executed))

        with unittest.mock.patch("handlers.ask_json", return_value={
            "reply": "We have blue in stock. What size do you need?", "order": None,
            "handoff": False, "show_product_images": [],
        }) as ask:
            handlers.sales_agent(cursor, event("I also need another color"))
            ask.assert_called_once()
        self.assertIn("blue in stock", cursor.outgoing[-1][1])
        self.assertEqual(cursor.handoff_mode, "pending")

        with unittest.mock.patch("handlers.ask_json", return_value={
            "reply": duplicate_notice, "order": None, "handoff": True,
            "handoff_reason": "Still needs staff", "show_product_images": [],
        }):
            handlers.sales_agent(cursor, event("Can you check another unavailable size?"))
        self.assertEqual(handlers.save_event.call_count, 1)
        self.assertNotIn("employee has been notified", cursor.outgoing[-1][1])
        self.assertIn("team is checking", cursor.outgoing[-1][1])

        cursor.handoff_mode = "human"
        with unittest.mock.patch("handlers.ask_json", side_effect=AssertionError("AI called after takeover")):
            handlers.sales_agent(cursor, event("Hello"))

    # Verify requested uploaded image is sent even if model requests handoff.
    def test_requested_uploaded_image_is_sent_even_if_model_requests_handoff(self):
        image_url = "/product-images/0123456789abcdef0123456789abcdef.webp"
        cursor = FakeCursor(image_urls=[image_url])
        with unittest.mock.patch("handlers.ask_json", return_value={
            "reply": "Here is the Cotton Shirt.", "order": None,
            "handoff": True, "handoff_reason": "Photo query", "show_product_images": [],
        }):
            handlers.sales_agent(cursor, event("Show me a picture of the Cotton Shirt"))

        media_urls = cursor.outgoing[-1][2].adapted
        self.assertEqual(media_urls, [image_url])
        self.assertEqual(cursor.handoff_mode, "ai")

    # Verify staff takeover reply and resume handlers.
    def test_staff_takeover_reply_and_resume_handlers(self):
        cursor = FakeCursor()
        cursor.handoff_mode = "human"
        takeover = ev.HumanTakeover(
            seller_id=SELLER_ID, correlation_id=CONVERSATION_ID,
            idempotency_key="takeover", conversation_id=CONVERSATION_ID,
        )
        reply = ev.HumanReply(
            seller_id=SELLER_ID, correlation_id=CONVERSATION_ID,
            idempotency_key="reply", conversation_id=CONVERSATION_ID, text="We have size M.",
        )
        resume = ev.HumanResume(
            seller_id=SELLER_ID, correlation_id=CONVERSATION_ID,
            idempotency_key="resume", conversation_id=CONVERSATION_ID,
        )
        handlers.human_takeover(cursor, takeover)
        handlers.human_reply(cursor, reply)
        handlers.human_resume(cursor, resume)
        self.assertTrue(any("set handoff_mode='human'" in sql for sql, _ in cursor.executed))
        self.assertTrue(any("insert into messages" in sql and "We have size M." in params
                            for sql, params in cursor.executed))
        self.assertTrue(any("set handoff_mode='ai'" in sql for sql, _ in cursor.executed))


class LLMClientConfigurationTests(unittest.TestCase):
    # Verify gemini client bypasses dead shell proxy.
    def test_gemini_client_bypasses_dead_shell_proxy(self):
        http_client = object()
        with unittest.mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
             unittest.mock.patch.object(llm, "_client", None), \
             unittest.mock.patch("llm.httpx.Client", return_value=http_client) as http_factory, \
             unittest.mock.patch("llm.OpenAI") as openai_factory:
            llm._get_client()

        http_factory.assert_called_once_with(trust_env=False, timeout=15.0)
        self.assertIs(openai_factory.call_args.kwargs["http_client"], http_client)


if __name__ == "__main__":
    unittest.main()
