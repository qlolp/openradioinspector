use ori_contracts::{JSONObject, RestoreKeys};
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const VERSION: &str = "robust-city-v1";
pub const MAX_BINS: usize = 65_536;
pub const MAX_WINDOW: usize = 15;
pub const MAX_BASELINE_SWEEPS: usize = 4096;
pub const MAX_BASELINE_CELLS: usize = 16_777_216;
pub const MAX_ALLOWED: usize = 1000;
pub const MAX_JSON_BYTES: usize = 8 * 1024 * 1024;
#[derive(Debug, thiserror::Error)]
pub enum MonitoringError {
    #[error("invalid sweep: {0}")]
    InvalidSweep(&'static str),
    #[error("invalid baseline: {0}")]
    InvalidBaseline(&'static str),
    #[error("invalid detector parameters: {0}")]
    InvalidParameters(&'static str),
    #[error("invalid checkpoint: {0}")]
    InvalidState(&'static str),
    #[error("baseline grid/settings are incompatible with this sweep")]
    Incompatible,
    #[error("monitoring data exceeds bounded {0}")]
    LimitExceeded(&'static str),
    #[error("invalid monitoring JSON: {0}")]
    Json(String),
}
pub type Result<T> = std::result::Result<T, MonitoringError>;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SourceRef {
    pub session_id: String,
    pub stream_id: String,
    pub sweep_id: String,
    #[serde(default, flatten)]
    pub extra: JSONObject,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RecentSource {
    pub session_id: String,
    pub stream_id: String,
    pub sweep_id: String,
    pub started_at: String,
    #[serde(default, flatten)]
    pub extra: JSONObject,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Baseline {
    pub fingerprint: String,
    pub frequencies_hz: Vec<u64>,
    pub p50_dbm: Vec<f64>,
    pub p95_dbm: Vec<f64>,
    pub mad_db: Vec<f64>,
    pub source_refs: Vec<SourceRef>,
    pub started_at: String,
    pub ended_at: String,
    pub detector_version: String,
    /// Contains sweep_period_seconds, preserving absent versus explicit null.
    #[serde(default, flatten)]
    pub extra: JSONObject,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Parameters {
    pub margin_db: f64,
    pub impulse_db: f64,
    pub hysteresis_db: f64,
    pub confirmations: usize,
    pub window_sweeps: usize,
    #[serde(default, flatten)]
    pub extra: JSONObject,
}
impl Default for Parameters {
    fn default() -> Self {
        Self {
            margin_db: 6.,
            impulse_db: 12.,
            hysteresis_db: 3.,
            confirmations: 3,
            window_sweeps: 5,
            extra: JSONObject::new(),
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AllowedSignal {
    pub id: String,
    pub start_hz: u64,
    pub end_hz: u64,
    #[serde(default)]
    pub watch_changes: bool,
    #[serde(default, flatten)]
    pub extra: JSONObject,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DataGap {
    pub kind: String,
    pub gap_seconds: f64,
    pub previous_started_at: String,
    #[serde(default, flatten)]
    pub extra: JSONObject,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DetectorState {
    pub window: Vec<Vec<bool>>,
    pub offsets: Vec<f64>,
    #[serde(
        default,
        deserialize_with = "present_value",
        skip_serializing_if = "Option::is_none"
    )]
    pub active: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recent_sources: Option<Vec<RecentSource>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_started_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub quality_issue: Option<DataGap>,
    #[serde(default, flatten)]
    pub extra: JSONObject,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RegionKind {
    Impulse,
    Persistent,
    BroadbandChange,
    DenseChange,
    AuthorizedChanged,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Region {
    pub first_bin: usize,
    pub last_bin: usize,
    pub start_frequency_hz: u64,
    pub end_frequency_hz: u64,
    pub center_frequency_hz: u64,
    pub peak_level: f64,
    pub peak_excess_db: f64,
    pub kind: RegionKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub component_regions_count: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub allowed_id: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DetectionStep {
    pub regions: Vec<Region>,
    pub excess_db: Vec<f64>,
    pub thresholds_db: Vec<f64>,
}
macro_rules! restore_extra {($($ty:ty),*)=>{$(impl RestoreKeys for $ty {fn restore_keys(&mut self){self.extra.restore_keys();}})*};}
restore_extra!(SourceRef, RecentSource, Parameters, AllowedSignal, DataGap);
impl RestoreKeys for Baseline {
    fn restore_keys(&mut self) {
        self.extra.restore_keys();
        self.source_refs.restore_keys();
    }
}
impl RestoreKeys for DetectorState {
    fn restore_keys(&mut self) {
        self.extra.restore_keys();
        self.active.restore_keys();
        self.recent_sources.restore_keys();
        self.quality_issue.restore_keys();
    }
}

fn present_value<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<Option<Value>, D::Error> {
    Value::deserialize(deserializer).map(Some)
}
