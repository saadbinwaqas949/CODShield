"""Twilio REST and webhook helpers implemented with Python's standard library."""
import base64
import hashlib
import hmac
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from psycopg2.extras import Json

from phone_crypto import decrypt_phone


# Record why a Twilio call could not be started.
def _unavailable(cur, session_id, reason):
    # Preserve the failure reason in session history for staff review and support.
    cur.execute("select transcript from call_sessions where id=%s", (session_id,))
    row = cur.fetchone()
    state = row[0] if row and isinstance(row[0], dict) else {}
    if row and isinstance(row[0], str):
        try:
            state = json.loads(row[0])
        except json.JSONDecodeError:
            state = {}
    state.setdefault("transcript", []).append({"role": "system", "text": reason})
    cur.execute("update call_sessions set status='unavailable',transcript=%s,ended_at=now() where id=%s",
                (Json(state), session_id))
    print(f"   Twilio call unavailable: {reason}")


# Place an outbound Twilio call when configuration permits.
def start_call(cur, session_id, order_id, context):
    # Verify setup and the encrypted recipient before making an external provider request.
    required = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL")
    if any(not os.getenv(name) for name in required):
        _unavailable(cur, session_id, "provider configuration is incomplete")
        return
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]
    base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    cur.execute(
        "select c.phone_enc from orders o "
        "join customers c on c.id=o.customer_id where o.id=%s and c.phone_enc is not null",
        (order_id,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        _unavailable(cur, session_id, "customer phone is not stored encrypted")
        return
    try:
        phone = decrypt_phone(row[0])
    except Exception:
        _unavailable(cur, session_id, "customer phone decryption failed")
        return
    # Trial accounts may call only numbers explicitly verified in Twilio.
    if os.getenv("TWILIO_TRIAL", "false").lower() == "true":
        verified = {x.strip() for x in os.getenv("TWILIO_VERIFIED_NUMBERS", "").split(",") if x.strip()}
        if phone not in verified:
            _unavailable(cur, session_id, "Twilio trial recipient is not verified")
            return
    # Twilio returns control to signed webhook endpoints for the speech dialogue.
    callback = f"{base}/api/calls/twilio/{session_id}"
    fields = {"To": phone, "From": from_number, "Url": callback, "Method": "POST"}
    request = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
        data=urlencode(fields).encode(),
        headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            pass
    except Exception:
        _unavailable(cur, session_id, "outbound call request failed")


def valid_signature(url, form, signature, auth_token):
    """Twilio's documented HMAC-SHA1 signature scheme (URL + sorted form fields)."""
    if not signature or not auth_token:
        return False
    value = url + "".join(k + str(form[k]) for k in sorted(form))
    expected = base64.b64encode(hmac.new(auth_token.encode(), value.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, signature)


# Build escaped TwiML that speaks and gathers a customer response.
def twiml_say(text, action_url, language="en-US", trial=False):
    import html
    greeting = '<Say>This is a call from a Twilio trial account.</Say>' if trial else ""
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>" + greeting +
            "<Say language=\"" + html.escape(language, quote=True) + "\">" + html.escape(text) +
            "</Say><Gather input=\"speech dtmf\" action=\"" + html.escape(action_url, quote=True) +
            "\" method=\"POST\" timeout=\"5\" speechTimeout=\"auto\" language=\"ur-PK\" "
            "numDigits=\"1\"></Gather><Redirect method=\"POST\">" +
            html.escape(action_url) + "</Redirect></Response>")
