-- Use indexed, normalized observation time; second-precision legacy timestamps
-- and mixed sessions must not distort the time window. Read lengths, not arrays.
WITH recent AS (
    SELECT i.started_at, i.ended_at,
           length(CAST(s.frequencies_json AS BLOB)) + length(CAST(s.levels_json AS BLOB)) AS arrays,
           length(CAST(s.settings_json AS BLOB)) + length(CAST(s.id AS BLOB))
             + length(CAST(s.stream_id AS BLOB)) + length(CAST(s.session_id AS BLOB))
             + length(CAST(s.level_unit AS BLOB)) + length(i.started_at) + length(i.ended_at) AS metadata
    FROM monitoring_sweep_index AS i JOIN sweeps AS s ON s.id = i.sweep_id
    WHERE i.session_id = ? AND s.stream_id = ?
    ORDER BY i.started_at DESC, i.sweep_id DESC LIMIT 128
)
SELECT count(*), sum(arrays), sum(arrays + metadata), min(started_at), max(ended_at) FROM recent
