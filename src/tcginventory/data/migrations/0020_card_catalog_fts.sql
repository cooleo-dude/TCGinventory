-- 0020_card_catalog_fts — make name autocomplete indexable.
--
-- CardCatalogRepository.search() matches every token with `LIKE '%tok%'`. A
-- *leading* wildcard cannot use an index, so 0013's indexes never applied to it
-- and each keystroke cost a full scan of the catalog plus a sort:
--
--     EXPLAIN QUERY PLAN → SCAN card_catalog
--                          USE TEMP B-TREE FOR ORDER BY
--
-- Measured at ~14 ms per keystroke over 53,289 rows, on the UI thread, at four
-- autofill call sites — and identical for a query that matches nothing, because
-- the whole table is read either way.
--
-- FTS5's **trigram** tokenizer is what makes a substring search indexable: it
-- indexes every 3-character window, so `MATCH '"harizar"'` is a seek rather than
-- a scan. The same queries drop to 0.02–0.16 ms. It is also the reason the UI
-- now requires 3 characters before searching — a 2-character substring is
-- shorter than a trigram and cannot be served from this index.
--
-- `content=` makes this an **external-content** table: it stores only the index
-- and reads columns back from card_catalog, so the 53k rows are not duplicated.
-- That also means SQLite does not keep it in sync on its own — the repository
-- rebuilds it inside _insert_many() and the two delete paths, which is every
-- write to card_catalog (see services/catalog.py). Explicit maintenance beats
-- triggers here: a catalog refresh replaces all 53k rows at once, and triggers
-- would tax that row by row.
--
-- card_id is UNINDEXED: it is fetched, never searched.

CREATE VIRTUAL TABLE card_catalog_fts USING fts5(
    card_id UNINDEXED,
    name,
    set_name,
    set_code,
    number,
    content='card_catalog',
    content_rowid='rowid',
    tokenize='trigram'
);

INSERT INTO card_catalog_fts (rowid, card_id, name, set_name, set_code, number)
SELECT rowid, card_id, name, set_name, set_code, number FROM card_catalog;

-- The other half of the same lag: display_number() derives a subset denominator
-- ("RC19/RC32") with a COUNT(DISTINCT number) per card, and 0013 left set_id
-- unindexed — so each of the 25 rows in a popup cost its own full scan. The
-- repository now batches those counts per (set_id, language, prefix); this makes
-- each one a range seek instead of a scan.
CREATE INDEX IF NOT EXISTS idx_card_catalog_set_lang_number
    ON card_catalog(set_id, language, number);
