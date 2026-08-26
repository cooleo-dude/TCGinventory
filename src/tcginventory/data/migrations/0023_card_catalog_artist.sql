-- 0023_card_catalog_artist -- who illustrated the card, as catalog reference data.
--
-- The Collections window derives template membership from card attributes, and
-- "every card illustrated by Mitsuhiro Arita" is the template the feature is
-- most asked for. There was nowhere to keep the answer: the catalog carried
-- name/set/number/rarity/supertype and nothing about the artist.
--
-- Nullable and un-backfilled. The value arrives with the next "Update card
-- database (EN)" pull, which now resolves it as part of building each row (see
-- services/catalog._fetch_en_attributes). Until then every row reads NULL, and
-- an artist template simply has nothing to offer -- which is the honest state,
-- and better than a half-populated column that looks authoritative.
--
-- English only, for now. Japanese rows come from tcgcsv, whose card_id is a
-- TCGplayer product id ("jp-694993"); TCGdex -- the only source that knows the
-- illustrator -- keys on its own ids ("base1-1"). There is no join between them
-- short of fuzzy name+set+number matching, which needs its own accuracy budget.

ALTER TABLE card_catalog ADD COLUMN artist TEXT;

-- Collections filters artist by equality over one language at a time, so a
-- b-tree on (artist, language) serves it as a seek. Deliberately NOT added to
-- card_catalog_fts (0020): that table is trigram-tokenized for substring
-- autocomplete, and an equality filter wants an index, not trigrams.
CREATE INDEX IF NOT EXISTS idx_card_catalog_artist
    ON card_catalog(artist, language);
