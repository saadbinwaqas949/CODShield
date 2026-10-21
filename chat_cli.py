"""Pretend to be a customer on WhatsApp, from the terminal.
worker.py must be running in another terminal."""
import time

from db import connect
from gateway import receive_message, start_conversation

conn = connect()
cur = conn.cursor()
phone = input("Fake phone number (any digits, e.g. 03001234567): ")
seller_id, conv_id, ph = start_conversation(cur, phone)
conn.commit()
print("Type as the customer. Ctrl+C to stop.\n")


# Count outgoing replies for the active CLI conversation.
def count_replies():
    cur.execute("select count(*) from messages where conversation_id=%s and direction='out'",
                (conv_id,))
    return cur.fetchone()[0]


while True:
    text = input("You: ")
    before = count_replies()
    receive_message(cur, seller_id, conv_id, ph, text)
    conn.commit()
    for _ in range(90):
        time.sleep(1)
        conn.commit()
        if count_replies() > before:
            break
    if count_replies() > before:
        cur.execute("select body from messages where conversation_id=%s and direction='out' "
                    "order by created_at desc limit 1", (conv_id,))
        reply = cur.fetchone()
        if reply:
            print("Shop:", reply[0])
        else:
            print("Shop: Sorry, I couldn't reply just now. Please try again.")
    else:
        print("Shop: I haven't received a reply yet. Please try sending your message again.")
    cur.execute("select status, risk_level from orders where conversation_id=%s", (conv_id,))
    order = cur.fetchone()
    if order:
        print(f"   [order status: {order[0]}, risk: {order[1]}]")
    conn.commit()
