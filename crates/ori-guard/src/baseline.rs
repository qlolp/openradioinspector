use crate::{GuardError, GuardService, Result, util::*};
use ori_contracts::Sweep;
use ori_monitoring::{AllowedSignal, Baseline, Detector, Parameters, build_baseline};
use serde_json::{Value, json};
use std::collections::BTreeSet;

impl GuardService {
    pub async fn create_baseline(&self, id: &str, payload: Value) -> Result<Value> {
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        // The public Python resource resolves the project before confirmation.
        let project = self.project(id).await?;
        fields(&payload, &["name", "location", "confirm"], false)
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
        let selection = self
            .archive
            .baseline_selection(
                s(&project, "active_session_id")?,
                s(&project, "analyzer_stream_id")?,
            )
            .await?;
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
        let name = text_with_label(&payload, "name", "Название эталона", 200)?;
        let location = text_with_label(&payload, "location", "Место", 200)?;
        let derived = build_baseline(&sweeps)?;
        let mut baseline = value(&derived)?;
        let fields = baseline
            .as_object_mut()
            .ok_or_else(|| invalid("baseline"))?;
        fields.insert("name".into(), json!(name));
        fields.insert("location".into(), json!(location));
        baseline = self.record(Some(id), baseline)?;
        baseline["confirmed"] = json!(true);
        baseline["sweep_period_seconds"] = json!(selection.sweep_period_seconds);
        baseline["sample_strategy"] = json!("uniform_window_plus_latest_twenty");
        baseline["preparation"] = project
            .get("preparation")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let positions = self.archive.capture_positions(&selection.sweep_ids).await?;
        let rowid = positions.values().map(|v| v.rowid).max().unwrap_or(0);
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        let mut current = self.project(id).await?;
        if current["active_session_id"] != project["active_session_id"]
            || self.runtime_snapshot(id)?.is_some()
        {
            return Err(GuardError::Conflict(
                "Проект изменился во время построения эталона".into(),
            ));
        }
        current["baseline_id"] = baseline["id"].clone();
        current["state"] = json!("ready");
        current["detector_state"] = Value::Null;
        current["detector_cursor"] = sweeps.last().map(|s| json!(s.id)).unwrap_or(Value::Null);
        current["detector_rowid"] = json!(rowid);
        self.archive
            .apply(vec![
                Self::mutation("baseline", baseline.clone()),
                Self::mutation("project", current),
            ])
            .await?;
        Ok(baseline)
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
    pub async fn analyze(&self, id: &str) -> Result<Value> {
        let _life = self.lock_project(id).await?;
        self.analyze_locked(id).await
    }
    pub(crate) async fn analyze_locked(&self, id: &str) -> Result<Value> {
        self.ensure_open()?;
        let mut project = self.project(id).await?;
        let baseline_id = project["baseline_id"]
            .as_str()
            .ok_or_else(|| invalid("Нужен подтверждённый эталон"))?
            .to_owned();
        let baseline_value = self
            .detection_baseline(self.get_record("baseline", &baseline_id, Some(id)).await?)
            .await?;
        let baseline: Baseline = decode(&baseline_value)?;
        let parameters: Parameters = decode(&project["parameters"])?;
        let mut detector = Detector::new(
            baseline,
            parameters,
            project
                .get("detector_state")
                .filter(|v| !v.is_null())
                .cloned(),
        )?;
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
        let mut quality_issues = 0;
        for capture in pending {
            let sweep: Sweep = decode(
                &self
                    .archive
                    .get_sweep(&capture.id)
                    .await?
                    .ok_or(GuardError::NotFound)?,
            )?;
            let step = detector.step(&sweep, &allowed)?;
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
                    fields["detector_version"] = json!(ori_monitoring::VERSION);
                    fields["detector_parameters"] = project["parameters"].clone();
                    self.record(Some(id), fields)?
                };
                if region.peak_excess_db > event["peak_excess_db"].as_f64().unwrap_or(-1.) {
                    event
                        .as_object_mut()
                        .ok_or_else(|| invalid("event"))?
                        .extend(rv.as_object().ok_or_else(|| invalid("region"))?.clone());
                }
                event["last_seen"] = json!(sweep.interval.ended_at.as_str());
                event["quiet_sweeps"] = json!(0);
                let event_id = s(&event, "id")?.to_owned();
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
            let expected_cursor = project.get("detector_cursor").cloned();
            let expected_state = project.get("detector_state").cloned();
            let expected_rowid = project.get("detector_rowid").cloned();
            let _tx = self.coordinator.lock().await;
            self.ensure_open()?;
            let current = self.project(id).await?;
            if current.get("detector_cursor") != expected_cursor.as_ref()
                || current.get("detector_state") != expected_state.as_ref()
                || current.get("detector_rowid") != expected_rowid.as_ref()
                || current["baseline_id"] != project["baseline_id"]
            {
                return Err(GuardError::Conflict(
                    "Курсор детектора изменился во время анализа".into(),
                ));
            }
            project = current;
            project["detector_cursor"] = json!(sweep.id);
            project["detector_rowid"] = json!(capture.rowid);
            project["detector_state"] = detector.checkpoint()?;
            mutations.push(Self::mutation("project", project.clone()));
            self.archive.apply(mutations).await?;
            processed += 1;
        }
        Ok(
            json!({"project_id":id,"event_ids":events.iter().take(100).collect::<Vec<_>>(),"events_updated":events.len(),"processed_sweeps":processed,"quality_issues":quality_issues,"cursor":project.get("detector_cursor")}),
        )
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
