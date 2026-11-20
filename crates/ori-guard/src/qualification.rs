//! Eligibility for automatic monitoring is independent of acquisition and storage.
use crate::{
    GuardService, Result,
    util::{decode, invalid, s},
};
use ori_monitoring::{ScanBand, Sweep};
use serde_json::{Value, json};
use std::collections::BTreeMap;

use ori_monitoring::scan::TINYSA_REFERENCE_LIMIT_HZ as REFERENCE_LIMIT_HZ;
pub(crate) const REFERENCE_REQUIRED: &str = "reference_required_above_800_mhz";

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Qualification {
    Supported,
    Partial,
    Unverified,
    Synthetic,
}

impl Qualification {
    fn for_range(adapter: Option<&str>, start_hz: u64, stop_hz: u64) -> Self {
        match adapter {
            Some("tinysa-sim") => Self::Synthetic,
            Some("tinysa-serial") if stop_hz <= REFERENCE_LIMIT_HZ => Self::Supported,
            Some("tinysa-serial") if start_hz <= REFERENCE_LIMIT_HZ => Self::Partial,
            _ => Self::Unverified,
        }
    }

    pub(crate) fn eligible(self) -> bool {
        matches!(self, Self::Supported | Self::Synthetic)
    }
}

/// Describes the existing receive profile. An ID, firmware string or nonflat
/// trace cannot promote an upper window to an independently verified reference.
pub fn measurement_qualification(adapter: Option<&str>, start_hz: u64, stop_hz: u64) -> Value {
    let status = Qualification::for_range(adapter, start_hz, stop_hz);
    let name = match status {
        Qualification::Supported => "supported",
        Qualification::Partial => "partial",
        Qualification::Unverified => "unverified",
        Qualification::Synthetic => "synthetic",
    };
    json!({
        "status": name,
        "reason": (!status.eligible() && stop_hz > REFERENCE_LIMIT_HZ).then_some(REFERENCE_REQUIRED),
        "verified_through_hz": (adapter == Some("tinysa-serial")).then_some(REFERENCE_LIMIT_HZ),
    })
}

pub(crate) fn analyzer_adapter(project: &Value) -> Option<&str> {
    project["profile"]["analyzer"]["settings"]["adapter"].as_str()
}

pub(crate) fn eligible_windows<'a>(
    project: &'a Value,
    band: &'a ScanBand,
) -> impl Iterator<Item = u32> + 'a {
    let adapter = analyzer_adapter(project);
    (0..band.window_count()).filter(move |&index| {
        band.window(index).is_some_and(|window| {
            Qualification::for_range(adapter, window.start_hz, window.stop_hz).eligible()
        })
    })
}

pub(crate) fn source_qualification(project: &Value, sweep: &Sweep) -> Result<Qualification> {
    let (Some(&first), Some(&last)) = (sweep.frequencies_hz.first(), sweep.frequencies_hz.last())
    else {
        return Err(invalid("Пустая частотная сетка исходного свипа"));
    };
    Ok(Qualification::for_range(
        source_adapter(project, sweep),
        first,
        last,
    ))
}

pub(crate) fn source_adapter<'a>(project: &'a Value, sweep: &Sweep) -> Option<&'a str> {
    if sweep
        .settings
        .as_ref()
        .and_then(|s| s.get("adapter"))
        .and_then(Value::as_str)
        == Some("tinysa-serial")
    {
        Some("tinysa-serial")
    } else {
        analyzer_adapter(project)
    }
}

pub(crate) fn event_eligible(project: &Value, event: &Value) -> Result<bool> {
    let adapter = analyzer_adapter(project);
    let Some(center) = event["center_frequency_hz"].as_u64() else {
        return Ok(false);
    };
    let start = event["start_frequency_hz"].as_u64().unwrap_or(center);
    let end = event["end_frequency_hz"].as_u64().unwrap_or(center);
    if !Qualification::for_range(adapter, start.min(center), end.max(center)).eligible() {
        return Ok(false);
    }
    if adapter != Some("tinysa-serial") {
        return Ok(true);
    }
    let Some(plan) = crate::scan_plan::configured_plan(project)? else {
        return Ok(true);
    };
    let band = plan.bands.iter().find(|b| event["scan_band_id"] == b.id);
    let Some(band) = band else { return Ok(false) };
    if event["scan_plan_id"] != project["scan_plan"]["id"]
        || event["scan_plan_revision"] != project["scan_plan"]["revision"]
    {
        return Ok(false);
    }
    let index = if band.step_hz.is_some() {
        let Some(index) = event["scan_window_index"]
            .as_u64()
            .and_then(|i| u32::try_from(i).ok())
        else {
            return Ok(false);
        };
        index
    } else {
        0
    };
    Ok(band.window(index).is_some_and(|w| {
        w.start_hz <= start
            && end <= w.stop_hz
            && Qualification::for_range(adapter, w.start_hz, w.stop_hz).eligible()
    }))
}

type ReferenceRanges = BTreeMap<String, Option<(u64, u64)>>;

impl GuardService {
    // Older events carry a peak and a reference, but a low peak can come from
    // a crossing window. Resolve the original once per reference on each page.
    pub(crate) async fn event_reference_eligible(
        &self,
        project: &Value,
        event: &Value,
        ranges: &mut ReferenceRanges,
    ) -> Result<bool> {
        if analyzer_adapter(project) != Some("tinysa-serial") {
            return Ok(analyzer_adapter(project) == Some("tinysa-sim"));
        }
        let Some(id) = event["baseline_id"].as_str() else {
            return Ok(false);
        };
        if !ranges.contains_key(id) {
            let range = self.qualified_reference_range(project, id).await?;
            ranges.insert(id.to_owned(), range);
        }
        Ok(ranges[id].is_some_and(|(lo, hi)| {
            [
                "start_frequency_hz",
                "center_frequency_hz",
                "end_frequency_hz",
            ]
            .iter()
            .all(|&field| {
                event[field]
                    .as_u64()
                    .is_some_and(|hz| (lo..=hi).contains(&hz))
            })
        }))
    }

    async fn qualified_reference_range(
        &self,
        project: &Value,
        id: &str,
    ) -> Result<Option<(u64, u64)>> {
        let Some(baseline) = self.archive.monitoring("baseline", id).await? else {
            return Ok(None);
        };
        if baseline["project_id"] != project["id"] || baseline["confirmed"] != true {
            return Ok(None);
        }
        let Some(reference) = baseline["source_refs"]
            .as_array()
            .and_then(|refs| refs.first())
        else {
            return Ok(None);
        };
        let Some(sweep_id) = reference["sweep_id"].as_str() else {
            return Ok(None);
        };
        let Some(raw) = self.archive.get_sweep(sweep_id).await? else {
            return Ok(None);
        };
        if raw["session_id"] != reference["session_id"]
            || raw["stream_id"] != reference["stream_id"]
            || raw["frequencies_hz"] != baseline["frequencies_hz"]
            || !self
                .archive
                .monitoring_member(s(project, "id")?, s(&raw, "session_id")?)
                .await?
        {
            return Ok(None);
        }
        let sweep = decode(&raw)?;
        if !source_qualification(project, &sweep)?.eligible() {
            return Ok(None);
        }
        Ok(sweep
            .frequencies_hz
            .first()
            .copied()
            .zip(sweep.frequencies_hz.last().copied()))
    }
}

pub(crate) fn unverified_reference() -> crate::GuardError {
    invalid("Окно выше 800 МГц требует независимой проверки тракта для автоматической охраны")
}
