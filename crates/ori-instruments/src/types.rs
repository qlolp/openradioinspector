use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{str::FromStr, time::Duration};

pub type Result<T> = std::result::Result<T, InstrumentError>;

#[derive(Debug, thiserror::Error)]
pub enum InstrumentError {
    #[error("invalid request: {0}")]
    InvalidRequest(String),
    #[error("unsupported verified device profile: {0}")]
    UnsupportedProfile(String),
    #[error("unsafe instrument state: {0}")]
    UnsafeState(String),
    #[error("command absent from verified help: {0}")]
    UnsupportedCommand(String),
    #[error("invalid device protocol frame: {0}")]
    Protocol(String),
    #[error("device rejected command: {0}")]
    Rejected(String),
    #[error("instrument I/O deadline exceeded")]
    Timeout,
    #[error("instrument operation cancelled; reconnect required")]
    Cancelled,
    #[error("instrument connection is tainted; reconnect required")]
    Tainted,
    #[error("instrument connection is closed")]
    Closed,
    #[error("operation denied by current core control policy")]
    PolicyDenied,
    #[error("device command queue is full")]
    QueueFull,
    #[error("device actor stopped")]
    ActorStopped,
    #[error("instrument I/O: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Clone, Debug)]
pub struct IoLimits {
    pub max_text_bytes: usize,
    pub max_binary_bytes: usize,
    pub write_timeout: Duration,
    pub text_timeout: Duration,
    pub scan_timeout: Duration,
    pub receiver_timeout: Duration,
}
impl Default for IoLimits {
    fn default() -> Self {
        Self {
            max_text_bytes: 1024 * 1024,
            max_binary_bytes: 8 * 1024 * 1024,
            write_timeout: Duration::from_secs(1),
            text_timeout: Duration::from_secs(3),
            scan_timeout: Duration::from_secs(90),
            receiver_timeout: Duration::from_millis(1500),
        }
    }
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReceiveConfig {
    pub start_hz: u64,
    pub stop_hz: u64,
    pub points: u32,
    pub rbw_hz: u32,
}
impl Default for ReceiveConfig {
    fn default() -> Self {
        Self {
            start_hz: 88_000_000,
            stop_hz: 108_000_000,
            points: 201,
            rbw_hz: 10_000,
        }
    }
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawScanConfig {
    pub start_hz: u64,
    pub stop_hz: u64,
    pub points: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeviceMode {
    Unknown,
    Input,
    Output,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlMode {
    Monitoring,
    Lab,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TinySaState {
    pub device_mode: DeviceMode,
    pub control_mode: ControlMode,
    pub restart_required: bool,
    pub closed: bool,
    pub profile_verified: bool,
    /// Identity plus framed help were queried on this owned connection. This
    /// does not imply receive preparation, input mode, or any specific command.
    #[serde(default)]
    pub control_profile_verified: bool,
    pub settings_source: String,
    pub settings: Value,
    pub device_mode_readback: bool,
    pub generator_output_readback: bool,
    pub settings_readback: bool,
    pub hardware_validation: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TinySaInspection {
    pub state: TinySaState,
    pub commands: Vec<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct OperationInterval {
    #[serde(with = "utc_micros")]
    pub started_at: DateTime<Utc>,
    #[serde(with = "utc_micros")]
    pub ended_at: DateTime<Utc>,
}
mod utc_micros {
    use chrono::{DateTime, SecondsFormat, Utc};
    use serde::{Deserialize, Deserializer, Serializer};
    pub fn serialize<S: Serializer>(
        value: &DateTime<Utc>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.to_rfc3339_opts(SecondsFormat::Micros, true))
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<DateTime<Utc>, D::Error> {
        let text = String::deserialize(deserializer)?;
        DateTime::parse_from_rfc3339(&text)
            .map(|v| v.with_timezone(&Utc))
            .map_err(serde::de::Error::custom)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SpectrumKind {
    RawSpectrum,
    ProcessedTrace,
    ZeroSpan,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SpectrumSample {
    pub kind: SpectrumKind,
    pub frequencies_hz: Vec<u64>,
    pub levels: Vec<f64>,
    pub level_unit: String,
    pub interval: OperationInterval,
    pub settings: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RawSpectrumSample {
    pub kind: SpectrumKind,
    pub frequencies_hz: Vec<u64>,
    pub levels: Vec<f64>,
    pub level_unit: String,
    pub interval: OperationInterval,
    pub settings: Value,
    pub wire: Vec<u8>,
    pub zero_db: u16,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum ReceiverMode {
    Auto,
    Dstar,
    Ysf,
    Alinco,
    Nxdn,
    P25,
    Dpmr,
    Dmr,
    Fm,
    Am,
}
impl ReceiverMode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "AUTO",
            Self::Dstar => "DSTAR",
            Self::Ysf => "YSF",
            Self::Alinco => "ALINCO",
            Self::Nxdn => "NXDN",
            Self::P25 => "P25",
            Self::Dpmr => "DPMR",
            Self::Dmr => "DMR",
            Self::Fm => "FM",
            Self::Am => "AM",
        }
    }
}
impl std::fmt::Display for ReceiverMode {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}
impl FromStr for ReceiverMode {
    type Err = InstrumentError;
    fn from_str(value: &str) -> Result<Self> {
        match value.trim().to_ascii_uppercase().as_str() {
            "AUTO" => Ok(Self::Auto),
            "DSTAR" => Ok(Self::Dstar),
            "YSF" => Ok(Self::Ysf),
            "ALINCO" => Ok(Self::Alinco),
            "NXDN" => Ok(Self::Nxdn),
            "P25" => Ok(Self::P25),
            "DPMR" => Ok(Self::Dpmr),
            "DMR" => Ok(Self::Dmr),
            "FM" => Ok(Self::Fm),
            "AM" => Ok(Self::Am),
            _ => Err(InstrumentError::InvalidRequest(
                "mode is absent from verified profile; AUTO excludes TETRA".into(),
            )),
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReceiverProfile {
    #[serde(rename = "legacy-fm-am")]
    LegacyFmAm,
    #[serde(rename = "ardv1-monitor-v1")]
    MonitorV1,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReceiverConfig {
    pub frequency_hz: u64,
    pub mode: ReceiverMode,
    pub profile: ReceiverProfile,
}
impl Default for ReceiverConfig {
    fn default() -> Self {
        Self {
            frequency_hz: 145_500_000,
            mode: ReceiverMode::Fm,
            profile: ReceiverProfile::LegacyFmAm,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ReceiverState {
    pub frequency_hz: Option<u64>,
    pub mode: Option<ReceiverMode>,
    pub requested_frequency_hz: u64,
    pub requested_mode: ReceiverMode,
    pub restart_required: bool,
    pub closed: bool,
    pub frequency_source: String,
    pub frequency_readback: bool,
    pub firmware: Option<String>,
    pub hardware_validation: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ReceiverSample {
    pub frequency_hz: u64,
    pub mode: ReceiverMode,
    pub level: u32,
    pub level_unit: String,
    pub squelch_open: bool,
    pub raw_squelch_status: u8,
    pub decoded_mode: Option<String>,
    pub raw_mode_status: Option<String>,
    pub interval: OperationInterval,
    pub metadata: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(
    tag = "setting",
    content = "value",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum Setting {
    StartHz(u64),
    StopHz(u64),
    CenterHz(u64),
    SpanHz(u64),
    Points(u32),
    RbwHz(Rbw),
    AttenuationDb(Attenuation),
    Lna(bool),
    Ultra(bool),
    Spur(SwitchMode),
    Repeat(u32),
    SweepAccuracy(SweepAccuracy),
    SweepSeconds(f64),
    ExternalGainDb(f64),
    Calculation(Calculation),
    TriggerMode(TriggerMode),
    TriggerDbm(f64),
    TraceUnit(TraceUnit),
    RefLevel(AutoNumber),
    ScaleDiv(AutoNumber),
    OutputHz(u64),
    OutputDbm(f64),
    OutputEnabled(bool),
    Modulation(Modulation),
    ModulationHz(u32),
    AmDepthPercent(u8),
    FmDeviationHz(u32),
    OutputDeltaDb(f64),
    #[serde(rename = "caloutput_mhz")]
    CalOutputMhz(Option<u8>),
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Rbw {
    Auto,
    Hz(u32),
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Attenuation {
    Auto,
    Db(u8),
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AutoNumber {
    Auto,
    Value(f64),
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SwitchMode {
    Off,
    On,
    Auto,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SweepAccuracy {
    Normal,
    Precise,
    Fast,
    Noise,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Calculation {
    Off,
    Minh,
    Maxh,
    Maxd,
    Aver4,
    Aver16,
    Aver,
    Quasi,
    Log,
    Lin,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TriggerMode {
    Auto,
    Normal,
    Single,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub enum TraceUnit {
    #[serde(rename = "dBm")]
    Dbm,
    #[serde(rename = "dBmV")]
    Dbmv,
    #[serde(rename = "dBuV")]
    Dbuv,
    #[serde(rename = "dBV")]
    Dbv,
    V,
    Vpp,
    W,
    #[serde(rename = "RAW")]
    Raw,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Modulation {
    Off,
    Am,
    Fm,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WireCommand {
    pub wire: String,
    pub requires_lab: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum TinySaControl {
    EnterLab,
    /// Explicit new lease after release/expiry while the owned device stays lab.
    /// Unlike EnterLab this requires that lab mode is already active.
    ReenterLab,
    OutputOff,
    ResumeMonitoring,
    Set(Setting),
    Trace(TraceCommand),
    GeneratorSweep(GeneratorSweepConfig),
    DeleteSd {
        filename: String,
        confirmed: bool,
    },
    Release,
    MenuHome,
    Keypad {
        text: String,
    },
    Touch {
        x: u16,
        y: u16,
    },
    SavePreset {
        slot: u8,
        confirmed: bool,
    },
    LoadPreset {
        slot: u8,
        confirmed: bool,
    },
    SelfTest {
        test: u8,
        confirmed: bool,
    },
    ApplyReceivePreset(ReceivePreset),
}

/// One prevalidated receive-only preset, in original document setting order.
/// Uses only the existing setting registry and documented combined sweep command.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReceivePreset {
    pub settings: Vec<Setting>,
    pub confirmed: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GeneratorSweepConfig {
    pub start_hz: u64,
    pub stop_hz: u64,
    pub points: u32,
    pub seconds: f64,
    pub level_dbm: f64,
    pub delta_db: f64,
    pub confirmed: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum TraceCommand {
    Copy { trace: u8, source_trace: u8 },
    Subtract { trace: u8, source_trace: u8 },
    Freeze { trace: u8, enabled: bool },
    View { trace: u8, enabled: bool },
    Calculation { trace: u8, calculation: Calculation },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum MarkerCommand {
    Read { marker: u8 },
    On { marker: u8 },
    Off { marker: u8 },
    Peak { marker: u8 },
    Frequency { marker: u8, frequency_hz: u64 },
    Delta { marker: u8, reference: u8 },
    Noise { marker: u8, enabled: bool },
    Tracking { marker: u8, enabled: bool },
    Trace { marker: u8, reference: u8 },
    TraceAverage { marker: u8, enabled: bool },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DeviceTextSample {
    pub body: String,
    pub level_unit: String,
    pub interval: OperationInterval,
    pub metadata: Value,
}
