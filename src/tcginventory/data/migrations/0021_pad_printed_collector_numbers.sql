-- 0021_pad_printed_collector_numbers — bring owned items onto the number that is
-- actually printed on the card.
--
-- Until ticket 49, services/catalog.py stripped TCGdex's zero padding to a
-- uniform "1". That is only right for the eras that do not pad: Base Set and
-- Celebrations really do print "4/102" and "5/25", but 151 and every SV-era set
-- print "006/165". So a card autofilled from the catalog was saved with a
-- card_number that its own card contradicts, and could not be found by typing
-- the number off the front of it.
--
-- The catalog is derived data and re-pulls with the padding intact, but `item`
-- is the ledger's own metadata and nothing re-derives it. card_number is part of
-- ItemRepository's identity key (_ITEM_IDENTITY_COLUMNS), so leaving these rows
-- behind would fork every affected card in two: the "6/165" bought last month
-- and the "006/165" bought next month would stop being the same item.
--
-- The padding cannot be read from card_catalog.number here — at the moment this
-- runs, that column is still the stripped form this migration exists to undo.
-- It is read from card_id instead, whose suffix is TCGdex's localId verbatim
-- ("sv03.5-006"), so the migration is correct whether or not the English
-- catalog has been refreshed yet.
--
-- Deliberately narrow:
--   * UPDATE on `item` only. No lot, consumption, transaction or audit row is
--     touched, and no two items are merged — moving lots between items is the
--     money path and belongs behind a service with an audit trail, not here.
--   * Only rows whose set AND numerator both match a catalog card, so a set the
--     catalog does not carry is left exactly as the user typed it.
--   * Ambiguous set names (the same name in two sets disagreeing on padding) are
--     skipped by the HAVING, rather than resolved by a guess.
--
-- Measured on the author's ledger before shipping: 64 of 978 items updated,
-- 4,447 catalog rows across 49 sets affected (Brilliant Stars onward), and zero
-- identity collisions created.

UPDATE item
SET card_number = pad.printed || pad.tail
FROM (
    SELECT
        i.id AS item_id,
        p.printed AS printed,
        CASE
            WHEN instr(i.card_number, '/') > 0
            THEN substr(i.card_number, instr(i.card_number, '/'))
            ELSE ''
        END AS tail
    FROM item i
    JOIN (
        SELECT
            set_name,
            language,
            number AS stored,
            substr(card_id, length(set_id) + 2) AS printed
        FROM card_catalog
        WHERE set_id IS NOT NULL
          AND number IS NOT NULL
          -- Not LIKE: '_' is a wildcard there and a set_id may contain one.
          AND substr(card_id, 1, length(set_id) + 1) = set_id || '-'
          AND substr(card_id, length(set_id) + 2) <> number
        GROUP BY set_name, language, number
        HAVING COUNT(DISTINCT substr(card_id, length(set_id) + 2)) = 1
    ) p
      ON p.set_name = i.set_name COLLATE NOCASE
     AND p.language = i.language
     AND p.stored = CASE
            WHEN instr(i.card_number, '/') > 0
            THEN substr(i.card_number, 1, instr(i.card_number, '/') - 1)
            ELSE i.card_number
        END
    WHERE i.card_number IS NOT NULL
      AND i.card_number <> ''
) AS pad
WHERE item.id = pad.item_id;
