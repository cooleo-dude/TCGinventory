-- 0022_item_variant — which printing this card is, as part of its identity.
--
-- tcgcsv lists several products for one collector number: a base print plus
-- stamped or alternate-material variants. Mew ex 205/165 exists as both
-- "Mew ex - 205/165" ($32.35) and "Mew ex - 205/165 (151 Metal Card)" ($25.57),
-- and CardEntryMixin's Printing dropdown already makes the user choose between
-- them — it applies the chosen price and TCGplayer link, then discards the
-- label, because there was nowhere on `item` to keep it.
--
-- With no column for it the two printings agreed on all eleven identity
-- columns, so ItemRepository.find_by_identity merged them into one item. The
-- reporter ended up with one item carrying three lots (two metal copies and a
-- regular), and editing item-level Notes on one Inventory row changed all
-- three, because the grid is one row per *lot* over a shared item.
--
-- Worse than the display: _match_or_create_item refreshes market_value_cents on
-- the item it matches, so acquiring a metal copy overwrote the base card's
-- market value with the metal one's. They are different assets and must be
-- different items.
--
-- Nullable and un-backfilled, deliberately:
--   * Every existing row reads NULL, so they go on matching one another exactly
--     as before — no retroactive fragmentation of 1,071 items, and every SKU
--     already issued stays byte-identical (services/sku.make_base only adds a
--     segment when variant is set).
--   * Nothing in the data says which of those three Mew ex lots are the metal
--     ones. Only the user knows, so the repair is a user-driven split
--     (services/inventory.split_lot_to_new_item), never a guess made here.
--
-- Free text rather than an enum: the picker fills it from tcgcsv's own product
-- label, but the set of variants is open-ended and vendor-defined, so a closed
-- enum would be wrong within a release.

ALTER TABLE item ADD COLUMN variant TEXT;

-- find_by_identity matches on all identity columns at once; this keeps the
-- common (name, set_name, variant) narrowing index-backed rather than a scan.
CREATE INDEX IF NOT EXISTS idx_item_variant ON item(set_name, name, variant);
