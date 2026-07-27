-- 0014_lot_missing — reversible "stolen / lost" write-down flag on a lot.
-- A missing lot is valued at $0 market while keeping its cost basis (impairment,
-- mirroring ripping): it shows a loss equal to its purchase price, is never
-- sellable/consumed, and drops out of on-hand. Clearing the flag restores it.

ALTER TABLE lot ADD COLUMN missing INTEGER NOT NULL DEFAULT 0;
