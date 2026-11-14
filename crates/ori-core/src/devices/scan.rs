//! One physical analyzer, bounded sequential scan bands. Coverage is derived
//! only from committed originals and is never part of the detector fingerprint.
use super::*;
use ori_guard::{ScanBand, ScanPlan};
use std::time::Instant;

#[derive(Clone)]
pub(super) struct Plan {
    id: String,
    revision: u64,
    pub config: ScanPlan,
}
impl Plan {
    pub fn read(stream: &DeviceStream) -> DeviceResult<Option<Self>> {
        let settings = stream.settings.as_ref();
        let Some(raw) = settings.and_then(|s| s.get("scan_plan")) else {
            return Ok(None);
        };
        if stream.role != DeviceRole::SpectrumAnalyzer {
            return Err(DeviceError::Invalid(
                "Scan plans require an analyzer stream".into(),
            ));
        }
        let maximum = match settings
            .and_then(|s| s.get("adapter"))
            .and_then(Value::as_str)
        {
            Some("tinysa-sim") => 20_000_000_000,
            Some("tinysa-serial") => 800_000_000,
            _ => {
                return Err(DeviceError::Invalid(
                    "Unsupported scan plan receive profile".into(),
                ));
            }
        };
        let config: ScanPlan = serde_json::from_value(raw["config"].clone())
            .map_err(|_| DeviceError::Invalid("Invalid scan plan configuration".into()))?;
        config
            .validate(maximum)
            .map_err(|e| DeviceError::Invalid(e.to_string()))?;
        let id = raw["id"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 200)
            .ok_or_else(|| DeviceError::Invalid("Scan plan ID required".into()))?
            .to_owned();
        let revision = raw["revision"]
            .as_u64()
            .filter(|n| *n > 0 && *n <= 9_007_199_254_740_991)
            .ok_or_else(|| DeviceError::Invalid("Scan plan revision required".into()))?;
        if raw.get("schema_version").is_some_and(|v| v != 1) {
            return Err(DeviceError::Invalid("Unsupported scan plan schema".into()));
        }
        Ok(Some(Self {
            id,
            revision,
            config,
        }))
    }
    pub fn stamp(&self, band: &ScanBand, source: &mut Value) -> DeviceResult<()> {
        let settings = source["settings"]
            .as_object_mut()
            .ok_or_else(|| DeviceError::Worker("Missing raw scan settings".into()))?;
        settings.insert("scan_band_id".into(), json!(band.id));
        settings.insert("scan_plan_id".into(), json!(self.id));
        settings.insert("scan_plan_revision".into(), json!(self.revision));
        Ok(())
    }
}
#[derive(Default)]
struct Observed {
    at: Option<Instant>,
    ended_at: Option<String>,
    source_id: Option<String>,
    revisit: Option<f64>,
    maximum_revisit: Option<f64>,
}
#[derive(Default)]
pub(super) struct Coverage {
    bands: Vec<Observed>,
}
impl Coverage {
    pub fn committed(&mut self, index: usize, observed: Instant, source: &Value) {
        self.bands
            .resize_with(self.bands.len().max(index + 1), Observed::default);
        let entry = &mut self.bands[index];
        if let Some(prior) = entry.at {
            let seconds = observed.saturating_duration_since(prior).as_secs_f64();
            entry.revisit = Some(seconds);
            entry.maximum_revisit = Some(entry.maximum_revisit.unwrap_or(0.).max(seconds));
        }
        entry.at = Some(observed);
        entry.ended_at = source["interval"]["ended_at"].as_str().map(str::to_owned);
        entry.source_id = source["id"].as_str().map(str::to_owned);
    }
    pub fn report(&self, plan: &Plan, paused: bool) -> Value {
        let now = Instant::now();
        let bands=plan.config.bands.iter().enumerate().map(|(index,band)|{
            let entry=self.bands.get(index);
            let age=entry.and_then(|e|e.at).map(|at|now.saturating_duration_since(at).as_secs_f64());
            json!({"id":band.id,"name":band.name,"start_hz":band.start_hz,"stop_hz":band.stop_hz,
                "max_revisit_seconds":band.max_revisit_seconds,"age_seconds":age,
                "state":if paused{"paused"}else if age.is_none(){"not_observed"}else if age.is_some_and(|a|a>band.max_revisit_seconds){"stale"}else{"fresh"},
                "last_measurement_id":entry.and_then(|e|e.source_id.as_deref()),
                "last_measurement_at":entry.and_then(|e|e.ended_at.as_deref()),
                "deadline_missed":age.is_some_and(|seconds|seconds>band.max_revisit_seconds)
                    || entry.and_then(|e|e.revisit).is_some_and(|seconds|seconds>band.max_revisit_seconds),
                "last_revisit_seconds":entry.and_then(|e|e.revisit),"maximum_revisit_seconds":entry.and_then(|e|e.maximum_revisit)})
        }).collect::<Vec<_>>();
        json!({"plan_id":plan.id,"revision":plan.revision,"sequential":true,"bands":bands})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn a_committed_frame_does_not_hide_a_late_revisit() {
        let plan = Plan {
            id: "plan".into(),
            revision: 1,
            config: ScanPlan {
                name: "Survey".into(),
                bands: vec![ScanBand {
                    id: "fm".into(),
                    name: "FM".into(),
                    start_hz: 88_000_000,
                    stop_hz: 108_000_000,
                    points: 201,
                    rbw_hz: 10_000,
                    max_revisit_seconds: 5.0,
                }],
            },
        };
        let mut coverage = Coverage::default();
        let now = Instant::now();
        coverage.committed(0, now - Duration::from_secs(8), &json!({"id":"first"}));
        assert_eq!(coverage.report(&plan, false)["bands"][0]["state"], "stale");
        coverage.committed(0, now, &json!({"id":"second"}));
        let report = coverage.report(&plan, false);
        assert_eq!(report["bands"][0]["state"], "fresh");
        assert_eq!(report["bands"][0]["last_revisit_seconds"], 8.0);
        assert_eq!(report["bands"][0]["deadline_missed"], true);
        assert_eq!(coverage.report(&plan, true)["bands"][0]["state"], "paused");
    }
}
