//! RF Explorer 3G Combo Standard 01.34 survey readback. Keep the checks in
//! agreement with the offline archive exporter in tools/rf_explorer/export_survey.py.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub(super) const POINTS: usize = 112;
pub(super) const STEP: u64 = 10_000;
const DETECTORS: [&str; 6] = [
    "normal",
    "max",
    "avg",
    "overwrite",
    "max_hold",
    "max_historical",
];

#[derive(Deserialize, Serialize)]
pub(super) struct Plan {
    pub pass: u64,
    pub start_hz: u64,
    pub stop_hz: u64,
    pub active_board: String,
    pub active_input: String,
    pub rbw_hz: u64,
    pub detector: String,
    pub offset_db: i16,
    pub simulated: bool,
}

impl Plan {
    pub fn count(&self) -> usize {
        ((self.stop_hz - self.start_hz) / STEP) as usize + 1
    }
    pub fn tiles(&self) -> usize {
        self.count().div_ceil(POINTS)
    }
    /// At most two physical windows cover a frequency on the inclusive grid.
    pub fn source_tiles(&self, index: usize) -> [usize; 2] {
        let primary = index / POINTS + 1;
        let tail = if index >= self.count() - POINTS {
            self.tiles()
        } else {
            primary
        };
        [primary, tail]
    }
    pub fn first(&self, tile: usize) -> usize {
        ((tile - 1) * POINTS).min(self.count() - POINTS)
    }
}

pub(super) struct Tile {
    pub plan: Plan,
    pub number: usize,
    pub samples: [u8; POINTS],
    pub signature: Vec<u8>,
}

pub(super) fn tile(settings: &Value, hz: &Value, levels: &Value, unit: &str) -> Option<Tile> {
    let survey = &settings["survey"];
    let start = survey["start_hz"].as_u64()?;
    let stop = survey["stop_hz"].as_u64()?;
    let board = settings["active_board"].as_str()?;
    let (low, high, input) = match board {
        "main" => (240_000_000, 960_000_000, "left"),
        "expansion" => (15_000_000, 2_700_000_000, "right"),
        _ => return None,
    };
    let board_min = settings["board_min_hz"].as_u64()?;
    let board_max = settings["board_max_hz"].as_u64()?;
    let max_span = settings["board_max_span_hz"].as_u64()?;
    let span = stop.checked_sub(start)?;
    let window_span = (POINTS as u64 - 1) * STEP;
    if !(low <= board_min && board_min <= start && stop <= board_max && board_max <= high)
        || span < window_span
        || !start.is_multiple_of(1000)
        || !span.is_multiple_of(STEP)
        || !(window_span..=600_000_000_u64.min(board_max - board_min)).contains(&max_span)
        || settings["active_input"] != input
        || survey["step_hz"].as_u64()? != STEP
        || settings["step_hz"].as_u64()? != STEP
        || settings["points"].as_u64()? != POINTS as u64
    {
        return None;
    }
    let identity = &settings["device_identity"];
    let rbw = settings["rbw_hz"].as_u64()?;
    let pass = survey["pass"].as_u64()?;
    let detector = *DETECTORS.get(usize::try_from(settings["detector_code"].as_u64()?).ok()?)?;
    if pass == 0
        || unit != "dBm"
        || settings["adapter"] != "rf-explorer-3g-serial"
        || settings["device_mode"] != "spectrum_analyzer"
        || settings["receive_profile"] != "rf-explorer-3g-combo-standard-1.34"
        || identity["main_model"].as_u64()? != 3
        || identity["expansion_model"].as_u64()? != 5
        || identity["firmware"] != "01.34"
        || !settings["settings_readback"].as_bool()?
        || settings["settings_source"] != "device_readback"
        || settings["timing_source"] != "host_frame_read"
        || settings["detector"] != detector
        || settings["artifact_kind"]
            != if detector == "normal" {
                "raw_spectrum"
            } else {
                "processed_trace"
            }
        || !(3000..=600_000).contains(&rbw)
        || settings["amplitude_top_dbm"].as_i64()? <= settings["amplitude_bottom_dbm"].as_i64()?
        || settings["level_encoding"] != "dBm = offset_db - u8 / 2"
    {
        return None;
    }
    let plan = Plan {
        pass,
        start_hz: start,
        stop_hz: stop,
        active_board: board.into(),
        active_input: input.into(),
        rbw_hz: rbw,
        detector: detector.into(),
        offset_db: i16::try_from(settings["offset_db"].as_i64()?).ok()?,
        simulated: settings["simulated"].as_bool()?,
    };
    let number = usize::try_from(survey["tile"].as_u64()?).ok()?;
    if !(1..=plan.tiles()).contains(&number) || survey["tiles"].as_u64()? != plan.tiles() as u64 {
        return None;
    }
    let first = start + plan.first(number) as u64 * STEP;
    let hz = hz.as_array()?;
    let levels = levels.as_array()?;
    if settings["start_hz"].as_u64()? != first
        || settings["stop_hz"].as_u64()? != first + window_span
        || hz.len() != POINTS
        || levels.len() != POINTS
    {
        return None;
    }
    let mut samples = [0; POINTS];
    for (index, byte) in samples.iter_mut().enumerate() {
        let code = (f64::from(plan.offset_db) - levels[index].as_f64()?) * 2.0;
        if hz[index].as_u64()? != first + index as u64 * STEP
            || !(0.0..=255.0).contains(&code)
            || code.fract() != 0.0
        {
            return None;
        }
        *byte = code as u8;
    }
    // Include unknown readback extensions as well. Only the requested survey
    // identity and the physical tile endpoints legitimately vary.
    let readback: BTreeMap<_, _> = settings
        .as_object()?
        .iter()
        .filter(|(key, _)| !matches!(key.as_str(), "survey" | "start_hz" | "stop_hz"))
        .collect();
    let signature = Sha256::digest(serde_json::to_vec(&(start, stop, readback)).ok()?).to_vec();
    Some(Tile {
        plan,
        number,
        samples,
        signature,
    })
}
