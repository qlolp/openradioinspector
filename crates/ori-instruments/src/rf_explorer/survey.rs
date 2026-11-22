use super::protocol::{Configuration, MAX_HZ, MIN_HZ};
use crate::{InstrumentError, Result};
use serde_json::{Value, json};

const STEP_HZ: u64 = 10_000;
const POINTS: u64 = 112;
const WINDOW_SPAN_HZ: u64 = (POINTS - 1) * STEP_HZ;

/// A survey covers an inclusive 10 kHz grid anchored at its first frequency.
/// Both ends must admit a complete 112-point source window inside the range.
pub fn validate_rf_explorer_survey_bounds(bounds: Option<(u64, u64)>) -> Result<()> {
    if let Some((start, stop)) = bounds
        && (!start.is_multiple_of(1000)
            || start < MIN_HZ
            || stop > MAX_HZ
            || !stop
                .checked_sub(start)
                .is_some_and(|span| span >= WINDOW_SPAN_HZ && span.is_multiple_of(STEP_HZ)))
    {
        return Err(InstrumentError::InvalidRequest(
            "RF Explorer survey requires a kHz-aligned start, an inclusive 10 kHz grid of at least 112 points, and endpoints within 15–2700 MHz".into(),
        ));
    }
    Ok(())
}

pub(super) struct Survey {
    start_hz: u64,
    stop_hz: u64,
    pass: u64,
    tile: u64,
    tiles: u64,
}

impl Survey {
    pub(super) fn new(requested: Option<(u64, u64)>, config: &Configuration) -> Result<Self> {
        let (board_min, board_max) = config.board_bounds();
        let (start_hz, stop_hz) = requested.unwrap_or((board_min, board_max));
        validate_rf_explorer_survey_bounds(Some((start_hz, stop_hz)))?;
        if start_hz < board_min || stop_hz > board_max {
            return Err(InstrumentError::InvalidRequest(
                "requested survey exceeds the active RF Explorer board limits".into(),
            ));
        }
        if config.points as u64 != POINTS {
            return Err(InstrumentError::UnsupportedProfile(
                "RF Explorer survey requires a 112-point device readback".into(),
            ));
        }
        let bins = (stop_hz - start_hz) / STEP_HZ + 1;
        Ok(Self {
            start_hz,
            stop_hz,
            pass: 1,
            tile: 1,
            tiles: bins.div_ceil(POINTS),
        })
    }

    pub(super) fn window(&self) -> (u64, u64) {
        // Keep the final sweep full length, including any overlap with its predecessor.
        let start =
            (self.start_hz + (self.tile - 1) * POINTS * STEP_HZ).min(self.stop_hz - WINDOW_SPAN_HZ);
        (start, start + WINDOW_SPAN_HZ)
    }

    pub(super) fn settings(&self) -> Value {
        json!({"start_hz": self.start_hz, "stop_hz": self.stop_hz, "step_hz": STEP_HZ,
               "pass": self.pass, "tile": self.tile, "tiles": self.tiles})
    }

    pub(super) fn advance(&mut self) {
        if self.tile == self.tiles {
            self.tile = 1;
            self.pass += 1;
        } else {
            self.tile += 1;
        }
    }
}
