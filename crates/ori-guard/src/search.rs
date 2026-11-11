use crate::{util::*, *};
use serde_json::{Value, json};
impl GuardService {
    pub async fn search_start(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &["token", "event_id", "span_hz"], true)
            .map_err(|_| invalid("Нужны token, event_id и span_hz"))?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let (r, _, lease) = self.lease(id, &payload["token"], true).await?;
        if lease.role != "analyzer" {
            return Err(invalid("Поиск использует ручной захват анализатора"));
        }
        if r.search.is_some() {
            return Err(GuardError::Conflict(
                "Сначала верните текущий узкий обзор".into(),
            ));
        }
        let event = self.event_record(id, s(&payload, "event_id")?).await?;
        let span = integer(&payload, "span_hz")
            .map_err(|_| invalid("span_hz должен быть 1000..100000000"))?;
        if !(1000..=100_000_000).contains(&span) {
            return Err(invalid("span_hz должен быть 1000..100000000"));
        }
        let center = integer(&event, "center_frequency_hz")?;
        let search = json!({"event_id":event["id"],"center_frequency_hz":center,"span_hz":span});
        let original = self.project(id).await?;
        let created = self.clone_session(&original, false, Some(&search))?;
        let (session, mut mutations) = self.running_mutations(created)?;
        let sid = s(&session, "id")?.to_owned();
        let stream = session["streams"]
            .as_array()
            .and_then(|a| a.first())
            .ok_or_else(|| invalid("search stream"))?["id"]
            .as_str()
            .ok_or_else(|| invalid("search stream id"))?
            .to_owned();
        let state = json!({"session_id":sid,"stream_id":stream,"event_id":event["id"],"center_frequency_hz":center,"span_hz":span,"detector_rowid":0,"maximum_level":null});
        {
            let _tx = self.coordinator.lock().await;
            self.ensure_open()?;
            let current = self.runtime_snapshot(id)?.ok_or(ControlError::Denied)?;
            if current.token != r.token
                || current.stopped
                || !current.manual.get(&r.analyzer).is_some_and(|m| {
                    m.token == lease.token && !m.returning && self.clock.monotonic() < m.expires
                })
            {
                return Err(ControlError::Denied.into());
            }
            let mut p = self.project(id).await?;
            let mut ids = p["session_ids"]
                .as_array()
                .cloned()
                .ok_or_else(|| invalid("session_ids"))?;
            ids.push(json!(sid));
            if ids.len() > 128 {
                ids.drain(..ids.len() - 128);
            }
            p["session_ids"] = json!(ids);
            p["state"] = json!("searching");
            p["coverage"] = json!("narrow");
            mutations.push(Self::mutation("project", p));
            self.archive.apply(mutations).await?;
            self.update_runtime(id, |runtime| {
                if runtime.token != r.token || runtime.stopped {
                    return Err(ControlError::Denied.into());
                }
                runtime.claims.insert((sid.clone(), stream.clone()));
                runtime.search = Some(state);
                runtime.search_error = None;
                Ok(())
            })?;
        }
        let current = self.runtime_snapshot(id)?.ok_or(ControlError::Denied)?;
        let result = self
            .control
            .start_session(
                &decode::<ori_contracts::Session>(&session)?,
                self.analyzer_permit(id, &current),
            )
            .await;
        if !result.as_ref().is_ok_and(|statuses| {
            statuses.len() == 1 && statuses.iter().all(|s| s.state != "error" && s.alive)
        }) {
            let _tx = self.coordinator.lock().await;
            let mut p = self.project(id).await?;
            p["coverage"] = json!("paused");
            self.archive.put_monitoring("project", p).await?;
            return Err(GuardError::Conflict(
                "Не удалось запустить узкий обзор".into(),
            ));
        }
        self.search_status_locked(id).await
    }
    pub async fn search_status(&self, id: &str) -> Result<Value> {
        let _life = self.lock_project(id).await?;
        self.search_status_locked(id).await
    }
    pub(crate) async fn search_status_locked(&self, id: &str) -> Result<Value> {
        self.search_tick(id).await?;
        let r = self.runtime_snapshot(id)?;
        let search = r.as_ref().and_then(|r| r.search.clone());
        let p = self.project(id).await?;
        let mut series = vec![];
        if let Some(search) = &search {
            let sweeps = self
                .archive
                .list_sweeps(s(search, "session_id")?, Some(s(search, "stream_id")?), 256)
                .await?;
            for raw in sweeps {
                let sweep: ori_contracts::Sweep = decode(&raw)?;
                let peak = peak(&sweep.levels)?;
                series.push(json!({"sweep_id":sweep.id,"started_at":sweep.interval.started_at.as_str(),"ended_at":sweep.interval.ended_at.as_str(),"peak_level":sweep.levels[peak],"frequency_hz":sweep.frequencies_hz[peak]}));
            }
        }
        let search_session = search
            .as_ref()
            .map(|search| s(search, "session_id"))
            .transpose()?;
        let marks = self.archive.position_marks(id, search_session, 256).await?;
        let mut value = search.clone().unwrap_or_else(|| json!({}));
        value["active"] = json!(search.is_some() && p["coverage"] == "narrow");
        value["coverage"] = p.get("coverage").cloned().unwrap_or(json!("stopped"));
        value["series"] = json!(series);
        value["maximum_level"] = search
            .as_ref()
            .and_then(|s| s.get("maximum_level"))
            .cloned()
            .unwrap_or(Value::Null);
        value["marks"] = json!(marks);
        value["error"] = json!(r.and_then(|r| r.search_error));
        Ok(value)
    }
    pub(crate) async fn search_tick(&self, id: &str) -> Result<bool> {
        let r = match self.runtime_snapshot(id)? {
            Some(r) => r,
            None => return Ok(false),
        };
        let mut search = match r.search.clone() {
            Some(s) => s,
            None => return Ok(false),
        };
        let statuses = self.control.list_workers(s(&search, "session_id")?).await?;
        let failed = statuses.is_empty() || statuses.iter().any(|s| !s.alive);
        let returning = r.manual.get(&r.analyzer).is_some_and(|m| m.returning);
        let pending = self
            .archive
            .legacy_pending_sweeps(
                s(&search, "session_id")?,
                s(&search, "stream_id")?,
                search["detector_rowid"].as_u64().unwrap_or(0),
                64,
            )
            .await?;
        let changed = !pending.is_empty();
        let original_cursor = search.get("detector_rowid").cloned();
        for capture in pending {
            let raw = self
                .archive
                .get_sweep(&capture.id)
                .await?
                .ok_or(GuardError::NotFound)?;
            let sweep: ori_contracts::Sweep = decode(&raw)?;
            let level = sweep.levels[peak(&sweep.levels)?];
            let maximum = search["maximum_level"].as_f64();
            if maximum.is_none_or(|m| level > m) {
                search["maximum_level"] = json!(level);
            }
            search["detector_rowid"] = json!(capture.rowid);
            search["detector_cursor"] = json!(capture.id);
        }
        if changed || failed && !returning {
            let _tx = self.coordinator.lock().await;
            let current = self.runtime_snapshot(id)?.ok_or(ControlError::Denied)?;
            if current.token != r.token
                || current.search.as_ref().is_none_or(|s| {
                    s["session_id"] != search["session_id"]
                        || s.get("detector_rowid") != original_cursor.as_ref()
                })
            {
                return Ok(false);
            }
            if changed {
                if search.get("created_at").is_none() {
                    search["created_at"] = json!(self.clock.now().as_str());
                }
                let mut record = search.clone();
                record["id"] = search["session_id"].clone();
                record["project_id"] = json!(id);
                self.archive.put_monitoring("source-search", record).await?;
            }
            if failed && !returning {
                let mut p = self.project(id).await?;
                p["coverage"] = json!("paused");
                p["last_error"] =
                    json!("Узкий обзор остановлен; широкое покрытие остаётся на паузе");
                self.archive.put_monitoring("project", p).await?;
            }
            self.update_runtime(id, |runtime| {
                if runtime.token != r.token {
                    return Err(ControlError::Denied.into());
                }
                runtime.search = Some(search);
                if failed && !returning {
                    runtime.search_error =
                        Some("Узкий обзор остановлен; широкое покрытие остаётся на паузе".into());
                }
                Ok(())
            })?;
        }
        Ok(changed)
    }
    pub async fn search_mark(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &["token", "label"], true).map_err(|_| invalid("Нужны token и label"))?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let (runtime, _, lease) = self.lease(id, &payload["token"], true).await?;
        if lease.role != "analyzer" || runtime.search.is_none() {
            return Err(GuardError::Conflict("Узкий обзор не включён".into()));
        }
        let label = text_with_label(&payload, "label", "Отметка", 200)?;
        let status = self.search_status_locked(id).await?;
        let latest = status["series"]
            .as_array()
            .and_then(|a| a.last())
            .ok_or_else(|| invalid("Ещё нет полного узкого свипа"))?;
        let record=self.record(Some(id),json!({"label":label,"search_session_id":status["session_id"],"event_id":status["event_id"],"sweep_id":latest["sweep_id"],"level":latest["peak_level"],"frequency_hz":latest["frequency_hz"]}))?;
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        let r = self.runtime_snapshot(id)?.ok_or(ControlError::Denied)?;
        if r.stopped
            || r.search
                .as_ref()
                .is_none_or(|s| s["session_id"] != status["session_id"])
            || !r.manual.get(&r.analyzer).is_some_and(|m| {
                m.token == lease.token && !m.returning && self.clock.monotonic() < m.expires
            })
        {
            return Err(ControlError::Denied.into());
        }
        self.archive
            .put_monitoring("position-mark", record.clone())
            .await?;
        Ok(record)
    }
}
fn peak(levels: &[f64]) -> Result<usize> {
    if levels.is_empty() {
        return Err(invalid("empty spectrum"));
    }
    let mut peak = 0;
    for (i, value) in levels.iter().enumerate().skip(1) {
        if *value > levels[peak] {
            peak = i;
        }
    }
    Ok(peak)
}
