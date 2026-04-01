-- Payment Service Schema

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS payments (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    sender_id        UUID NOT NULL,
    recipient_id     UUID NOT NULL,
    amount           NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
    currency         VARCHAR(3) NOT NULL,
    description      TEXT,
    status           VARCHAR(32) NOT NULL DEFAULT 'pending',
    idempotency_key  VARCHAR(255) UNIQUE NOT NULL,
    failure_reason   TEXT,
    processed_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT valid_status CHECK (status IN ('pending','processing','completed','failed','cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_payments_sender   ON payments(sender_id);
CREATE INDEX IF NOT EXISTS idx_payments_status   ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_created  ON payments(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payments_idem_key ON payments(idempotency_key);
