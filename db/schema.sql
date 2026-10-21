-- CODShield base schema reconstructed from the application's SQL and migrations.
-- This is a documented recovery, not a recovered original. Review it against the
-- target PostgreSQL project before using it to initialize or modify a database.

CREATE TABLE IF NOT EXISTS sellers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS customers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id uuid NOT NULL REFERENCES sellers(id),
  phone_hash text NOT NULL,
  phone_enc bytea,
  name text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (seller_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS products (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id uuid NOT NULL REFERENCES sellers(id),
  sku text,
  name text NOT NULL,
  name_ur text,
  price numeric(12,2) NOT NULL CHECK (price >= 0),
  stock integer NOT NULL DEFAULT 0 CHECK (stock >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (seller_id, sku)
);

CREATE TABLE IF NOT EXISTS conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id uuid NOT NULL REFERENCES sellers(id),
  customer_id uuid NOT NULL REFERENCES customers(id),
  state jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id),
  direction text NOT NULL CHECK (direction IN ('in','out')),
  kind text NOT NULL DEFAULT 'text' CHECK (kind IN ('text','voice')),
  body text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS addresses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id uuid NOT NULL REFERENCES customers(id),
  raw_text text NOT NULL,
  city text,
  area text,
  street text,
  quality_score numeric(4,3) CHECK (quality_score BETWEEN 0 AND 1),
  addr_hash text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id uuid NOT NULL REFERENCES sellers(id),
  customer_id uuid NOT NULL REFERENCES customers(id),
  address_id uuid NOT NULL REFERENCES addresses(id),
  conversation_id uuid NOT NULL REFERENCES conversations(id),
  total_amount numeric(12,2) NOT NULL CHECK (total_amount >= 0),
  status text NOT NULL DEFAULT 'created'
    CHECK (status IN ('created','scoring','cleared','awaiting_confirmation',
                      'needs_human','confirmed','cancelled','dispatched',
                      'delivered','refused','returned')),
  risk_score numeric(4,3) CHECK (risk_score BETWEEN 0 AND 1),
  risk_level text CHECK (risk_level IN ('low','medium','high')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_items (
  order_id uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  product_id uuid NOT NULL REFERENCES products(id),
  qty integer NOT NULL CHECK (qty > 0),
  unit_price numeric(12,2) NOT NULL CHECK (unit_price >= 0),
  PRIMARY KEY (order_id, product_id)
);

-- The event payload is validated by events.py; retry columns arrive in migration 004.
CREATE TABLE IF NOT EXISTS events (
  id uuid PRIMARY KEY,
  type text NOT NULL,
  seller_id uuid NOT NULL REFERENCES sellers(id),
  correlation_id uuid NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);

CREATE TABLE IF NOT EXISTS risk_assessments (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  model_version text NOT NULL,
  score numeric(4,3) NOT NULL CHECK (score BETWEEN 0 AND 1),
  level text NOT NULL CHECK (level IN ('low','medium','high')),
  reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
  features jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS risk_entities (
  entity_type text NOT NULL CHECK (entity_type IN ('phone','address','device')),
  entity_hash text NOT NULL,
  orders_total integer NOT NULL DEFAULT 0,
  orders_refused integer NOT NULL DEFAULT 0,
  orders_returned integer NOT NULL DEFAULT 0,
  flagged boolean NOT NULL DEFAULT false,
  last_seen timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_type, entity_hash)
);

CREATE TABLE IF NOT EXISTS confirmation_calls (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES orders(id),
  attempt integer NOT NULL DEFAULT 1,
  outcome text NOT NULL CHECK (outcome IN ('confirmed','cancelled','changed_address',
                                          'no_answer','unclear')),
  transcript text,
  duration_s integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (order_id, attempt)
);

CREATE TABLE IF NOT EXISTS deliveries (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL UNIQUE REFERENCES orders(id),
  courier text NOT NULL,
  outcome text CHECK (outcome IN ('delivered','refused','returned','unreachable')),
  outcome_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id bigserial PRIMARY KEY,
  order_id uuid REFERENCES orders(id),
  agent text NOT NULL,
  model text NOT NULL,
  tokens_in integer,
  tokens_out integer,
  latency_ms integer,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Keep the main list and history queries efficient on a fresh installation.
CREATE INDEX IF NOT EXISTS messages_conversation_time_idx
  ON messages (conversation_id, created_at, id);
CREATE INDEX IF NOT EXISTS orders_conversation_created_idx
  ON orders (conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS orders_customer_created_idx
  ON orders (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS risk_assessments_order_created_idx
  ON risk_assessments (order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS events_pending_created_idx
  ON events (created_at, id) WHERE processed_at IS NULL;
