//! Bounded, receive-only scan plans. Hardware range admission remains a
//! separate instrument-profile decision; this module never opens a device.
use crate::{MonitoringError, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

pub const GRID_STEP_HZ: u64 = 10_000;
pub const GRID_MAX_HZ: u64 = 1_300_000_000;
pub const WINDOW_POINTS: u32 = 290;
pub const MAX_SCAN_BANDS: usize = 16;
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScanBand {
    pub id: String,
    pub name: String,
    pub start_hz: u64,
    pub stop_hz: u64,
    pub points: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub step_hz: Option<u64>,
    pub rbw_hz: u32,
    pub max_revisit_seconds: f64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ScanWindow {
    pub step_hz: Option<u64>,
    pub start_hz: u64,
    pub stop_hz: u64,
    pub points: u32,
    pub rbw_hz: u32,
}

impl ScanBand {
    pub fn window_count(&self) -> u32 {
        self.points.div_ceil(WINDOW_POINTS)
    }

    /// Called on a validated band. The text scan needs at least two samples.
    /// Move the penultimate endpoint to the final window if it would be alone.
    pub fn window(&self, index: u32) -> Option<ScanWindow> {
        let count = self.window_count();
        if self.points < 2 || index >= count {
            return None;
        }
        let (start_hz, stop_hz, points) = match self.step_hz {
            None if count == 1 => (self.start_hz, self.stop_hz, self.points),
            Some(step @ GRID_STEP_HZ) => {
                let singleton_tail = self.points % WINDOW_POINTS == 1;
                let first = index * WINDOW_POINTS - u32::from(singleton_tail && index + 1 == count);
                let points = (self.points - first).min(WINDOW_POINTS)
                    - u32::from(singleton_tail && index + 2 == count);
                let start = self.start_hz.checked_add(u64::from(first) * step)?;
                (
                    start,
                    start.checked_add(u64::from(points - 1) * step)?,
                    points,
                )
            }
            _ => return None,
        };
        Some(ScanWindow {
            step_hz: self.step_hz,
            start_hz,
            stop_hz,
            points,
            rbw_hz: self.rbw_hz,
        })
    }

    pub fn context_key(&self, index: u32) -> String {
        if self.step_hz.is_some() {
            format!("{}:{index}", self.id)
        } else {
            self.id.clone()
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScanPlan {
    pub name: String,
    pub bands: Vec<ScanBand>,
}
impl ScanPlan {
    /// Validate both legacy and gridded bands within an externally imposed limit.
    pub fn validate(&self, maximum_hz: u64) -> Result<()> {
        self.validate_ranges(maximum_hz, maximum_hz)
    }

    /// Offline proposals may request the exact ZS405 grid profile. The driver
    /// verifies the reported hardware revision before activating wide RX.
    pub fn validate_receive_plan(&self, legacy_maximum_hz: u64) -> Result<()> {
        self.validate_ranges(legacy_maximum_hz, GRID_MAX_HZ)
    }

    fn validate_ranges(&self, maximum_hz: u64, grid_maximum_hz: u64) -> Result<()> {
        let invalid = MonitoringError::InvalidParameters;
        if self.name.trim().is_empty() || self.name.chars().count() > 200 {
            return Err(invalid("scan plan name requires 1–200 characters"));
        }
        if self.bands.is_empty() || self.bands.len() > MAX_SCAN_BANDS {
            return Err(invalid("scan plan requires 1–16 bands"));
        }
        let mut ids = BTreeSet::new();
        for band in &self.bands {
            if band.id.is_empty()
                || band.id.len() > 64
                || !band
                    .id
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
                || !ids.insert(&band.id)
                || band.name.trim().is_empty()
                || band.name.chars().count() > 200
            {
                return Err(invalid("unique scan band IDs and nonempty names required"));
            }
            let profile_ok = match band.step_hz {
                None => (2..=WINDOW_POINTS).contains(&band.points) && band.stop_hz <= maximum_hz,
                Some(GRID_STEP_HZ) => {
                    band.points >= 2
                        && band.stop_hz <= grid_maximum_hz.min(GRID_MAX_HZ)
                        && band.stop_hz.checked_sub(band.start_hz)
                            == Some(u64::from(band.points.saturating_sub(1)) * GRID_STEP_HZ)
                }
                _ => false,
            };
            if !profile_ok
                || band.start_hz < 100_000
                || band.start_hz >= band.stop_hz
                || !matches!(band.rbw_hz, 10_000 | 30_000)
            {
                return Err(invalid("scan band violates receive profile"));
            }
            if !band.max_revisit_seconds.is_finite()
                || !(1.0..=3600.0).contains(&band.max_revisit_seconds)
            {
                return Err(invalid("scan revisit deadline requires 1–3600 seconds"));
            }
        }
        Ok(())
    }
}
