use crate::*;
use serde_json::{Value, json};

/// Semantic allow-list and unit conversion from the existing Python profile.
/// A setting name or menu label is never treated as a shell command.
pub fn setting_command(setting: &Setting) -> Result<WireCommand> {
    let (_, command, _, token, requires_lab) = parts(setting)?;
    Ok(WireCommand {
        wire: format!("{command} {token}"),
        requires_lab,
    })
}
pub(crate) fn setting_value(setting: &Setting) -> Result<(&'static str, Value)> {
    let (key, _, value, _, _) = parts(setting)?;
    Ok((key, value))
}
fn parts(setting: &Setting) -> Result<(&'static str, &'static str, Value, String, bool)> {
    let boolean = |name, command, value: bool, lab| {
        Ok((
            name,
            command,
            json!(value),
            if value { "on" } else { "off" }.into(),
            lab,
        ))
    };
    let numeric = |name, command, value: f64, minimum: f64, maximum: f64, lab| -> Result<_> {
        if !value.is_finite() || value < minimum || value > maximum {
            return Err(InstrumentError::InvalidRequest(format!(
                "{name} outside verified setting range"
            )));
        }
        Ok((name, command, json!(value), format_number(value), lab))
    };
    let integer = |name, command, value: u64, minimum: u64, maximum: u64, lab| -> Result<_> {
        if !(minimum..=maximum).contains(&value) {
            return Err(InstrumentError::InvalidRequest(format!(
                "{name} outside verified setting range"
            )));
        }
        Ok((name, command, json!(value), value.to_string(), lab))
    };
    let named = |name, command, value: Value, lab| -> Result<_> {
        let token = value.as_str().unwrap().to_owned();
        Ok((name, command, value, token, lab))
    };
    match setting {
        Setting::StartHz(value) => integer(
            "start_hz",
            "sweep start",
            *value,
            100_000,
            6_000_000_000,
            false,
        ),
        Setting::StopHz(value) => integer(
            "stop_hz",
            "sweep stop",
            *value,
            100_000,
            6_000_000_000,
            false,
        ),
        Setting::CenterHz(value) => integer(
            "center_hz",
            "sweep center",
            *value,
            100_000,
            6_000_000_000,
            false,
        ),
        Setting::SpanHz(value) => integer("span_hz", "sweep span", *value, 0, 5_999_900_000, false),
        Setting::Points(value) => integer("points", "sweep", *value as u64, 2, 450, false),
        Setting::RbwHz(Rbw::Auto) => named("rbw_hz", "rbw", json!("auto"), false),
        Setting::RbwHz(Rbw::Hz(value)) => {
            if ![
                200, 1000, 3000, 10000, 30000, 100000, 300000, 600000, 850000,
            ]
            .contains(value)
            {
                return Err(InstrumentError::InvalidRequest(
                    "unsupported RBW request".into(),
                ));
            }
            Ok((
                "rbw_hz",
                "rbw",
                json!(value),
                format_number(*value as f64 / 1000.0),
                false,
            ))
        }
        Setting::AttenuationDb(Attenuation::Auto) => {
            named("attenuation_db", "attenuate", json!("auto"), false)
        }
        Setting::AttenuationDb(Attenuation::Db(value)) => {
            integer("attenuation_db", "attenuate", *value as u64, 0, 31, false)
        }
        Setting::Lna(value) => boolean("lna", "lna", *value, false),
        Setting::Ultra(value) => boolean("ultra", "ultra", *value, false),
        Setting::Spur(value) => named("spur", "spur", json!(value), false),
        Setting::Repeat(value) => integer("repeat", "repeat", *value as u64, 1, 1000, false),
        Setting::SweepAccuracy(value) => named("sweep_accuracy", "sweep", json!(value), false),
        Setting::SweepSeconds(value) => {
            numeric("sweep_seconds", "sweeptime", *value, 0.0, 60.0, false)
        }
        Setting::ExternalGainDb(value) => {
            numeric("external_gain_db", "ext_gain", *value, -100.0, 100.0, false)
        }
        Setting::Calculation(value) => named("calculation", "calc", json!(value), false),
        Setting::TriggerMode(value) => named("trigger_mode", "trigger", json!(value), false),
        Setting::TriggerDbm(value) => {
            numeric("trigger_dbm", "trigger", *value, -174.0, 30.0, false)
        }
        Setting::TraceUnit(value) => named("trace_unit", "trace", json!(value), false),
        Setting::RefLevel(AutoNumber::Auto) => {
            named("ref_level", "trace reflevel", json!("auto"), false)
        }
        Setting::RefLevel(AutoNumber::Value(value)) => {
            numeric("ref_level", "trace reflevel", *value, -174.0, 150.0, false)
        }
        Setting::ScaleDiv(AutoNumber::Auto) => {
            named("scale_div", "trace scale", json!("auto"), false)
        }
        Setting::ScaleDiv(AutoNumber::Value(value)) => {
            numeric("scale_div", "trace scale", *value, 0.01, 100.0, false)
        }
        Setting::OutputHz(value) => {
            integer("output_hz", "freq", *value, 100_000, 800_000_000, true)
        }
        Setting::OutputDbm(value) => numeric("output_dbm", "level", *value, -115.0, -19.0, true),
        Setting::OutputEnabled(value) => boolean("output_enabled", "output", *value, true),
        Setting::Modulation(value) => named("modulation", "modulation", json!(value), true),
        Setting::ModulationHz(value) => integer(
            "modulation_hz",
            "modulation freq",
            *value as u64,
            50,
            5000,
            true,
        ),
        Setting::AmDepthPercent(value) => integer(
            "am_depth_percent",
            "modulation depth",
            *value as u64,
            10,
            100,
            true,
        ),
        Setting::FmDeviationHz(value) => integer(
            "fm_deviation_hz",
            "modulation deviation",
            *value as u64,
            1000,
            300000,
            true,
        ),
        Setting::OutputDeltaDb(value) => {
            numeric("output_delta_db", "levelchange", *value, -90.0, 90.0, true)
        }
        Setting::CalOutputMhz(None) => named("caloutput_mhz", "caloutput", json!("off"), true),
        Setting::CalOutputMhz(Some(value)) => {
            if ![1, 2, 3, 4, 10, 15, 30].contains(value) {
                return Err(InstrumentError::InvalidRequest(
                    "unsupported CAL frequency".into(),
                ));
            }
            integer("caloutput_mhz", "caloutput", *value as u64, 1, 30, true)
        }
    }
}
/// Python's .12g policy for existing finite bounded shell values.
pub(crate) fn format_number(value: f64) -> String {
    format_general(value, 12)
}
pub(crate) fn format_generator(value: f64) -> String {
    format_general(value, 6)
}
fn format_general(value: f64, precision: i32) -> String {
    let scientific = format!("{value:.digits$e}", digits = (precision - 1) as usize);
    let (mantissa, exponent) = scientific.split_once('e').unwrap();
    let exponent: i32 = exponent.parse().unwrap();
    if !(-4..precision).contains(&exponent) {
        return format!(
            "{}e{exponent:+03}",
            mantissa.trim_end_matches('0').trim_end_matches('.')
        );
    }
    let decimals = (precision - 1 - exponent).max(0) as usize;
    let text = format!("{value:.decimals$}");
    if text.contains('.') {
        text.trim_end_matches('0').trim_end_matches('.').to_owned()
    } else {
        text
    }
}
