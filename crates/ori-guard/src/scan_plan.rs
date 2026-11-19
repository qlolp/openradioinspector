use crate::{GuardService, Result, util::*};
use ori_monitoring::{ScanBand, ScanPlan, Sweep};
use serde_json::{Value, json};

pub(crate) fn configured_plan(project: &Value) -> Result<Option<ScanPlan>> {
    project
        .get("scan_plan")
        .filter(|p| !p.is_null())
        .map(|p| {
            let plan: ScanPlan = serde_json::from_value(p["config"].clone())
                .map_err(|_| invalid("Повреждён сохранённый план обзора"))?;
            plan.validate_receive_plan(profile_limit(project)?)?;
            Ok(plan)
        })
        .transpose()
}

pub(crate) fn all_baselines_ready(project: &Value) -> Result<bool> {
    Ok(match configured_plan(project)? {
        Some(plan) => plan.bands.iter().all(|b| {
            (0..b.window_count()).all(|w| project["band_baselines"][b.context_key(w)].is_string())
        }),
        None => project["baseline_id"].is_string(),
    })
}

fn profile_limit(project: &Value) -> Result<u64> {
    match project["profile"]["analyzer"]["settings"]["adapter"].as_str() {
        Some("tinysa-sim") => Ok(20_000_000_000),
        Some("tinysa-serial") => Ok(800_000_000),
        _ => Err(invalid("Нет поддерживаемого RX-профиля для плана обзора")),
    }
}

/// The historical one-window format did not always include a revision.
/// An explicit mismatching revision or any incomplete grid scope is rejected.
pub(crate) fn source_scope<'a>(
    project: &Value,
    plan: &'a ScanPlan,
    sweep: &Sweep,
) -> Result<(&'a ScanBand, u32)> {
    let mismatch = || invalid("Исходник не совпадает с версией плана или сеткой окна");
    let settings = sweep.settings.as_ref().ok_or_else(mismatch)?;
    let band = settings
        .get("scan_band_id")
        .and_then(Value::as_str)
        .and_then(|id| plan.bands.iter().find(|b| b.id == id))
        .ok_or_else(mismatch)?;
    let revision = &project["scan_plan"]["revision"];
    if settings.get("scan_plan_id") != project["scan_plan"].get("id")
        || settings
            .get("scan_plan_revision")
            .is_some_and(|r| r != revision)
    {
        return Err(mismatch());
    }
    let index = if let Some(step) = band.step_hz {
        if settings.get("scan_plan_revision") != Some(revision)
            || settings.get("scan_step_hz").and_then(Value::as_u64) != Some(step)
            || settings.get("scan_window_count").and_then(Value::as_u64)
                != Some(band.window_count().into())
            || settings.get("rbw_hz").and_then(Value::as_u64) != Some(band.rbw_hz.into())
        {
            return Err(mismatch());
        }
        settings
            .get("scan_window_index")
            .and_then(Value::as_u64)
            .and_then(|n| u32::try_from(n).ok())
            .ok_or_else(mismatch)?
    } else {
        if settings.contains_key("scan_window_index") {
            return Err(mismatch());
        }
        0
    };
    let window = band.window(index).ok_or_else(mismatch)?;
    if sweep.frequencies_hz.len() != window.points as usize
        || sweep.frequencies_hz.first() != Some(&window.start_hz)
        || sweep.frequencies_hz.last() != Some(&window.stop_hz)
        || band.step_hz.is_some_and(|step| {
            sweep
                .frequencies_hz
                .iter()
                .enumerate()
                .any(|(i, &hz)| hz != window.start_hz + i as u64 * step)
        })
    {
        return Err(mismatch());
    }
    Ok((band, index))
}

/// Keep source search on its own session and only on grid points inside
/// the requested interval. A separate plan ID prevents wide detector reuse.
pub(crate) fn narrow_grid(
    project: &Value,
    origin: Option<&Value>,
    center: u64,
    lo: u64,
    hi: u64,
) -> Result<Option<ScanPlan>> {
    if hi <= 800_000_000 {
        return Ok(None);
    }
    let Some(plan) = configured_plan(project)? else {
        return Ok(None);
    };
    let mut candidates = plan
        .bands
        .iter()
        .filter(|b| b.step_hz.is_some() && (b.start_hz..=b.stop_hz).contains(&center));
    let parent = if let Some(origin) = origin {
        let band = s(origin, "band_id")?;
        let index = u32::try_from(integer(origin, "window_index")?)
            .map_err(|_| invalid("Неверное окно события"))?;
        candidates
            .find(|b| {
                b.id == band
                    && b.window(index)
                        .is_some_and(|window| (window.start_hz..=window.stop_hz).contains(&center))
            })
            .ok_or_else(|| invalid("Исходное окно события не найдено"))?
    } else {
        let Some(band) = candidates.next() else {
            return Ok(None);
        };
        if candidates.next().is_some() {
            return Err(invalid(
                "Для пересекающихся сеток нужен исходный поддиапазон события",
            ));
        }
        band
    };
    let step = ori_monitoring::scan::GRID_STEP_HZ;
    let low = parent.start_hz + (lo.max(parent.start_hz) - parent.start_hz).div_ceil(step) * step;
    let high = parent.start_hz + (hi.min(parent.stop_hz) - parent.start_hz) / step * step;
    if high <= low {
        return Err(invalid(
            "Для узкого обзора выше 800 МГц нужны две частоты исходной сетки 10 кГц",
        ));
    }
    let config = ScanPlan {
        name: "Narrow RX".into(),
        bands: vec![ScanBand {
            id: "search".into(),
            name: "Narrow RX".into(),
            start_hz: low,
            stop_hz: high,
            points: ((high - low) / step + 1) as u32,
            step_hz: Some(step),
            rbw_hz: 10_000,
            max_revisit_seconds: parent.max_revisit_seconds,
        }],
    };
    config.validate_receive_plan(profile_limit(project)?)?;
    Ok(Some(config))
}

pub(crate) fn check_baseline(
    project: &Value,
    band: &ScanBand,
    window: u32,
    baseline: &Value,
) -> Result<()> {
    if baseline["scan_plan_id"] != project["scan_plan"]["id"]
        || baseline["scan_band_id"] != band.id
        || baseline
            .get("scan_plan_revision")
            .is_some_and(|v| v != &project["scan_plan"]["revision"])
        || band.step_hz.is_some()
            && (baseline["scan_window_index"] != window
                || baseline["scan_plan_revision"] != project["scan_plan"]["revision"])
    {
        return Err(invalid(
            "Эталон не относится к текущему окну и версии плана",
        ));
    }
    Ok(())
}

impl GuardService {
    pub(crate) fn scan_plan_record(&self, project: &Value, config: Value) -> Result<Value> {
        let config: ScanPlan = serde_json::from_value(config).map_err(|_| {
            invalid("Нужны название плана и 1–16 поддиапазонов с известными полями")
        })?;
        config.validate_receive_plan(profile_limit(project)?)?;
        let revision = project["scan_plan"]["revision"]
            .as_u64()
            .unwrap_or(0)
            .checked_add(1)
            .filter(|v| *v <= 9_007_199_254_740_991)
            .ok_or_else(|| invalid("Исчерпана версия плана обзора"))?;
        self.record(Some(s(project,"id")?),json!({"schema_version":1,"revision":revision,"config":value(&config)?,"hardware_validation":"not_run"}))
    }
    pub async fn scan_plan(&self, id: &str) -> Result<Value> {
        let project = self.project(id).await?;
        Ok(project.get("scan_plan").cloned().unwrap_or(Value::Null))
    }
}
