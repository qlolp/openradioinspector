use crate::*;
use chrono::{DateTime, Datelike, SecondsFormat, Timelike, Utc};
use ori_contracts::{JSONObject, Sweep};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn instant(value: &str) -> Result<i64> {
    let time = DateTime::parse_from_rfc3339(value)
        .map_err(|_| MonitoringError::InvalidState("timezone-aware timestamp required"))?
        .with_timezone(&Utc);
    if !(1..=9999).contains(&time.year()) || time.nanosecond() >= 1_000_000_000 {
        return Err(MonitoringError::InvalidState(
            "timestamp outside Python UTC range",
        ));
    }
    Ok(time.timestamp_micros())
}
fn python_time(micros: i64, zulu: bool) -> Result<String> {
    let time = DateTime::<Utc>::from_timestamp_micros(micros)
        .ok_or(MonitoringError::InvalidState("timestamp outside UTC range"))?;
    Ok(time.to_rfc3339_opts(
        if micros.rem_euclid(1_000_000) == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        zulu,
    ))
}
fn seconds(delta: i64) -> f64 {
    delta as f64 / 1_000_000.0
}
fn validate_sweep(sweep: &Sweep) -> Result<()> {
    let size = sweep.frequencies_hz.len();
    if size == 0 || size > MAX_BINS || size != sweep.levels.len() {
        return Err(MonitoringError::InvalidSweep(
            "nonempty equal bounded dimensions required",
        ));
    }
    if sweep.levels.iter().any(|v| !v.is_finite()) {
        return Err(MonitoringError::InvalidSweep("levels must be finite"));
    }
    if sweep.frequencies_hz.windows(2).any(|p| p[1] <= p[0]) {
        return Err(MonitoringError::InvalidSweep(
            "strictly increasing grid required",
        ));
    }
    if sweep.interval.ended_at.unix_micros() < sweep.interval.started_at.unix_micros() {
        return Err(MonitoringError::InvalidSweep("interval is reversed"));
    }
    Ok(())
}
pub fn canonical_fingerprint_json(sweep: &Sweep) -> Result<String> {
    validate_sweep(sweep)?;
    let settings = sweep
        .settings
        .as_ref()
        .filter(|m| !m.is_empty())
        .ok_or(MonitoringError::InvalidSweep("known settings required"))?;
    if sweep.level_unit != "dBm"
        || ["overload", "incomplete"]
            .iter()
            .any(|key| settings.get(*key) == Some(&Value::Bool(true)))
    {
        return Err(MonitoringError::InvalidSweep(
            "only complete non-overloaded raw dBm sweeps are eligible",
        ));
    }
    let settings = Value::Object(
        settings
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect(),
    );
    let settings = stable(&settings)?;
    python_canonical_json(&json!({"grid":sweep.frequencies_hz,"settings":settings}))
}
pub fn fingerprint(sweep: &Sweep) -> Result<String> {
    Ok(
        Sha256::digest(canonical_fingerprint_json(sweep)?.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
    )
}
fn median(ordered: &[f64]) -> f64 {
    let mid = ordered.len() / 2;
    if ordered.len().is_multiple_of(2) {
        (ordered[mid - 1] + ordered[mid]) / 2.0
    } else {
        ordered[mid]
    }
}
fn sorted(values: &mut [f64]) {
    values.sort_by(|a, b| a.partial_cmp(b).expect("finite validated samples"));
}
pub fn build_baseline(sweeps: &[Sweep]) -> Result<Baseline> {
    if !(20..=MAX_BASELINE_SWEEPS).contains(&sweeps.len()) {
        return Err(MonitoringError::InvalidBaseline(
            "20..4096 full sweeps required",
        ));
    }
    let size = sweeps[0].frequencies_hz.len();
    if size.saturating_mul(sweeps.len()) > MAX_BASELINE_CELLS {
        return Err(MonitoringError::LimitExceeded("baseline cells"));
    }
    let mut start = i64::MAX;
    let mut end = i64::MIN;
    for sweep in sweeps {
        validate_sweep(sweep)?;
        start = start.min(sweep.interval.started_at.unix_micros());
        end = end.max(sweep.interval.ended_at.unix_micros());
    }
    if end - start < 60_000_000 {
        return Err(MonitoringError::InvalidBaseline(
            "at least sixty seconds required",
        ));
    }
    let expected = fingerprint(&sweeps[0])?;
    for sweep in sweeps.iter().skip(1) {
        if fingerprint(sweep)? != expected {
            return Err(MonitoringError::Incompatible);
        }
    }
    let (mut p50, mut p95, mut mad) = (
        Vec::with_capacity(size),
        Vec::with_capacity(size),
        Vec::with_capacity(size),
    );
    for bin in 0..size {
        let mut column: Vec<f64> = sweeps.iter().map(|s| s.levels[bin]).collect();
        sorted(&mut column);
        let center = median(&column);
        p50.push(center);
        p95.push(column[((0.95 * column.len() as f64).ceil() as usize) - 1]);
        for value in &mut column {
            *value = (*value - center).abs();
        }
        sorted(&mut column);
        mad.push(median(&column));
    }
    let mut periods: Vec<f64> = sweeps
        .windows(2)
        .map(|pair| {
            seconds(
                pair[1].interval.started_at.unix_micros()
                    - pair[0].interval.started_at.unix_micros(),
            )
        })
        .collect();
    sorted(&mut periods);
    let mut extra = JSONObject::new();
    extra.insert("sweep_period_seconds".into(), json!(median(&periods)));
    let baseline = Baseline {
        fingerprint: expected,
        frequencies_hz: sweeps[0].frequencies_hz.clone(),
        p50_dbm: p50,
        p95_dbm: p95,
        mad_db: mad,
        source_refs: sweeps
            .iter()
            .map(|s| SourceRef {
                session_id: s.session_id.clone(),
                stream_id: s.stream_id.clone(),
                sweep_id: s.id.clone(),
                extra: JSONObject::new(),
            })
            .collect(),
        started_at: python_time(start, false)?,
        ended_at: python_time(end, false)?,
        detector_version: VERSION.into(),
        extra,
    };
    validate_baseline(&baseline)?;
    Ok(baseline)
}
fn validate_parameters(p: &Parameters) -> Result<()> {
    if !p.margin_db.is_finite()
        || !(1.0..=60.0).contains(&p.margin_db)
        || !p.impulse_db.is_finite()
        || !(1.0..=120.0).contains(&p.impulse_db)
        || !p.hysteresis_db.is_finite()
        || !(0.0..=20.0).contains(&p.hysteresis_db)
        || p.impulse_db < p.margin_db
        || p.hysteresis_db > p.margin_db
        || !(1..=MAX_WINDOW).contains(&p.window_sweeps)
        || p.confirmations == 0
        || p.confirmations > p.window_sweeps
    {
        return Err(MonitoringError::InvalidParameters(
            "finite application ranges and 1<=confirmations<=window<=15 required",
        ));
    }
    let _ = python_canonical_json(
        &serde_json::to_value(p).map_err(|error| MonitoringError::Json(error.to_string()))?,
    )?;
    Ok(())
}
fn validate_baseline(b: &Baseline) -> Result<()> {
    let n = b.frequencies_hz.len();
    if n == 0
        || n > MAX_BINS
        || b.frequencies_hz.windows(2).any(|p| p[1] <= p[0])
        || [b.p50_dbm.len(), b.p95_dbm.len(), b.mad_db.len()]
            .iter()
            .any(|&len| len != n)
        || b.p50_dbm
            .iter()
            .chain(&b.p95_dbm)
            .chain(&b.mad_db)
            .any(|v| !v.is_finite())
        || b.mad_db.iter().any(|v| *v < 0.0)
        || b.source_refs.len() > MAX_BASELINE_SWEEPS
        || b.fingerprint.len() != 64
        || !b.fingerprint.bytes().all(|b| b.is_ascii_hexdigit())
        || b.detector_version != VERSION
    {
        return Err(MonitoringError::InvalidBaseline(
            "unsupported version, dimensions or nonfinite statistics",
        ));
    }
    if instant(&b.ended_at)? < instant(&b.started_at)? {
        return Err(MonitoringError::InvalidBaseline("interval is reversed"));
    }
    let _ = python_canonical_json(
        &serde_json::to_value(b).map_err(|e| MonitoringError::Json(e.to_string()))?,
    )?;
    Ok(())
}
fn period(b: &Baseline) -> Result<Option<f64>> {
    if let Some(value) = b.extra.get("sweep_period_seconds") {
        if value.is_null() {
            return Ok(None);
        }
        return value.as_f64().filter(|p| p.is_finite()).map(Some).ok_or(
            MonitoringError::InvalidBaseline("cadence must be finite or explicitly null"),
        );
    }
    Ok(Some(
        (seconds(instant(&b.ended_at)? - instant(&b.started_at)?)
            / (b.source_refs.len().saturating_sub(1).max(1) as f64))
            .max(0.01),
    ))
}
fn validate_state(state: &DetectorState, size: usize, p: &Parameters) -> Result<()> {
    if state.offsets.len() != size
        || state
            .offsets
            .iter()
            .any(|v| !v.is_finite() || v.abs() > 3.0)
        || state.window.len() > p.window_sweeps
        || state.window.iter().any(|row| row.len() != size)
        || state
            .recent_sources
            .as_ref()
            .is_some_and(|s| s.len() > p.window_sweeps)
    {
        return Err(MonitoringError::InvalidState(
            "bounded finite offsets and matching window dimensions required",
        ));
    }
    if let Some(previous) = &state.last_started_at {
        instant(previous)?;
    }
    if let Some(sources) = &state.recent_sources {
        for source in sources {
            instant(&source.started_at)?;
        }
    }
    if let Some(quality) = &state.quality_issue {
        if !quality.gap_seconds.is_finite() || quality.kind != "data_gap" {
            return Err(MonitoringError::InvalidState("invalid data-gap evidence"));
        }
        instant(&quality.previous_started_at)?;
    }
    let _ = python_canonical_json(
        &serde_json::to_value(state).map_err(|e| MonitoringError::Json(e.to_string()))?,
    )?;
    Ok(())
}
/// Stateful only in memory; the core atomically persists checkpoint + events +
/// cursor after handling this result. This type never creates event/archive IDs.
pub struct Detector {
    baseline: Baseline,
    parameters: Parameters,
    state: DetectorState,
    period: Option<f64>,
    null_last_started_at: bool,
    null_quality_issue: bool,
}
impl Detector {
    pub fn new(baseline: Baseline, parameters: Parameters, state: Option<Value>) -> Result<Self> {
        validate_baseline(&baseline)?;
        validate_parameters(&parameters)?;
        let cadence = period(&baseline)?;
        let null_last_started_at = state
            .as_ref()
            .is_some_and(|value| value.get("last_started_at") == Some(&Value::Null));
        let null_quality_issue = state
            .as_ref()
            .is_some_and(|value| value.get("quality_issue") == Some(&Value::Null));
        if state
            .as_ref()
            .is_some_and(|value| value.get("recent_sources") == Some(&Value::Null))
        {
            return Err(MonitoringError::InvalidState(
                "recent_sources must be an array when present",
            ));
        }
        let state = match state {
            None | Some(Value::Null) => None,
            Some(Value::Object(map)) if map.is_empty() => None,
            Some(value) => Some(value),
        };
        let state = if let Some(value) = state {
            let bytes = python_canonical_json(&value)?;
            ori_contracts::from_json_slice::<DetectorState>(bytes.as_bytes(), MAX_JSON_BYTES)
                .map_err(|e| MonitoringError::Json(e.to_string()))?
        } else {
            DetectorState {
                window: Vec::new(),
                offsets: vec![0.0; baseline.frequencies_hz.len()],
                active: Some(json!([])),
                recent_sources: None,
                last_started_at: None,
                quality_issue: None,
                extra: JSONObject::new(),
            }
        };
        validate_state(&state, baseline.frequencies_hz.len(), &parameters)?;
        Ok(Self {
            baseline,
            parameters,
            state,
            period: cadence,
            null_last_started_at,
            null_quality_issue,
        })
    }
    pub fn restore_json(baseline: Baseline, parameters: Parameters, bytes: &[u8]) -> Result<Self> {
        let state = ori_contracts::parse_json_value(bytes, MAX_JSON_BYTES)
            .map_err(|e| MonitoringError::Json(e.to_string()))?;
        Self::new(baseline, parameters, Some(state))
    }
    pub fn state(&self) -> &DetectorState {
        &self.state
    }
    pub fn baseline(&self) -> &Baseline {
        &self.baseline
    }
    pub fn parameters(&self) -> &Parameters {
        &self.parameters
    }
    pub fn period_seconds(&self) -> Option<f64> {
        self.period
    }
    pub fn checkpoint(&self) -> Result<Value> {
        let mut checkpoint =
            serde_json::to_value(&self.state).map_err(|e| MonitoringError::Json(e.to_string()))?;
        if self.null_last_started_at {
            checkpoint["last_started_at"] = Value::Null;
        }
        if self.null_quality_issue {
            checkpoint["quality_issue"] = Value::Null;
        }
        Ok(checkpoint)
    }
    pub fn checkpoint_json(&self) -> Result<Vec<u8>> {
        Ok(python_canonical_json(&self.checkpoint()?)?.into_bytes())
    }
    pub fn take_quality_issue(&mut self) -> Option<DataGap> {
        self.null_quality_issue = false;
        self.state.quality_issue.take()
    }
    pub fn step(&mut self, sweep: &Sweep, allowed: &[AllowedSignal]) -> Result<DetectionStep> {
        if fingerprint(sweep)? != self.baseline.fingerprint {
            return Err(MonitoringError::Incompatible);
        }
        if allowed.len() > MAX_ALLOWED {
            return Err(MonitoringError::LimitExceeded("allowed signals"));
        }
        if allowed
            .iter()
            .any(|a| a.start_hz > a.end_hz || a.end_hz > 20_000_000_000)
        {
            return Err(MonitoringError::InvalidParameters(
                "allowed range must use valid integer Hz",
            ));
        }
        let (b, p) = (&self.baseline, &self.parameters);
        let mut state = self.state.clone();
        state.quality_issue = None;
        let started = sweep.interval.started_at.unix_micros();
        if let Some(previous) = &state.last_started_at {
            let gap = seconds(started - instant(previous)?);
            if gap < 0.0
                || self
                    .period
                    .is_some_and(|period| period != 0.0 && gap > 1.0_f64.max(3.0 * period))
            {
                state.window.clear();
                state.recent_sources = Some(Vec::new());
                state.quality_issue = Some(DataGap {
                    kind: "data_gap".into(),
                    gap_seconds: gap,
                    previous_started_at: previous.clone(),
                    extra: JSONObject::new(),
                });
            }
        }
        state.last_started_at = Some(python_time(started, true)?);
        let excess: Vec<f64> = sweep
            .levels
            .iter()
            .zip(&b.p95_dbm)
            .zip(&state.offsets)
            .map(|((&level, &reference), &offset)| level - (reference + offset))
            .collect();
        let thresholds: Vec<f64> = b
            .mad_db
            .iter()
            .map(|mad| p.margin_db.max(3.0 * 1.4826 * mad))
            .collect();
        if excess.iter().chain(&thresholds).any(|v| !v.is_finite()) {
            return Err(MonitoringError::InvalidSweep(
                "detection arithmetic is nonfinite",
            ));
        }
        let above: Vec<bool> = excess
            .iter()
            .zip(&thresholds)
            .map(|(value, threshold)| value >= threshold)
            .collect();
        let impulses: Vec<bool> = excess.iter().map(|v| *v >= p.impulse_db).collect();
        let suppressed: Vec<bool> = b
            .frequencies_hz
            .iter()
            .map(|&hz| {
                allowed
                    .iter()
                    .any(|a| !a.watch_changes && a.start_hz <= hz && hz <= a.end_hz)
            })
            .collect();
        state.window.push(above.clone());
        if state.window.len() > p.window_sweeps {
            state.window.remove(0);
        }
        let recent = state.recent_sources.get_or_insert_with(Vec::new);
        recent.push(RecentSource {
            session_id: sweep.session_id.clone(),
            stream_id: sweep.stream_id.clone(),
            sweep_id: sweep.id.clone(),
            started_at: python_time(started, false)?,
            extra: JSONObject::new(),
        });
        if recent.len() > p.window_sweeps {
            recent.remove(0);
        }
        let active: Vec<bool> = (0..above.len())
            .map(|i| {
                !suppressed[i]
                    && ((above[i]
                        && state.window.iter().filter(|row| row[i]).count() >= p.confirmations)
                        || impulses[i])
            })
            .collect();
        let eligible: Vec<usize> = (0..above.len())
            .filter(|&i| above[i] && !suppressed[i])
            .collect();
        let broad_limit = 2.max(above.len().div_ceil(2));
        let broad = eligible.len() >= broad_limit;
        let mut regions = Vec::new();
        let mut start = None;
        for i in 0..=active.len() {
            let flag = active.get(i).copied().unwrap_or(false);
            if flag && start.is_none() {
                start = Some(i);
            }
            if !flag && let Some(first) = start.take() {
                let peak = first_peak(first..i, &excess);
                regions.push(region(
                    b,
                    sweep,
                    &excess,
                    first,
                    i - 1,
                    peak,
                    if impulses[first..i].iter().any(|flag| *flag) {
                        RegionKind::Impulse
                    } else {
                        RegionKind::Persistent
                    },
                ));
            }
        }
        if broad {
            let peak = first_peak(eligible.iter().copied(), &excess);
            regions = vec![region(
                b,
                sweep,
                &excess,
                eligible[0],
                *eligible.last().unwrap(),
                peak,
                RegionKind::BroadbandChange,
            )];
        } else if regions.len() > 32 {
            let count = regions.len();
            let first = regions[0].first_bin;
            let last = regions.last().unwrap().last_bin;
            let peak = first_peak(
                regions.iter().map(|r| {
                    b.frequencies_hz
                        .binary_search(&r.center_frequency_hz)
                        .expect("validated region center")
                }),
                &excess,
            );
            let mut summary = region(
                b,
                sweep,
                &excess,
                first,
                last,
                peak,
                RegionKind::DenseChange,
            );
            summary.component_regions_count = Some(count);
            regions = vec![summary];
        }
        let mut result = Vec::new();
        for mut region in regions {
            if let Some(rule) = allowed.iter().find(|a| {
                a.start_hz <= region.start_frequency_hz && a.end_hz >= region.end_frequency_hz
            }) {
                if !rule.watch_changes {
                    continue;
                }
                region.kind = RegionKind::AuthorizedChanged;
                region.allowed_id = Some(rule.id.clone());
            }
            result.push(region);
        }
        if above.iter().filter(|flag| **flag).count() < broad_limit {
            let duration = seconds(sweep.interval.ended_at.unix_micros() - started).max(0.01);
            let alpha = -(-duration.min(30.0) / 30.0).exp_m1();
            for i in 0..above.len() {
                let known = allowed
                    .iter()
                    .any(|a| a.start_hz <= b.frequencies_hz[i] && b.frequencies_hz[i] <= a.end_hz);
                if !above[i] && !impulses[i] && !known {
                    let target = (sweep.levels[i] - b.p50_dbm[i]).clamp(-3.0, 3.0);
                    state.offsets[i] += alpha * (target - state.offsets[i]);
                }
            }
        }
        validate_state(&state, b.frequencies_hz.len(), p)?;
        self.state = state;
        self.null_last_started_at = false;
        self.null_quality_issue = false;
        Ok(DetectionStep {
            regions: result,
            excess_db: excess,
            thresholds_db: thresholds,
        })
    }
}
fn first_peak(indices: impl IntoIterator<Item = usize>, values: &[f64]) -> usize {
    let mut indices = indices.into_iter();
    let mut peak = indices.next().expect("nonempty region");
    for index in indices {
        if values[index] > values[peak] {
            peak = index;
        }
    }
    peak
}
fn region(
    b: &Baseline,
    sweep: &Sweep,
    excess: &[f64],
    first: usize,
    last: usize,
    peak: usize,
    kind: RegionKind,
) -> Region {
    Region {
        first_bin: first,
        last_bin: last,
        start_frequency_hz: b.frequencies_hz[first],
        end_frequency_hz: b.frequencies_hz[last],
        center_frequency_hz: b.frequencies_hz[peak],
        peak_level: sweep.levels[peak],
        peak_excess_db: excess[peak],
        kind,
        component_regions_count: None,
        allowed_id: None,
    }
}
