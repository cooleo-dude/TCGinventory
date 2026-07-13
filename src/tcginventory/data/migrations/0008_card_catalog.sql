-- 0008_card_catalog — a local reference catalog of every (English) Pokémon card,
-- pulled from the open pokemontcg.io dataset (see services/catalog.py). Powers
-- name autocomplete + set/number auto-fill on the Transaction screen. Refreshed
-- wholesale on demand; keyed by the dataset's stable card id (e.g. "sv9-163").

CREATE TABLE card_catalog (
    card_id    TEXT PRIMARY KEY,   -- pokemontcg id, e.g. "sv9-163"
    name       TEXT NOT NULL,
    set_id     TEXT NOT NULL,      -- dataset set id, e.g. "sv9"
    set_name   TEXT,               -- "Journey Together"
    set_code   TEXT,               -- ptcgoCode, e.g. "JTG"
    number     TEXT,               -- collector number, e.g. "163"
    rarity     TEXT,
    supertype  TEXT,               -- Pokémon | Trainer | Energy
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_catalog_name ON card_catalog(name);
CREATE INDEX idx_catalog_set ON card_catalog(set_name, number);
