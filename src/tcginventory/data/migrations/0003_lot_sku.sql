-- 0003_lot_sku — move SKU from item to lot (per-copy, auto-generated).
--
-- SKUs identify an acquired copy for quick lookup during selling/trading, with a
-- per-identity sequence (see services/sku.py), so they belong on the lot, not the
-- item. The old manual item.sku is retired. Existing lots are backfilled by the
-- app on startup.

ALTER TABLE lot ADD COLUMN sku TEXT;
CREATE UNIQUE INDEX idx_lot_sku ON lot(sku) WHERE sku IS NOT NULL;

ALTER TABLE item DROP COLUMN sku;
