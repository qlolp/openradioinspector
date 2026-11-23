use crate::{GuardService, Result, aor::block_id, qualification::source_qualification, util::*};
use ori_monitoring::{Sweep, build_baseline};
use serde_json::{Value, json};

pub(crate) fn survey(project: &Value) -> bool {
    project["profile"]["analyzer"]["settings"]["adapter"] == "rf-explorer-3g-serial"
        && project["profile"]["analyzer"]["settings"]["survey_step_hz"] == 10000
}

impl GuardService {
    pub(crate) async fn check_rfe_start(
        &self,
        p: &Value,
        workers: &[crate::WorkerStatus],
    ) -> Result<()> {
        if p["primary_source_kind"] != "rf_explorer" {
            return Ok(());
        }
        let sid = s(p, "analyzer_stream_id")?;
        let worker = workers
            .iter()
            .find(|w| w.stream_id == sid)
            .ok_or_else(|| invalid("Нет исполнителя RF Explorer"))?;
        let actual = json!(worker.current);
        let baseline = if survey(p) {
            self.get_record(
                crate::aor::BLOCK,
                &block_id(s(p, "baseline_id")?, 0),
                Some(s(p, "id")?),
            )
            .await?
        } else {
            self.get_record("baseline", s(p, "baseline_id")?, Some(s(p, "id")?))
                .await?
        };
        let source = baseline["source_refs"]
            .as_array()
            .and_then(|refs| refs.first())
            .ok_or_else(|| invalid("Нет источника эталона RF Explorer"))?;
        let original = self
            .archive
            .get_sweep(s(source, "sweep_id")?)
            .await?
            .ok_or(crate::GuardError::NotFound)?;
        let expected = &original["settings"];
        let fields = [
            "device_identity",
            "active_board",
            "active_input",
            "board_min_hz",
            "board_max_hz",
            "board_max_span_hz",
            "step_hz",
            "points",
            "rbw_hz",
            "amplitude_top_dbm",
            "amplitude_bottom_dbm",
            "offset_db",
            "detector_code",
            "receive_profile",
            "simulated",
        ];
        if original["session_id"] != source["session_id"]
            || original["stream_id"] != source["stream_id"]
            || !self
                .archive
                .monitoring_member(s(p, "id")?, s(source, "session_id")?)
                .await?
            || fields
                .iter()
                .any(|&key| actual.get(key).is_none() || actual[key] != expected[key])
            || (!survey(p)
                && (actual["start_hz"] != expected["start_hz"]
                    || actual["stop_hz"] != expected["stop_hz"]))
        {
            return Err(invalid(
                "Readback RF Explorer не совпал с подтверждённым эталоном Normal",
            ));
        }
        Ok(())
    }

    pub(crate) async fn fixed_rfe_selection(
        &self,
        p: &Value,
    ) -> Result<ori_archive::BaselineSelection> {
        let candidates = self
            .archive
            .rfe_guard_candidates(s(p, "active_session_id")?, s(p, "analyzer_stream_id")?)
            .await?;
        let mut selected = Vec::new();
        let mut wanted = None;
        let mut next_start = None;
        for raw in candidates.iter().rev() {
            let sweep: Sweep = decode(raw)?;
            if !source_qualification(p, &sweep)?.eligible() {
                continue;
            }
            let fingerprint = ori_monitoring::fingerprint(&sweep)?;
            if *wanted.get_or_insert(fingerprint.clone()) != fingerprint
                || next_start.is_some_and(|next| {
                    sweep.interval.ended_at.unix_micros() > next
                        || sweep.interval.started_at.unix_micros() >= next
                })
            {
                continue;
            }
            next_start = Some(sweep.interval.started_at.unix_micros());
            selected.push(sweep);
        }
        selected.reverse();
        let first = selected
            .first()
            .map(|s| s.interval.started_at.as_str().to_owned());
        let last = selected
            .last()
            .map(|s| s.interval.ended_at.as_str().to_owned());
        let span = first
            .as_ref()
            .zip(last.as_ref())
            .map(|(a, b)| elapsed(a, b))
            .transpose()?
            .unwrap_or(0.);
        let periods = selected
            .windows(2)
            .map(|pair| {
                elapsed(
                    pair[0].interval.started_at.as_str(),
                    pair[1].interval.started_at.as_str(),
                )
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(ori_archive::BaselineSelection {
            sweep_ids: selected.iter().map(|s| s.id.clone()).collect(),
            count: selected.len() as u64,
            first_started_at: first,
            last_ended_at: last,
            elapsed_seconds: span,
            sweep_period_seconds: median(periods),
        })
    }

    pub(crate) async fn rfe_collection(&self, p: &Value) -> Result<Value> {
        let mut report = self
            .archive
            .rfe_guard_selection(s(p, "active_session_id")?, s(p, "analyzer_stream_id")?)
            .await?;
        if let Some(values) = report.as_object_mut() {
            values.remove("pass_keys");
        }
        Ok(report)
    }

    pub(crate) async fn rfe_reference(&self, p: &Value, payload: &Value) -> Result<Value> {
        let sid = s(p, "active_session_id")?;
        let stream = s(p, "analyzer_stream_id")?;
        let selected = self.archive.rfe_guard_selection(sid, stream).await?;
        if selected["ready"] != true {
            return Err(invalid(
                "Для каждого физического окна нужны 20 совместимых проходов RF Explorer Normal за 60 секунд",
            ));
        }
        let mut build = self.reference_build(p, &selected).await?;
        let inputs = build["inputs"].clone();
        let pass_keys: Vec<i64> = serde_json::from_value(inputs["pass_keys"].clone())
            .map_err(|_| invalid("RF Explorer passes"))?;
        let windows = integer(&inputs, "windows")? as usize;
        let mut next = integer(&build, "next_block")? as usize;
        let mut boundary = build["boundary"].as_u64().unwrap_or(0);
        let mut cadence = build["minimum_cadence"].as_f64().unwrap_or(f64::MAX);
        while next < windows {
            let sources = self
                .archive
                .rfe_guard_window(
                    sid,
                    stream,
                    &pass_keys,
                    next + 1,
                    s(&inputs, "context_key")?,
                )
                .await?;
            let sweeps = sources
                .iter()
                .map(decode::<Sweep>)
                .collect::<Result<Vec<_>>>()?;
            for sweep in &sweeps {
                if !source_qualification(p, sweep)?.eligible() {
                    return Err(invalid("Несовместимый исходный свип RF Explorer Normal"));
                }
            }
            let mut reference = value(&build_baseline(&sweeps)?)?;
            let ids = sweeps.iter().map(|s| s.id.clone()).collect::<Vec<_>>();
            let positions = self.archive.capture_positions(&ids).await?;
            boundary = boundary.max(
                positions
                    .values()
                    .map(|p| p.rowid)
                    .max()
                    .ok_or_else(|| invalid("RF Explorer source positions"))?,
            );
            reference["confirmed"] = json!(true);
            reference["window_index"] = json!(next);
            reference["reference_set_id"] = build["baseline_id"].clone();
            cadence = cadence.min(number(&reference, "sweep_period_seconds")?);
            build["boundary"] = json!(boundary);
            build["minimum_cadence"] = json!(cadence);
            self.persist_reference_block(&mut build, next, reference)
                .await?;
            next += 1;
        }
        self.publish_reference(p,payload,build,json!({"windows":windows,"level_unit":"dBm","detector":"normal",
            "context_key":inputs["context_key"],"passes":inputs["passes"],"pass_keys":pass_keys,"sweep_period_seconds":cadence,
            "start_hz":inputs["start_hz"],"stop_hz":inputs["stop_hz"],"boundary":boundary,"source_count":windows*pass_keys.len()})).await
    }
}

pub(crate) fn window_id(p: &Value, sweep: &Sweep) -> Result<String> {
    let survey = sweep
        .settings
        .as_ref()
        .and_then(|s| s.get("survey"))
        .ok_or_else(|| invalid("RF Explorer survey metadata"))?;
    let tile = integer(survey, "tile")? as usize;
    if !(1..=2398).contains(&tile) {
        return Err(invalid("RF Explorer tile index"));
    }
    Ok(block_id(s(p, "baseline_id")?, tile - 1))
}
