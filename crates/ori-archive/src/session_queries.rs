//! Typed session selectors. Each operation is one deadline/byte-bounded snapshot.
use crate::*;
use rusqlite::{params, params_from_iter, types::Value as SqlValue};

impl Archive {
    pub async fn matching_observation_ids(
        &self,
        session: &str,
        stream: &str,
        frequency_hz: u64,
        started_at: &str,
        mode: Option<&str>,
        limit: Option<usize>,
    ) -> Result<Vec<String>> {
        let frequency = i64::try_from(frequency_hz).map_err(|_| error::invalid("frequency_hz"))?;
        let limit = match limit {
            Some(0) => return Err(error::invalid("positive evidence limit")),
            Some(value) => i64::try_from(value).map_err(|_| error::invalid("evidence limit"))?,
            None => -1,
        };
        let started_at = validate::utc(started_at)?;
        let session = session.to_owned();
        let stream = stream.to_owned();
        let mode = mode.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let mut statement = db.prepare(
                "SELECT id FROM observations WHERE session_id=? AND stream_id=? AND frequency_hz=?
                 AND context_utc(started_at)>=? AND (? IS NULL OR mode=?)
                 ORDER BY context_utc(started_at),context_utf8(id) LIMIT ?",
            )?;
                let mut rows = statement.query(params![
                    session, stream, frequency, started_at, mode, mode, limit
                ])?;
                let mut output = Vec::new();
                let mut remaining = maximum.saturating_sub(2);
                while let Some(row) = rows.next()? {
                    let id: String = row.get(0)?;
                    let bytes = serde_json::to_vec(&id)?.len() + usize::from(!output.is_empty());
                    if bytes > remaining {
                        return Err(ArchiveError::QueryBudget);
                    }
                    remaining -= bytes;
                    output.push(id);
                }
                Ok(output)
            })
            .await
    }

    pub async fn candidate_has_open_investigation(
        &self,
        session: &str,
        candidate: &str,
    ) -> Result<bool> {
        let session = session.to_owned();
        let candidate = candidate.to_owned();
        self.inner
            .read(move |db| {
                Ok(db.query_row(
            "SELECT EXISTS(SELECT 1 FROM investigations WHERE session_id=? AND candidate_id=?
             AND state IN ('queued','tuning','listening','decoding'))",
            params![session,candidate], |row| row.get::<_,i64>(0),
        )? != 0)
            })
            .await
    }

    pub async fn receiver_has_runtime_investigation(
        &self,
        session: &str,
        stream: &str,
        except: Option<&str>,
    ) -> Result<bool> {
        let session = session.to_owned();
        let stream = stream.to_owned();
        let except = except.map(str::to_owned);
        self.inner
            .read(move |db| {
                Ok(db.query_row(
            "SELECT EXISTS(SELECT 1 FROM investigations WHERE session_id=? AND target_stream_id=?
             AND state IN ('tuning','listening','decoding') AND (? IS NULL OR id!=?))",
            params![session,stream,except,except], |row| row.get::<_,i64>(0),
        )? != 0)
            })
            .await
    }

    pub async fn next_queued_investigation(
        &self,
        session: &str,
    ) -> Result<Option<serde_json::Value>> {
        let session = session.to_owned();
        self.inner
            .read(move |db| {
                let mut statement = db.prepare(
                    "SELECT * FROM investigations WHERE session_id=? AND state='queued'
                 ORDER BY priority DESC,context_utc(created_at),context_utf8(id) LIMIT 1",
                )?;
                let mut records = statement.query([session])?;
                records.next()?.map(rows::investigation).transpose()
            })
            .await
    }

    pub async fn session_investigations(
        &self,
        session: &str,
        state: Option<&str>,
    ) -> Result<Vec<serde_json::Value>> {
        let session = session.to_owned();
        let state = state.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let mut statement = db.prepare(
                    "SELECT * FROM investigations WHERE session_id=? AND (? IS NULL OR state=?)
                 ORDER BY CASE WHEN state='queued' THEN 0 ELSE 1 END,
                 CASE WHEN state='queued' THEN -priority ELSE 0 END,
                 context_utc(created_at),context_utf8(id)",
                )?;
                crate::queries::collect(
                    statement.query(params![session, state, state])?,
                    rows::investigation,
                    maximum,
                )
            })
            .await
    }

    pub async fn session_candidates(
        &self,
        session: &str,
        state: Option<&str>,
    ) -> Result<Vec<serde_json::Value>> {
        let session = session.to_owned();
        let state = state.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let sql = format!(
                    "{} WHERE c.session_id=? AND (? IS NULL OR c.state=?)
                ORDER BY context_utc(c.started_at),context_utf8(c.id)",
                    rows::CANDIDATE_SELECT
                );
                let mut statement = db.prepare(&sql)?;
                crate::queries::collect(
                    statement.query(params![session, state, state])?,
                    rows::candidate,
                    maximum,
                )
            })
            .await
    }

    pub async fn session_measurement_results(
        &self,
        session: &str,
        methodology: Option<&str>,
        limit: Option<usize>,
    ) -> Result<Vec<serde_json::Value>> {
        let limit = match limit {
            Some(0) => return Err(error::invalid("positive result limit")),
            Some(value) => i64::try_from(value).map_err(|_| error::invalid("result limit"))?,
            None => -1,
        };
        let session = session.to_owned();
        let methodology = methodology.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let mut arguments = vec![SqlValue::Text(session)];
                let filter = if let Some(methodology) = methodology {
                    arguments.push(SqlValue::Text(methodology));
                    " AND methodology=?"
                } else {
                    ""
                };
                arguments.push(SqlValue::Integer(limit));
                let sql = format!(
                    "SELECT * FROM (SELECT * FROM measurement_results WHERE session_id=?{filter}
                ORDER BY context_utc(created_at) DESC,context_utf8(id) DESC LIMIT ?)
                ORDER BY context_utc(created_at),context_utf8(id)"
                );
                let mut statement = db.prepare(&sql)?;
                crate::queries::collect(
                    statement.query(params_from_iter(arguments.iter()))?,
                    rows::measurement,
                    maximum,
                )
            })
            .await
    }
}
