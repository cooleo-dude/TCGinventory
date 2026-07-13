-- 0007_card_show_status — the user's per-show attendance status for the shows
-- calendar (denied | pending | attending | walking). Kept in its own table keyed
-- by tcdb's show id (NOT a card_show FK) so it survives the wholesale
-- delete+reinsert that a state refresh does. Absence of a row means "no status".

CREATE TABLE card_show_status (
    source_id INTEGER PRIMARY KEY,   -- tcdb show id (matches card_show.source_id)
    status    TEXT NOT NULL          -- denied | pending | attending | walking
);
