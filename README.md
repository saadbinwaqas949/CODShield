# CODShield

In Pakistan, a lot of online orders are paid for in cash when the parcel arrives. Customers like it because they only pay for something they can hold. Sellers get burned by it: someone orders, the ad has been paid for, the parcel has been packed and shipped, and then nobody opens the door. The seller pays the courier both ways and gets nothing.

CODShield is our attempt at a back office that catches those orders early. A customer chats with the shop in English, Urdu or Roman Urdu, and an AI assistant takes the order. The system then scores how risky the order looks and explains why. Shaky orders get a confirmation call, and when the AI gets stuck, the chat goes to a human on the staff inbox.

It is a prototype, built for the Nebius x NVIDIA Global AI Hackathon. We've tried to be straight about what is real and what is pretend, so please read the table below before you run anything.

## What works and what doesn't

| Piece | Where it stands |
|---|---|
| Browser chat that takes orders | Works in our tests. Prices and stock always come from the database, never from the AI or the customer. |
| Address cleaning | Works. Rule-based: it tidies Pakistani addresses using a list of local areas and spellings. It does not use maps or geocoding. |
| Risk scoring | Works. Simple, readable rules. Every score is saved with its reasons. |
| Staff inbox (`/staff`) | Built and tested with mocks. When the AI can't answer a product question, it hands the chat to a person. Needs migration 005. |
| Product manager | Built. Staff can edit price, stock, description and image links. |
| Confirmation calls | The default "fake" provider works. The browser and Twilio providers are written and tested with mocks, but we haven't yet run them end to end against a real browser or real Twilio. |
| Event worker | Works. Retries failed events. |
| Couriers | Simulated only. `simulate_courier.py` invents delivery outcomes. |
| WhatsApp | Not built. The web chat uses the same functions a WhatsApp webhook would call. |
| Cross-seller fraud graph | Not built yet. Today we only flag a phone number after repeated refused or returned orders. |

## How it fits together

Nothing in the system calls anything else directly. Every step leaves a small record (an "event") in a Postgres table, and a background worker hands each event to the function that cares about it.

```
customer (browser chat)
        |
        v
   FastAPI web gateway  --->  events table (Postgres / Supabase)
                                      |
                                      v
                                 worker.py
                                      |
      sales agent -> address cleaner -> risk score -> confirmation -> dispatch -> delivery outcome
```

One order goes like this:

1. The customer sends a message. The gateway saves it and writes a `message.received` event.
2. The worker passes it to the sales agent, which asks the model what to say. When the customer clearly agrees, an order is created.
3. The address is cleaned and the order gets a risk score with reasons.
4. Low-risk orders are dispatched. The rest get a confirmation call.
5. The call result moves the order to confirmed, cancelled, or "needs a human".
6. When a delivery result arrives, the order is closed. A phone number with repeated bad outcomes gets flagged, so its next order scores higher.

Order status can only move along allowed steps, which are defined in `events.py`. For example, an order can't jump from created to delivered.

## Models and infrastructure

Right now the sales agent calls Gemini through `llm.py`.

<!-- TODO before submitting to the hackathon: the rules require running on Nebius Token Factory or AI Cloud with at least one NVIDIA open-source model. Replace this section with what actually runs: which Nemotron models, where, and which Nebius services are used. -->

## Getting started

You'll need Python 3.10+, a PostgreSQL 15+ database with the `pgcrypto` and `vector` extensions (we use Supabase), and an API key for the model provider.

**1. Install**

```powershell
python -m pip install -r requirements.txt
```

**2. Configure**

```powershell
Copy-Item .env.example .env      # macOS/Linux: cp .env.example .env
```

Fill in `.env`. Keep it private and never commit it. The main settings are:

| Setting | What it's for |
|---|---|
| `DATABASE_URL` | Your Postgres connection string |
| `GEMINI_API_KEY` | The model key (will change when we move to Nebius) |
| `STAFF_ACCESS_TOKEN` | The password for the staff page |
| `PHONE_ENC_KEY` | Encrypts customer phone numbers. Create one with the command below and back it up, because a lost key means saved numbers can't be read. |
| `HASH_SALT` | Secret used when hashing phones and addresses. Keep it stable. |
| `CALL_PROVIDER` | `fake` (default), `browser`, or `twilio` |
| `PUBLIC_BASE_URL`, `TWILIO_*` | Only needed for real calls |

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`.env.example` lists every variable.

**3. Set up the database**

Run `db/schema.sql` once on an empty database (paste it into the Supabase SQL editor). It creates the core tables. Then apply the incremental changes:

```powershell
python migrate.py
```

If you ever run a migration by hand, add its filename to the `schema_migrations` table so the runner doesn't apply it again.

**4. Add the demo shop**

```powershell
python seed.py
```

## Running the demo

Open two terminals in the project folder.

```powershell
python worker.py                          # terminal 1: the event worker
python -m uvicorn web_gateway:app --reload   # terminal 2: the web server
```

- Customer chat: http://127.0.0.1:8000/
- Staff page: http://127.0.0.1:8000/staff (sign in with your `STAFF_ACCESS_TOKEN`)

Try typing "1 shirt chahiye", then give a name and address, and say yes. Watch the worker terminal as the order moves along.

A few helpers for testing without the full chat:

```powershell
python fake_order.py          # creates an order directly, no AI (type DHA for a riskier address)
python simulate_courier.py    # pretends the courier delivered or failed
python chat_cli.py            # chat from the terminal (needs the worker and a model key)
python smoke_test.py          # runs two full orders through the real database (stop the worker first)
```

## The staff page

- **Inbox:** conversations the AI handed off. Staff can take over, reply, and hand the chat back to the AI. While a chat is waiting for or with staff, the AI stays quiet.
- **Products:** edit prices, stock, descriptions and image links.
- **Overview:** order counts, delivered value and AI usage cost. Cost tracking is partial, because some model calls can't be tied to a single order.

## Tests

```powershell
python -m unittest
```

All tests run offline and mock the database-facing services, the model and Twilio. That means they say nothing about a live Supabase connection or live Twilio credentials. `scripts/eval_sales.py` is a separate, optional check that talks to the real model with scripted customer conversations, so it costs API credits.

## Security notes

- Phone numbers are stored hashed (for matching) and encrypted (so a call can dial them). Anything shared across sellers uses hashes only.
- The staff page is protected by one shared token. That's fine for a demo and not enough for production. Don't put this on the open internet as it is.
- The demo web endpoints have simple limits (message length, rate limiting in memory) and no user accounts.

## Known limitations

- The chat and couriers are for demos. There is no WhatsApp or live courier integration.
- Address cleaning and risk scoring are rules, not a trained model and not a map lookup.
- Urdu voice on Twilio calls is unconfirmed. We haven't verified which voice and speech settings work for Urdu on a real account.
- We haven't measured real return rates ourselves yet, so we don't quote any numbers here.

## Things we'd like to build next

WhatsApp, real courier tracking, a fraud graph that links phones and addresses across sellers, Urdu voice notes for ordering, and a replay mode that shows how many returns the system would have caught on past orders.

## Project layout

| File | What it does |
|---|---|
| `worker.py` | Reads events and runs the right handlers |
| `handlers.py` | Sales agent, orchestrator, address cleaner, risk engine, calls, dispatch, learning |
| `events.py` | Event types and allowed order status changes |
| `web_gateway.py`, `web/` | Web server, chat page, staff page, call page |
| `gateway.py`, `phone_crypto.py` | Receiving messages, phone hashing and encryption |
| `llm.py` | Model client |
| `risk.py`, `address_clean.py`, `data/areas.csv` | Scoring rules and address cleaning |
| `call_agent.py`, `call_providers/` | Confirmation call logic (fake, browser, Twilio) |
| `migrations/`, `migrate.py` | Database changes and the runner |
| `tests/` | Offline tests |



