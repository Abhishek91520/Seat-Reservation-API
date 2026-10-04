-- 001_init.sql: Initial schema for Seat Reservation API

CREATE TABLE IF NOT EXISTS shows (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  price_paise bigint NOT NULL CHECK (price_paise > 0),
  per_user_limit int NOT NULL DEFAULT 4 CHECK (per_user_limit BETWEEN 1 AND 100),
  total_seats int NOT NULL CHECK (total_seats > 0),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seats (
  show_id uuid NOT NULL REFERENCES shows(id),
  label text NOT NULL,
  status text NOT NULL DEFAULT 'available' CHECK (status IN ('available','held','confirmed')),
  reservation_id uuid NULL,
  user_id text NULL,
  PRIMARY KEY (show_id, label),
  CHECK ((status = 'available') = (reservation_id IS NULL))
);

CREATE TABLE IF NOT EXISTS reservations (
  id uuid PRIMARY KEY,
  show_id uuid NOT NULL REFERENCES shows(id),
  user_id text NOT NULL,
  seats text[] NOT NULL,
  amount_paise bigint NOT NULL,
  status text NOT NULL CHECK (status IN ('confirmed','cancelled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  cancelled_at timestamptz NULL
);

CREATE TABLE IF NOT EXISTS user_show_quota (
  show_id uuid NOT NULL,
  user_id text NOT NULL,
  held int NOT NULL DEFAULT 0 CHECK (held >= 0),
  PRIMARY KEY (show_id, user_id)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  user_id text NOT NULL,
  key text NOT NULL,
  request_hash text NOT NULL,
  reservation_id uuid NULL,
  response_body jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key)
);

-- Index to support querying reservations by user and show
CREATE INDEX IF NOT EXISTS idx_reservations_show_user ON reservations(show_id, user_id);
-- Index to support seats status queries
CREATE INDEX IF NOT EXISTS idx_seats_show_status ON seats(show_id, status);
