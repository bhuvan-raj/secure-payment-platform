-- Transaction Service Schema

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS accounts (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     UUID NOT NULL,
    currency    VARCHAR(3) NOT NULL,
    balance     NUMERIC(18, 2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, currency)
);

CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);

CREATE TABLE IF NOT EXISTS transactions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    payment_id      UUID NOT NULL,
    account_id      UUID NOT NULL REFERENCES accounts(id),
    type            VARCHAR(10) NOT NULL CHECK (type IN ('debit', 'credit')),
    amount          NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
    currency        VARCHAR(3) NOT NULL,
    running_balance NUMERIC(18, 2) NOT NULL,
    metadata        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Transactions are immutable — no updates allowed
CREATE INDEX IF NOT EXISTS idx_txn_account     ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_txn_payment     ON transactions(payment_id);
CREATE INDEX IF NOT EXISTS idx_txn_created     ON transactions(created_at DESC);
