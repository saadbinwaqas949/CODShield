-- Store each confirmation-call attempt and its resumable dialogue state.
CREATE TABLE IF NOT EXISTS call_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id uuid NOT NULL REFERENCES orders(id),
  attempt int NOT NULL DEFAULT 1,
  provider text NOT NULL CHECK (provider IN ('fake','browser','twilio')),
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','completed','unavailable')),
  token_hash text,
  transcript jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz
);
-- Validate browser links without storing tokens that could be reused directly.
CREATE UNIQUE INDEX IF NOT EXISTS call_sessions_token_hash_uq
  ON call_sessions(token_hash) WHERE token_hash IS NOT NULL;
-- Support order history views grouped by session attempt.
CREATE INDEX IF NOT EXISTS call_sessions_order_attempt_idx ON call_sessions(order_id,attempt);
