use crate::Result;
use crate::*;
use ori_contracts::{parse_json_value, to_json_vec};
use ori_instruments::*;
use serde::{
    Deserialize,
    de::{self, MapAccess, Visitor},
};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::fmt;

pub const MAX_REQUEST_BYTES: usize = 65_536;

/// Private parsed admission data; intentionally no Debug/Serialize for secrets.
pub struct Submission {
    pub(crate) kind: OperationKind,
    pub(crate) parameters: Value,
    pub(crate) expected: Option<Value>,
    pub(crate) lease_id: Option<String>,
    pub(crate) request_id: Option<String>,
    pub(crate) fingerprint: [u8; 32],
    pub(crate) client_digest: Option<[u8; 32]>,
    pub(crate) bytes: usize,
}
impl Submission {
    pub fn kind(&self) -> OperationKind {
        self.kind
    }
    pub fn operation(&self) -> Result<Operation> {
        parse_operation(self.kind, &self.parameters)
    }
    pub fn expected_revision(&self) -> Result<Option<u64>> {
        self.expected
            .as_ref()
            .map(|v| integer(v, 0, u64::MAX, "expected_revision"))
            .transpose()
    }
    pub(crate) fn tracked(&self) -> bool {
        !matches!(
            self.kind,
            OperationKind::Inspect
                | OperationKind::ServiceGuide
                | OperationKind::LeaseRenew
                | OperationKind::OutputOff
                | OperationKind::AudioInputs
        ) && !(self.kind == OperationKind::Capture
            && self.parameters.get("save") != Some(&Value::Bool(true)))
    }
}
pub fn parse_submission(raw: &[u8], client: Option<&str>) -> Result<Submission> {
    if raw.len() > MAX_REQUEST_BYTES {
        return Err(ControlError::Invalid("request exceeds 64 KiB"));
    }
    let value = parse_json_value(raw, MAX_REQUEST_BYTES)
        .map_err(|_| ControlError::Invalid("strict JSON request required"))?;
    let obj = value
        .as_object()
        .ok_or(ControlError::Invalid("request must be an object"))?;
    if obj.keys().any(|k| {
        !matches!(
            k.as_str(),
            "operation" | "parameters" | "lease_id" | "expected_revision" | "request_id"
        )
    }) {
        return Err(ControlError::Invalid("unknown request field"));
    }
    let kind = OperationKind::parse(
        obj.get("operation")
            .and_then(Value::as_str)
            .ok_or(ControlError::Invalid("operation required"))?,
    )?;
    let parameters = obj.get("parameters").cloned().unwrap_or_else(|| json!({}));
    if !parameters.is_object() {
        return Err(ControlError::Invalid("parameters must be an object"));
    }
    let client_digest = client
        .map(|v| {
            if !identifier(v, 16, 128) {
                return Err(ControlError::Invalid(
                    "invalid X-Instrument-Client capability",
                ));
            }
            Ok(Sha256::digest(v.as_bytes()).into())
        })
        .transpose()?;
    if kind == OperationKind::LeaseAcquire && client_digest.is_none() {
        return Err(ControlError::Invalid(
            "lab acquisition requires private X-Instrument-Client capability",
        ));
    }
    let request_id = obj
        .get("request_id")
        .map(|v| {
            v.as_str()
                .filter(|s| identifier(s, 1, 80))
                .map(str::to_owned)
                .ok_or(ControlError::Invalid("invalid request_id"))
        })
        .transpose()?;
    let canonical =
        to_json_vec(&value).map_err(|_| ControlError::Invalid("invalid finite request"))?;
    Ok(Submission {
        kind,
        parameters,
        expected: obj.get("expected_revision").cloned(),
        lease_id: obj
            .get("lease_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
        request_id,
        fingerprint: Sha256::digest(canonical).into(),
        client_digest,
        bytes: raw.len(),
    })
}
pub(crate) fn identifier(value: &str, min: usize, max: usize) -> bool {
    (min..=max).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
}
fn integer(v: &Value, min: u64, max: u64, name: &'static str) -> Result<u64> {
    v.as_u64()
        .filter(|n| (min..=max).contains(n))
        .ok_or(ControlError::Invalid(name))
}
fn number(v: &Value, min: f64, max: f64, name: &'static str) -> Result<f64> {
    v.as_f64()
        .filter(|n| n.is_finite() && *n >= min && *n <= max)
        .ok_or(ControlError::Invalid(name))
}
fn boolean(v: &Value) -> Result<bool> {
    v.as_bool().ok_or(ControlError::Invalid("boolean required"))
}
fn named<T: serde::de::DeserializeOwned>(v: &Value) -> Result<T> {
    serde_json::from_value(v.clone())
        .map_err(|_| ControlError::Invalid("unsupported setting choice"))
}
fn keys(p: &Map<String, Value>, allowed: &[&str]) -> Result<()> {
    if p.keys().any(|k| !allowed.contains(&k.as_str())) {
        Err(ControlError::Invalid("unknown operation parameter"))
    } else {
        Ok(())
    }
}
fn get<'a>(p: &'a Map<String, Value>, key: &str, default: &'a Value) -> &'a Value {
    p.get(key).unwrap_or(default)
}
fn uint(p: &Map<String, Value>, key: &str, default: u64, min: u64, max: u64) -> Result<u64> {
    integer(
        get(p, key, &json!(default)),
        min,
        max,
        "integer parameter outside verified bounds",
    )
}
fn num(p: &Map<String, Value>, key: &str, default: f64, min: f64, max: f64) -> Result<f64> {
    number(
        get(p, key, &json!(default)),
        min,
        max,
        "number parameter outside verified bounds",
    )
}
fn flag(p: &Map<String, Value>, key: &str, default: bool) -> Result<bool> {
    boolean(get(p, key, &json!(default)))
}
fn text(p: &Map<String, Value>, key: &str, default: &str, max: usize) -> Result<String> {
    get(p, key, &json!(default))
        .as_str()
        .filter(|s| s.chars().count() <= max && !s.contains('\0'))
        .map(str::to_owned)
        .ok_or(ControlError::Invalid("invalid bounded text parameter"))
}
fn confirm(p: &Map<String, Value>, key: &str) -> Result<bool> {
    if flag(p, key, false)? {
        Ok(true)
    } else {
        Err(ControlError::Invalid("explicit confirmation required"))
    }
}
fn filename(p: &Map<String, Value>) -> Result<String> {
    let s = text(p, "filename", "TRACE.CSV", 64)?;
    if !identifier(&s, 1, 64) || !s.as_bytes()[0].is_ascii_alphanumeric() || s.contains("..") {
        return Err(ControlError::Invalid("filename without paths required"));
    }
    Ok(s)
}
fn raw_range(p: &Map<String, Value>, zero_span: bool, default: bool) -> Result<RawRange> {
    let value = |key, initial, min, max| -> Result<Option<u64>> {
        match p.get(key) {
            Some(v) => Ok(Some(integer(
                v,
                min,
                max,
                "invalid scanraw range/point count",
            )?)),
            None if default => Ok(Some(initial)),
            None => Ok(None),
        }
    };
    let range = RawRange {
        start_hz: value("start_hz", 100_000_000, 100_000, 6_000_000_000)?,
        stop_hz: if zero_span {
            None
        } else {
            value("stop_hz", 101_000_000, 100_000, 6_000_000_000)?
        },
        points: value("points", 450, if default { 2 } else { 1 }, 65_536)?.map(|n| n as u32),
        zero_span,
    };
    if let (Some(start), Some(stop)) = (range.start_hz, range.stop_hz)
        && start > stop
    {
        return Err(ControlError::Invalid("scanraw range must be increasing"));
    }
    Ok(range)
}

pub fn parse_setting(name: &str, v: &Value) -> Result<Setting> {
    let int = |min, max| integer(v, min, max, "integer setting outside verified bounds");
    let n = |min, max| number(v, min, max, "number setting outside verified bounds");
    let setting = match name {
        "start_hz" => Setting::StartHz(int(100_000, 6_000_000_000)?),
        "stop_hz" => Setting::StopHz(int(100_000, 6_000_000_000)?),
        "center_hz" => Setting::CenterHz(int(100_000, 6_000_000_000)?),
        "span_hz" => Setting::SpanHz(int(0, 5_999_900_000)?),
        "points" => Setting::Points(int(2, 450)? as u32),
        "rbw_hz" => Setting::RbwHz(if v == "auto" {
            Rbw::Auto
        } else {
            Rbw::Hz(int(0, u32::MAX as u64)? as u32)
        }),
        "attenuation_db" => Setting::AttenuationDb(if v == "auto" {
            Attenuation::Auto
        } else {
            Attenuation::Db(int(0, 31)? as u8)
        }),
        "lna" => Setting::Lna(boolean(v)?),
        "ultra" => Setting::Ultra(boolean(v)?),
        "spur" => Setting::Spur(named(v)?),
        "repeat" => Setting::Repeat(int(1, 1000)? as u32),
        "sweep_accuracy" => Setting::SweepAccuracy(named(v)?),
        "sweep_seconds" => Setting::SweepSeconds(n(0., 60.)?),
        "external_gain_db" => Setting::ExternalGainDb(n(-100., 100.)?),
        "calculation" => Setting::Calculation(named(v)?),
        "trigger_mode" => Setting::TriggerMode(named(v)?),
        "trigger_dbm" => Setting::TriggerDbm(n(-174., 30.)?),
        "trace_unit" => Setting::TraceUnit(named(v)?),
        "ref_level" => Setting::RefLevel(if v == "auto" {
            AutoNumber::Auto
        } else {
            AutoNumber::Value(n(-174., 150.)?)
        }),
        "scale_div" => Setting::ScaleDiv(if v == "auto" {
            AutoNumber::Auto
        } else {
            AutoNumber::Value(n(0.01, 100.)?)
        }),
        "output_hz" => Setting::OutputHz(int(100_000, 800_000_000)?),
        "output_dbm" => Setting::OutputDbm(n(-115., -19.)?),
        "output_enabled" => Setting::OutputEnabled(boolean(v)?),
        "modulation" => Setting::Modulation(named(v)?),
        "modulation_hz" => Setting::ModulationHz(int(50, 5000)? as u32),
        "am_depth_percent" => Setting::AmDepthPercent(int(10, 100)? as u8),
        "fm_deviation_hz" => Setting::FmDeviationHz(int(1000, 300000)? as u32),
        "output_delta_db" => Setting::OutputDeltaDb(n(-90., 90.)?),
        "caloutput_mhz" => Setting::CalOutputMhz(if v == "off" {
            None
        } else {
            Some(int(1, 30)? as u8)
        }),
        _ => return Err(ControlError::Invalid("unknown instrument setting")),
    };
    setting_command(&setting)
        .map_err(|_| ControlError::Invalid("setting outside verified profile"))?;
    Ok(setting)
}

pub fn parse_operation(kind: OperationKind, parameters: &Value) -> Result<Operation> {
    let p = parameters
        .as_object()
        .ok_or(ControlError::Invalid("parameters must be object"))?;
    Ok(match kind {
        OperationKind::Inspect => {
            keys(p, &[])?;
            Operation::Inspect
        }
        OperationKind::Capture => Operation::Capture {
            save: p.get("save") == Some(&Value::Bool(true)),
        },
        OperationKind::LeaseAcquire => Operation::LeaseAcquire {
            acknowledge_rf: confirm(p, "acknowledge_rf")?,
        },
        OperationKind::LeaseRenew => Operation::LeaseRenew,
        OperationKind::LeaseRelease => Operation::LeaseRelease,
        OperationKind::MonitorResume => Operation::MonitorResume,
        OperationKind::Setting => Operation::Setting(parse_setting(
            p.get("name")
                .and_then(Value::as_str)
                .ok_or(ControlError::Invalid("setting name required"))?,
            p.get("value")
                .ok_or(ControlError::Invalid("setting value required"))?,
        )?),
        OperationKind::Touch => Operation::Touch {
            x: uint(p, "x", u64::MAX, 0, 479)? as u16,
            y: uint(p, "y", u64::MAX, 0, 319)? as u16,
        },
        OperationKind::Release => Operation::Release,
        OperationKind::MenuHome => Operation::MenuHome,
        OperationKind::Keypad => {
            let s = text(p, "text", "", 32)?;
            if !valid_keypad(&s) {
                return Err(ControlError::Invalid("numeric keypad input required"));
            }
            Operation::Keypad { text: s }
        }
        OperationKind::SdList => {
            keys(p, &[])?;
            Operation::SdList
        }
        OperationKind::SdRead => {
            keys(p, &["filename"])?;
            Operation::SdRead {
                filename: filename(p)?,
            }
        }
        OperationKind::SdDelete => {
            keys(p, &["filename", "confirm_delete"])?;
            Operation::SdDelete {
                filename: filename(p)?,
                confirm_delete: confirm(p, "confirm_delete")?,
            }
        }
        OperationKind::PresetSave | OperationKind::PresetLoad => {
            keys(p, &["slot", "confirm"])?;
            let slot = uint(p, "slot", 0, 0, 4)? as u8;
            let confirm = confirm(p, "confirm")?;
            if kind == OperationKind::PresetSave {
                Operation::PresetSave { slot, confirm }
            } else {
                Operation::PresetLoad { slot, confirm }
            }
        }
        OperationKind::AcquireRaw => Operation::AcquireRaw(raw_range(p, false, false)?),
        OperationKind::AcquireZero => Operation::AcquireZero(raw_range(p, true, false)?),
        OperationKind::ServiceGuide => {
            keys(p, &["name"])?;
            let name = text(p, "name", "multiband", 64)?;
            if !GUIDE_NAMES.contains(&name.as_str()) {
                return Err(ControlError::Invalid("unknown service guide"));
            }
            Operation::ServiceGuide { name }
        }
        OperationKind::Trace => {
            keys(
                p,
                &["trace", "action", "source_trace", "enabled", "calculation"],
            )?;
            let trace = uint(p, "trace", 1, 1, 4)? as u8;
            let source_trace = uint(p, "source_trace", 2, 1, 4)? as u8;
            let enabled = flag(p, "enabled", false)?;
            let calculation: Calculation = named(get(p, "calculation", &json!("off")))?;
            let action = text(p, "action", "read", 32)?;
            Operation::Trace(match action.as_str() {
                "read" => TraceOperation::Read { trace },
                "copy" => TraceOperation::Control(TraceCommand::Copy {
                    trace,
                    source_trace,
                }),
                "subtract" => TraceOperation::Control(TraceCommand::Subtract {
                    trace,
                    source_trace,
                }),
                "freeze" => TraceOperation::Control(TraceCommand::Freeze { trace, enabled }),
                "view" => TraceOperation::Control(TraceCommand::View { trace, enabled }),
                "calculation" => {
                    TraceOperation::Control(TraceCommand::Calculation { trace, calculation })
                }
                _ => return Err(ControlError::Invalid("unknown trace action")),
            })
        }
        OperationKind::Marker => {
            keys(
                p,
                &["marker", "action", "frequency_hz", "reference", "enabled"],
            )?;
            let marker = uint(p, "marker", 1, 1, 8)? as u8;
            let reference = uint(p, "reference", 1, 1, 4)? as u8;
            let frequency_hz = uint(p, "frequency_hz", 100_000_000, 100_000, 6_000_000_000)?;
            let enabled = flag(p, "enabled", true)?;
            let action = text(p, "action", "read", 32)?;
            Operation::Marker(match action.as_str() {
                "read" => MarkerCommand::Read { marker },
                "on" => MarkerCommand::On { marker },
                "off" => MarkerCommand::Off { marker },
                "peak" => MarkerCommand::Peak { marker },
                "frequency" => MarkerCommand::Frequency {
                    marker,
                    frequency_hz,
                },
                "delta" => MarkerCommand::Delta { marker, reference },
                "noise" => MarkerCommand::Noise { marker, enabled },
                "tracking" => MarkerCommand::Tracking { marker, enabled },
                "trace" => MarkerCommand::Trace { marker, reference },
                "trace_aver" => MarkerCommand::TraceAverage { marker, enabled },
                _ => return Err(ControlError::Invalid("unknown marker action")),
            })
        }
        OperationKind::Measurement => {
            keys(p, &["method", "start_hz", "stop_hz", "points"])?;
            let method = match text(p, "method", "peak", 32)?.as_str() {
                "peak" => MeasurementMethod::Peak,
                "occupied_bandwidth" => MeasurementMethod::OccupiedBandwidth,
                "bandwidth_3db" => MeasurementMethod::Bandwidth3Db,
                "centroid" => MeasurementMethod::Centroid,
                "channel_power" => MeasurementMethod::ChannelPower,
                _ => return Err(ControlError::Invalid("unknown core measurement method")),
            };
            Operation::Measurement {
                method,
                range: raw_range(p, false, true)?,
            }
        }
        OperationKind::PresetExport => {
            keys(p, &[])?;
            Operation::PresetExport
        }
        OperationKind::PresetImport => {
            keys(p, &["document", "confirm"])?;
            let confirm = confirm(p, "confirm")?;
            let document = text(p, "document", "", 32768)?;
            Operation::PresetImport {
                settings: parse_preset(&document)?,
                confirm,
            }
        }
        OperationKind::SelfTest => {
            keys(p, &["test", "confirm"])?;
            Operation::SelfTest {
                test: uint(p, "test", 1, 1, 3)? as u8,
                confirm: confirm(p, "confirm")?,
            }
        }
        OperationKind::GeneratorSweep => {
            keys(
                p,
                &[
                    "start_hz",
                    "stop_hz",
                    "points",
                    "seconds",
                    "level_dbm",
                    "delta_db",
                    "confirm",
                ],
            )?;
            let request = GeneratorSweepConfig {
                start_hz: uint(p, "start_hz", 30_000_000, 100_000, 800_000_000)?,
                stop_hz: uint(p, "stop_hz", 31_000_000, 100_000, 800_000_000)?,
                points: uint(p, "points", 100, 2, 450)? as u32,
                seconds: num(p, "seconds", 1., 0.003, 60.)?,
                level_dbm: num(p, "level_dbm", -30., -115., -19.)?,
                delta_db: num(p, "delta_db", 0., -90., 90.)?,
                confirmed: confirm(p, "confirm")?,
            };
            if request.start_hz >= request.stop_hz
                || !(-115. ..=-19.).contains(&(request.level_dbm + request.delta_db))
            {
                return Err(ControlError::Invalid(
                    "invalid confirmed generator sweep range/level",
                ));
            }
            Operation::GeneratorSweep(request)
        }
        OperationKind::OutputOff => {
            keys(p, &[])?;
            Operation::OutputOff
        }
        OperationKind::AudioInputs => {
            keys(p, &[])?;
            Operation::AudioInputs
        }
        OperationKind::AudioCapture => {
            keys(
                p,
                &["device", "seconds", "sample_rate_hz", "confirm_connection"],
            )?;
            let sample_rate_hz = uint(p, "sample_rate_hz", 48000, 8000, 48000)? as u32;
            if ![8000, 12000, 24000, 48000].contains(&sample_rate_hz) {
                return Err(ControlError::Invalid("unsupported audio rate"));
            }
            Operation::AudioCapture(AudioParameters {
                device: uint(p, "device", 0, 0, 1024)? as u32,
                seconds: num(p, "seconds", 5., 0.1, 30.)?,
                sample_rate_hz,
                confirm_connection: confirm(p, "confirm_connection")?,
            })
        }
    })
}
const GUIDE_NAMES: [&str; 12] = [
    "multiband",
    "vbw",
    "measurements",
    "listen",
    "calibration",
    "selftest",
    "firmware",
    "expert",
    "generator_extended",
    "generator_sweep",
    "corrections",
    "sd_csv",
];
fn valid_keypad(s: &str) -> bool {
    let s = s.strip_prefix('-').unwrap_or(s);
    let s = if s.as_bytes().last().is_some_and(|b| b"kMGmu".contains(b)) {
        &s[..s.len() - 1]
    } else {
        s
    };
    let mut parts = s.split('.');
    let whole = parts.next().unwrap_or("");
    if whole.is_empty() || whole.len() > 12 || !whole.bytes().all(|b| b.is_ascii_digit()) {
        return false;
    }
    if let Some(fraction) = parts.next()
        && (fraction.is_empty()
            || fraction.len() > 6
            || !fraction.bytes().all(|b| b.is_ascii_digit()))
    {
        return false;
    }
    parts.next().is_none()
}

struct OrderedSettings(Vec<String>);
impl<'de> Deserialize<'de> for OrderedSettings {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct Pairs;
        impl<'de> Visitor<'de> for Pairs {
            type Value = OrderedSettings;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("ordered settings object")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                let mut pairs = Vec::new();
                // Only recover order here. All semantic values come from the
                // preserving SDK parser below, never serde's ValueVisitor.
                while let Some((key, _)) = map.next_entry::<String, de::IgnoredAny>()? {
                    if pairs.contains(&key) {
                        return Err(de::Error::custom("duplicate preset setting"));
                    }
                    pairs.push(key);
                }
                Ok(OrderedSettings(pairs))
            }
        }
        d.deserialize_map(Pairs)
    }
}
fn parse_preset(raw: &str) -> Result<Vec<Setting>> {
    let preserved = parse_json_value(raw.as_bytes(), MAX_REQUEST_BYTES)
        .map_err(|_| ControlError::Invalid("invalid strict preset JSON"))?;
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Preset {
        schema_version: u64,
        profile: String,
        settings: OrderedSettings,
    }
    let data: Preset = serde_json::from_str(raw)
        .map_err(|_| ControlError::Invalid("unsupported preset document"))?;
    if data.schema_version != 1 || data.profile != CONTROL_PROFILE || data.settings.0.len() > 29 {
        return Err(ControlError::Invalid("unsupported preset profile/version"));
    }
    data.settings
        .0
        .into_iter()
        .map(|key| {
            let setting = parse_setting(&key, &preserved["settings"][&key])?;
            if setting_command(&setting)
                .map_err(|_| ControlError::Invalid("invalid preset setting"))?
                .requires_lab
                || matches!(setting, Setting::CenterHz(_) | Setting::SpanHz(_))
            {
                return Err(ControlError::Invalid(
                    "preset contains forbidden receive setting",
                ));
            }
            Ok(setting)
        })
        .collect()
}
