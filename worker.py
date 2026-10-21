"""Poll the PostgreSQL event queue and run CODShield's registered handlers."""
import time

from pydantic import TypeAdapter

import events as ev
from db import connect, mark_done
from handlers import HANDLERS


# Failed events get three total attempts; later attempts wait before becoming due.
RETRY_DELAYS_SECONDS = (10, 60)
MAX_ATTEMPTS = len(RETRY_DELAYS_SECONDS) + 1

# Give a customer a clear response if their message cannot be processed after retries.
TECHNICAL_ERROR_REPLY = (
    "Sorry, the shop assistant could not process your message just now. "
    "Please try again shortly or contact the shop directly."
)

# The discriminator selects the Pydantic event model from its `type` field.
_EVENT_ADAPTER = TypeAdapter(ev.Event)


# Read JSONB values whether psycopg returns decoded objects or serialized text.
def _parse_event(payload):
    if isinstance(payload, str):
        return _EVENT_ADAPTER.validate_json(payload)
    return _EVENT_ADAPTER.validate_python(payload)


# Select one due event while locking it so concurrent workers skip this row.
def _next_due_event(cur):
    cur.execute(
        "select id,payload,attempts from events "
        "where processed_at is null and failed_at is null "
        "and (next_attempt_at is null or next_attempt_at <= now()) "
        "order by created_at,id limit 1 for update skip locked"
    )
    return cur.fetchone()


# Save a retry schedule or permanently fail an event after its final attempt.
def _record_failure(conn, event_id, attempts, error_name, event=None):
    next_attempt = attempts + 1
    delay_index = next_attempt - 1
    cur = conn.cursor()
    try:
        if next_attempt >= MAX_ATTEMPTS:
            # Mark it processed as well as failed so it leaves the active queue.
            cur.execute(
                "update events set attempts=%s,last_error=%s,failed_at=now(),processed_at=now() "
                "where id=%s",
                (next_attempt, error_name, event_id),
            )
            if isinstance(event, ev.MessageReceived):
                # Only customer message events have a conversation to receive a failure reply.
                cur.execute(
                    "insert into messages (conversation_id,direction,kind,body) "
                    "values (%s,'out','text',%s)",
                    (event.conversation_id, TECHNICAL_ERROR_REPLY),
                )
            conn.commit()
            print(f"   event failed after {next_attempt} attempts ({error_name})")
            return

        delay = RETRY_DELAYS_SECONDS[delay_index]
        # Store a due time in SQL so worker restarts preserve the backoff schedule.
        cur.execute(
            "update events set attempts=%s,last_error=%s, "
            "next_attempt_at=now()+(%s * interval '1 second') where id=%s",
            (next_attempt, error_name, delay, event_id),
        )
        conn.commit()
        print(f"   event retry {next_attempt}/{MAX_ATTEMPTS} in {delay}s ({error_name})")
    finally:
        cur.close()


# Process one available event; return False only when the queue has no due work.
def process_one(conn):
    event_id = None
    attempts = 0
    event = None
    cur = conn.cursor()
    try:
        row = _next_due_event(cur)
        if not row:
            # End the read transaction and release any locks before the next poll.
            conn.commit()
            return False

        event_id, payload, attempts = row
        event = _parse_event(payload)
        # A single event can fan out to multiple ordered handlers in the registry.
        for handler in HANDLERS.get(event.type, []):
            handler(cur, event)

        mark_done(cur, event_id)
        conn.commit()
        print(f"   processed {event.type} ({str(event_id)[:8]})")
        return True
    except Exception as exc:
        # Discard partial handler writes before scheduling a clean retry transaction.
        conn.rollback()
        if event_id is None:
            # Queue/database failures cannot be attached to one event; let the poll loop recover.
            raise
        # Do not persist exception text, which may contain customer or provider data.
        _record_failure(conn, event_id, int(attempts or 0), type(exc).__name__, event)
        return True
    finally:
        cur.close()


# Keep polling through temporary database outages until the process is stopped.
def main():
    conn = None
    try:
        while True:
            try:
                if conn is None or conn.closed:
                    conn = connect()
                if not process_one(conn):
                    time.sleep(1)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                # Log only the exception class and reconnect after a short pause.
                print(f"   worker connection or queue error ({type(exc).__name__}); retrying")
                if conn is not None:
                    conn.close()
                    conn = None
                time.sleep(2)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


if __name__ == "__main__":
    main()
