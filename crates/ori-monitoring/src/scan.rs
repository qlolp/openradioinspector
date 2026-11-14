//! Bounded, receive-only scan plans. Hardware range admission remains a
//! separate instrument-profile decision; this module never opens a device.
use crate::{MonitoringError, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

pub const MAX_SCAN_BANDS: usize = 16;
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScanBand {
    pub id: String,
    pub name: String,
    pub start_hz: u64,
    pub stop_hz: u64,
    pub points: u32,
    pub rbw_hz: u32,
    pub max_revisit_seconds: f64,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScanPlan {
    pub name: String,
    pub bands: Vec<ScanBand>,
}
impl ScanPlan {
    pub fn validate(&self, maximum_hz: u64) -> Result<()> {
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
            if band.start_hz < 100_000
                || band.stop_hz > maximum_hz
                || band.start_hz >= band.stop_hz
                || !(2..=290).contains(&band.points)
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
