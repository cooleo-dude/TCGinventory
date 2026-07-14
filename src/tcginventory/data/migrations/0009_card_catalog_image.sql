-- 0009_card_catalog_image — store the small card-image URL from the
-- pokemontcg.io dataset so hovering a card can lazily fetch + cache it locally.
-- Existing rows are NULL until the next "Update card database" re-pull.

ALTER TABLE card_catalog ADD COLUMN image_small TEXT;
