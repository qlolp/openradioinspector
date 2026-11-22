use ori_instruments::{InstrumentError, ReceiverMode, ReceiverScope, Result};
use serde::Deserialize;
use std::time::Duration;

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Plan {
    RfExplorerSweep {
        start_hz: Option<u64>,
        stop_hz: Option<u64>,
    },
    RfExplorerSurvey {
        step_hz: Option<u64>,
        start_hz: Option<u64>,
        stop_hz: Option<u64>,
    },
    TinysaRx {
        start_hz: u64,
        stop_hz: u64,
        points: u32,
        rbw_hz: u32,
    },
    Ardv1Single {
        #[serde(default = "tune_default")]
        settle_ms: u64,
        #[serde(default)]
        continuous: bool,
        frequency_hz: u64,
        #[serde(default = "fm")]
        mode: ReceiverMode,
    },
    Ardv1List {
        #[serde(default = "tune_default")]
        settle_ms: u64,
        #[serde(default)]
        continuous: bool,
        frequencies_hz: Vec<u64>,
        #[serde(default = "fm")]
        mode: ReceiverMode,
    },
    Ardv1Range {
        #[serde(default)]
        continuous: bool,
        start_hz: u64,
        stop_hz: u64,
        step_hz: u64,
        #[serde(default = "settle_default")]
        settle_ms: u64,
        #[serde(default = "fm")]
        mode: ReceiverMode,
    },
}

fn tune_default() -> u64 {
    25
}
fn fm() -> ReceiverMode {
    ReceiverMode::Fm
}
fn settle_default() -> u64 {
    15
}

pub(crate) fn bounds(start: Option<u64>, stop: Option<u64>) -> Result<Option<(u64, u64)>> {
    match (start, stop) {
        (None, None) => Ok(None),
        (Some(a), Some(b)) => Ok(Some((a, b))),
        _ => Err(invalid("set both RF Explorer endpoints or omit both")),
    }
}

pub(crate) fn invalid(message: &str) -> InstrumentError {
    InstrumentError::InvalidRequest(message.into())
}

pub(crate) fn validate_list(frequencies: &[u64]) -> Result<()> {
    if frequencies.is_empty()
        || frequencies.len() > 10_000
        || frequencies
            .iter()
            .any(|f| !(100_000..=1_300_000_000).contains(f) || !f.is_multiple_of(10))
    {
        return Err(invalid(
            "AR-DV1 list requires 1–10000 frequencies on the supported 10 Hz raster",
        ));
    }
    Ok(())
}

pub(crate) struct Range {
    pub start: u64,
    stop: u64,
    step: u64,
    settle: Duration,
    pub total: u64,
}

impl Range {
    pub fn new(start: u64, stop: u64, step: u64, settle_ms: u64) -> Result<Self> {
        validate_list(&[start, stop])?;
        if start >= stop
            || !(10..=1_300_000_000).contains(&step)
            || !step.is_multiple_of(10)
            || settle_ms > 1000
        {
            return Err(invalid("invalid AR-DV1 range, step or settling time"));
        }
        Ok(Self {
            start,
            stop,
            step,
            settle: Duration::from_millis(settle_ms),
            total: (stop - start).div_ceil(step) + 1,
        })
    }

    pub fn chunk(&self, offset: u64) -> Result<ReceiverScope> {
        let remaining = self
            .total
            .checked_sub(offset)
            .filter(|n| *n >= 2)
            .ok_or_else(|| invalid("no complete receiver scope remains"))?;
        // A scope needs two endpoints. Give the last scope two samples if a
        // naive split would leave just one, e.g. 1001 samples become 999 + 2.
        let count = if remaining == 1001 {
            999
        } else {
            remaining.min(1000)
        };
        let hz = |i: u64| (self.start + i * self.step).min(self.stop);
        ReceiverScope::new(
            hz(offset),
            hz(offset + count - 1),
            self.step,
            self.settle,
            true,
        )
    }
}
