-- Track retry state so failed handlers can be delayed and inspected.
ALTER TABLE events
  ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_error text,
  ADD COLUMN IF NOT EXISTS failed_at timestamptz,
  ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz;

-- Find pending events whose scheduled retry time has arrived.
CREATE INDEX IF NOT EXISTS events_retry_due_idx
  ON events (next_attempt_at, created_at)
  WHERE processed_at IS NULL AND failed_at IS NULL;

-- Durable provider-call marker: once this row commits, retries must not dial again.
CREATE UNIQUE INDEX IF NOT EXISTS call_sessions_provider_attempt_uq
  ON call_sessions (order_id, attempt)
  WHERE provider IN ('browser', 'twilio');
