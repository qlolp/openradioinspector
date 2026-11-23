//! Detection for RX-verified AR-DV1 S-meter counts. Smaller originals indicate
//! greater strength. Only the internal comparison coordinate changes sign.
use crate::{MonitoringError, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Bind the guard reference and the worker restart cursor to the same port and
/// requested settings. Stream and session IDs change when Guard clones a run.
pub fn ardv1_plan_key(stream: &ori_contracts::DeviceStream) -> Result<String> {
    let identity = ori_contracts::to_json_vec(&(&stream.port, &stream.settings))
        .map_err(|e| MonitoringError::Json(e.to_string()))?;
    let digest = Sha256::digest(identity)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("ardv1-cyclic-v1-{digest}"))
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CountReference {
    pub oriented_p50: f64,
    pub oriented_p95: f64,
    pub mad_counts: f64,
}

impl CountReference {
    pub fn from_counts(counts: &[u32]) -> Result<Self> {
        if !(20..=21).contains(&counts.len()) {
            return Err(MonitoringError::InvalidBaseline(
                "20 independent receiver passes required",
            ));
        }
        let mut values: Vec<_> = counts.iter().map(|count| -f64::from(*count)).collect();
        values.sort_by(f64::total_cmp);
        let center = median(&values);
        let p95 = values[(values.len() * 95).div_ceil(100) - 1];
        for value in &mut values {
            *value = (*value - center).abs();
        }
        values.sort_by(f64::total_cmp);
        Ok(Self {
            oriented_p50: center,
            oriented_p95: p95,
            mad_counts: median(&values),
        })
    }

    pub fn sigma(&self) -> f64 {
        (1.4826 * self.mad_counts).max(1.)
    }

    pub fn compare(&self, count: u32, offset: f64) -> CountComparison {
        let oriented = -f64::from(count);
        let excess = oriented - (self.oriented_p95 + offset);
        let sigma = self.sigma();
        CountComparison {
            excess,
            above: excess >= 3. * sigma,
            impulse: excess >= 6. * sigma,
            quiet_offset: (offset
                + 0.02 * (oriented - self.oriented_p50 - offset).clamp(-sigma, sigma))
            .clamp(-3. * sigma, 3. * sigma),
        }
    }
}

pub struct CountComparison {
    pub excess: f64,
    pub above: bool,
    pub impulse: bool,
    pub quiet_offset: f64,
}

fn median(ordered: &[f64]) -> f64 {
    (ordered[(ordered.len() - 1) / 2] + ordered[ordered.len() / 2]) / 2.
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn polarity_and_thresholds_follow_native_count_dispersion() {
        let mut levels = vec![100; 10];
        levels.extend([104; 10]);
        let reference = CountReference::from_counts(&levels).unwrap();
        assert_eq!(reference.oriented_p50, -102.);
        assert_eq!(reference.oriented_p95, -100.);
        assert_eq!(reference.mad_counts, 2.);
        assert!(!reference.compare(105, 0.).above);
        assert!(!reference.compare(92, 0.).above);
        let sustained = reference.compare(91, 0.);
        assert!(sustained.above && !sustained.impulse);
        assert!(reference.compare(82, 0.).impulse);
        let flat = CountReference::from_counts(&[999999; 20]).unwrap();
        assert_eq!(flat.sigma(), 1.);
        assert!(flat.compare(999996, 0.).above);
    }
}
