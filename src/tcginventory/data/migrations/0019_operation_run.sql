-- Record every maintenance operation that runs.
--
-- The Operations window shows each job's last run, and the imports/exports had
-- no trace at all — nothing wrote one. The same table backs the Activity list,
-- so results survive a restart instead of living in a dismissed popup.
--
-- ``source`` separates a run the user started ('ui') from the background daily
-- task ('scheduled') and the automatic startup backup ('startup'); that split is
-- what a later "since you last opened" digest reads.
--
-- Local telemetry, not ledger data: deliberately excluded from
-- importexport._ALL_TABLES, so a full-backup restore neither wipes nor replays it.
CREATE TABLE IF NOT EXISTS operation_run (
    id          INTEGER PRIMARY KEY,
    op          TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    ok          INTEGER NOT NULL DEFAULT 1,
    summary     TEXT,
    source      TEXT    NOT NULL DEFAULT 'ui'
);

CREATE INDEX IF NOT EXISTS idx_operation_run_op
    ON operation_run(op, started_at DESC);
