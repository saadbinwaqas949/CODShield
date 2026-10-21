-- Add catalog details used to answer questions from approved shop information.
ALTER TABLE products
  ADD COLUMN IF NOT EXISTS description text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS image_urls jsonb NOT NULL DEFAULT '[]'::jsonb;

-- Keep attached media references with each customer or assistant message.
ALTER TABLE messages
  ADD COLUMN IF NOT EXISTS media_urls jsonb NOT NULL DEFAULT '[]'::jsonb;

-- Track whether AI, a requested takeover, or staff currently owns a chat.
ALTER TABLE conversations
  ADD COLUMN IF NOT EXISTS handoff_mode text NOT NULL DEFAULT 'ai'
    CHECK (handoff_mode IN ('ai','pending','human')),
  ADD COLUMN IF NOT EXISTS handoff_reason text,
  ADD COLUMN IF NOT EXISTS handoff_requested_at timestamptz;

-- List pending takeovers and active human conversations efficiently.
CREATE INDEX IF NOT EXISTS conversations_handoff_idx
  ON conversations (handoff_mode, handoff_requested_at DESC)
  WHERE handoff_mode IN ('pending','human');
