//! One physical analyzer, bounded sequential scan bands. Coverage is derived
//! only from committed originals and is never part of the detector fingerprint.
use super::*;
use ori_guard::{ScanBand, ScanPlan, ScanWindow};
use std::time::Instant;

#[derive(Clone)]
pub(super) struct Plan {
    id: String,
    revision: u64,
    pub config: ScanPlan,
    adapter: &'static str,
    serial_baud: Option<u32>,
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
        let (adapter, maximum) = match settings
            .and_then(|s| s.get("adapter"))
            .and_then(Value::as_str)
        {
            Some("tinysa-sim") => ("tinysa-sim", 20_000_000_000),
            Some("tinysa-serial") => ("tinysa-serial", 800_000_000),
            _ => {
                return Err(DeviceError::Invalid(
                    "Unsupported scan plan receive profile".into(),
                ));
            }
        };
        let config: ScanPlan = serde_json::from_value(raw["config"].clone())
            .map_err(|_| DeviceError::Invalid("Invalid scan plan configuration".into()))?;
        config
            .validate_receive_plan(maximum)
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
        let serial_baud =
            if maximum == 800_000_000 && config.bands.iter().any(|b| b.step_hz.is_some()) {
                Some(simulator::integer(
                    settings.expect("validated adapter"),
                    "baud",
                    9600,
                    9600,
                    115200,
                )? as u32)
            } else {
                None
            };
        Ok(Some(Self {
            adapter,
            serial_baud,
            id,
            revision,
            config,
        }))
    }
    pub fn uses_grid(&self) -> bool {
        self.config.bands.iter().any(|band| band.step_hz.is_some())
    }
    pub fn needs_wide_identity(&self) -> bool {
        self.config
            .bands
            .iter()
            .any(|band| band.stop_hz > 800_000_000)
    }
    pub fn stamp(&self, band: &ScanBand, index: u32, source: &mut Value) -> DeviceResult<()> {
        let settings = source["settings"]
            .as_object_mut()
            .ok_or_else(|| DeviceError::Worker("Missing raw scan settings".into()))?;
        let window = band.window(index).ok_or_else(|| {
            DeviceError::Worker("Scan window is outside the selected band".into())
        })?;
        settings.insert(
            "measurement_qualification".into(),
            ori_guard::measurement_qualification(
                Some(self.adapter),
                window.start_hz,
                window.stop_hz,
            ),
        );
        if let Some(step) = band.step_hz {
            if let Some(baud) = self.serial_baud {
                settings.insert("scan_serial_baud".into(), json!(baud));
            }
            settings.insert("scan_window_index".into(), json!(index));
            settings.insert("scan_window_count".into(), json!(band.window_count()));
            settings.insert("scan_step_hz".into(), json!(step));
        }
        settings.insert("scan_band_id".into(), json!(band.id));
        settings.insert("scan_plan_id".into(), json!(self.id));
        settings.insert("scan_plan_revision".into(), json!(self.revision));
        Ok(())
    }
}
#[derive(Default)]
struct Observed {
    at: Option<Instant>,
    revisit: Option<f64>,
    maximum_revisit: Option<f64>,
}
#[derive(Default)]
struct BandCoverage {
    windows: Vec<Observed>,
    passes: u64,
    last_window: Value,
}
#[derive(Default)]
pub(super) struct Coverage {
    bands: Vec<BandCoverage>,
}
impl Coverage {
    pub fn committed(
        &mut self,
        band: usize,
        index: u32,
        window: ScanWindow,
        count: u32,
        observed: Instant,
        source: &Value,
    ) {
        self.bands
            .resize_with(self.bands.len().max(band + 1), BandCoverage::default);
        let coverage = &mut self.bands[band];
        coverage
            .windows
            .resize_with(count as usize, Observed::default);
        let entry = &mut coverage.windows[index as usize];
        if let Some(prior) = entry.at {
            let seconds = observed.saturating_duration_since(prior).as_secs_f64();
            entry.revisit = Some(seconds);
            entry.maximum_revisit = Some(entry.maximum_revisit.unwrap_or(0.).max(seconds));
        }
        entry.at = Some(observed);
        coverage.passes = coverage
            .passes
            .saturating_add(u64::from(index + 1 == count));
        coverage.last_window = json!({"index":index,"start_hz":window.start_hz,"stop_hz":window.stop_hz,
            "points":window.points,"measurement_id":source["id"],"started_at":source["interval"]["started_at"],
            "ended_at":source["interval"]["ended_at"], "measurement_qualification":source["settings"]["measurement_qualification"]});
    }
    pub fn report(&self, plan: &Plan, paused: bool) -> Value {
        let now = Instant::now();
        let bands = plan.config.bands.iter().enumerate().map(|(index,band)|{
            let coverage=self.bands.get(index);
            let windows=coverage.map_or(&[][..],|c|c.windows.as_slice());
            let observed=windows.iter().filter_map(|w|w.at).count();
            let age=windows.iter().filter_map(|w|w.at).min().map(|at|now.saturating_duration_since(at).as_secs_f64());
            let last=coverage.map(|c| &c.last_window).unwrap_or(&Value::Null);
            let revisit=windows.iter().filter_map(|w|w.revisit).reduce(f64::max);
            let stale=age.is_some_and(|a|a>band.max_revisit_seconds);
            let count=band.window_count();
            json!({"id":band.id,"name":band.name,"start_hz":band.start_hz,"stop_hz":band.stop_hz,
                "max_revisit_seconds":band.max_revisit_seconds,"age_seconds":age,"step_hz":band.step_hz,
                "window_count":count,"observed_windows":observed,"completed_passes":coverage.map_or(0,|c| c.passes),
                "last_window":last,
                "measurement_qualification":ori_guard::measurement_qualification(Some(plan.adapter),band.start_hz,band.stop_hz),
                "state":if paused{"paused"}else if observed==0{"not_observed"}else if observed<count as usize{"partial"}else if stale{"stale"}else{"fresh"},
                "last_measurement_id":last["measurement_id"],"last_measurement_at":last["ended_at"],
                "deadline_missed":stale||revisit.is_some_and(|s|s>band.max_revisit_seconds),
                "last_revisit_seconds":revisit,"maximum_revisit_seconds":windows.iter().filter_map(|w|w.maximum_revisit).reduce(f64::max)})
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
            adapter: "tinysa-serial",
            serial_baud: None,
            revision: 1,
            config: ScanPlan {
                name: "Survey".into(),
                bands: vec![ScanBand {
                    id: "fm".into(),
                    name: "FM".into(),
                    start_hz: 88_000_000,
                    stop_hz: 108_000_000,
                    points: 201,
                    step_hz: None,
                    rbw_hz: 10_000,
                    max_revisit_seconds: 5.0,
                }],
            },
        };
        let mut coverage = Coverage::default();
        let now = Instant::now();
        let window = plan.config.bands[0].window(0).unwrap();
        coverage.committed(
            0,
            0,
            window,
            1,
            now - Duration::from_secs(8),
            &json!({"id":"first"}),
        );
        assert_eq!(coverage.report(&plan, false)["bands"][0]["state"], "stale");
        coverage.committed(0, 0, window, 1, now, &json!({"id":"second"}));
        let report = coverage.report(&plan, false);
        assert_eq!(report["bands"][0]["state"], "fresh");
        assert_eq!(report["bands"][0]["last_revisit_seconds"], 8.0);
        assert_eq!(report["bands"][0]["deadline_missed"], true);
        assert_eq!(coverage.report(&plan, true)["bands"][0]["state"], "paused");
    }
}

#[cfg(test)]
#[path = "grid_worker_tests.rs"]
mod grid_worker;
