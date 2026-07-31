-- 0017_calendar_entry — user-created personal calendar entries (vacations,
-- obligations, travel…) shown on the Calendar alongside scraped card shows so
-- overlaps are visible and you don't double-book. These are owned by the user;
-- the tcdb show refresh never touches them.

CREATE TABLE calendar_entry (
    id         INTEGER PRIMARY KEY,
    title      TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'other',  -- vacation | obligation | travel | other
    starts_on  TEXT    NOT NULL,                  -- ISO date (YYYY-MM-DD)
    ends_on    TEXT    NOT NULL,                  -- ISO date; == starts_on for single-day
    all_day    INTEGER NOT NULL DEFAULT 1,
    start_time TEXT,                              -- free text when not all-day
    color      TEXT,                              -- hex; NULL → theme default
    notes      TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX idx_calendar_entry_dates ON calendar_entry(starts_on, ends_on);
