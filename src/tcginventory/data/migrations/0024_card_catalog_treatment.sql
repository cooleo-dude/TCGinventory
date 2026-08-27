-- 0024_card_catalog_treatment -- which foil printings each catalog card exists in.
--
-- The Collections window needs "every reverse holo in this set" to be an
-- enumerable checklist, and nothing in card_catalog said which treatments a card
-- comes in. This is the "foil column" idea, normalised: a card has *several*
-- treatments (45.6% of TCGplayer products carry more than one), so one column
-- could only ever record one of them and would lose the question being asked.
--
-- Source is tcgcsv's `subTypeName`, in the same price files services/pricing.py
-- already downloads -- so this costs group resolution, not bandwidth. Measured
-- across all 218 English groups: exactly seven values (Normal, Reverse Holofoil,
-- Holofoil, 1st Edition, 1st Edition Holofoil, Unlimited, Unlimited Holofoil).
--
-- Stored as tcgcsv's raw string rather than decomposed into
-- (holo_pattern, edition), and decomposed on read. The vendor vocabulary is the
-- source of truth and is open-ended in principle -- a closed enum would be wrong
-- within a release, exactly as migration 0022 reasoned about `variant`.
--
-- No foreign key to card_catalog: CardCatalogRepository.replace_for_language
-- DELETEs and re-inserts a whole language's rows on every pull, and an FK would
-- either block that or cascade the treatments away mid-rebuild. The two are
-- rewritten together inside one transaction instead (see services/catalog.py).

CREATE TABLE card_catalog_treatment (
    card_id   TEXT NOT NULL,   -- card_catalog.card_id, e.g. "base1-4"
    language  TEXT NOT NULL,   -- denormalised so a pull can clear its own rows
    treatment TEXT NOT NULL,   -- tcgcsv subTypeName, e.g. "Reverse Holofoil"
    PRIMARY KEY (card_id, treatment)
);

-- "every card in this set that has a reverse holo" -- the Collections query.
CREATE INDEX IF NOT EXISTS idx_treatment_lookup
    ON card_catalog_treatment(language, treatment, card_id);
