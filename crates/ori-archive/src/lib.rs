//! Compatible canonical SQLite archive. All mutations run on one bounded dedicated writer.
mod actor;
mod backup;
mod domains;
mod error;
mod json;
mod model;
mod queries;
mod rows;
mod schema;
mod validate;
mod writer;
pub use backup::{restore_backup, verify_backup};
pub use error::{ArchiveError, Result};
pub use json::parse_json_value;
pub use model::*;
pub use schema::{MAIN_VERSION, MONITORING_VERSION, PRECISION_DDL, PRECISION_VERSION};
use serde_json::{Value, json};
use std::sync::Arc;

#[derive(Clone)]
pub struct Archive {
    inner: Arc<actor::Inner>,
}
impl Archive {
    pub fn open(config: ArchiveConfig) -> Result<Self> {
        Ok(Self {
            inner: actor::Inner::open(config)?,
        })
    }
    pub async fn apply(&self, mutations: Vec<Mutation>) -> Result<CommittedBatch> {
        self.inner.apply(mutations).await
    }
    pub async fn create_session(&self, value: Value) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::CreateSession(value)]).await
    }
    pub async fn save_session(
        &self,
        value: Value,
        event: Option<EventDraft>,
    ) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::SaveSession { value, event }])
            .await
    }
    pub async fn append_sweeps(&self, values: Vec<Value>) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::Sweeps(values)]).await
    }
    pub async fn append_observations(&self, values: Vec<Value>) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::Observations(values)]).await
    }
    pub async fn get_session(&self, id: &str) -> Result<Option<Value>> {
        let id = id.to_owned();
        self.inner.read(move |db| rows::session(db, &id)).await
    }
    pub async fn get_sweep(&self, id: &str) -> Result<Option<Value>> {
        let id = id.to_owned();
        self.inner.read(move |db| rows::get_sweep(db, &id)).await
    }
    pub async fn get_observation(&self, id: &str) -> Result<Option<Value>> {
        let id = id.to_owned();
        self.inner
            .read(move |db| rows::get_observation(db, &id))
            .await
    }
    pub async fn events(&self, after: u64, limit: usize) -> Result<Vec<Value>> {
        validate_limit(limit, 1000)?;
        if after > i64::MAX as u64 {
            return Err(error::invalid("event cursor"));
        }
        {
            let maximum = self.inner.config.max_response_bytes;
            self.inner
                .read(move |db| {
                    let mut stmt = db.prepare(
                        "SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
                    )?;
                    queries::collect(
                        stmt.query(rusqlite::params![after as i64, limit as i64])?,
                        rows::event,
                        maximum,
                    )
                })
                .await
        }
    }
    pub fn subscribe_commits(&self) -> tokio::sync::broadcast::Receiver<CommitNotice> {
        self.inner.notices.subscribe()
    }
    pub fn queue_status(&self) -> QueueStatus {
        self.inner.queue_status()
    }
    pub async fn status(&self) -> Result<Value> {
        let queue = self.queue_status();
        let synchronous = self.inner.writer_synchronous;
        let readers = self.inner.config.readers;
        self.inner.read(move|db|Ok(json!({"sqlite_version":rusqlite::version(),"schema_version":db.query_row("PRAGMA user_version",[],|r|r.get::<_,i64>(0))?,"monitoring_schema_version":db.query_row("SELECT version FROM monitoring_schema",[],|r|r.get::<_,i64>(0))?,"precision_schema_version":db.query_row("SELECT version FROM ori_archive_schema",[],|r|r.get::<_,i64>(0))?,"journal_mode":db.query_row("PRAGMA journal_mode",[],|r|r.get::<_,String>(0))?,"synchronous":synchronous,"readers":readers,"queue":queue}))).await
    }
    pub async fn close(&self) -> Result<()> {
        self.inner.close().await
    }
}
fn validate_limit(limit: usize, max: usize) -> Result<()> {
    if !(1..=max).contains(&limit) {
        return Err(error::invalid("bounded page limit"));
    }
    Ok(())
}
