-- 0006_card_show — upcoming card/TCG shows scraped from tcdb.com (see
-- services/cardshows.py). Powers the Dashboard shows calendar. One row per
-- show occurrence; `source_id` is tcdb's show ID (its ?MODE=VIEW&ID=<n>) and is
-- the upsert key. Times/venue are free text as tcdb presents them.

CREATE TABLE card_show (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL UNIQUE,   -- tcdb show ID (upsert key)
    name        TEXT    NOT NULL,
    venue       TEXT,                       -- "Baymont by Wyndham"
    city        TEXT,                       -- "Pompton Plains"
    state       TEXT    NOT NULL,           -- 2-letter code, e.g. "NJ"
    starts_on   TEXT    NOT NULL,           -- ISO date (YYYY-MM-DD)
    start_time  TEXT,                       -- "3:00 PM" (free text)
    end_time    TEXT,                       -- "9:00 PM" (free text)
    detail_url  TEXT,                       -- absolute tcdb link to the show page
    fetched_at  TEXT    NOT NULL            -- ISO datetime of the scrape
);
CREATE INDEX idx_card_show_state_date ON card_show(state, starts_on);
CREATE INDEX idx_card_show_date ON card_show(starts_on);
