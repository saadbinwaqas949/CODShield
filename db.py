"""Database helpers. Everything talks to Supabase through here."""
import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
psycopg2.extras.register_uuid()  # lets us pass UUID objects directly


# Open the configured PostgreSQL connection.
def connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def save_event(cur, e):
    """Store an event. Saving the same event twice is harmless (idempotency_key)."""
    cur.execute(
        "insert into events (id,type,seller_id,correlation_id,idempotency_key,payload) "
        "values (%s,%s,%s,%s,%s,%s) on conflict (idempotency_key) do nothing",
        (e.event_id, e.type, e.seller_id, e.correlation_id,
         e.idempotency_key, e.model_dump_json()),
    )


def next_event(cur):
    """Oldest event nobody has handled yet (locked so two workers can't grab it)."""
    cur.execute(
        "select id, payload from events where processed_at is null "
        "order by created_at limit 1 for update skip locked"
    )
    return cur.fetchone()


# Mark an event as successfully processed.
def mark_done(cur, event_id):
    cur.execute("update events set processed_at = now() where id = %s", (event_id,))