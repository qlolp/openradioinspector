use serde_json::{Value, json};
use std::{
    future::Future,
    path::PathBuf,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::sync::Notify;

pub type Result<T> = std::result::Result<T, LegacyError>;
pub type ScienceFuture = Pin<Box<dyn Future<Output = Result<Value>> + Send + 'static>>;

#[derive(Debug, thiserror::Error)]
pub enum LegacyError {
    #[error("{0}")]
    Request(String),
    #[error("Legacy workspace is busy")]
    Busy,
    #[error("Legacy workspace is closed")]
    Closed,
    #[error("Legacy work was cancelled")]
    Cancelled,
    #[error("Legacy source changed; reload the database")]
    Stale,
    #[error("{0}")]
    ResourceLimit(String),
    #[error("Legacy cleanup is not confirmed; file ownership is retained")]
    CleanupPending,
    #[error("Legacy storage operation failed")]
    Storage(String),
    #[error("{0}")]
    Sqlite(String),
    #[error("Invalid legacy scientific response: {0}")]
    Protocol(&'static str),
    #[error("{message}")]
    Science {
        status: u16,
        code: String,
        message: String,
    },
}
impl LegacyError {
    pub fn http_status(&self) -> u16 {
        match self {
            Self::Request(_) | Self::Sqlite(_) => 400,
            Self::ResourceLimit(_) => 413,
            Self::Stale => 409,
            Self::Busy | Self::Closed | Self::Cancelled | Self::CleanupPending => 503,
            Self::Protocol(_) | Self::Storage(_) => 502,
            Self::Science { status, .. } => *status,
        }
    }
    pub fn code(&self) -> &str {
        match self {
            Self::Request(_) | Self::Sqlite(_) => "invalid_request",
            Self::ResourceLimit(_) => "resource_limit",
            Self::Stale => "legacy_source_changed",
            Self::Busy => "legacy_busy",
            Self::Closed => "legacy_closed",
            Self::Cancelled => "cancelled",
            Self::CleanupPending => "cleanup_pending",
            Self::Protocol(_) => "legacy_protocol",
            Self::Storage(_) => "legacy_storage",
            Self::Science { code, .. } => code,
        }
    }
    pub fn api_message(&self) -> String {
        self.to_string()
    }
    pub fn legacy_body(&self) -> Value {
        json!({"ok":false,"error":self.api_message()})
    }
}
impl From<std::io::Error> for LegacyError {
    fn from(value: std::io::Error) -> Self {
        Self::Storage(value.to_string())
    }
}
impl From<rusqlite::Error> for LegacyError {
    fn from(value: rusqlite::Error) -> Self {
        match value.sqlite_error_code() {
            Some(rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked) => {
                Self::Busy
            }
            _ => Self::Sqlite(value.to_string()),
        }
    }
}
impl From<ori_contracts::CodecError> for LegacyError {
    fn from(_: ori_contracts::CodecError) -> Self {
        Self::Protocol("strict JSON")
    }
}

#[derive(Default)]
struct CancelState {
    cancelled: AtomicBool,
    notify: Notify,
}
#[derive(Clone, Default)]
pub struct LegacyCancellation(Arc<CancelState>);
impl LegacyCancellation {
    pub fn cancel(&self) {
        self.0.cancelled.store(true, Ordering::SeqCst);
        self.0.notify.notify_waiters();
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.cancelled.load(Ordering::SeqCst)
    }
    pub async fn cancelled(&self) {
        loop {
            let wake = self.0.notify.notified();
            tokio::pin!(wake);
            wake.as_mut().enable();
            if self.is_cancelled() {
                return;
            }
            wake.await;
        }
    }
}

/// Reservation must cover snapshot preparation and its one child. The core
/// implements this using its shared ResearchPool, not another semaphore.
pub trait LegacyScience: Send + Sync + 'static {
    fn try_reserve(&self) -> Result<Box<dyn LegacySciencePermit>>;
}
/// Optional shared logical I/O admission for preparation and canonical writes.
/// Implementations must cooperate with cancellation while blocking.
pub trait LegacyIoBudget: Send + Sync {
    fn account(&self, bytes: u64, cancel: &LegacyCancellation) -> Result<()>;
}
/// Cancellation completion means the child and its IPC threads were reaped.
/// A provider must not return early merely because cancellation was signalled.
pub trait LegacySciencePermit: Send {
    /// Clone from the original reservation before any snapshot I/O begins.
    fn io_budget(&self) -> Option<Arc<dyn LegacyIoBudget>> {
        None
    }
    fn execute(self: Box<Self>, request: Value, cancel: LegacyCancellation) -> ScienceFuture;
}

#[derive(Clone, Debug)]
pub struct LegacyLimits {
    pub max_database_bytes: u64,
    pub max_rows: u64,
    pub max_total_points: u64,
    pub max_record_bytes: u64,
    pub max_result_bytes: usize,
    pub max_sidecar_bytes: u64,
    pub max_cache_bytes: usize,
}
impl Default for LegacyLimits {
    fn default() -> Self {
        Self {
            max_database_bytes: 128 * 1024 * 1024,
            max_rows: 10_000,
            max_total_points: 1_000_000,
            max_record_bytes: 8 * 1024 * 1024,
            max_result_bytes: 16 * 1024 * 1024,
            max_sidecar_bytes: 16 * 1024 * 1024,
            max_cache_bytes: 64 * 1024 * 1024,
        }
    }
}
impl LegacyLimits {
    pub(crate) fn validate(&self) -> Result<()> {
        let max = Self::default();
        if [
            self.max_database_bytes,
            self.max_rows,
            self.max_total_points,
            self.max_record_bytes,
            self.max_sidecar_bytes,
        ]
        .contains(&0)
            || self.max_result_bytes == 0
            || self.max_cache_bytes == 0
            || self.max_database_bytes > max.max_database_bytes
            || self.max_rows > max.max_rows
            || self.max_total_points > max.max_total_points
            || self.max_record_bytes > max.max_record_bytes
            || self.max_sidecar_bytes > max.max_sidecar_bytes
            || self.max_result_bytes > max.max_result_bytes
            || self.max_cache_bytes > max.max_cache_bytes
        {
            return Err(LegacyError::Request(
                "Legacy limits may only lower the existing caps".into(),
            ));
        }
        Ok(())
    }
    pub(crate) fn rpc(&self) -> Value {
        json!({"max_database_bytes":self.max_database_bytes,"max_rows":self.max_rows,"max_total_points":self.max_total_points,"max_record_bytes":self.max_record_bytes,"max_result_bytes":self.max_result_bytes,"max_sidecar_bytes":self.max_sidecar_bytes})
    }
}
#[derive(Clone, Debug)]
pub struct LegacyOptions {
    pub library: Option<PathBuf>,
    pub scratch_directory: PathBuf,
    pub limits: LegacyLimits,
    pub queue_capacity: usize,
    pub close_timeout: Duration,
    pub sql_timeout: Duration,
    pub io_bytes_per_second: u64,
}
impl Default for LegacyOptions {
    fn default() -> Self {
        Self {
            library: None,
            scratch_directory: std::env::temp_dir(),
            limits: LegacyLimits::default(),
            queue_capacity: 16,
            close_timeout: Duration::from_secs(5),
            sql_timeout: Duration::from_secs(15),
            io_bytes_per_second: 20 * 1024 * 1024,
        }
    }
}
impl LegacyOptions {
    pub(crate) fn validate(&self) -> Result<()> {
        self.limits.validate()?;
        if !(1..=128).contains(&self.queue_capacity)
            || self.close_timeout.is_zero()
            || self.close_timeout > Duration::from_secs(60)
            || self.sql_timeout.is_zero()
            || self.sql_timeout > Duration::from_secs(60)
            || self.io_bytes_per_second == 0
            || self.io_bytes_per_second > 20 * 1024 * 1024
        {
            return Err(LegacyError::Request(
                "Invalid bounded legacy options".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub enum LegacyQuery {
    State {
        include_waterfall: bool,
    },
    Signals,
    Coverage {
        rows: u32,
        cols: u32,
    },
    Occupancy {
        threshold_db: Option<f64>,
    },
    Efficiency {
        threshold_db: Option<f64>,
    },
    Measurements {
        start_hz: f64,
        stop_hz: f64,
        norm_hz: Option<f64>,
    },
    Keywords,
}
impl LegacyQuery {
    pub(crate) fn rpc(&self) -> Result<(&'static str, Value)> {
        let number = |value: f64| {
            if value.is_finite() {
                Ok(value)
            } else {
                Err(LegacyError::Request("Число должно быть конечным".into()))
            }
        };
        Ok(match self {
            Self::State { include_waterfall } => {
                ("state", json!({"include_waterfall":include_waterfall}))
            }
            Self::Signals => ("signals", json!({})),
            Self::Coverage { rows, cols } => {
                if !(1..=100).contains(rows) || !(1..=100).contains(cols) {
                    return Err(LegacyError::Request(
                        "Размеры сетки должны быть от 1 до 100".into(),
                    ));
                }
                ("coverage", json!({"rows":rows,"cols":cols}))
            }
            Self::Occupancy { threshold_db } | Self::Efficiency { threshold_db } => {
                let mut parameters = json!({});
                if let Some(value) = threshold_db {
                    parameters["threshold"] = json!(number(*value)?);
                }
                (
                    if matches!(self, Self::Occupancy { .. }) {
                        "occupancy"
                    } else {
                        "efficiency"
                    },
                    parameters,
                )
            }
            Self::Measurements {
                start_hz,
                stop_hz,
                norm_hz,
            } => {
                number(*start_hz)?;
                number(*stop_hz)?;
                if stop_hz <= start_hz {
                    return Err(LegacyError::Request(
                        "Конец диапазона должен быть больше начала".into(),
                    ));
                }
                let mut parameters = json!({"start":start_hz,"stop":stop_hz});
                if let Some(value) = norm_hz {
                    parameters["norm"] = json!(number(*value)?);
                }
                ("measurements", parameters)
            }
            Self::Keywords => ("keywords", json!({})),
        })
    }
}
