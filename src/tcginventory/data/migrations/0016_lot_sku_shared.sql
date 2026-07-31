-- 0016_lot_sku_shared — a SKU identifies an *acquisition line*, not one lot.
--
-- When a purchase's cost doesn't divide evenly across its units, the line splits
-- into two lots — (qty - r) units at the base cost and r units at base + 1 — so
-- the cost basis reconciles to the cent (see services/allocation.py). Those two
-- lots are the same acquired copies and intentionally **share one SKU**
-- (services/transactions._post, services/sku.py). The UNIQUE index added in 0003
-- forbade that, so any non-divisible multi-quantity purchase (e.g. 6 copies for
-- $5.00) crashed with "UNIQUE constraint failed: lot.sku".
--
-- Replace the unique index with a plain lookup index. Per-acquisition uniqueness
-- is still guaranteed at generation time by services.sku.generate_sku (which
-- bumps the sequence until the SKU is free), and the repository already tolerates
-- shared SKUs (find_by_sku uses LIMIT 1; counts use DISTINCT).

DROP INDEX idx_lot_sku;
CREATE INDEX idx_lot_sku ON lot(sku) WHERE sku IS NOT NULL;
