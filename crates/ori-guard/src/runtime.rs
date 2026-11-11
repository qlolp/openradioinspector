//! Receive-only ownership and lifecycle. Every callback runs outside the shared
//! archive coordinator and outside synchronous policy/runtime locks.
use crate::{service::Ownership, util::*, *};
use ori_archive::{EventDraft, Mutation};
use ori_contracts::Session;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    sync::atomic::Ordering,
    time::Duration,
};
use tokio::task::JoinHandle;

pub(crate) const MAX_ACTIVE_PROJECTS: usize = 64;
#[derive(Clone)]
pub(crate) struct ManualLease {
    pub token: String,
    pub role: String,
    pub expires: Duration,
    pub returning: bool,
    pub worker_instance: Option<String>,
    pub control: std::sync::Arc<crate::grants::ManualControlState>,
}
#[derive(Clone)]
pub(crate) struct Preparation {
    pub sid: String,
}
#[derive(Clone)]
pub(crate) struct ProjectRuntime {
    pub token: String,
    pub sid: String,
    pub analyzer: String,
    pub receiver: String,
    pub epoch: u64,
    pub analyzer_epoch: u64,
    pub stopped: bool,
    pub manual: BTreeMap<String, ManualLease>,
    pub claims: BTreeSet<(String, String)>,
    pub receiver_busy: bool,
    pub error: Option<String>,
    pub warning: Option<String>,
    pub faults: BTreeMap<String, Option<String>>,
    pub search: Option<Value>,
    pub search_error: Option<String>,
    pub critical_alert: Option<Value>,
    pub cached_project: Value,
    pub storage_checked: Option<Duration>,
}
pub(crate) struct RuntimeState {
    pub projects: HashMap<String, ProjectRuntime>,
    pub preparing: HashMap<String, Preparation>,
}
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum JobKind {
    Receiver,
    Restore,
    Supervisor,
}
pub(crate) type Jobs =
    BTreeMap<(String, JobKind), std::sync::Arc<tokio::sync::Mutex<Option<JoinHandle<Result<()>>>>>>;
impl GuardService {
    pub(crate) fn runtime_snapshot(&self, id: &str) -> Result<Option<ProjectRuntime>> {
        Ok(self
            .runtime
            .lock()
            .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?
            .projects
            .get(id)
            .cloned())
    }
    pub(crate) fn permit(&self, id: &str, r: &ProjectRuntime) -> ControlPermit {
        ControlPermit {
            project_id: id.into(),
            token: r.token.clone(),
            epoch: r.epoch,
            expected_worker_instance: None,
        }
    }
    pub(crate) fn target(r: &ProjectRuntime, stream: &str) -> ControlTarget {
        ControlTarget {
            session_id: r.sid.clone(),
            stream_id: stream.into(),
        }
    }
    pub(crate) fn analyzer_permit(&self, id: &str, r: &ProjectRuntime) -> ControlPermit {
        ControlPermit {
            project_id: id.into(),
            token: r.token.clone(),
            epoch: r.analyzer_epoch,
            expected_worker_instance: None,
        }
    }
    pub(crate) fn sync_ownership(&self, id: &str, r: &ProjectRuntime) -> Result<()> {
        let mut owners = self
            .owners
            .write()
            .map_err(|_| GuardError::Conflict("guard policy lock failed".into()))?;
        owners.retain(|_, v| v.project_id != id);
        for claim in &r.claims {
            self.invalidate_unclaimed(claim)?;
            let lease = if claim.0 == r.sid {
                r.manual.get(&claim.1)
            } else {
                None
            };
            owners.insert(
                claim.clone(),
                Ownership {
                    project_id: id.into(),
                    token: lease
                        .map(|l| l.token.clone())
                        .unwrap_or_else(|| r.token.clone()),
                    epoch: if claim.0 == r.sid && claim.1 == r.receiver {
                        r.epoch
                    } else {
                        r.analyzer_epoch
                    },
                    stopped: r.stopped,
                    expires: lease.map(|l| l.expires),
                    returning: lease.is_some_and(|l| l.returning),
                    guard_token: lease.map(|_| r.token.clone()),
                    manual: lease.map(|lease| lease.control.clone()),
                },
            );
        }
        self.control_changed.notify_waiters();
        Ok(())
    }
    pub(crate) fn update_runtime<T>(
        &self,
        id: &str,
        update: impl FnOnce(&mut ProjectRuntime) -> Result<T>,
    ) -> Result<T> {
        let mut all = self
            .runtime
            .lock()
            .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
        let r = all
            .projects
            .get_mut(id)
            .ok_or_else(|| GuardError::Conflict("Охрана не активна".into()))?;
        let result = update(r)?;
        self.sync_ownership(id, r)?;
        Ok(result)
    }
    pub(crate) fn check_job(
        &self,
        id: &str,
        token: &str,
        epoch: u64,
        stream: &str,
    ) -> Result<ProjectRuntime> {
        let r = self.runtime_snapshot(id)?.ok_or(ControlError::Denied)?;
        if self.closed.load(Ordering::Acquire)
            || r.stopped
            || r.token != token
            || r.epoch != epoch
            || r.manual.contains_key(stream)
        {
            return Err(ControlError::Denied.into());
        }
        self.authorize_control(&Self::target(&r, stream), Some(&self.permit(id, &r)))?;
        Ok(r)
    }
    pub(crate) fn spawn_job(
        &self,
        id: &str,
        kind: JobKind,
        future: impl std::future::Future<Output = Result<()>> + Send + 'static,
    ) -> Result<()> {
        self.ensure_open()?;
        let mut jobs = self
            .jobs
            .lock()
            .map_err(|_| GuardError::Conflict("guard task lock failed".into()))?;
        let key = (id.to_owned(), kind);
        if jobs.get(&key).is_some_and(|slot| {
            !slot
                .try_lock()
                .is_ok_and(|h| h.as_ref().is_none_or(|h| h.is_finished()))
        }) {
            return Err(GuardError::Conflict(
                "Предыдущее задание ещё завершается".into(),
            ));
        }
        if jobs.len() > MAX_ACTIVE_PROJECTS * 2 && !jobs.contains_key(&key) {
            return Err(GuardError::Conflict("Предел фоновых заданий охраны".into()));
        }
        jobs.insert(
            key,
            std::sync::Arc::new(tokio::sync::Mutex::new(Some(tokio::spawn(future)))),
        );
        Ok(())
    }
    pub(crate) async fn drain_jobs(&self, id: Option<&str>) -> Result<()> {
        let keys = {
            let jobs = self
                .jobs
                .lock()
                .map_err(|_| GuardError::Conflict("guard task lock failed".into()))?;
            jobs.keys()
                .filter(|(project, _)| id.is_none_or(|id| id == project))
                .cloned()
                .collect::<Vec<_>>()
        };
        for key in keys {
            let slot = self
                .jobs
                .lock()
                .map_err(|_| GuardError::Conflict("guard task lock failed".into()))?
                .get(&key)
                .cloned();
            if let Some(slot) = slot {
                let mut handle_slot = slot.lock().await;
                let Some(handle) = handle_slot.as_mut() else {
                    continue;
                };
                let result = match tokio::time::timeout(Duration::from_secs(15), handle).await {
                    Ok(Ok(result)) => result,
                    Ok(Err(_)) => Err(GuardError::Conflict(
                        "Фоновое задание завершилось аварийно".into(),
                    )),
                    Err(_) => {
                        return Err(GuardError::Conflict(
                            "Фоновое задание не остановлено".into(),
                        ));
                    }
                };
                handle_slot.take();
                let mut jobs = self
                    .jobs
                    .lock()
                    .map_err(|_| GuardError::Conflict("guard task lock failed".into()))?;
                if jobs
                    .get(&key)
                    .is_some_and(|current| std::sync::Arc::ptr_eq(current, &slot))
                {
                    jobs.remove(&key);
                }
                result?;
            }
        }
        Ok(())
    }
    pub(crate) fn clone_session(
        &self,
        p: &Value,
        preparation: bool,
        search: Option<&Value>,
    ) -> Result<Value> {
        let id = self.ids.next_id(IdKind::Session);
        let mut streams = vec![];
        for role in if search.is_some() {
            vec!["analyzer"]
        } else {
            vec!["analyzer", "receiver"]
        } {
            let mut stream = p["profile"][role].clone();
            if !stream.is_object() {
                return Err(invalid("monitoring receive profile"));
            }
            stream["id"] = json!(self.ids.next_id(IdKind::Stream));
            stream["session_id"] = json!(id);
            stream["state"] = json!("disconnected");
            if !stream["settings"].is_object() {
                stream["settings"] = json!({});
            }
            if !preparation && role == "receiver" {
                stream["settings"]["receiver_profile"] = json!("ardv1-monitor-v1");
                stream["settings"]["mode"] =
                    p.get("receiver_mode").cloned().unwrap_or(json!("AUTO"));
            }
            if let Some(search) = search {
                let center = integer(search, "center_frequency_hz")?;
                let span = integer(search, "span_hz")?;
                stream["settings"]["start_hz"] = json!(
                    center
                        .checked_sub(span / 2)
                        .ok_or_else(|| invalid("search frequency range"))?
                );
                stream["settings"]["stop_hz"] = json!(
                    center
                        .checked_add(span - span / 2)
                        .ok_or_else(|| invalid("search frequency range"))?
                );
                if stream["settings"]["adapter"] == "tinysa-serial" {
                    crate::preflight::receive_settings(&stream["settings"])?;
                }
            }
            streams.push(stream);
        }
        let mut metadata = json!({"monitoring_project_id":p["id"]});
        let name = if search.is_some() {
            metadata["source_search"] = json!(true);
            format!("{} · поиск", s(p, "name")?)
        } else if preparation {
            metadata["baseline_collection"] = json!(true);
            format!("{} · эталон", s(p, "name")?)
        } else {
            s(p, "name")?.into()
        };
        Ok(
            json!({"contract_version":1,"id":id,"name":name,"state":"draft","created_at":self.clock.now().as_str(),"started_at":null,"ended_at":null,"metadata":metadata,"streams":streams}),
        )
    }
    pub(crate) fn running_mutations(&self, created: Value) -> Result<(Value, Vec<Mutation>)> {
        let mut running = created.clone();
        running["state"] = json!("running");
        running["started_at"] = json!(self.clock.now().as_str());
        let draft = EventDraft {
            event_type: "session.started".into(),
            occurred_at: self.clock.now().as_str().into(),
            session_id: Some(s(&running, "id")?.into()),
            payload: json!({"session":running}),
        };
        Ok((
            running.clone(),
            vec![
                Mutation::CreateSession(created),
                Mutation::SaveSession {
                    value: running,
                    event: Some(draft),
                },
            ],
        ))
    }
    pub(crate) fn select_project_session(p: &mut Value, session: &Value) -> Result<()> {
        let streams = session["streams"]
            .as_array()
            .filter(|a| a.len() == 2)
            .ok_or_else(|| invalid("monitoring session streams"))?;
        p["active_session_id"] = session["id"].clone();
        p["analyzer_stream_id"] = streams[0]["id"].clone();
        p["receiver_stream_id"] = streams[1]["id"].clone();
        let mut ids = p["session_ids"]
            .as_array()
            .cloned()
            .ok_or_else(|| invalid("session_ids"))?;
        ids.push(session["id"].clone());
        if ids.len() > 128 {
            ids.drain(..ids.len() - 128);
        }
        p["session_ids"] = json!(ids);
        Ok(())
    }
    pub(crate) async fn stop_archived_session(&self, sid: &str) -> Result<()> {
        let _tx = self.coordinator.lock().await;
        let session = self
            .archive
            .get_session(sid)
            .await?
            .ok_or(GuardError::NotFound)?;
        if session["state"] == "running" {
            self.archive
                .stop_session(session, self.clock.now().as_str())
                .await?;
        }
        Ok(())
    }
    pub async fn start_preparation(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &[], true)?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let original = self.project(id).await?;
        let already_preparing = {
            let all = self
                .runtime
                .lock()
                .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
            if all.projects.contains_key(id) {
                return Err(GuardError::Conflict("Сначала снимите охрану".into()));
            }
            all.preparing.contains_key(id)
        };
        if already_preparing {
            return Ok(
                json!({"project":original,"workers":value(&self.control.list_workers(s(&original,"active_session_id")?).await?)?}),
            );
        }
        if self.preflight(id, true).await?["ready"] != true {
            return Err(invalid("Подготовка не завершена"));
        }
        let old = s(&original, "active_session_id")?.to_owned();
        self.ensure_session_unclaimed(id, &old, false)?;
        self.control.stop_session(&old).await?;
        self.stop_archived_session(&old).await?;
        let created = self.clone_session(&original, true, None)?;
        let (session, mut mutations) = self.running_mutations(created)?;
        let token = self.ids.next_id(IdKind::Owner);
        let sid = s(&session, "id")?.to_owned();
        let mut p = original.clone();
        Self::select_project_session(&mut p, &session)?;
        p["state"] = json!("collecting");
        p["coverage"] = json!("preparation");
        {
            let _tx = self.coordinator.lock().await;
            self.ensure_open()?;
            self.ensure_session_unclaimed(id, &old, false)?;
            self.archive
                .apply({
                    mutations.push(Self::mutation("project", p.clone()));
                    mutations
                })
                .await?;
            let mut all = self
                .runtime
                .lock()
                .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
            if all.preparing.len() + all.projects.len() >= MAX_ACTIVE_PROJECTS {
                return Err(GuardError::Conflict(
                    "Предел активных проектов охраны".into(),
                ));
            }
            all.preparing
                .insert(id.into(), Preparation { sid: sid.clone() });
            let mut owners = self
                .owners
                .write()
                .map_err(|_| GuardError::Conflict("guard policy lock failed".into()))?;
            for stream in session["streams"]
                .as_array()
                .ok_or_else(|| invalid("streams"))?
            {
                self.invalidate_unclaimed(&(sid.clone(), s(stream, "id")?.into()))?;
                owners.insert(
                    (sid.clone(), s(stream, "id")?.into()),
                    Ownership {
                        project_id: id.into(),
                        token: token.clone(),
                        epoch: 0,
                        stopped: false,
                        expires: None,
                        returning: false,
                        guard_token: None,
                        manual: None,
                    },
                );
            }
            self.control_changed.notify_waiters();
        }
        let permit = ControlPermit {
            project_id: id.into(),
            token,
            epoch: 0,
            expected_worker_instance: None,
        };
        let result = self
            .control
            .start_session(&decode::<Session>(&session)?, permit)
            .await;
        let statuses = match result {
            Ok(statuses)
                if statuses.len() == 2
                    && statuses.iter().all(|s| s.state != "error" && s.alive) =>
            {
                statuses
            }
            _ => {
                self.stop_preparation_locked(id).await?;
                return Err(GuardError::Conflict(
                    "Не удалось начать приём; проверьте исполнителей".into(),
                ));
            }
        };
        self.ensure_open()?;
        Ok(json!({"project":p,"workers":value(&statuses)?}))
    }
    pub(crate) fn ensure_session_unclaimed(
        &self,
        id: &str,
        sid: &str,
        allow_own_preparation: bool,
    ) -> Result<()> {
        let all = self
            .runtime
            .lock()
            .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
        if all.projects.values().any(|r| r.sid == sid)
            || all
                .preparing
                .iter()
                .any(|(owner, p)| p.sid == sid && (!allow_own_preparation || owner != id))
        {
            return Err(GuardError::Conflict(
                "Сеанс уже принадлежит другому проекту/сбору".into(),
            ));
        }
        if all.projects.len() + all.preparing.len() >= MAX_ACTIVE_PROJECTS {
            return Err(GuardError::Conflict(
                "Предел активных проектов охраны".into(),
            ));
        }
        Ok(())
    }
    pub async fn stop_preparation(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &[], true)?;
        self.ensure_open()?;
        let _life = self.lock_project(id).await?;
        self.stop_preparation_locked(id).await
    }
    pub(crate) async fn stop_preparation_locked(&self, id: &str) -> Result<Value> {
        let prep = self
            .runtime
            .lock()
            .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?
            .preparing
            .get(id)
            .cloned();
        if let Some(prep) = &prep {
            self.control.stop_session(&prep.sid).await?;
            self.stop_archived_session(&prep.sid).await?;
        }
        let _tx = self.coordinator.lock().await;
        let mut p = self.project(id).await?;
        let has_runtime = {
            let all = self
                .runtime
                .lock()
                .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
            all.projects.contains_key(id)
        };
        if !has_runtime {
            p["state"] = json!(if p["baseline_id"].is_null() {
                "draft"
            } else {
                "ready"
            });
            p["coverage"] = json!("stopped");
        }
        self.archive.put_monitoring("project", p.clone()).await?;
        self.runtime
            .lock()
            .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?
            .preparing
            .remove(id);
        if prep.is_some() {
            self.owners
                .write()
                .map_err(|_| GuardError::Conflict("guard policy lock failed".into()))?
                .retain(|_, o| o.project_id != id);
            self.control_changed.notify_waiters();
        }
        drop(_tx);
        Ok(
            json!({"project":p,"workers":value(&self.control.list_workers(s(&p,"active_session_id")?).await?)?}),
        )
    }
    pub async fn arm(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &[], true)?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let original = self.project(id).await?;
        if self.runtime_snapshot(id)?.is_some() {
            return Ok(original);
        }
        if original["baseline_id"].is_null() || self.preflight(id, false).await?["ready"] != true {
            return Err(invalid(
                "Требуются подтверждённый эталон и успешная подготовка",
            ));
        }
        let context = self.guard_acquisition_context(&original).await?;
        let old = s(&original, "active_session_id")?.to_owned();
        self.ensure_session_unclaimed(id, &old, true)?;
        self.control.stop_session(&old).await?;
        self.stop_archived_session(&old).await?;
        let mut created = self.clone_session(&original, false, None)?;
        if let Some(context) = context {
            created["streams"][0]["settings"]["monitoring_instrument_context"] = json!(context);
        }
        let (session, mut mutations) = self.running_mutations(created)?;
        let mut p = original;
        Self::select_project_session(&mut p, &session)?;
        p["state"] = json!("armed");
        p["detector_state"] = Value::Null;
        p["detector_cursor"] = Value::Null;
        p["detector_rowid"] = json!(0);
        p["last_error"] = Value::Null;
        p["coverage"] = json!("wide");
        p["armed_at"] = json!(self.clock.now().as_str());
        let token = self.ids.next_id(IdKind::Owner);
        let sid = s(&session, "id")?.to_owned();
        let analyzer = s(&p, "analyzer_stream_id")?.to_owned();
        let receiver = s(&p, "receiver_stream_id")?.to_owned();
        let runtime = ProjectRuntime {
            token,
            sid: sid.clone(),
            analyzer: analyzer.clone(),
            receiver: receiver.clone(),
            epoch: 0,
            analyzer_epoch: 0,
            stopped: false,
            manual: BTreeMap::new(),
            claims: [(sid.clone(), analyzer), (sid, receiver)].into(),
            receiver_busy: false,
            error: None,
            warning: None,
            faults: BTreeMap::new(),
            search: None,
            search_error: None,
            critical_alert: None,
            cached_project: p.clone(),
            storage_checked: None,
        };
        {
            let _tx = self.coordinator.lock().await;
            self.ensure_open()?;
            self.ensure_session_unclaimed(id, &old, true)?;
            mutations.push(Self::mutation("project", p.clone()));
            self.archive.apply(mutations).await?;
            let mut all = self
                .runtime
                .lock()
                .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
            all.preparing.remove(id);
            all.projects.insert(id.into(), runtime.clone());
            self.sync_ownership(id, &runtime)?;
        }
        match self
            .control
            .start_session(&decode::<Session>(&session)?, self.permit(id, &runtime))
            .await
        {
            Ok(statuses)
                if statuses.len() == 2
                    && statuses.iter().all(|s| s.state != "error" && s.alive) =>
            {
                self.ensure_open()?;
                if self
                    .runtime_snapshot(id)?
                    .is_none_or(|r| r.stopped || r.token != runtime.token)
                {
                    return Err(ControlError::Denied.into());
                }
                Ok(p)
            }
            Err(e) => {
                if self.closed.load(Ordering::Acquire)
                    || self.runtime_snapshot(id)?.is_none_or(|r| r.stopped)
                {
                    return Err(e.into());
                }
                self.fail_runtime(id, &e.to_string()).await?;
                Err(e.into())
            }
            _ => {
                let message = "Не удалось запустить оба прибора; проверьте журнал исполнителей";
                self.fail_runtime(id, message).await?;
                Err(GuardError::Conflict(message.into()))
            }
        }
    }
    pub async fn disarm(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &[], true)?;
        self.ensure_open()?;
        if self.runtime_snapshot(id)?.is_some() {
            self.update_runtime(id, |r| {
                r.stopped = true;
                r.epoch = r
                    .epoch
                    .checked_add(1)
                    .ok_or_else(|| invalid("control epoch"))?;
                r.analyzer_epoch = r
                    .analyzer_epoch
                    .checked_add(1)
                    .ok_or_else(|| invalid("control epoch"))?;
                Ok(())
            })?;
        }
        self.wake.notify_waiters();
        {
            let _life = self.lock_project(id).await?;
            let _tx = self.coordinator.lock().await;
            self.ensure_open()?;
            let mut p = self.project(id).await?;
            p["resume_requested"] = json!(false);
            self.archive.put_monitoring("project", p).await?;
        }
        self.wake.notify_waiters();
        self.cleanup_project(id, false).await
    }
    pub(crate) async fn cleanup_project(&self, id: &str, preserve_intent: bool) -> Result<Value> {
        let r = self.runtime_snapshot(id)?;
        let mut failure = None;
        if let Some(r) = &r {
            for sid in r.claims.iter().map(|(sid, _)| sid).collect::<BTreeSet<_>>() {
                let stopped = match self.control.stop_session(sid).await {
                    Ok(_) => self.stop_archived_session(sid).await,
                    Err(error) => Err(GuardError::Control(error)),
                };
                if failure.is_none()
                    && let Err(error) = stopped
                {
                    failure = Some(error);
                }
            }
        } else {
            let _life = self.lock_project(id).await?;
            self.stop_preparation_locked(id).await?;
        }
        let drained = self.drain_jobs(Some(id)).await;
        if let Some(error) = failure {
            return Err(error);
        }
        drained?;
        let _life = self.lock_project(id).await?;
        let _tx = self.coordinator.lock().await;
        let mut p = self.project(id).await?;
        p["state"] = json!("ready");
        p["coverage"] = json!("stopped");
        if !preserve_intent {
            p["resume_requested"] = json!(false);
        }
        self.archive.put_monitoring("project", p.clone()).await?;
        self.runtime
            .lock()
            .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?
            .projects
            .remove(id);
        self.owners
            .write()
            .map_err(|_| GuardError::Conflict("guard policy lock failed".into()))?
            .retain(|_, o| o.project_id != id);
        self.control_changed.notify_waiters();
        Ok(p)
    }
    pub async fn guard_status(&self, id: &str) -> Result<Value> {
        let (r, mut p) = {
            // A restore commits its project before updating the in-memory lease.
            // Read both sides of that transition under the same coordinator.
            let _tx = self.coordinator.lock().await;
            let r = self.runtime_snapshot(id)?;
            let p = match self.project(id).await {
                Ok(p) => p,
                Err(e) => r.as_ref().map(|r| r.cached_project.clone()).ok_or(e)?,
            };
            (r, p)
        };
        if let Some(r) = &r
            && let Some(error) = &r.error
        {
            p["state"] = json!("error");
            p["coverage"] = json!("error");
            p["last_error"] = json!(error);
        }
        let manual=r.as_ref().map(|r|r.manual.values().map(|m|json!({"role":m.role,"expired":self.clock.monotonic()>=m.expires,"returning":m.returning,"expires_seconds":m.expires.saturating_sub(self.clock.monotonic()).as_secs_f64().round_ties_even() as u64})).collect::<Vec<_>>()).unwrap_or_default();
        let workers = self
            .control
            .list_workers(s(&p, "active_session_id")?)
            .await?;
        Ok(
            json!({"project":p,"active":!self.closed.load(Ordering::Acquire)&&r.as_ref().is_some_and(|r|!r.stopped),"receiver_busy":r.as_ref().is_some_and(|r|r.receiver_busy),"error":r.as_ref().and_then(|r|r.error.as_ref().or(r.warning.as_ref())).map(|e|json!(e)).unwrap_or_else(||p.get("last_error").cloned().unwrap_or(Value::Null)),"manual":manual,"workers":value(&workers)?,"audio_configured":p.get("audio").is_some_and(|a|!a.is_null()),"hardware_validation":"not_run"}),
        )
    }
}
