-- 0015_collection_value_snapshot — daily record of the portfolio's *effective*
-- collection value (the Dashboard "Collection Value" tile figure), per intent.
-- The collection-value chart historically reconstructed value from raw poll
-- snapshots only, so it excluded never-polled/graded/manual cards and used NM
-- polls for reviewed cards — diverging from the tile. Recording the tile figure
-- daily lets the chart match the tile and stay accurate going forward.

CREATE TABLE collection_value_snapshot (
    as_of       TEXT    NOT NULL,   -- ISO date; one row per day per intent
    intent      TEXT    NOT NULL,   -- 'all' | 'personal' | 'resale' | 'ripping'
    value_cents INTEGER NOT NULL,
    PRIMARY KEY (as_of, intent)
);
