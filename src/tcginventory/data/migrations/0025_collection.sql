-- 0025_collection -- the Personal Collection window's projects.
--
-- A collection is either a **template** (membership derived from card
-- attributes: a set, a subset, an artist, a rarity, a supertype) or **custom**
-- (a hand-picked list). Templates need no membership rows at all, which is why
-- there is only one child table here and it is small.
--
-- Why no membership table for templates: ownership *is* membership. A slot reads
--   filled     -- owned, lot intent 'personal'
--   importable -- owned, lot intent 'resale' (matches, not yet brought in)
--   missing    -- not owned
-- so a second source of truth would need syncing on every import, sale and void.
-- Bringing a card in is an intent change on its lot (services/inventory
-- .split_lot_for_intent), not a row written here.
--
-- `slot_mode` is per collection, because the same set is two different projects:
--   'number' -- one slot per collector number. Journey Together = 190.
--   'master' -- one slot per printing.        Journey Together = 333, since 143
--               of its cards also exist as a reverse holo.
-- Master mode reads card_catalog_treatment (migration 0024), whose coverage is
-- 95%; the rest must render as *unknown*, never as single-printing, or a master
-- set silently under-counts and reads complete when it is not.
--
-- `selector` is deliberately one free-text column rather than one column per
-- template kind: it holds a set_id, an artist name, a rarity, or a subset prefix
-- depending on `template`, and the registry in services/collections.py owns the
-- interpretation. A column per kind would be five nullable columns with a
-- four-of-five-must-be-NULL rule that nothing could enforce.

CREATE TABLE collection (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    template   TEXT    NOT NULL,          -- set|subset|artist|rarity|supertype|custom
    selector   TEXT,                      -- meaning depends on template; NULL for custom
    slot_mode  TEXT    NOT NULL DEFAULT 'number',   -- number|master
    language   TEXT    NOT NULL DEFAULT 'EN',
    notes      TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

-- Custom collections only: the hand-picked slots. `treatment` names the printing
-- in 'master' mode, so one card can be two slots in a master set without a
-- second table.
--
-- NOT NULL with '' for "the card itself, printing-agnostic", rather than a
-- nullable column: SQLite treats NULLs as **distinct** in a UNIQUE or PRIMARY
-- KEY index, so a nullable `treatment` would let (1, 'base1-4', NULL) be
-- inserted twice and the primary key would not stop a duplicate slot.
CREATE TABLE collection_card (
    collection_id INTEGER NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
    card_id       TEXT    NOT NULL,
    treatment     TEXT    NOT NULL DEFAULT '',
    added_at      TEXT    NOT NULL,
    PRIMARY KEY (collection_id, card_id, treatment)
);

CREATE INDEX IF NOT EXISTS idx_collection_card_lookup
    ON collection_card(collection_id, card_id);
