-- 0002_add_language — per-item printing language (see specs/02-data-model.md).
-- A card in a different language is a distinct item, so language joins the item
-- identity key. Existing rows default to English. The runner records the version.

ALTER TABLE item ADD COLUMN language TEXT NOT NULL DEFAULT 'EN';
