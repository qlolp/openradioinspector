//! Per-frequency guard statistics count each pass once, even if an original
//! point was retried. Every chunk uses the same immutable ingestion boundary.
use super::Grid;
use crate::{
    Archive, ArchiveError, Result, actor::check_response_size, error::invalid, rows, validate_limit,
};
use rusqlite::params;
use serde_json::{Value, json};

impl Archive {
    pub async fn ardv1_reference_progress(&self, session: &str, stream: &str) -> Result<Value> {
        let [session, stream] = [session, stream].map(str::to_owned);
        self.inner.read(move |db| {
            let mut stmt = db.prepare("SELECT id,context_key,firmware,start_hz,stop_hz,step_hz,total,settle_s,last_seq,seen,ready,plan_key
                FROM ori_ardv1_contexts_v1 WHERE session_id=? AND stream_id=? ORDER BY last_seq DESC LIMIT 1")?;
            let mut cursor = stmt.query(params![session,stream])?;
            let Some(row) = cursor.next()? else {
                return Ok(json!({"ready":false,"context_key":null,"plan_key":null,"points":0,"ready_points":0,
                    "minimum_passes_observed":0,"elapsed_seconds":0.0,"boundary":0}));
            };
            let context: i64 = row.get(0)?;
            let grid = Grid::from_columns(row, 3)?;
            let total = grid.total as i64;
            let seen: i64 = row.get(9)?;
            let ready: i64 = row.get(10)?;
            // Separate indexed minima. Unseen frequencies have no observations or span.
            let (samples,elapsed_us) = if seen==total {
                (db.query_row("SELECT MIN(samples) FROM ori_ardv1_progress_v1 WHERE context_id=?",[context],|r|r.get::<_,i64>(0))?,
                 db.query_row("SELECT MIN(last_us-first_us) FROM ori_ardv1_progress_v1 WHERE context_id=?",[context],|r|r.get::<_,i64>(0))?)
            } else {(0,0)};
            Ok(json!({"context_key":row.get::<_,String>(1)?,"plan_key":row.get::<_,String>(11)?,"firmware":row.get::<_,String>(2)?,
                "scan_start_hz":grid.start,"scan_stop_hz":grid.stop,"scan_step_hz":grid.step,
                "points":total,"settle_s":row.get::<_,f64>(7)?,"mode":"FM","boundary":row.get::<_,i64>(8)?,
                "ready_points":ready,"minimum_passes_observed":samples,"elapsed_seconds":elapsed_us as f64/1e6,"ready":ready==total}))
        }).await
    }

    pub async fn ardv1_reference_points(
        &self,
        session: &str,
        stream: &str,
        context_key: &str,
        boundary: u64,
        first_index: usize,
        count: usize,
    ) -> Result<Vec<Value>> {
        validate_limit(count, 64)?;
        let boundary =
            i64::try_from(boundary).map_err(|_| invalid("receiver reference boundary"))?;
        let [session, stream, key] = [session, stream, context_key].map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move |db| {
            let mut context_stmt = db.prepare(
                "SELECT id,start_hz,stop_hz,step_hz,total FROM ori_ardv1_contexts_v1
                 WHERE session_id=? AND stream_id=? AND context_key=?"
            )?;
            let mut contexts = context_stmt.query(params![session, stream, key])?;
            let row = contexts.next()?.ok_or(ArchiveError::NotFound)?;
            let context: i64 = row.get(0)?;
            let grid = Grid::from_columns(row, 1)?;
            if first_index>=grid.total { return Err(invalid("receiver reference index")); }
            let end = (first_index+count).min(grid.total);
            let mut result = Vec::with_capacity(end-first_index);
            let mut remaining = maximum;
            // Both ranges use (context_id,idx,sequence); later commits cannot
            // change the prefix or make a frozen lookup scan later history.
            let mut stmt = db.prepare("WITH latest AS (
                    SELECT pass,level,first_id,started_at,ended_at,sequence FROM ori_ardv1_points_v1
                    WHERE context_id=?1 AND idx=?2 AND sequence<=?3 ORDER BY sequence DESC LIMIT 20
                ), earliest AS (
                    SELECT pass,level,first_id,started_at,ended_at,sequence FROM ori_ardv1_points_v1
                    WHERE context_id=?1 AND idx=?2 AND sequence<=?3 ORDER BY sequence LIMIT 1
                ) SELECT * FROM latest UNION SELECT * FROM earliest ORDER BY pass")?;
            for index in first_index..end {
                let mut cursor = stmt.query(params![context,index as i64,boundary])?;
                let mut samples = Vec::with_capacity(21);
                while let Some(row) = cursor.next()? {
                    let id = row.get_ref(2)?.as_str().map_err(|_|ArchiveError::Corrupt("receiver observation ID".into()))?;
                    remaining = remaining.checked_sub(id.len()).ok_or(ArchiveError::QueryBudget)?;
                    samples.push(json!({"pass":row.get::<_,i64>(0)?,"level":row.get::<_,i64>(1)?,"observation_id":id,
                        "started_at":row.get::<_,String>(3)?,"ended_at":row.get::<_,String>(4)?}));
                }
                result.push(json!({"index":index,"frequency_hz":grid.frequency(index),"samples":samples}));
            }
            Ok(result)
        }).await
    }

    pub async fn ardv1_points_after(
        &self,
        session: &str,
        stream: &str,
        after: u64,
        limit: usize,
    ) -> Result<Vec<(u64, Value)>> {
        validate_limit(limit, 256)?;
        let after = i64::try_from(after).map_err(|_| invalid("receiver guard cursor"))?;
        let [session, stream] = [session, stream].map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move |db| {
            let mut stmt = db.prepare("SELECT d.sequence,c.context_key,o.*,p.level_bits_le FROM ori_ardv1_points_v1 d
                CROSS JOIN ori_ardv1_contexts_v1 c ON c.id=d.context_id CROSS JOIN observations o ON o.id=d.first_id
                LEFT JOIN ori_observation_level_bits p ON p.observation_id=o.id
                WHERE d.stream_id=(SELECT id FROM streams WHERE session_id=? AND id=?) AND d.sequence>?
                ORDER BY d.sequence LIMIT ?")?;
            let mut cursor = stmt.query(params![session,stream,after,limit as i64])?;
            let mut remaining = maximum;
            let mut items = Vec::new();
            while let Some(r) = cursor.next()? {
                let observation = rows::observation(r)?;
                let item = (r.get::<_,i64>(0)? as u64,json!({"context_key":r.get::<_,String>(1)?,"observation":observation}));
                remaining -= check_response_size(&item,remaining)?;
                items.push(item);
            }
            Ok(items)
        }).await
    }
}
