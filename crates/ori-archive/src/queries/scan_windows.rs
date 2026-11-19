//! Window-specific progress and reference selection. Queries return source IDs.
//! The archive reader executes each operation in one bounded read transaction.
use super::*;

// Gridded sweeps were introduced with this table. Installing it must never
// scan legacy raw arrays or build an expression index over an existing archive.
pub(crate) fn install_scan_window_scope(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS ori_scan_window_sources (
            session_id TEXT NOT NULL, stream_id TEXT NOT NULL, plan_id TEXT NOT NULL,
            revision INTEGER NOT NULL, band_id TEXT NOT NULL, window_index INTEGER NOT NULL,
            started_at TEXT NOT NULL, sweep_id TEXT NOT NULL UNIQUE REFERENCES sweeps(id) ON DELETE CASCADE,
            ended_at TEXT NOT NULL,
            PRIMARY KEY(session_id,stream_id,plan_id,revision,band_id,window_index,started_at,sweep_id)
        ) WITHOUT ROWID",
    )?;
    Ok(())
}

/// Only the sweep writer calls this, inside the transaction that inserts the
/// raw original. Unscoped or unknown historical extensions remain ordinary raw sweeps.
pub(crate) fn insert_scan_window_scope(db: &Connection, sweep: &Value) -> Result<()> {
    let settings = &sweep["settings"];
    let Some(index) = settings["scan_window_index"].as_u64().filter(|i| *i < 449) else {
        return Ok(());
    };
    let (Some(plan), Some(revision), Some(band)) = (
        settings["scan_plan_id"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 200),
        settings["scan_plan_revision"]
            .as_i64()
            .filter(|r| (1..=9_007_199_254_740_991).contains(r)),
        settings["scan_band_id"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 64),
    ) else {
        return Ok(());
    };
    db.execute("INSERT INTO ori_scan_window_sources(session_id,stream_id,plan_id,revision,band_id,window_index,started_at,sweep_id,ended_at)
                       VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)", params![
        sweep["session_id"].as_str(), sweep["stream_id"].as_str(), plan, revision, band,
        index as i64, sweep["interval"]["started_at"].as_str(), sweep["id"].as_str(), sweep["interval"]["ended_at"].as_str()
    ])?;
    Ok(())
}

const SCOPE: &str = "FROM ori_scan_window_sources
    WHERE session_id=?1 AND stream_id=?2 AND plan_id=?3 AND revision=?4 AND band_id=?5";

fn duration_seconds(first: &str, last: &str) -> Result<f64> {
    Ok(micros_seconds(duration_micros(
        datetime(first)?,
        datetime(last)?,
    )?))
}

fn window_cadence(recent: &[Sample]) -> Result<Option<f64>> {
    let mut periods = Vec::with_capacity(recent.len().saturating_sub(1));
    for pair in recent.windows(2) {
        let delta = duration_micros(pair[0].started_at, pair[1].started_at)?;
        if delta > 0 {
            periods.push(micros_seconds(delta));
        }
    }
    if periods.is_empty() {
        return Ok(None);
    }
    periods.sort_by(f64::total_cmp);
    let middle = periods.len() / 2;
    Ok(Some(if periods.len().is_multiple_of(2) {
        (periods[middle - 1] + periods[middle]) / 2.0
    } else {
        periods[middle]
    }))
}

impl Archive {
    /// Missing windows have zero observations and are filled in by the caller.
    pub async fn scan_window_counts(
        &self,
        session: &str,
        stream: &str,
        plan: &str,
        revision: u64,
        band: &str,
    ) -> Result<Vec<(u32, u64, f64)>> {
        let revision = i64::try_from(revision).map_err(|_| error::invalid("scan revision"))?;
        let scope = [session, stream, plan, band].map(str::to_owned);
        self.inner
            .read(move |db| {
                let [session, stream, plan, band] = &scope;
                let sql = format!(
                    "SELECT window_index,
                        COUNT(*), MIN(started_at), MAX(ended_at)
                 {SCOPE} GROUP BY window_index ORDER BY window_index LIMIT 450"
                );
                let mut query = db.prepare(&sql)?;
                let mut rows = query.query(params![session, stream, plan, revision, band])?;
                let mut result = Vec::new();
                while let Some(row) = rows.next()? {
                    let first: String = row.get(2)?;
                    let last: String = row.get(3)?;
                    result.push((
                        row.get(0)?,
                        row.get::<_, i64>(1)? as u64,
                        duration_seconds(&first, &last)?,
                    ));
                }
                Ok(result)
            })
            .await
    }

    /// Use the twenty most recent passes. Include one older observation if the
    /// recent passes do not span sixty seconds. Cadence excludes that older gap.
    pub async fn baseline_for_scan_window(
        &self,
        session: &str,
        stream: &str,
        plan: &str,
        revision: u64,
        band: &str,
        window: u32,
    ) -> Result<BaselineSelection> {
        let revision = i64::try_from(revision).map_err(|_| error::invalid("scan revision"))?;
        let scope = [session, stream, plan, band].map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let [session, stream, plan, band] = &scope;
                let exact = format!("{SCOPE} AND window_index=?6");
                let parameters = params![session, stream, plan, revision, band, window];
                let statistics = format!("SELECT COUNT(*), MIN(started_at), MAX(ended_at) {exact}");
                let (count, first, last) = db.query_row(&statistics, parameters, |row| {
                    Ok((
                        row.get::<_, i64>(0)? as u64,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, Option<String>>(2)?,
                    ))
                })?;
                let elapsed_seconds = match (&first, &last) {
                    (Some(first), Some(last)) => duration_seconds(first, last)?,
                    _ => 0.0,
                };
                let select = format!("SELECT sweep_id AS id, started_at, ended_at {exact}");
                let mut query = db.prepare(&format!(
                    "{select} ORDER BY started_at DESC, sweep_id DESC LIMIT 20"
                ))?;
                let mut rows = query.query(parameters)?;
                let mut sources = Vec::with_capacity(21);
                let mut budget = maximum;
                while let Some(row) = rows.next()? {
                    let source = sample(row, maximum)?;
                    charge(&source.id, &mut budget)?;
                    sources.push(source);
                }
                sources.reverse();
                let sweep_period_seconds = window_cadence(&sources)?;
                if count > 20
                    && elapsed_seconds >= 60.0
                    && sources.last().expect("twenty recent observations").ended_at
                        - sources[0].started_at
                        < TimeDelta::seconds(60)
                {
                    let mut oldest =
                        db.prepare(&format!("{select} ORDER BY started_at, sweep_id LIMIT 1"))?;
                    let mut rows = oldest.query(parameters)?;
                    if let Some(row) = rows.next()? {
                        let earlier = sample(row, maximum)?;
                        if !sources.iter().any(|source| source.id == earlier.id) {
                            charge(&earlier.id, &mut budget)?;
                            sources.insert(0, earlier);
                        }
                    }
                }
                Ok(BaselineSelection {
                    sweep_ids: sources.into_iter().map(|source| source.id).collect(),
                    count,
                    first_started_at: first,
                    last_ended_at: last,
                    elapsed_seconds,
                    sweep_period_seconds,
                })
            })
            .await
    }

    pub async fn latest_window_baseline(
        &self,
        project: &str,
        plan: &str,
        revision: u64,
        band: &str,
        window: u32,
    ) -> Result<Option<Value>> {
        let revision = i64::try_from(revision).map_err(|_| error::invalid("scan revision"))?;
        let scope = [project, plan, band].map(str::to_owned);
        self.inner.read(move |db| {
            let [project, plan, band] = &scope;
            let payload: Option<String> = db.query_row(
                "SELECT payload FROM monitoring_records WHERE kind='baseline' AND project_id=?1
                 AND json_extract(payload,'$.scan_plan_id')=?2 AND json_extract(payload,'$.scan_plan_revision')=?3
                 AND json_extract(payload,'$.scan_band_id')=?4 AND json_extract(payload,'$.scan_window_index')=?5
                 ORDER BY created_at DESC, id DESC LIMIT 1",
                params![project, plan, revision, band, window],
                |row| row.get(0),
            ).optional()?;
            payload.map(rows::object).transpose()
        }).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };

    #[test]
    fn scope_installation_has_constant_work_on_populated_v6_and_uses_primary_key() {
        let mut db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "PRAGMA user_version=6;
            CREATE TABLE sweeps(id TEXT PRIMARY KEY, settings_json TEXT NOT NULL);
            WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000)
            INSERT INTO sweeps SELECT printf('legacy-%d',x), 'historic opaque settings' FROM n;",
        )
        .unwrap();
        let instructions = Arc::new(AtomicUsize::new(0));
        let observed = instructions.clone();
        // A data-dependent backfill or full-table index build exhausts this
        // SQLite VM budget. It includes all installation bytecode.
        db.progress_handler(
            1,
            Some(move || observed.fetch_add(1, Ordering::Relaxed) > 1000),
        )
        .unwrap();
        install_scan_window_scope(&db).unwrap();
        db.progress_handler(0, None::<fn() -> bool>).unwrap();
        assert!(instructions.load(Ordering::Relaxed) < 1000);
        assert_eq!(
            db.query_row("SELECT COUNT(*) FROM ori_scan_window_sources", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        let sql = format!(
            "EXPLAIN QUERY PLAN SELECT sweep_id, started_at, ended_at {SCOPE} AND window_index=?6 ORDER BY started_at DESC, sweep_id DESC LIMIT 20"
        );
        let plan = db
            .query_row(
                &sql,
                params!["session", "stream", "plan", 1, "upper", 0],
                |r| r.get::<_, String>(3),
            )
            .unwrap();
        assert!(
            plan.contains("SEARCH ori_scan_window_sources USING PRIMARY KEY"),
            "{plan}"
        );
        assert!(plan.contains("window_index=?"), "{plan}");
    }
}
