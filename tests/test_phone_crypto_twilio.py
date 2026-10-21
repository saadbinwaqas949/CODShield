import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import call_providers.twilio as twilio
import phone_crypto


class FakeCursor:
    # Set up this test double's initial state.
    def __init__(self, phone_blob):
        self.phone_blob = phone_blob
        self.one = None
        self.state = {"context": {}, "transcript": []}
        self.status = None

    # Record the query and prepare a mocked database result.
    def execute(self, sql, params=None):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("select c.phone_enc"):
            self.one = (self.phone_blob,) if self.phone_blob is not None else None
        elif normalized.startswith("select transcript"):
            self.one = (self.state,)
        elif normalized.startswith("update call_sessions set status='unavailable'"):
            self.status = "unavailable"
            self.state = params[0].adapted

    # Return and clear the next mocked database row.
    def fetchone(self):
        result, self.one = self.one, None
        return result


class PhoneCryptoAndTwilioTests(unittest.TestCase):
    # Verify normalization and fernet round trip api.
    def test_normalization_and_fernet_round_trip_api(self):
        self.assertEqual(phone_crypto.normalize_phone_pk("0300-1234567"), "+923001234567")
        self.assertEqual(phone_crypto.normalize_phone_pk("3001234567"), "+923001234567")
        fake_fernet = Mock()
        fake_fernet.encrypt.return_value = b"ciphertext"
        fake_fernet.decrypt.return_value = b"+923001234567"
        with patch.object(phone_crypto, "_fernet", return_value=fake_fernet):
            encrypted = phone_crypto.encrypt_phone("0300-1234567")
            decrypted = phone_crypto.decrypt_phone(encrypted)
        fake_fernet.encrypt.assert_called_once_with(b"+923001234567")
        self.assertEqual(encrypted, b"ciphertext")
        self.assertEqual(decrypted, "+923001234567")

    # Start the provider without placing an outbound call.
    def _run_start_without_dial(self, blob, decrypt_error=None):
        cur = FakeCursor(blob)
        env = {
            "TWILIO_ACCOUNT_SID": "AC-test", "TWILIO_AUTH_TOKEN": "test-auth",
            "TWILIO_FROM_NUMBER": "+15005550006", "PUBLIC_BASE_URL": "https://demo.example",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch.object(twilio, "decrypt_phone", side_effect=decrypt_error or AssertionError("decrypt called")) as decrypt, \
             patch.object(twilio, "urlopen") as outbound:
            twilio.start_call(cur, uuid4(), uuid4(), {})
        outbound.assert_not_called()
        return cur, decrypt

    # Verify null phone enc records no answer reason without dialing.
    def test_null_phone_enc_records_no_answer_reason_without_dialing(self):
        cur, decrypt = self._run_start_without_dial(None)
        decrypt.assert_not_called()
        self.assertEqual(cur.status, "unavailable")
        self.assertIn("not stored encrypted", cur.state["transcript"][-1]["text"])

    # Verify decryption failure records no answer reason without dialing.
    def test_decryption_failure_records_no_answer_reason_without_dialing(self):
        cur, decrypt = self._run_start_without_dial(b"bad-ciphertext", ValueError("bad token"))
        decrypt.assert_called_once_with(b"bad-ciphertext")
        self.assertEqual(cur.status, "unavailable")
        self.assertIn("decryption failed", cur.state["transcript"][-1]["text"])


if __name__ == "__main__":
    unittest.main()
