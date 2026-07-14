-- 0011_item_holo_edition — track a card's foil pattern and print edition as
-- part of its identity (a reverse-holo or 1st-edition copy is a distinct item
-- with a distinct value). Existing rows are NULL (unspecified).

ALTER TABLE item ADD COLUMN holo_pattern TEXT;
ALTER TABLE item ADD COLUMN edition TEXT;
