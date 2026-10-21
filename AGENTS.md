# CODShield project guide

CODShield is a Python back office for online sellers handling cash-on-delivery orders in Pakistan. Keep explanations beginner-friendly and changes focused.

## Product and architecture

- The customer uses the FastAPI web chat. WhatsApp is not implemented.
- The sales agent uses Gemini through `llm.py`. Gemini is the intended model provider going forward; keep model configuration and integration aligned with Gemini.
- PostgreSQL stores customers, products, orders, messages, events, and related records. `worker.py` processes pending events and `handlers.py` contains the event handlers and order orchestration.
- Events coordinate the workflow. Handlers can use domain helpers and call providers; do not describe the codebase as having no direct module calls.
- The courier is simulated. The default confirmation-call provider is fake; browser and Twilio implementations exist but are not verified live end to end.

## Important rules

- Only orchestrator logic in `handlers.py` should change order status, through `set_status()` and the allowed transitions in `events.py`.
- Prices and available stock must come from database rows, not model output or customer claims. The application validates and quotes orders before creating them.
- Use `%s` parameters for SQL values; do not interpolate values into SQL strings.
- Never commit `.env` or print credentials. Phone numbers are encrypted for dialing and hashed for matching. Keep `PHONE_ENC_KEY` and `HASH_SALT` stable and private.
- Do not run schema setup or migrations against a live database without understanding their effects.

## Reconstructed base schema

`db/schema.sql` has been reconstructed from the current application queries and migrations because the original base schema was not available. It is a best-effort recovery, not a verified copy of the original. Review it against the intended database before applying it, especially constraints and column types. Migrations remain incremental and should be applied after the reviewed base schema.

## Worker behavior

`worker.py` retries failed events up to three attempts total, with delays of 10 seconds and then 60 seconds. After the final failure it marks the event failed; a failed customer-message event also receives a technical-error reply. Keep documentation aligned with this behavior.

## Main files

- `events.py`: Pydantic event models and allowed order status transitions.
- `db.py`: PostgreSQL connection and event persistence helpers.
- `worker.py`: event polling, handler dispatch, retry and failure handling.
- `handlers.py`: sales agent, order lifecycle, risk orchestration, confirmation calls, delivery outcomes, staff handoff.
- `web_gateway.py`, `web/`: customer chat, staff pages, product tools, and call pages.
- `gateway.py`, `phone_crypto.py`: conversation intake and phone hashing/encryption.
- `llm.py`: Gemini client and JSON response parsing.
- `risk.py`, `address_clean.py`, `data/areas.csv`: rules-based risk assessment and address cleanup.
- `call_agent.py`, `call_providers/`: confirmation dialogue and fake, browser, and Twilio providers.
- `migrations/`, `migrate.py`: incremental database changes and migration runner.

## Known limitations

- The checked-in base schema is reconstructed from application code and has not been verified against the original database design.
- Gemini is the current and planned provider. Do not describe another provider as planned unless the project owner changes this decision.
- No WhatsApp or live courier integration; courier outcomes are simulated.
- Browser and Twilio call paths have mock coverage but have not been verified end to end against live services.
- Risk scoring and address cleanup are rules-based, not trained models or geocoding.
- Tests are primarily offline. `tests/test_sales_agent.py` contains pytest-style tests, but pytest is not listed in `requirements.txt`; the documented unittest command will not run those function tests.
