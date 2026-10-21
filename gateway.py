"""The front desk: receives customer messages and turns them into events.
Later, the WhatsApp webhook will call these same functions."""
import hashlib
import os
from uuid import uuid4
from dotenv import load_dotenv
import psycopg2
from phone_crypto import encrypt_phone, normalize_phone_pk

import events as ev

load_dotenv()


# Create a salted lookup hash from a normalized phone number.
def hash_phone(phone):
    digits = normalize_phone_pk(phone).lstrip("+")
    salt = os.getenv("HASH_SALT") or "codshield-salt-"
    return hashlib.sha256((salt + digits).encode("utf-8")).hexdigest()


# Find or create a customer and start a conversation.
def start_conversation(cur, phone, shop_name="Demo Store", name=None):
    cur.execute("select id from sellers where name=%s", (shop_name,))
    seller_id = cur.fetchone()[0]
    ph = hash_phone(phone)
    enc = psycopg2.Binary(encrypt_phone(phone))
    cur.execute("select id from customers where seller_id=%s and phone_hash=%s",
                (seller_id, ph))
    row = cur.fetchone()
    if row:
        cust_id = row[0]
        cur.execute("update customers set phone_enc=%s where id=%s and phone_enc is null", (enc, cust_id))
        if name:
            cur.execute("update customers set name=%s where id=%s", (name, cust_id))
    else:
        cust_id = uuid4()
        cur.execute("insert into customers (id,seller_id,phone_hash,phone_enc,name) values (%s,%s,%s,%s,%s)",
                    (cust_id, seller_id, ph, enc, name))
    conv_id = uuid4()
    cur.execute("insert into conversations (id,seller_id,customer_id) values (%s,%s,%s)",
                (conv_id, seller_id, cust_id))
    return seller_id, conv_id, ph


# Save an incoming message and enqueue its event.
def receive_message(cur, seller_id, conv_id, phone_hash, text):
    cur.execute("insert into messages (conversation_id,direction,kind,body) "
                "values (%s,'in','text',%s)", (conv_id, text))
    from db import save_event
    save_event(cur, ev.MessageReceived(
        seller_id=seller_id, correlation_id=conv_id,
        idempotency_key=f"msg-{uuid4()}", conversation_id=conv_id,
        customer_phone_hash=phone_hash, kind="text", text=text))
