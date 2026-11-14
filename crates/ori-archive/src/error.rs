#[derive(Debug, thiserror::Error)]
pub enum ArchiveError {
    #[error("invalid archive input: {0}")]
    Invalid(String),
    #[error("unsupported archive schema {domain} version {version}")]
    UnsupportedSchema { domain: &'static str, version: i64 },
    #[error("archive writer is already open in this process")]
    AlreadyOpen,
    #[error("archive is closing or closed")]
    Closed,
    #[error("archive {queue} queue is full (count or bytes); input was not accepted")]
    QueueFull { queue: &'static str },
    #[error(
        "archive capacity exceeded ({database_bytes} / {max_database_bytes} logical bytes); transaction rolled back"
    )]
    Capacity {
        database_bytes: u64,
        max_database_bytes: u64,
    },
    #[error("archive query exceeded its time or response budget")]
    QueryBudget,
    #[error("archive record not found")]
    NotFound,
    #[error("archive immutable owner or reference conflicts")]
    Conflict,
    #[error("archive data is corrupt: {0}")]
    Corrupt(String),
    #[error("archive worker stopped")]
    WorkerStopped,
    #[error("SQLite operation failed: {0}")]
    Sql(#[from] rusqlite::Error),
    #[error("archive I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("archive JSON is invalid: {0}")]
    Json(#[from] serde_json::Error),
}
pub type Result<T> = std::result::Result<T, ArchiveError>;
pub(crate) fn invalid(field: &str) -> ArchiveError {
    ArchiveError::Invalid(field.into())
}
