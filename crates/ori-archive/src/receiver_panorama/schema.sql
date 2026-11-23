-- Rebuildable references to unmodified canonical observations. A rollback
-- writer still queues cyclic claims; the next Rust open validates them.
CREATE TABLE IF NOT EXISTS ori_ardv1_pending_v1 (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT NOT NULL UNIQUE REFERENCES observations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ori_ardv1_contexts_v1 (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, stream_id TEXT NOT NULL,
    context_key TEXT NOT NULL, plan_key TEXT NOT NULL, firmware TEXT NOT NULL,
    start_hz INTEGER NOT NULL, stop_hz INTEGER NOT NULL, step_hz INTEGER NOT NULL,
    total INTEGER NOT NULL, settle_s REAL NOT NULL, last_seq INTEGER NOT NULL,
    last_id TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0,
    UNIQUE(stream_id,context_key),
    FOREIGN KEY(stream_id,session_id) REFERENCES streams(id,session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ori_ardv1_latest_v1 ON ori_ardv1_contexts_v1(session_id,stream_id,last_seq);
CREATE INDEX IF NOT EXISTS ori_ardv1_checkpoint_v1 ON ori_ardv1_contexts_v1(session_id,stream_id,plan_key,last_seq);
CREATE TABLE IF NOT EXISTS ori_ardv1_passes_v1 (
    context_id INTEGER NOT NULL REFERENCES ori_ardv1_contexts_v1(id) ON DELETE CASCADE,
    stream_id TEXT NOT NULL, pass INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT NOT NULL, last_seq INTEGER NOT NULL,
    PRIMARY KEY(context_id,pass)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ori_ardv1_pass_lookup_v1 ON ori_ardv1_passes_v1(stream_id,pass,last_seq);
CREATE TABLE IF NOT EXISTS ori_ardv1_points_v1 (
    sequence INTEGER PRIMARY KEY, stream_id TEXT NOT NULL,
    context_id INTEGER NOT NULL REFERENCES ori_ardv1_contexts_v1(id) ON DELETE CASCADE,
    pass INTEGER NOT NULL, idx INTEGER NOT NULL, level INTEGER NOT NULL,
    first_id TEXT NOT NULL UNIQUE REFERENCES observations(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL, ended_at TEXT NOT NULL, best_seq INTEGER NOT NULL, best_level INTEGER NOT NULL,
    best_id TEXT REFERENCES observations(id) ON DELETE SET NULL,
    UNIQUE(context_id,pass,idx)
);
CREATE INDEX IF NOT EXISTS ori_ardv1_detection_v1 ON ori_ardv1_points_v1(stream_id,sequence);
CREATE INDEX IF NOT EXISTS ori_ardv1_history_v1 ON ori_ardv1_points_v1(context_id,idx,sequence);
CREATE TABLE IF NOT EXISTS ori_ardv1_progress_v1 (
    context_id INTEGER NOT NULL REFERENCES ori_ardv1_contexts_v1(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, samples INTEGER NOT NULL, first_us INTEGER NOT NULL, last_us INTEGER NOT NULL,
    ready INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(context_id,idx)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ori_ardv1_min_samples_v1 ON ori_ardv1_progress_v1(context_id,samples);
CREATE INDEX IF NOT EXISTS ori_ardv1_min_span_v1 ON ori_ardv1_progress_v1(context_id,last_us-first_us);
