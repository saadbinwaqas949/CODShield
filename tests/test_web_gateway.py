import unittest
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

import web_gateway


class FakeDatabase:
    # Set up this test double's initial state.
    def __init__(self):
        self.seller_id = uuid4()
        self.customer_id = uuid4()
        self.conversation_id = uuid4()
        self.phone_hash = "hashed-phone"
        self.messages = []
        self.order = (uuid4(), "awaiting_confirmation", "medium")
        self.reasons = ["Example reason"]
        self.exists = True
        self.product_id = uuid4()
        self.executed = []
        self.connections = []

    # Create and track a fake database connection.
    def connection(self):
        conn = FakeConnection(self)
        self.connections.append(conn)
        return conn


class FakeConnection:
    # Set up this test double's initial state.
    def __init__(self, database):
        self.database = database
        self.committed = False
        self.closed = False
        self.rolled_back = False

    # Return a fake cursor for the current test.
    def cursor(self):
        return FakeCursor(self.database)

    # Record that the fake connection committed.
    def commit(self):
        self.committed = True

    # Record that the fake connection rolled back.
    def rollback(self):
        self.rolled_back = True

    # Record that the fake database resource was closed.
    def close(self):
        self.closed = True


class FakeCursor:
    # Set up this test double's initial state.
    def __init__(self, database):
        self.database = database
        self.one = None
        self.many = []
        self.closed = False
        self.rowcount = 1

    # Record the query and prepare a mocked database result.
    def execute(self, query, params=None):
        sql = " ".join(query.lower().split())
        self.database.executed.append((sql, params))
        # Match route queries by their leading SQL shape and return controlled fixture rows.
        self.one, self.many = None, []
        if sql.startswith("select id from sellers"):
            self.one = (self.database.seller_id,)
        elif sql.startswith("select id from customers"):
            self.one = None
        elif sql.startswith("select c.seller_id,cu.phone_hash"):
            if self.database.exists:
                self.one = (self.database.seller_id, self.database.phone_hash)
        elif sql.startswith("select created_at from messages"):
            message_id, conversation_id = params
            for item in self.database.messages:
                if item[0] == message_id and conversation_id == self.database.conversation_id:
                    self.one = (item[3],)
                    break
        elif sql.startswith("select id,direction,body,created_at from messages"):
            if len(params) == 1:
                self.many = list(self.database.messages)
            else:
                conversation_id, after_time, _same_time, after_id = params
                if conversation_id == self.database.conversation_id:
                    self.many = [row for row in self.database.messages
                                 if (row[3], row[0]) > (after_time, after_id)]
        elif sql.startswith("select id,status,risk_level from orders"):
            self.one = self.database.order
        elif sql.startswith("select reasons from risk_assessments"):
            self.one = (self.database.reasons,)
        elif sql.startswith("select count(*) filter (where created_at"):
            self.one = (2, 1)
        elif sql.startswith("select coalesce(sum(total_amount),0) from orders"):
            self.one = ("1250.00",)
        elif sql.startswith("select count(*) from conversations where seller_id="):
            self.one = (1,)
        elif sql.startswith("select o.id,o.status,o.total_amount"):
            self.many = [(self.database.order[0], self.database.order[1], "1250.00", 0.2,
                          "medium", datetime.now(timezone.utc), "Demo Customer",
                          self.database.reasons, "confirmed", None, 100, 25, 500,
                          "gemini-test")]
        elif sql.startswith("select type,correlation_id,created_at,processed_at,attempts,failed_at"):
            self.many = [("order.created", self.database.order[0], datetime.now(timezone.utc),
                          datetime.now(timezone.utc), 1, None)]
        elif sql.startswith("update products set description="):
            self.rowcount = 1
        elif sql.startswith("insert into products"):
            self.one = (self.database.product_id,)

    # Return and clear the next mocked database row.
    def fetchone(self):
        row, self.one = self.one, None
        return row

    # Return and clear the mocked database rows.
    def fetchall(self):
        rows, self.many = self.many, []
        return rows

    # Record that the fake database resource was closed.
    def close(self):
        self.closed = True


class WebGatewayTests(unittest.TestCase):
    # Create fresh fixtures for this test.
    def setUp(self):
        self.db = FakeDatabase()
        web_gateway._send_times.clear()
        self.encryption_patch = patch("gateway.encrypt_phone", return_value=b"test-encrypted-phone")
        self.encryption_patch.start()
        self.connection_patch = patch("web_gateway.connect", side_effect=self.db.connection)
        self.connection_patch.start()
        self.client = TestClient(web_gateway.app)

    # Restore temporary test state after the test.
    def tearDown(self):
        self.client.close()
        self.connection_patch.stop()
        self.encryption_patch.stop()

    # Verify root serves the plain demo chat.
    def test_root_serves_the_plain_demo_chat(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("DEMO", response.text)
        self.assertIn('dir = "auto"', response.text)
        self.assertIn('^\\/product-images\\/', response.text)

    # Verify staff page and staff api authentication.
    def test_staff_page_and_staff_api_authentication(self):
        page = self.client.get("/staff")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Needs staff", page.text)
        self.assertIn('token=tokenInput.value.trim()||sessionStorage.getItem("codshieldStaffToken")', page.text)
        with patch.dict(os.environ, {}, clear=True):
            unconfigured = self.client.get("/api/staff/conversations")
        self.assertEqual(unconfigured.status_code, 503)
        with patch.dict(os.environ, {"STAFF_ACCESS_TOKEN": "test-token"}):
            unauthorized = self.client.get("/api/staff/conversations")
            authorized = self.client.get("/api/staff/conversations",
                                         headers={"X-Staff-Token": "test-token"})
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.json(), {"conversations": []})

    # Verify staff cockpit is seller scoped and reports linked ai usage.
    def test_staff_cockpit_is_seller_scoped_and_reports_linked_ai_usage(self):
        seller_id = self.db.seller_id
        with patch.dict(os.environ, {
            "STAFF_ACCESS_TOKEN": "test-token", "STAFF_SELLER_ID": str(seller_id),
            "GEMINI_INPUT_USD_PER_MILLION": "1", "GEMINI_OUTPUT_USD_PER_MILLION": "2",
        }):
            response = self.client.get("/api/staff/cockpit",
                                       headers={"X-Staff-Token": "test-token"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["metrics"]["orders_today"], 2)
        self.assertEqual(body["metrics"]["staff_handoffs"], 1)
        self.assertTrue(body["ai_cost_configured"])
        self.assertEqual(body["orders"][0]["tokens_in"], 100)
        self.assertAlmostEqual(body["orders"][0]["estimated_ai_cost_usd"], 0.00015)
        scoped_queries = [params for sql, params in self.db.executed
                         if sql.startswith("select o.id,o.status,o.total_amount")]
        self.assertEqual(scoped_queries, [(seller_id,)])

    # Verify staff can update product price and stock.
    def test_staff_can_update_product_price_and_stock(self):
        product_id = uuid4()
        payload = {"price": "2499.50", "stock": 12, "description": "Cotton shirt",
                   "attributes": {"sizes": ["S", "M"]}, "image_urls": []}
        with patch.dict(os.environ, {"STAFF_ACCESS_TOKEN": "test-token"}):
            response = self.client.put(
                f"/api/staff/products/{product_id}", json=payload,
                headers={"X-Staff-Token": "test-token"},
            )
            invalid = self.client.put(
                f"/api/staff/products/{product_id}", json={**payload, "stock": -1},
                headers={"X-Staff-Token": "test-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(invalid.status_code, 422)
        sql, params = next((sql, params) for sql, params in self.db.executed
                           if sql.startswith("update products set description="))
        self.assertEqual(str(params[3]), "2499.50")
        self.assertEqual(params[4], 12)

    # Verify staff can create a product with manual inventory.
    def test_staff_can_create_a_product_with_manual_inventory(self):
        product = {"name": "Blue Hoodie", "name_ur": "", "sku": "HOODIE-1",
                   "price": "3500.00", "stock": 7, "description": "Cotton hoodie",
                   "attributes": {"sizes": ["M", "L"]}, "image_urls": []}
        with patch.dict(os.environ, {"STAFF_ACCESS_TOKEN": "test-token"}, clear=True):
            response = self.client.post("/api/staff/products", json=product,
                                        headers={"X-Staff-Token": "test-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(self.db.product_id))
        sql, params = next((sql, params) for sql, params in self.db.executed
                           if sql.startswith("insert into products"))
        self.assertIn("values (%s,%s,%s,%s,%s,%s,%s,%s,%s)", sql)
        self.assertEqual(params[2], "Blue Hoodie")
        self.assertEqual(params[5], 7)

    # Verify staff image upload accepts valid image and rejects fake type.
    def test_staff_image_upload_accepts_valid_image_and_rejects_fake_type(self):
        with patch.dict(os.environ, {"STAFF_ACCESS_TOKEN": "test-token"}, clear=True), \
             patch.object(Path, "mkdir"), patch.object(Path, "write_bytes") as write_bytes:
            headers = {"X-Staff-Token": "test-token", "Content-Type": "image/png"}
            uploaded = self.client.post("/api/staff/products/upload-image",
                                        content=b"\x89PNG\r\n\x1a\nimage-data", headers=headers)
            invalid = self.client.post("/api/staff/products/upload-image",
                                       content=b"not-a-png", headers=headers)
        self.assertEqual(uploaded.status_code, 200)
        self.assertRegex(uploaded.json()["url"], r"^/product-images/[a-f0-9]{32}\.png$")
        write_bytes.assert_called_once()
        self.assertTrue(write_bytes.call_args.args[0].startswith(b"\x89PNG"))
        self.assertEqual(invalid.status_code, 415)

    # Verify start uses gateway and stores only hashed phone.
    def test_start_uses_gateway_and_stores_only_hashed_phone(self):
        response = self.client.post("/api/chat/start", json={
            "phone": "+92 300 1234567", "name": "Demo Customer",
        })
        self.assertEqual(response.status_code, 200)
        created_conversation = next(params[0] for sql, params in self.db.executed
                                   if sql.startswith("insert into conversations"))
        self.assertEqual(response.json(), {"conversation_id": str(created_conversation)})
        customer_insert = next(params for sql, params in self.db.executed
                               if sql.startswith("insert into customers"))
        self.assertNotIn("+92 300 1234567", customer_insert)
        self.assertTrue(self.db.connections[-1].committed)
        self.assertTrue(self.db.connections[-1].closed)

    # Verify send enqueues through gateway and rejects overlong text.
    def test_send_enqueues_through_gateway_and_rejects_overlong_text(self):
        response = self.client.post("/api/chat/send", json={
            "conversation_id": str(self.db.conversation_id), "text": "1 shirt chahiye",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(sql.startswith("insert into events") for sql, _ in self.db.executed))
        too_long = self.client.post("/api/chat/send", json={
            "conversation_id": str(self.db.conversation_id), "text": "x" * 501,
        })
        self.assertEqual(too_long.status_code, 422)

    # Verify bad or unknown conversation ids do not return messages.
    def test_bad_or_unknown_conversation_ids_do_not_return_messages(self):
        invalid = self.client.get("/api/chat/messages?conversation_id=not-a-uuid")
        self.assertEqual(invalid.status_code, 422)
        self.db.exists = False
        unknown_id = self.client.get(
            f"/api/chat/messages?conversation_id={self.db.conversation_id}")
        self.assertEqual(unknown_id.status_code, 404)
        send = self.client.post("/api/chat/send", json={
            "conversation_id": str(self.db.conversation_id), "text": "hello",
        })
        self.assertEqual(send.status_code, 404)

    # Verify rate limit is per conversation.
    def test_rate_limit_is_per_conversation(self):
        payload = {"conversation_id": str(self.db.conversation_id), "text": "hello"}
        statuses = [self.client.post("/api/chat/send", json=payload).status_code
                    for _ in range(web_gateway.RATE_LIMIT + 1)]
        self.assertEqual(statuses[:-1], [200] * web_gateway.RATE_LIMIT)
        self.assertEqual(statuses[-1], 429)

    # Verify message cursor and optional debug data are scoped.
    def test_message_cursor_and_optional_debug_data_are_scoped(self):
        first_id, second_id = uuid4(), uuid4()
        first_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        second_time = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        self.db.messages = [
            (first_id, "in", "1 shirt chahiye", first_time),
            (second_id, "out", "What size?", second_time),
        ]
        response = self.client.get(
            f"/api/chat/messages?conversation_id={self.db.conversation_id}&after={first_id}&debug=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([m["id"] for m in response.json()["messages"]], [str(second_id)])
        self.assertEqual(response.json()["order"]["risk_reasons"], self.db.reasons)
        regular = self.client.get(
            f"/api/chat/messages?conversation_id={self.db.conversation_id}")
        self.assertNotIn("risk_reasons", regular.json()["order"])


if __name__ == "__main__":
    unittest.main()
