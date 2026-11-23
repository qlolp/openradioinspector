use crate::Sweep;
use serde_json::{Value, json};

/// Only Standard 01.34 hardware readback from a Normal raw frame can
/// participate in an automatic baseline. Preserve every frequency and dBm.
pub fn normal_sweep(sweep: &Sweep) -> bool {
    let Some(settings) = sweep.settings.as_ref() else {
        return false;
    };
    let s = json!(settings);
    let (low, high, input) = match s["active_board"].as_str() {
        Some("main") => (240_000_000, 960_000_000, "left"),
        Some("expansion") => (15_000_000, 2_700_000_000, "right"),
        _ => return false,
    };
    let (
        Some(start),
        Some(stop),
        Some(step),
        Some(board_low),
        Some(board_high),
        Some(span),
        Some(offset),
    ) = (
        s["start_hz"].as_u64(),
        s["stop_hz"].as_u64(),
        s["step_hz"].as_u64(),
        s["board_min_hz"].as_u64(),
        s["board_max_hz"].as_u64(),
        s["board_max_span_hz"].as_u64(),
        s["offset_db"].as_i64(),
    )
    else {
        return false;
    };
    normal_readback(&s)
        && s["device_identity"]["firmware"] == "01.34"
        && s["device_identity"]["main_model"] == 3
        && s["device_identity"]["expansion_model"] == 5
        && s["receive_profile"] == "rf-explorer-3g-combo-standard-1.34"
        && s["timing_source"] == "host_frame_read"
        && s["level_encoding"] == "dBm = offset_db - u8 / 2"
        && s["simulated"] == false
        && s["active_input"] == input
        && s["rbw_hz"]
            .as_u64()
            .is_some_and(|v| (3000..=600000).contains(&v))
        && s["amplitude_top_dbm"]
            .as_i64()
            .zip(s["amplitude_bottom_dbm"].as_i64())
            .is_some_and(|(a, b)| a > b)
        && low <= board_low
        && board_low <= start
        && stop <= board_high
        && board_high <= high
        && stop > start
        && stop - start <= span
        && step > 0
        && step.checked_mul(111).and_then(|n| start.checked_add(n)) == Some(stop)
        && sweep.level_unit == "dBm"
        && s["points"] == 112
        && sweep.frequencies_hz.len() == 112
        && sweep.levels.len() == 112
        && sweep
            .frequencies_hz
            .iter()
            .enumerate()
            .all(|(i, hz)| *hz == start + i as u64 * step)
        && sweep.levels.iter().all(|level| {
            let code = (offset as f64 - level) * 2.;
            (0.0..=255.).contains(&code) && code.fract() == 0.
        })
}

fn detector_readback(s: &Value, detector: &str, code: u8, kind: &str) -> bool {
    s["adapter"] == "rf-explorer-3g-serial"
        && s["device_mode"] == "spectrum_analyzer"
        && s["device_identity"]["main_model"] == 3
        && s["device_identity"]["expansion_model"] == 5
        && s["device_identity"]["firmware"] == "01.34"
        && s["receive_profile"] == "rf-explorer-3g-combo-standard-1.34"
        && s["simulated"] == false
        && s["detector"] == detector
        && s["detector_code"] == code
        && s["artifact_kind"] == kind
        && s["settings_source"] == "device_readback"
        && s["settings_readback"] == true
}

pub fn normal_readback(s: &Value) -> bool {
    detector_readback(s, "normal", 0, "raw_spectrum")
}

pub fn max_hold_readback(s: &Value) -> bool {
    detector_readback(s, "max_hold", 4, "processed_trace")
}
