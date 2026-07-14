-- 0012_card_catalog_language — tag each catalog card's printing language so the
-- English (pokemontcg.io) and Japanese (TCGdex) catalogs coexist in one table.
-- Existing rows are the English pull.

ALTER TABLE card_catalog ADD COLUMN language TEXT NOT NULL DEFAULT 'EN';
