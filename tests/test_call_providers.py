import base64
import hashlib
import hmac
import unittest

from call_providers.twilio import twiml_say, valid_signature


class TwilioTests(unittest.TestCase):
    # Verify signature valid and rejects tampering.
    def test_signature_valid_and_rejects_tampering(self):
        url = "https://demo.example/api/call?x=1"
        form = {"CallSid": "CA123", "SpeechResult": "haan"}
        raw = url + "".join(k + str(form[k]) for k in sorted(form))
        signature = base64.b64encode(hmac.new(b"test-token", raw.encode(), hashlib.sha1).digest()).decode()
        self.assertTrue(valid_signature(url, form, signature, "test-token"))
        self.assertFalse(valid_signature(url, {**form, "SpeechResult": "cancel"}, signature, "test-token"))
        self.assertFalse(valid_signature(url, form, "", "test-token"))


    # Verify twiml gathers speech and digits and trial notice first.
    def test_twiml_gathers_speech_and_digits_and_trial_notice_first(self):
        xml = twiml_say("Assalam-o-alaikum", "https://demo.example/turn", trial=True)
        self.assertTrue(xml.startswith("<?xml"))
        self.assertLess(xml.index("Twilio trial account"), xml.index("<Gather"))
        self.assertIn('input="speech dtmf"', xml)
        self.assertIn('language="ur-PK"', xml)
        self.assertIn("https://demo.example/turn", xml)
