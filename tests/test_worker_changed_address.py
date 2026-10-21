import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import handlers
import llm
import worker
from address_clean import clean_address
from events import MessageReceived


class RetryCursor:
    # Set up this test double's initial state.
    def __init__(self, database):
        self.database = database
        self.row = None

    # Record the query and prepare a mocked database result.
    def execute(self, sql, params=None):
        normalized = " ".join(sql.lower().split())
        # Emulate only the SQL effects the retry worker relies on; no real database is used.
        if normalized.startswith("select id,payload,attempts from events"):
            if not self.database.failed_at:
                self.row = (self.database.event_id, self.database.payload,
                            self.database.attempts)
        elif normalized.startswith("update events set attempts="):
            # Distinguish scheduled retries from the final dead-letter update by its SQL fields.
            if "failed_at=now()" in normalized:
                attempts, error, _event_id = params
                self.database.attempts = attempts
                self.database.last_error = error
                self.database.failed_at = True
                self.database.processed_at = True
            else:
                attempts, error, delay, _event_id = params
                self.database.attempts = attempts
                self.database.last_error = error
                self.database.delays.append(delay)
        elif normalized.startswith("insert into messages"):
            self.database.messages.append(params)

    # Return and clear the next mocked database row.
    def fetchone(self):
        row, self.row = self.row, None
        return row

    # Record that the fake database resource was closed.
    def close(self):
        pass


class RetryConnection:
    # Set up this test double's initial state.
    def __init__(self, payload):
        self.event_id = uuid4()
        self.payload = payload
        self.attempts = 0
        self.last_error = None
        self.failed_at = False
        self.processed_at = False
        self.delays = []
        self.messages = []
        self.commits = 0
        self.rollbacks = 0

    # Return a fake cursor for the current test.
    def cursor(self):
        return RetryCursor(self)

    # Record that the fake connection committed.
    def commit(self):
        self.commits += 1

    # Record that the fake connection rolled back.
    def rollback(self):
        self.rollbacks += 1


class ChangedAddressCursor:
    # Set up this test double's initial state.
    def __init__(self):
        self.one = None
        self.inserted_address = None
        self.updated_order = None
        self.deleted_rows = False

    # Record the query and prepare a mocked database result.
    def execute(self, sql, params=None):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("select customer_id from orders"):
            self.one = (CUSTOMER_ID,)
        elif normalized.startswith("insert into addresses"):
            self.inserted_address = params
        elif normalized.startswith("update orders set address_id"):
            self.updated_order = params
        elif normalized.startswith("delete from addresses"):
            self.deleted_rows = True

    # Return and clear the next mocked database row.
    def fetchone(self):
        row, self.one = self.one, None
        return row


CUSTOMER_ID = uuid4()
ORDER_ID = uuid4()


class ProjectReliabilityTests(unittest.TestCase):
    # Verify message event retries twice then dead letters and replies.
    def test_message_event_retries_twice_then_dead_letters_and_replies(self):
        event = MessageReceived(
            seller_id=uuid4(), correlation_id=uuid4(), idempotency_key="retry-test",
            conversation_id=uuid4(), customer_phone_hash="test-hash", kind="text", text="hello",
        )
        conn = RetryConnection(event.model_dump(mode="json"))

        # Capture the event failure state and customer reply.
        def fail_handler(_cur, _event):
            raise RuntimeError("mock handler failure")

        with patch.dict(worker.HANDLERS, {"message.received": [fail_handler]}):
            self.assertTrue(worker.process_one(conn))
            self.assertTrue(worker.process_one(conn))
            self.assertTrue(worker.process_one(conn))

        self.assertEqual(conn.delays, [10, 60])
        self.assertEqual(conn.attempts, 3)
        self.assertEqual(conn.last_error, "RuntimeError")
        self.assertTrue(conn.failed_at)
        self.assertTrue(conn.processed_at)
        self.assertEqual(len(conn.messages), 1)
        self.assertEqual(conn.messages[0][0], event.conversation_id)
        self.assertEqual(conn.messages[0][1], worker.TECHNICAL_ERROR_REPLY)
        self.assertFalse(worker.process_one(conn))

    # Verify changed address adds new cleaned row and repoints order.
    def test_changed_address_adds_new_cleaned_row_and_repoints_order(self):
        cur = ChangedAddressCursor()
        event = SimpleNamespace(
            outcome="changed_address", new_address="House 20, Street 3, Gulberg III, Lahore",
            order_id=ORDER_ID, seller_id=uuid4(),
        )
        with patch.object(handlers, "set_status", return_value=True):
            handlers.orchestrator_confirmed(cur, event)

        self.assertIsNotNone(cur.inserted_address)
        new_id, customer_id, raw, city, area, street, quality, addr_hash, flags = cur.inserted_address
        expected = clean_address(event.new_address)
        self.assertEqual(customer_id, CUSTOMER_ID)
        self.assertEqual(raw, event.new_address)
        self.assertEqual((city, area, street, quality, addr_hash),
                         (expected["city"], expected["area"], expected["street"],
                          expected["quality_score"], expected["addr_hash"]))
        self.assertEqual(cur.updated_order, (new_id, ORDER_ID))
        self.assertFalse(cur.deleted_rows)

    # Verify existing twilio session prevents redial on handler retry.
    def test_existing_twilio_session_prevents_redial_on_handler_retry(self):
        existing_id = uuid4()

        class ExistingSessionCursor:
            # Record the query and prepare a mocked database result.
            def execute(self, sql, params=None):
                self.sql = " ".join(sql.lower().split())

            # Return and clear the next mocked database row.
            def fetchone(self):
                return (existing_id,)

        cur = ExistingSessionCursor()
        requested = SimpleNamespace(order_id=ORDER_ID, attempt=1)
        with patch.dict(os.environ, {"CALL_PROVIDER": "twilio"}), \
                patch.object(handlers.twilio_calls, "start_call") as dial, \
                patch.object(handlers, "order_context") as load_context:
            handlers.start_confirmation(cur, requested)
        self.assertIn("select id from call_sessions", cur.sql)
        dial.assert_not_called()
        load_context.assert_not_called()

    # Verify llm uses configured limit timeout and retries invalid json once.
    def test_llm_uses_configured_limit_timeout_and_retries_invalid_json_once(self):
        replies = ["not JSON", '<think>hidden</think>{"reply":"ok","order":null}']
        completions = Mock()
        completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None)
            for text in replies
        ]
        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=completions,
        ))
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "unit-test-key",
            "GEMINI_MODEL": "gemini-test-model",
            "GEMINI_REASONING_EFFORT": "low",
            "LLM_MAX_TOKENS": "2345",
            "LLM_TIMEOUT_SECONDS": "12.5",
        }, clear=False), patch.object(llm, "_client", None), \
                patch.object(llm, "OpenAI", return_value=fake_client) as client_factory:
            result = llm.ask_json("system", [])

        self.assertEqual({key: value for key, value in client_factory.call_args.kwargs.items()
                          if key != "http_client"}, {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "api_key": "unit-test-key", "timeout": 12.5, "max_retries": 1,
        })
        self.assertEqual(completions.create.call_count, 2)
        for call in completions.create.call_args_list:
            self.assertEqual(call.kwargs["max_tokens"], 2345)
            self.assertEqual(call.kwargs["reasoning_effort"], "low")
        self.assertEqual(result["result"], {"reply": "ok", "order": None})


if __name__ == "__main__":
    unittest.main()
