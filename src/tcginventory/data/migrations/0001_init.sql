-- 0001_init — initial schema (see specs/02-data-model.md).
-- Tables are ordered so every foreign-key target is created before its referrer.
-- The migration runner records the applied version in schema_version; this
-- script must NOT insert into it.

-- Distinct sellable things
CREATE TABLE item (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL,       -- "Charizard VMAX"
    game               TEXT    NOT NULL DEFAULT 'pokemon',
    set_name           TEXT,                   -- "Champion's Path"
    set_code           TEXT,                   -- "CPA"
    card_number        TEXT,                   -- "074/073"
    category           TEXT    NOT NULL,       -- enum: single | sealed | accessory | other
    condition          TEXT,                   -- enum: NM | LP | MP | HP | DMG | sealed; NULL when graded
    is_graded          INTEGER NOT NULL DEFAULT 0,  -- graded cards are identified by grade, not condition
    grading_company    TEXT,                   -- enum: PSA | BGS | CGC | SGC | null
    grade              TEXT,                   -- "10", "9.5"
    cert_number        TEXT,                   -- slab cert #
    sku                TEXT,                   -- user's own SKU/label
    location           TEXT,                   -- "Binder A", "Safe", "Box 3"
    tcgplayer_url      TEXT,                   -- reference link
    ebay_url           TEXT,                   -- reference link
    market_value_cents INTEGER,                -- current value (most-recent entry wins)
    market_value_asof  TEXT,                   -- ISO date of that value
    notes              TEXT,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);
CREATE INDEX idx_item_name ON item(name);
CREATE INDEX idx_item_set  ON item(set_name, card_number);

-- An inventory/value-affecting event
CREATE TABLE "transaction" (
    id            INTEGER PRIMARY KEY,
    type          TEXT    NOT NULL,            -- enum: purchase | sale | trade | adjustment
    occurred_at   TEXT    NOT NULL,            -- ISO datetime
    counterparty  TEXT,                        -- who
    reference     TEXT,                        -- order #, platform
    cash_delta_cents INTEGER NOT NULL DEFAULT 0,-- net cash in(+)/out(-) incl. boot (fees folded into cash/values)
    status        TEXT    NOT NULL DEFAULT 'posted', -- enum: posted | voided
    voided_at     TEXT,                        -- set when voided (soft, never hard-deleted)
    notes         TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX idx_txn_date ON "transaction"(occurred_at);

-- Per-item detail of a transaction
CREATE TABLE transaction_line (
    id             INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES "transaction"(id) ON DELETE CASCADE,
    item_id        INTEGER NOT NULL REFERENCES item(id),
    direction      TEXT    NOT NULL,           -- enum: in | out
    quantity       INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,         -- agreed per-unit value (proceeds or cost)
    realized_pl_cents INTEGER,                 -- set for 'out' lines at post time
    notes          TEXT
);
CREATE INDEX idx_line_txn ON transaction_line(transaction_id);
CREATE INDEX idx_line_item ON transaction_line(item_id);

-- A batch of one item acquired at one cost
CREATE TABLE lot (
    id                  INTEGER PRIMARY KEY,
    item_id             INTEGER NOT NULL REFERENCES item(id),
    acquired_txn_line_id INTEGER REFERENCES transaction_line(id),
    acquired_at         TEXT    NOT NULL,      -- ISO date (drives FIFO order)
    quantity            INTEGER NOT NULL,      -- units originally acquired
    remaining_qty       INTEGER NOT NULL,      -- units still on hand
    unit_cost_cents     INTEGER NOT NULL,      -- inferred basis / unit (market-value-weighted split)
    acquired_market_value_cents INTEGER NOT NULL, -- per-unit market value at acquisition (allocation snapshot)
    intent              TEXT    NOT NULL,      -- enum: personal | resale | ripping (editable, no history in V1)
    source              TEXT,                  -- vendor/seller/friend
    notes               TEXT,                  -- acquisition notes (per this lot)
    created_at          TEXT    NOT NULL
);
CREATE INDEX idx_lot_item_open ON lot(item_id, remaining_qty);

-- Which lots an 'out' line drew down (FIFO), for auditable basis
CREATE TABLE lot_consumption (
    id                  INTEGER PRIMARY KEY,
    transaction_line_id INTEGER NOT NULL REFERENCES transaction_line(id) ON DELETE CASCADE,
    lot_id              INTEGER NOT NULL REFERENCES lot(id),
    quantity            INTEGER NOT NULL,
    unit_cost_cents     INTEGER NOT NULL       -- copied from lot at time of disposal
);
CREATE INDEX idx_consumption_line ON lot_consumption(transaction_line_id);

-- Append-only audit trail of every create / edit / void
CREATE TABLE audit_log (
    id             INTEGER PRIMARY KEY,
    entity         TEXT    NOT NULL,           -- e.g. 'transaction'
    entity_id      INTEGER NOT NULL,
    action         TEXT    NOT NULL,           -- enum: create | edit | void
    at             TEXT    NOT NULL,           -- ISO datetime
    before_json    TEXT,                       -- snapshot prior to change (null on create)
    after_json     TEXT,                       -- snapshot after change (null on void)
    note           TEXT
);
CREATE INDEX idx_audit_entity ON audit_log(entity, entity_id);

-- Applied-migration ledger (managed by the migration runner)
CREATE TABLE schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL
);
