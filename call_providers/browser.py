"""Browser call session startup."""
import os


# Print the private browser-call link for the session.
def start_call(session_id, token, context):
    # The token is shown once in the link; the database stores only its digest.
    base = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    print(f"   browser confirmation call: {base}/call/{token}")
