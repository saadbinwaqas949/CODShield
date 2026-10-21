-- Persist transparent quality flags alongside the normalized address score.
ALTER TABLE addresses
  ADD COLUMN IF NOT EXISTS address_flags jsonb NOT NULL DEFAULT '[]'::jsonb;
