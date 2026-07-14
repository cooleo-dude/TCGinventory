-- 0013_card_catalog_indexes — speed up card lookups (image_for / autocomplete)
-- now that the catalog holds ~47k rows (English + Japanese). The exact-match
-- gathers on name/number/set_code go from full scans to index seeks.

CREATE INDEX IF NOT EXISTS idx_card_catalog_name
    ON card_catalog(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_card_catalog_number
    ON card_catalog(number);
CREATE INDEX IF NOT EXISTS idx_card_catalog_set_code
    ON card_catalog(set_code COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_card_catalog_set_name
    ON card_catalog(set_name COLLATE NOCASE);
