use crate::{
    GuardError, GuardService, Result,
    qualification::{eligible_windows, source_qualification, unverified_reference},
    util::*,
};
use ori_contracts::Sweep;
use ori_monitoring::{AllowedSignal, Baseline, Detector, Parameters, build_baseline};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::atomic::Ordering,
};

impl GuardService {
    pub async fn create_baseline(&self, id: &str, payload: Value) -> Result<Value> {
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        // The public Python resource resolves the project before confirmation.
        let project = self.project(id).await?;
        fields(&payload, &["name", "location", "confirm", "band_id"], false)
            .map_err(|_| invalid("Подтвердите пригодность эталона обычной обстановки"))?;
        if payload["confirm"] != true {
            return Err(invalid(
                "Подтвердите пригодность эталона обычной обстановки",
            ));
        }
        if self.runtime_snapshot(id)?.is_some()
            || ["armed", "searching", "degraded"].contains(&s(&project, "state")?)
        {
            return Err(invalid("Снимите проект с охраны перед заменой эталона"));
        }
        if project["primary_source_kind"] == "ardv1" || crate::rfe::survey(&project) {
            if payload.get("band_id").is_some() {
                return Err(invalid("Поддиапазон не входит в этот профиль"));
            }
            text(&payload, "name", 200)?;
            text(&payload, "location", 200)?;
            return if project["primary_source_kind"] == "ardv1" {
                self.receiver_reference(&project, &payload).await
            } else {
                self.rfe_reference(&project, &payload).await
            };
        }
        let plan = crate::scan_plan::configured_plan(&project)?;
        let band = if let Some(plan) = &plan {
            let requested = s(&payload, "band_id")?;
            Some(
                plan.bands
                    .iter()
                    .find(|b| b.id == requested)
                    .ok_or_else(|| invalid("Неизвестный поддиапазон эталона"))?,
            )
        } else {
            if payload.get("band_id").is_some() {
                return Err(invalid("Поддиапазон требует сохранённый план обзора"));
            }
            None
        };
        let windows = band.map_or_else(|| vec![0], |b| eligible_windows(&project, b).collect());
        if windows.is_empty() {
            return Err(unverified_reference());
        }
        if let Some(band) = band.filter(|b| b.step_hz.is_some()) {
            let counts = self.window_counts(&project, band).await?;
            if counts
                .iter()
                .any(|(index, _, _)| *index >= band.window_count())
                || windows.iter().any(|index| {
                    counts
                        .binary_search_by_key(index, |row| row.0)
                        .ok()
                        .is_none_or(|i| counts[i].1 < 20 || counts[i].2 < 60.)
                })
            {
                return Err(invalid(
                    "Для каждого пригодного окна нужны 20 проходов за 60 секунд",
                ));
            }
        }
        let mut records = Vec::with_capacity(windows.len());
        let mut boundary = (0, String::new());
        for &index in &windows {
            let selection = match band {
                Some(b) if b.step_hz.is_some() => {
                    self.archive
                        .baseline_for_scan_window(
                            s(&project, "active_session_id")?,
                            s(&project, "analyzer_stream_id")?,
                            s(&project["scan_plan"], "id")?,
                            integer(&project["scan_plan"], "revision")?,
                            &b.id,
                            index,
                        )
                        .await?
                }
                Some(b) => {
                    self.archive
                        .baseline_selection_for_scan_band(
                            s(&project, "active_session_id")?,
                            s(&project, "analyzer_stream_id")?,
                            s(&project["scan_plan"], "id")?,
                            &b.id,
                        )
                        .await?
                }
                None if project["primary_source_kind"] == "rf_explorer" => {
                    self.fixed_rfe_selection(&project).await?
                }
                None => {
                    self.archive
                        .baseline_selection(
                            s(&project, "active_session_id")?,
                            s(&project, "analyzer_stream_id")?,
                        )
                        .await?
                }
            };
            let (baseline, position) = self
                .derive_window(&project, &payload, plan.as_ref(), band, index, selection)
                .await?;
            boundary = boundary.max(position);
            records.push(baseline);
        }

        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        let mut current = self.project(id).await?;
        if current["active_session_id"] != project["active_session_id"]
            || current["scan_plan"] != project["scan_plan"]
            || self.runtime_snapshot(id)?.is_some()
        {
            return Err(GuardError::Conflict(
                "Проект изменился во время построения эталона".into(),
            ));
        }
        let first = records[0].clone();
        if let Some(band) = band {
            for (&index, record) in windows.iter().zip(&records) {
                current["band_baselines"][band.context_key(index)] = record["id"].clone();
            }
            current["band_baselines"][&band.id] = first["id"].clone();
            crate::scan_plan::select_eligible_baselines(
                &mut current,
                plan.as_ref().expect("configured band"),
            );
            current["state"] = json!(if crate::scan_plan::all_baselines_ready(&current)? {
                "ready"
            } else {
                "draft"
            });
            if self
                .runtime
                .lock()
                .map_err(|_| invalid("guard runtime"))?
                .preparing
                .contains_key(id)
            {
                current["state"] = json!("collecting");
            }
        } else {
            current["baseline_id"] = first["id"].clone();
            current["state"] = json!("ready");
        }
        reset_detectors(&mut current);
        current["detector_rowid"] = json!(boundary.0);
        current["detector_cursor"] = json!(boundary.1);
        let mut mutations = records
            .into_iter()
            .map(|record| Self::mutation("baseline", record))
            .collect::<Vec<_>>();
        mutations.push(Self::mutation("project", current));
        self.archive.apply(mutations).await?;
        Ok(first)
    }

    async fn derive_window(
        &self,
        project: &Value,
        payload: &Value,
        plan: Option<&ori_monitoring::ScanPlan>,
        band: Option<&ori_monitoring::ScanBand>,
        index: u32,
        selection: ori_archive::BaselineSelection,
    ) -> Result<(Value, (u64, String))> {
        let id = s(project, "id")?;
        let mut sweeps = Vec::with_capacity(selection.sweep_ids.len());
        for source in &selection.sweep_ids {
            sweeps.push(decode::<Sweep>(
                &self
                    .archive
                    .get_sweep(source)
                    .await?
                    .ok_or(GuardError::NotFound)?,
            )?);
        }
        for sweep in &sweeps {
            if !source_qualification(project, sweep)?.eligible() {
                return Err(unverified_reference());
            }
        }
        if let (Some(plan), Some(b)) = (plan, band) {
            for sweep in &sweeps {
                let (actual, window) = crate::scan_plan::source_scope(project, plan, sweep)?;
                if b.id != actual.id || window != index {
                    return Err(invalid("Исходник не совпадает с сеткой поддиапазона"));
                }
            }
        }
        let name = text_with_label(payload, "name", "Название эталона", 200)?;
        let location = text_with_label(payload, "location", "Место", 200)?;
        let derived = build_baseline(&sweeps)?;
        let mut baseline = value(&derived)?;
        let fields = baseline
            .as_object_mut()
            .ok_or_else(|| invalid("baseline"))?;
        fields.insert("name".into(), json!(name));
        fields.insert("location".into(), json!(location));
        baseline = self.record(Some(id), baseline)?;
        baseline["confirmed"] = json!(true);
        if project["primary_source_kind"].is_string() {
            baseline["primary_source_kind"] = project["primary_source_kind"].clone();
            baseline["level_unit"] = json!("dBm");
        }
        baseline["sweep_period_seconds"] = json!(selection.sweep_period_seconds);
        baseline["sample_strategy"] = json!("uniform_window_plus_latest_twenty");
        if let Some(band) = &band {
            if band.step_hz.is_some() {
                baseline["scan_window_index"] = json!(index);
                baseline["scan_window_count"] = json!(band.window_count());
                baseline["sample_strategy"] =
                    json!("latest_twenty_window_passes_plus_earliest_if_needed");
            }
            baseline["scan_band_id"] = json!(band.id);
            baseline["scan_plan_id"] = project["scan_plan"]["id"].clone();
            baseline["scan_plan_revision"] = project["scan_plan"]["revision"].clone();
        }
        baseline["preparation"] = project
            .get("preparation")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let positions = self.archive.capture_positions(&selection.sweep_ids).await?;
        let position = positions
            .into_values()
            .max_by_key(|p| p.rowid)
            .ok_or_else(|| invalid("Нет подтверждённых источников"))?;
        let cursor = if band.is_some_and(|b| b.step_hz.is_some()) {
            position.id
        } else {
            sweeps
                .last()
                .expect("validated baseline sources")
                .id
                .clone()
        };
        Ok((baseline, (position.rowid, cursor)))
    }
    pub async fn add_allowed(&self, id: &str, payload: Value) -> Result<Value> {
        let _life = self.lock_project(id).await?;
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        self.project(id).await?;
        fields(
            &payload,
            &["name", "start_hz", "end_hz", "comment", "watch_changes"],
            false,
        )
        .map_err(|_| invalid("Неизвестное поле разрешённого сигнала"))?;
        if self
            .archive
            .list_monitoring(
                "allowed",
                ori_archive::MonitoringQuery {
                    project_id: Some(id.into()),
                    limit: 1000,
                    ..Default::default()
                },
            )
            .await?
            .len()
            >= 1000
        {
            return Err(invalid("Предел каталога: 1000 правил"));
        }
        let range_error = || invalid("Требуется допустимый диапазон в целых Гц");
        let start = integer(&payload, "start_hz").map_err(|_| range_error())?;
        let end = integer(&payload, "end_hz").map_err(|_| range_error())?;
        if start > end || end > 20_000_000_000 {
            return Err(range_error());
        }
        let watch = payload
            .get("watch_changes")
            .cloned()
            .unwrap_or(json!(false));
        if !watch.is_boolean() {
            return Err(invalid("watch_changes должен быть boolean"));
        }
        let name = text_with_label(&payload, "name", "Название", 200)?;
        let comment = truncate(&py_str(payload.get("comment").unwrap_or(&json!(""))), 1000);
        let v=self.record(Some(id),json!({"name":name,"start_hz":start,"end_hz":end,"comment":comment,"watch_changes":watch}))?;
        self.archive.put_monitoring("allowed", v.clone()).await?;
        Ok(v)
    }
    pub async fn detection_baseline(&self, mut baseline: Value) -> Result<Value> {
        if baseline["sweep_period_seconds"]
            .as_f64()
            .is_some_and(|p| p != 0.)
        {
            return Ok(baseline);
        }
        let refs = baseline["source_refs"]
            .as_array()
            .ok_or_else(|| invalid("baseline source_refs"))?;
        if refs.len() > 256 {
            return Err(invalid(
                "Слишком много источников старого эталона; соберите новый эталон",
            ));
        }
        let ids = refs
            .iter()
            .map(|r| s(r, "sweep_id").map(str::to_owned))
            .collect::<Result<Vec<_>>>()?;
        let positions = self.archive.capture_positions(&ids).await?;
        let mut previous: Option<&ori_archive::CapturePosition> = None;
        let mut periods = vec![];
        for source in refs {
            let found = positions.get(s(source, "sweep_id")?).filter(|p| {
                Some(p.session_id.as_str()) == source["session_id"].as_str()
                    && Some(p.stream_id.as_str()) == source["stream_id"].as_str()
            });
            if let (Some(a), Some(b)) = (previous, found)
                && a.session_id == b.session_id
                && a.stream_id == b.stream_id
            {
                let duration = elapsed(&a.started_at, &b.started_at)?;
                if duration > 0. {
                    periods.push(duration);
                }
            }
            previous = found;
        }
        baseline["sweep_period_seconds"] = json!(median(periods));
        Ok(baseline)
    }
    // Called while holding the commit coordinator, including when a retained
    // original only advances the cursor and never touches a detector.
    async fn current_detector_project(&self, id: &str, project: &Value) -> Result<Value> {
        let current = self.project(id).await?;
        if [
            "detector_cursor",
            "detector_state",
            "band_detectors",
            "detector_rowid",
            "baseline_id",
        ]
        .iter()
        .any(|key| current.get(*key) != project.get(*key))
        {
            return Err(GuardError::Conflict(
                "Курсор детектора изменился во время анализа".into(),
            ));
        }
        Ok(current)
    }
    pub async fn analyze(&self, id: &str) -> Result<Value> {
        let _life = self.lock_project(id).await?;
        self.analyze_locked(id).await
    }
    pub(crate) async fn analyze_locked(&self, id: &str) -> Result<Value> {
        self.ensure_open()?;
        let mut project = self.project(id).await?;
        let scan_plan = crate::scan_plan::configured_plan(&project)?;
        if !crate::scan_plan::all_baselines_ready(&project)? {
            return Err(invalid(if scan_plan.is_some() {
                "Подтвердите эталон каждого поддиапазона"
            } else {
                "Нужен подтверждённый эталон"
            }));
        }
        if project["primary_source_kind"] == "ardv1" {
            return self.analyze_receiver(project).await;
        }
        let survey = crate::rfe::survey(&project);
        let survey_reference = if survey {
            Some(
                self.get_record("baseline", s(&project, "baseline_id")?, Some(id))
                    .await?,
            )
        } else {
            None
        };
        let parameters: Parameters = decode(&project["parameters"])?;
        // A full survey has 449 windows. Load only the windows touched by this
        // bounded ingestion batch, keeping checkpoints separate in the archive.
        let mut detectors = BTreeMap::new();
        let allowed = self
            .archive
            .list_monitoring(
                "allowed",
                ori_archive::MonitoringQuery {
                    project_id: Some(id.into()),
                    limit: 1000,
                    ..Default::default()
                },
            )
            .await?
            .iter()
            .map(decode::<AllowedSignal>)
            .collect::<Result<Vec<_>>>()?;
        let pending = self
            .archive
            .legacy_pending_sweeps(
                s(&project, "active_session_id")?,
                s(&project, "analyzer_stream_id")?,
                project["detector_rowid"].as_u64().unwrap_or(0),
                64,
            )
            .await?;
        let mut events = BTreeSet::new();
        let mut processed = 0;
        let mut excluded = 0;
        let mut quality_issues = 0;
        for capture in pending {
            let sweep: Sweep = decode(
                &self
                    .archive
                    .get_sweep(&capture.id)
                    .await?
                    .ok_or(GuardError::NotFound)?,
            )?;
            let source_scope = scan_plan
                .as_ref()
                .map(|p| crate::scan_plan::source_scope(&project, p, &sweep))
                .transpose()?;
            if !source_qualification(&project, &sweep)?.eligible()
                || (survey_reference.is_some()
                    && self
                        .archive
                        .rfe_guard_source_context(&sweep.id)
                        .await?
                        .as_deref()
                        != survey_reference
                            .as_ref()
                            .and_then(|r| r["context_key"].as_str()))
            {
                let _tx = self.coordinator.lock().await;
                self.ensure_open()?;
                project = self.current_detector_project(id, &project).await?;
                project["detector_cursor"] = json!(sweep.id);
                project["detector_rowid"] = json!(capture.rowid);
                self.archive
                    .put_monitoring("project", project.clone())
                    .await?;
                excluded += 1;
                continue;
            }
            let key = if survey {
                crate::rfe::window_id(&project, &sweep)?
            } else {
                source_scope.map_or_else(String::new, |(b, w)| b.context_key(w))
            };
            let grid = source_scope.is_some_and(|(b, _)| b.step_hz.is_some());
            let baseline_id = if survey {
                key.as_str()
            } else if source_scope.is_some() {
                s(&project["band_baselines"], &key)?
            } else {
                s(&project, "baseline_id")?
            }
            .to_owned();
            if let std::collections::btree_map::Entry::Vacant(entry) = detectors.entry(key.clone())
            {
                let baseline = self
                    .detection_baseline(
                        self.get_record(
                            if survey {
                                crate::aor::BLOCK
                            } else {
                                "baseline"
                            },
                            &baseline_id,
                            Some(id),
                        )
                        .await?,
                    )
                    .await?;
                if let Some((band, index)) = source_scope {
                    crate::scan_plan::check_baseline(&project, band, index, &baseline)?;
                }
                let checkpoint = if survey {
                    let generation = project["survey_generation"]
                        .as_str()
                        .unwrap_or(&sweep.session_id);
                    self.archive
                        .monitoring(
                            "scan-window-detector",
                            &format!("{baseline_id}.{generation}"),
                        )
                        .await?
                        .map(|v| v["checkpoint"].clone())
                } else if grid {
                    if let Some(reference) = project["band_detectors"][&key].as_str() {
                        if reference != baseline_id {
                            return Err(invalid("Контрольная точка другого эталона"));
                        }
                        Some(
                            self.get_record("scan-window-detector", reference, Some(id))
                                .await?["checkpoint"]
                                .clone(),
                        )
                    } else {
                        None
                    }
                } else if source_scope.is_some() {
                    project["band_detectors"]
                        .get(&key)
                        .filter(|s| !s.is_null())
                        .cloned()
                } else {
                    project
                        .get("detector_state")
                        .filter(|s| !s.is_null())
                        .cloned()
                };
                entry.insert(Detector::new(
                    decode::<Baseline>(&baseline)?,
                    parameters.clone(),
                    checkpoint,
                )?);
            }
            let detector = detectors.get_mut(&key).expect("loaded detector");
            let step = detector.step(&sweep, &allowed)?;
            // Receiver results and operator dispositions share these event rows.
            // Read the latest rows under the same coordinator that commits them.
            let _tx = self.coordinator.lock().await;
            self.ensure_open()?;
            project = self.current_detector_project(id, &project).await?;
            let mut opens = self
                .archive
                .open_events(id, &sweep.session_id, &baseline_id)
                .await?;
            let mut mutations = vec![];
            if let Some(quality) = detector.take_quality_issue() {
                let mut q = value(&quality)?;
                q["sweep_id"] = json!(sweep.id);
                q["session_id"] = json!(sweep.session_id);
                q["stream_id"] = json!(sweep.stream_id);
                mutations.push(Self::mutation("quality", self.record(Some(id), q)?));
                quality_issues += 1;
                for mut event in opens.drain(..) {
                    event["state"] = json!("interrupted");
                    event["end_reason"] = json!("data_gap");
                    mutations.push(Self::mutation("event", event));
                }
                if matches!(
                    project["state"].as_str(),
                    Some("armed" | "searching" | "degraded")
                ) {
                    mutations.push(ori_archive::Mutation::EnqueueNotification(
                        self.notification(
                            &project,
                            &sweep.id,
                            "data_gap",
                            "warning",
                            "Разрыв измерений; подтверждение 3/5 начато заново",
                            None,
                        )?,
                    ));
                }
            }
            let mut touched = BTreeSet::new();
            let active_dispatch = self.active_event_dispatch(id)?;
            for region in &step.regions {
                let rv = value(region)?;
                let broad = |v: &Value| {
                    matches!(
                        v["kind"].as_str(),
                        Some("broadband_change" | "dense_change")
                    )
                };
                let found = opens.iter().position(|e| {
                    e["start_frequency_hz"]
                        .as_u64()
                        .is_some_and(|x| x <= region.end_frequency_hz)
                        && e["end_frequency_hz"]
                            .as_u64()
                            .is_some_and(|x| x >= region.start_frequency_hz)
                        && broad(e) == broad(&rv)
                });
                let evidence = detector
                    .state()
                    .recent_sources
                    .as_ref()
                    .into_iter()
                    .flatten()
                    .zip(&detector.state().window)
                    .filter(|(_, row)| {
                        row.get(region.first_bin..=region.last_bin)
                            .is_some_and(|r| r.iter().any(|x| *x))
                    })
                    .map(|(r, _)| value(r))
                    .collect::<Result<Vec<_>>>()?;
                let mut event = if let Some(i) = found {
                    opens[i].clone()
                } else {
                    let mut fields = rv.clone();
                    fields["state"] = json!("open");
                    fields["disposition"] = json!("unreviewed");
                    fields["run_session_id"] = json!(sweep.session_id);
                    fields["first_seen"] = evidence
                        .first()
                        .map(|v| v["started_at"].clone())
                        .unwrap_or_else(|| json!(sweep.interval.started_at.as_str()));
                    fields["source_count"] = json!(0);
                    fields["source_refs"] = json!([]);
                    fields["baseline_id"] = json!(baseline_id);
                    if let Some((band, index)) = source_scope {
                        fields["scan_band_id"] = json!(band.id);
                        if grid {
                            fields["scan_window_index"] = json!(index);
                        }
                        fields["scan_plan_id"] = project["scan_plan"]["id"].clone();
                        fields["scan_plan_revision"] = project["scan_plan"]["revision"].clone();
                    }
                    if project["primary_source_kind"].is_string() {
                        fields["primary_source_kind"] = project["primary_source_kind"].clone();
                        fields["level_unit"] = json!("dBm");
                    }
                    if survey {
                        fields["rfe_window_index"] = json!(
                            sweep
                                .settings
                                .as_ref()
                                .and_then(|s| s.get("survey"))
                                .and_then(|s| s["tile"].as_u64())
                                .unwrap()
                                - 1
                        );
                        fields["reference_set_id"] = project["baseline_id"].clone();
                    }
                    fields["detector_version"] = json!(ori_monitoring::VERSION);
                    fields["detector_parameters"] = project["parameters"].clone();
                    self.record(Some(id), fields)?
                };
                let higher_peak =
                    region.peak_excess_db > event["peak_excess_db"].as_f64().unwrap_or(-1.);
                if higher_peak {
                    event
                        .as_object_mut()
                        .ok_or_else(|| invalid("event"))?
                        .extend(rv.as_object().ok_or_else(|| invalid("region"))?.clone());
                }
                event["last_seen"] = json!(sweep.interval.ended_at.as_str());
                event["quiet_sweeps"] = json!(0);
                let event_id = s(&event, "id")?.to_owned();
                if found.is_none() || higher_peak {
                    // Keep peak provenance when the event's recent source list rolls over.
                    mutations.push(Self::mutation("event-peak", json!({
                        "id": event_id, "project_id": id, "created_at": event["created_at"],
                        "session_id": sweep.session_id, "stream_id": sweep.stream_id, "sweep_id": sweep.id,
                        "center_frequency_hz": event["center_frequency_hz"], "peak_level": event["peak_level"]
                    })));
                }
                if higher_peak
                    && let Some(dispatch) = active_dispatch.as_ref()
                    && dispatch.event_id == event_id
                    && event["center_frequency_hz"] != dispatch.center_hz
                {
                    dispatch.retargeted.store(true, Ordering::Release);
                }
                let mut count = integer(&event, "source_count")?;
                let mut refs = event["source_refs"]
                    .as_array()
                    .cloned()
                    .ok_or_else(|| invalid("source_refs"))?;
                let source_record = |reference: &Value| -> Result<Value> {
                    let mut v = reference.clone();
                    v["id"] = json!(format!("{event_id}.{}", s(reference, "sweep_id")?));
                    v["project_id"] = json!(id);
                    v["created_at"] = event["created_at"].clone();
                    v["event_id"] = json!(event_id);
                    Ok(v)
                };
                if count == 0 {
                    for prior in evidence.iter().take(evidence.len().saturating_sub(1)) {
                        let mut source = prior.clone();
                        source
                            .as_object_mut()
                            .ok_or_else(|| invalid("source"))?
                            .remove("started_at");
                        mutations.push(Self::mutation("event-source", source_record(&source)?));
                        refs.push(source);
                        count = count
                            .checked_add(1)
                            .ok_or_else(|| invalid("source_count"))?;
                    }
                }
                if !touched.contains(&event_id) {
                    let source = json!({"session_id":sweep.session_id,"stream_id":sweep.stream_id,"sweep_id":sweep.id});
                    mutations.push(Self::mutation("event-source", source_record(&source)?));
                    count = count
                        .checked_add(1)
                        .ok_or_else(|| invalid("source_count"))?;
                    refs.push(source);
                    if refs.len() > 100 {
                        refs.drain(..refs.len() - 100);
                    }
                }
                event["source_count"] = json!(count);
                event["source_refs"] = json!(refs);
                if matches!(
                    project["state"].as_str(),
                    Some("armed" | "searching" | "degraded")
                ) {
                    let sustained = (region.first_bin..=region.last_bin).any(|bin| {
                        detector
                            .state()
                            .window
                            .iter()
                            .rev()
                            .take(5)
                            .filter(|row| row[bin])
                            .count()
                            >= 3
                    });
                    let occurrence = if sustained {
                        "sustained"
                    } else if found.is_some() {
                        "repeat"
                    } else {
                        "impulse"
                    };
                    mutations.push(
                        self.duty_detection(&project, &mut event, occurrence, &sweep.id)
                            .await?,
                    );
                    mutations.push(ori_archive::Mutation::EnqueueNotification(
                        self.event_notification(&project, &event)?,
                    ));
                }
                mutations.push(Self::mutation("event", event.clone()));
                if let Some(index) = found {
                    opens[index] = event;
                }
                touched.insert(event_id.clone());
                events.insert(event_id);
            }
            for mut event in opens {
                let event_id = s(&event, "id")?;
                if touched.contains(event_id) {
                    continue;
                }
                let lo = integer(&event, "first_bin")? as usize;
                let hi = integer(&event, "last_bin")? as usize;
                if lo > hi || hi >= step.excess_db.len() {
                    return Err(invalid("event bin range"));
                }
                if (lo..=hi).all(|i| {
                    step.excess_db[i]
                        < step.thresholds_db[i]
                            - number(&project["parameters"], "hysteresis_db").unwrap_or(3.)
                }) {
                    let quiet = event["quiet_sweeps"]
                        .as_u64()
                        .unwrap_or(0)
                        .checked_add(1)
                        .ok_or_else(|| invalid("quiet_sweeps"))?;
                    event["quiet_sweeps"] = json!(quiet);
                    if quiet >= 5 {
                        event["state"] = json!("closed");
                    }
                } else {
                    event["quiet_sweeps"] = json!(0);
                }
                mutations.push(Self::mutation("event", event));
            }
            project["detector_cursor"] = json!(sweep.id);
            project["detector_rowid"] = json!(capture.rowid);
            if survey {
                let tile = sweep
                    .settings
                    .as_ref()
                    .and_then(|s| s.get("survey"))
                    .and_then(|s| s["tile"].as_u64())
                    .expect("validated RFE window");
                project["detector_state"] = json!({"scan_window_index":tile-1,"recent_sources":[{"session_id":sweep.session_id,"stream_id":sweep.stream_id,"sweep_id":sweep.id}]});
                let generation = project["survey_generation"]
                    .as_str()
                    .unwrap_or(&sweep.session_id);
                mutations.push(Self::mutation("scan-window-detector",json!({"id":format!("{baseline_id}.{generation}"),"project_id":id,
                    "created_at":sweep.interval.started_at.as_str(),"checkpoint":detector.checkpoint()?})));
            } else if grid {
                let record = json!({"id":baseline_id,"project_id":id,"created_at":detector.baseline().ended_at,
                    "updated_at":sweep.interval.ended_at.as_str(),"checkpoint":detector.checkpoint()?});
                mutations.push(Self::mutation("scan-window-detector", record));
                project["band_detectors"][&key] = json!(baseline_id);
                // Keep one current anchor per logical band for the idle receiver.
                // Its detector history is still isolated in the window checkpoint.
                let (band, index) = source_scope.expect("grid source scope");
                project["band_detectors"][&band.id] = json!({"scan_window_index":index,"recent_sources":[{
                    "session_id":sweep.session_id,"stream_id":sweep.stream_id,"sweep_id":sweep.id
                }]});
            } else if source_scope.is_some() {
                project["band_detectors"][&key] = detector.checkpoint()?;
            } else {
                project["detector_state"] = detector.checkpoint()?;
            }
            mutations.push(Self::mutation("project", project.clone()));
            self.archive.apply(mutations).await?;
            processed += 1;
        }
        let mut result = json!({"project_id":id,"event_ids":events.iter().take(100).collect::<Vec<_>>(),"events_updated":events.len(),"processed_sweeps":processed,"quality_issues":quality_issues,"cursor":project.get("detector_cursor")});
        if excluded > 0 {
            result["excluded_sweeps"] = json!(excluded);
        }
        Ok(result)
    }
    pub async fn review_event(&self, id: &str, event_id: &str, payload: Value) -> Result<Value> {
        let _life = self.lock_project(id).await?;
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        self.project(id).await?;
        let mut event = self.event_record(id, event_id).await?;
        fields(&payload, &["disposition", "comment"], false)
            .map_err(|_| invalid("Неизвестный операторский вердикт"))?;
        if !matches!(
            payload["disposition"].as_str(),
            Some("unreviewed" | "authorized" | "false_positive" | "investigate")
        ) {
            return Err(invalid("Неизвестный операторский вердикт"));
        }
        event["disposition"] = payload["disposition"].clone();
        event["operator_comment"] = json!(truncate(
            &py_str(payload.get("comment").unwrap_or(&json!(""))),
            1000
        ));
        if payload["disposition"] != "unreviewed"
            && let Some(dispatch) = self.active_event_dispatch(id)?
            && dispatch.event_id == event_id
        {
            dispatch.reviewed.store(true, Ordering::Release);
        }
        self.archive.put_monitoring("event", event.clone()).await?;
        Ok(event)
    }
    pub async fn event_sources(
        &self,
        id: &str,
        event: &str,
        after: &str,
        limit: usize,
    ) -> Result<Value> {
        self.event_record(id, event).await?;
        if !(1..=64).contains(&limit) {
            return Err(invalid("limit должен быть 1..64"));
        }
        value(&self.archive.event_sources(id, event, after, limit).await?)
    }
}
