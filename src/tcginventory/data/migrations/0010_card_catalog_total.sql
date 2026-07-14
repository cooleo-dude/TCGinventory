-- 0010_card_catalog_total — store each set's printed total so a card's full
-- collector number can be shown (e.g. "SV103/SV122", "124/142"). Existing rows
-- are NULL until the next "Update card database" re-pull.

ALTER TABLE card_catalog ADD COLUMN printed_total INTEGER;
