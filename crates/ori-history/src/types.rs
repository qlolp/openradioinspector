use ori_runtime::{qos::BackgroundQos, worker::EnforcementCapabilities};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::PathBuf;

pub const MAX_CACHE_BYTES: u64 = 10 * 1024 * 1024 * 1024;
pub const MAX_RESULT_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_RECORD_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    Export,
    Summary,
    Occupancy,
}
impl Operation {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Export => "export",
            Self::Summary => "summary",
            Self::Occupancy => "occupancy",
        }
    }
}
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Filters {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stream_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub started_at_us: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ended_at_us: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_frequency_hz: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_frequency_hz: Option<i64>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Threshold {
    pub value: f64,
    pub unit: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HistoryRequest {
    pub operation: Operation,
    #[serde(default)]
    pub filters: Filters,
    #[serde(default = "default_page")]
    pub page_size: u16,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub threshold: Option<Threshold>,
}
fn default_page() -> u16 {
    100
}
pub(crate) fn valid_id(value: &str, maximum: usize) -> bool {
    !value.is_empty() && value.len() <= maximum && value.chars().all(|c| !c.is_control())
}
impl HistoryRequest {
    pub fn new(operation: Operation) -> Self {
        Self {
            operation,
            filters: Filters::default(),
            page_size: 100,
            threshold: None,
        }
    }
    pub fn validate(&self) -> crate::Result<()> {
        let f = &self.filters;
        if !(1..=500).contains(&self.page_size)
            || f.session_id.as_deref().is_some_and(|s| !valid_id(s, 1024))
            || f.stream_id.as_deref().is_some_and(|s| !valid_id(s, 1024))
            || matches!((f.started_at_us,f.ended_at_us),(Some(a),Some(b)) if a>=b)
            || f.min_frequency_hz.is_some_and(|v| v < 0)
            || f.max_frequency_hz.is_some_and(|v| v < 0)
            || matches!((f.min_frequency_hz,f.max_frequency_hz),(Some(a),Some(b)) if a>b)
        {
            return Err(crate::HistoryError::Invalid(
                "history filters or page budget",
            ));
        }
        match (self.operation, &self.threshold) {
            (Operation::Occupancy, Some(t)) if t.value.is_finite() && valid_id(&t.unit, 128) => {
                Ok(())
            }
            (Operation::Occupancy, _) => Err(crate::HistoryError::Invalid(
                "occupancy threshold and raw unit",
            )),
            (_, None) => Ok(()),
            _ => Err(crate::HistoryError::Invalid("threshold requires occupancy")),
        }
    }
}

#[derive(Clone)]
pub struct HistoryConfig {
    pub cache_directory: PathBuf,
    pub snapshot_directory: PathBuf,
    pub completed_limit: usize,
    pub retained_result_bytes: usize,
    pub max_cache_bytes: u64,
    pub max_source_bytes: u64,
    pub worker_rss_bytes: u64,
    pub qos: BackgroundQos,
}
impl HistoryConfig {
    pub fn new(cache_directory: PathBuf, snapshot_directory: PathBuf) -> Self {
        Self {
            cache_directory,
            snapshot_directory,
            completed_limit: 32,
            retained_result_bytes: 8 * 1024 * 1024,
            max_cache_bytes: MAX_CACHE_BYTES,
            max_source_bytes: 256 * 1024 * 1024,
            worker_rss_bytes: 768 * 1024 * 1024,
            qos: BackgroundQos::default(),
        }
    }
    pub(crate) fn validate(&self) -> crate::Result<()> {
        if !(1..=32).contains(&self.completed_limit)
            || !(MAX_RESULT_BYTES..=64 * 1024 * 1024).contains(&self.retained_result_bytes)
            || self.max_cache_bytes == 0
            || self.max_cache_bytes > MAX_CACHE_BYTES
            || self.max_source_bytes == 0
            || self.max_source_bytes > self.max_cache_bytes / 2
            || self.worker_rss_bytes == 0
            || self.worker_rss_bytes > 768 * 1024 * 1024
        {
            return Err(crate::HistoryError::Invalid(
                "history storage or retention budget",
            ));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum JobPhase {
    Snapshot,
    Running,
    Completed,
    Cancelled,
    Failed,
}
#[derive(Debug, Clone, Serialize)]
pub struct JobFailure {
    pub code: String,
    pub retryable: bool,
}
#[derive(Debug, Clone, Serialize)]
pub struct JobView {
    pub id: String,
    pub operation: Operation,
    pub phase: JobPhase,
    pub source_boundary: Option<u64>,
    pub source_sha256: Option<String>,
    pub result_available: bool,
    pub enforcement: Option<EnforcementCapabilities>,
    pub failure: Option<JobFailure>,
}
impl JobView {
    pub(crate) fn new(id: String, operation: Operation) -> Self {
        Self {
            id,
            operation,
            phase: JobPhase::Snapshot,
            source_boundary: None,
            source_sha256: None,
            result_available: false,
            enforcement: None,
            failure: None,
        }
    }
}
#[derive(Clone, Serialize)]
pub struct JobOutput {
    pub data: Value,
    pub metrics: Value,
    pub enforcement: EnforcementCapabilities,
}
