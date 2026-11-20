-- Limit the exact stream by ingestion sequence before reading measurements.
-- The time index supplies normalized UTC even for historical offset timestamps.
WITH captures AS (
    SELECT sweep_id FROM sweep_ingestion INDEXED BY sweep_ingestion_stream
    WHERE session_id = ? AND stream_id = ?
    ORDER BY sequence DESC LIMIT 128
), recent AS (
    SELECT i.started_at, i.ended_at,
           length(CAST(s.frequencies_json AS BLOB)) + length(CAST(s.levels_json AS BLOB)) AS arrays,
           length(CAST(s.settings_json AS BLOB)) + length(CAST(s.id AS BLOB))
             + length(CAST(s.stream_id AS BLOB)) + length(CAST(s.session_id AS BLOB))
             + length(CAST(s.level_unit AS BLOB)) + length(i.started_at) + length(i.ended_at) AS metadata
    FROM captures AS c
    CROSS JOIN sweeps AS s ON s.id = c.sweep_id
    CROSS JOIN monitoring_sweep_index AS i ON i.sweep_id = c.sweep_id
)
SELECT count(*), sum(arrays), sum(arrays) + sum(metadata), min(started_at), max(ended_at) FROM recent
