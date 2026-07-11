-- 0005_item_review — "Mark for Review" flag on items.
-- A reviewed item's current market value is user-maintained: auto-refresh still
-- records price_snapshots (history) but does not overwrite the shown value.

ALTER TABLE item ADD COLUMN review INTEGER NOT NULL DEFAULT 0;
