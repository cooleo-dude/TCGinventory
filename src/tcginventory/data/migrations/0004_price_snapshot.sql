-- 0004_price_snapshot — polled market prices over time (see services/pricing.py).
-- One row per item per refresh; drives the current market column, unrealized P/L,
-- and the price-history chart. Prices are per-unit integer cents.

CREATE TABLE price_snapshot (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES item(id),
    source      TEXT    NOT NULL,            -- e.g. 'tcgcsv'
    price_cents INTEGER NOT NULL,            -- per-unit market price
    currency    TEXT    NOT NULL DEFAULT 'USD',
    as_of       TEXT    NOT NULL,            -- ISO datetime of the poll
    note        TEXT
);
CREATE INDEX idx_price_item_asof ON price_snapshot(item_id, as_of);
