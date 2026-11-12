use crate::{
    runtime::{JobKind, MAX_ACTIVE_PROJECTS},
    util::*,
    *,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{Arc, atomic::Ordering},
    time::Duration,
};

#[derive(Default)]
pub(crate) struct Startup {
    pub started: bool,
    pub complete: bool,
    pub recovery_error: Option<String>,
    pub delivery_error: Option<String>,
}
pub(crate) struct CloseAttempt {
    done: std::sync::atomic::AtomicBool,
    error: std::sync::Mutex<Option<String>>,
    intent_done: std::sync::atomic::AtomicBool,
    intent_error: std::sync::Mutex<Option<String>>,
    notify: tokio::sync::Notify,
}
#[derive(Clone)]
pub struct GuardShutdown {
    attempt: Arc<CloseAttempt>,
}
impl GuardShutdown {
    pub async fn intent_persisted(&self) -> Result<()> {
        self.wait(true).await
    }
    pub async fn completed(&self) -> Result<()> {
        self.wait(false).await
    }
    async fn wait(&self, intent: bool) -> Result<()> {
        loop {
            let notified = self.attempt.notify.notified();
            if intent && self.attempt.intent_done.load(Ordering::Acquire) {
                return self
                    .attempt
                    .intent_error
                    .lock()
                    .map_err(|_| GuardError::Conflict("guard intent result lock failed".into()))?
                    .as_ref()
                    .map_or(Ok(()), |e| Err(GuardError::Conflict(e.clone())));
            }
            if self.attempt.done.load(Ordering::Acquire) {
                let error =
                    self.attempt.error.lock().map_err(|_| {
                        GuardError::Conflict("guard close result lock failed".into())
                    })?;
                if intent {
                    return Err(GuardError::Conflict(error.clone().unwrap_or_else(|| {
                        "Не подтверждена запись намерения возобновления".into()
                    })));
                }
                return error
                    .as_ref()
                    .map_or(Ok(()), |e| Err(GuardError::Conflict(e.clone())));
            }
            notified.await;
        }
    }
}
impl GuardService {
    pub(crate) async fn owned_close(&self) -> Result<()> {
        self.begin_shutdown()?.completed().await
    }
    pub fn any_armed(&self) -> bool {
        self.owners.read().map_or(true, |owners| !owners.is_empty())
    }
    pub fn begin_shutdown(&self) -> Result<GuardShutdown> {
        tokio::runtime::Handle::try_current().map_err(|_| {
            GuardError::Conflict("Для очистки охраны нужен исполнитель Tokio".into())
        })?;
        let (attempt, launch) = {
            let mut current = self
                .close_attempt
                .lock()
                .map_err(|_| GuardError::Conflict("guard close state lock failed".into()))?;
            let reusable = if let Some(attempt) = current.as_ref() {
                !attempt.done.load(Ordering::Acquire)
                    || attempt
                        .error
                        .lock()
                        .map_err(|_| GuardError::Conflict("guard close result lock failed".into()))?
                        .is_none()
            } else {
                false
            };
            if reusable {
                (current.as_ref().ok_or(ControlError::Denied)?.clone(), false)
            } else {
                let attempt = Arc::new(CloseAttempt {
                    done: std::sync::atomic::AtomicBool::new(false),
                    error: std::sync::Mutex::new(None),
                    intent_done: std::sync::atomic::AtomicBool::new(false),
                    intent_error: std::sync::Mutex::new(None),
                    notify: tokio::sync::Notify::new(),
                });
                *current = Some(attempt.clone());
                (attempt, true)
            }
        };
        if launch {
            if !self.closed.swap(true, Ordering::AcqRel) {
                self.control_changed.notify_waiters();
                let active = {
                    let state = self
                        .runtime
                        .lock()
                        .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
                    state
                        .projects
                        .iter()
                        .filter(|(_, r)| !r.stopped)
                        .map(|(id, _)| id.clone())
                        .collect::<Vec<_>>()
                };
                self.close_active
                    .lock()
                    .map_err(|_| GuardError::Conflict("guard shutdown intent lock failed".into()))?
                    .extend(active);
            }
            let owned = self.weak.upgrade().ok_or(ControlError::Denied)?;
            let completion = attempt.clone();
            tokio::spawn(async move {
                let job = tokio::spawn(async move { owned.close_runtime().await });
                let failure = match job.await {
                    Ok(result) => result.err().map(|e| e.to_string()),
                    Err(_) => Some("Аварийное завершение очистки охраны".into()),
                };
                if let Ok(mut error) = completion.error.lock() {
                    *error = failure;
                    completion.done.store(true, Ordering::Release);
                    completion.notify.notify_waiters();
                }
            });
        }
        Ok(GuardShutdown { attempt })
    }
    fn publish_shutdown_intent(&self, error: Option<&GuardError>) -> Result<()> {
        let attempt = self
            .close_attempt
            .lock()
            .map_err(|_| GuardError::Conflict("guard close state lock failed".into()))?
            .clone()
            .ok_or(ControlError::Denied)?;
        *attempt
            .intent_error
            .lock()
            .map_err(|_| GuardError::Conflict("guard intent result lock failed".into()))? =
            error.map(ToString::to_string);
        attempt.intent_done.store(true, Ordering::Release);
        attempt.notify.notify_waiters();
        Ok(())
    }
    pub async fn start(self: &Arc<Self>) -> Result<()> {
        let mut startup = self.startup.lock().await;
        self.ensure_open()?;
        if startup.started {
            return Ok(());
        }
        let recovered = self.recover_projects().await;
        startup.complete = true;
        startup.recovery_error = recovered
            .as_ref()
            .err()
            .map(|_| "recovery_archive_error".into());
        recovered?;
        startup.started = true;
        let weak = Arc::downgrade(self);
        let wake = self.wake.clone();
        self.spawn_job("", JobKind::Supervisor, async move {
            loop {
                {
                    let Some(guard) = weak.upgrade() else {
                        return Ok(());
                    };
                    if guard.closed.load(Ordering::Acquire) {
                        return Ok(());
                    }
                    let ids = guard
                        .runtime
                        .lock()
                        .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?
                        .projects
                        .iter()
                        .filter(|(_, r)| !r.stopped)
                        .map(|(id, _)| id.clone())
                        .collect::<Vec<_>>();
                    for id in ids {
                        if let Err(error) = guard.tick(&id).await
                            && guard.runtime_snapshot(&id)?.is_some_and(|r| !r.stopped)
                            && guard.fail_runtime(&id, &error.to_string()).await.is_err()
                        {
                            guard.startup.lock().await.delivery_error =
                                Some("monitoring_archive_error".into());
                        }
                    }
                    if guard.closed.load(Ordering::Acquire) {
                        return Ok(());
                    }
                    if guard.deliver_one().await.is_err() {
                        guard.startup.lock().await.delivery_error =
                            Some("notification_archive_error".into());
                    }
                }
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(200)) => {},
                    _ = wake.notified() => {}
                }
            }
        })?;
        Ok(())
    }
    pub async fn startup_status(&self) -> Value {
        let startup = self.startup.lock().await;
        json!({"recovery_complete":startup.complete,"recovery_error":startup.recovery_error,"delivery_error":startup.delivery_error})
    }
    pub async fn recover_projects(&self) -> Result<()> {
        let mut after = String::new();
        loop {
            self.ensure_open()?;
            let projects = self
                .archive
                .list_monitoring(
                    "project",
                    ori_archive::MonitoringQuery {
                        after: after.clone(),
                        limit: 100,
                        ..Default::default()
                    },
                )
                .await?;
            if projects.is_empty() {
                break;
            }
            for original in &projects {
                let id = s(original, "id")?.to_owned();
                let should_resume = {
                    let _life = self.lock_project(&id).await?;
                    self.ensure_open()?;
                    if self.runtime_snapshot(&id)?.is_some() {
                        false
                    } else {
                        let p = self.project(&id).await?;
                        let requested = p["resume_requested"] == true
                            || matches!(
                                p["state"].as_str(),
                                Some("armed" | "searching" | "degraded")
                            );
                        if !requested {
                            false
                        } else {
                            let mut event_after = String::new();
                            loop {
                                let items = self
                                    .archive
                                    .list_monitoring(
                                        "event",
                                        ori_archive::MonitoringQuery {
                                            project_id: Some(id.clone()),
                                            after: event_after.clone(),
                                            limit: 1000,
                                            ..Default::default()
                                        },
                                    )
                                    .await?;
                                if items.is_empty() {
                                    break;
                                }
                                let mut updates = vec![];
                                for mut event in items.iter().cloned() {
                                    if matches!(
                                        event["analysis"]["state"].as_str(),
                                        Some("queued" | "listening" | "decoding")
                                    ) {
                                        event["analysis"]["state"] = json!("interrupted");
                                        event["analysis"]["reason"] = json!(
                                            "Ядро перезапущено; старое задание не повторяется"
                                        );
                                        updates.push(Self::mutation("event", event));
                                    }
                                }
                                if !updates.is_empty() {
                                    let _tx = self.coordinator.lock().await;
                                    self.ensure_open()?;
                                    self.archive.apply(updates).await?;
                                }
                                event_after =
                                    s(items.last().ok_or_else(|| invalid("event page"))?, "id")?
                                        .into();
                            }
                            let _tx = self.coordinator.lock().await;
                            self.ensure_open()?;
                            let mut current = self.project(&id).await?;
                            current["state"] = json!("interrupted");
                            current["coverage"] = json!("stopped");
                            current["resume_requested"] = json!(false);
                            self.archive
                                .put_monitoring("project", current.clone())
                                .await?;
                            current["autostart"] == true
                        }
                    }
                };
                if should_resume && self.arm(&id, json!({})).await.is_err() {
                    let _tx = self.coordinator.lock().await;
                    let mut p = self.project(&id).await?;
                    let message =
                        "Восстановление охраны не прошло проверку приборов/эталона/архива";
                    p["state"] = json!("error");
                    p["last_error"] = json!(message);
                    let note = self.notification(
                        &p,
                        s(&p, "active_session_id")?,
                        "restart_error",
                        "critical",
                        message,
                        None,
                    )?;
                    self.archive
                        .apply(vec![
                            Self::mutation("project", p),
                            ori_archive::Mutation::EnqueueNotification(note),
                        ])
                        .await?;
                }
            }
            after = s(
                projects.last().ok_or_else(|| invalid("project page"))?,
                "id",
            )?
            .into();
        }
        Ok(())
    }
    pub async fn tick(&self, id: &str) -> Result<()> {
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let r = self
            .runtime_snapshot(id)?
            .filter(|r| !r.stopped)
            .ok_or(ControlError::Denied)?;
        let now = self.clock.monotonic();
        if r.storage_checked
            .is_none_or(|at| now.saturating_sub(at) >= Duration::from_secs(2))
        {
            if self.storage_report(id, true).await?["ready"] != true {
                return Err(GuardError::Conflict(
                    "Архив не обеспечивает резерв места/запись/заданный срок хранения".into(),
                ));
            }
            self.update_runtime(id, |r| {
                r.storage_checked = Some(now);
                Ok(())
            })?;
        }
        let statuses = self.control.list_workers(&r.sid).await?;
        self.device_health(id, &statuses).await?;
        let current = self
            .runtime_snapshot(id)?
            .filter(|r| !r.stopped)
            .ok_or(ControlError::Denied)?;
        if !current.manual.contains_key(&current.analyzer) {
            self.analyze_locked(id).await?;
        }
        if self.runtime_snapshot(id)?.is_none_or(|r| r.stopped) {
            return Ok(());
        }
        self.search_tick(id).await?;
        self.queue_receiver(id).await?;
        Ok(())
    }
    async fn device_health(&self, id: &str, statuses: &[WorkerStatus]) -> Result<()> {
        let r = self.runtime_snapshot(id)?.ok_or(ControlError::Denied)?;
        let mut faults = BTreeMap::new();
        for stream in [&r.analyzer, &r.receiver] {
            if r.manual.contains_key(stream) {
                continue;
            }
            let status = statuses.iter().find(|s| s.stream_id == *stream);
            if status.is_none_or(|s| !s.alive) {
                faults.insert(
                    stream.clone(),
                    status
                        .and_then(|w| w.extra.get("started_at"))
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                );
            }
        }
        if faults == r.faults {
            return Ok(());
        }
        let _tx = self.coordinator.lock().await;
        let mut p = self.project(id).await?;
        let current = self
            .runtime_snapshot(id)?
            .filter(|current| !current.stopped && current.token == r.token)
            .ok_or(ControlError::Denied)?;
        let mut changes = vec![];
        if !faults.is_empty() {
            let message = "Часть приборов недоступна; исправный прибор продолжает регистрацию";
            p["state"] = json!("degraded");
            p["last_error"] = json!(message);
            if faults.contains_key(&r.analyzer) {
                p["coverage"] = json!("unavailable");
            }
            for (stream, started) in &faults {
                if current.faults.get(stream) != Some(started) {
                    let worker = statuses.iter().find(|w| w.stream_id == *stream);
                    let device = worker
                        .and_then(|w| w.extra.get("device"))
                        .and_then(Value::as_str)
                        .unwrap_or("unknown");
                    changes.push(ori_archive::Mutation::EnqueueNotification(
                        self.notification(
                            &p,
                            &format!("{stream}:{}", started.as_deref().unwrap_or("None")),
                            "device_unavailable",
                            "critical",
                            &format!("Прибор недоступен: {device}"),
                            None,
                        )?,
                    ));
                }
            }
        } else {
            p["state"] = json!(if r.search.is_some() {
                "searching"
            } else {
                "armed"
            });
            p["last_error"] = Value::Null;
            if p["coverage"] == "unavailable" {
                p["coverage"] = json!("wide");
            }
        }
        changes.push(Self::mutation("project", p.clone()));
        self.archive.apply(changes).await?;
        self.update_runtime(id, |runtime| {
            runtime.warning = if faults.is_empty() {
                None
            } else {
                Some("Часть приборов недоступна; исправный прибор продолжает регистрацию".into())
            };
            runtime.faults = faults;
            runtime.cached_project = p;
            Ok(())
        })?;
        Ok(())
    }
    pub(crate) async fn fail_runtime(&self, id: &str, message: &str) -> Result<()> {
        let message = truncate(message, 2000);
        let r = self.runtime_snapshot(id)?.ok_or(ControlError::Denied)?;
        let mut p = self
            .project(id)
            .await
            .unwrap_or_else(|_| r.cached_project.clone());
        p["state"] = json!("error");
        p["coverage"] = json!("error");
        p["last_error"] = json!(message);
        let critical = self.volatile_error(&p, &message)?;
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
            r.error = Some(message.clone());
            r.critical_alert = Some(critical);
            r.cached_project = p.clone();
            Ok(())
        })?;
        self.wake.notify_waiters();
        let stored = async {
            let _tx = self.coordinator.lock().await;
            let mut current = self.project(id).await?;
            for field in ["state", "coverage", "last_error"] {
                current[field] = p[field].clone();
            }
            let note = self.notification(
                &current,
                s(&current, "active_session_id")?,
                "coverage_error",
                "critical",
                &message,
                None,
            )?;
            self.archive
                .apply(vec![
                    Self::mutation("project", current.clone()),
                    ori_archive::Mutation::EnqueueNotification(note),
                ])
                .await?;
            self.update_runtime(id, |r| {
                r.cached_project = current;
                if let Some(a) = r.critical_alert.as_mut() {
                    a["volatile"] = json!(false);
                }
                Ok(())
            })?;
            Ok::<(), GuardError>(())
        }
        .await;
        let mut stop_error = None;
        for sid in r
            .claims
            .iter()
            .map(|(sid, _)| sid)
            .collect::<std::collections::BTreeSet<_>>()
        {
            if let Err(error) = self.control.stop_session(sid).await {
                stop_error = Some(GuardError::Control(error));
            }
        }
        stored?;
        if let Some(error) = stop_error {
            return Err(error);
        }
        Ok(())
    }
    pub(crate) fn volatile_alert(&self, id: &str) -> Result<Option<Value>> {
        Ok(self.runtime_snapshot(id)?.and_then(|r| r.critical_alert))
    }
    pub(crate) fn acknowledge_volatile(&self, id: &str, alert: &str) -> Result<bool> {
        let mut all = self
            .runtime
            .lock()
            .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
        if let Some(a) = all
            .projects
            .get_mut(id)
            .and_then(|r| r.critical_alert.as_mut())
            .filter(|a| a["id"] == alert)
        {
            a["acknowledged_at"] = json!(self.clock.now().as_str());
            return Ok(a["volatile"] == true);
        }
        Ok(false)
    }
    pub(crate) async fn close_runtime(&self) -> Result<()> {
        let first = !self.closed.swap(true, Ordering::AcqRel);
        let snapshots = {
            let all = self
                .runtime
                .lock()
                .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
            all.projects
                .iter()
                .map(|(id, r)| (id.clone(), r.clone()))
                .collect::<Vec<_>>()
        };
        if first {
            let mut active = self
                .close_active
                .lock()
                .map_err(|_| GuardError::Conflict("guard shutdown intent lock failed".into()))?;
            for (id, r) in &snapshots {
                if !r.stopped {
                    active.insert(id.clone());
                }
            }
        }
        for (id, _) in &snapshots {
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
        let mut failure = None;
        for (id, _) in &snapshots {
            let saved = async {
                let _tx = self.coordinator.lock().await;
                let mut p = self.project(id).await?;
                let was_active = self
                    .close_active
                    .lock()
                    .map_err(|_| GuardError::Conflict("guard shutdown intent lock failed".into()))?
                    .contains(id);
                p["resume_requested"] =
                    json!(p["resume_requested"] == true || was_active && p["autostart"] == true);
                self.archive.put_monitoring("project", p).await?;
                Ok::<(), GuardError>(())
            }
            .await;
            if failure.is_none()
                && let Err(error) = saved
            {
                failure = Some(error);
            }
        }
        self.publish_shutdown_intent(failure.as_ref())?;
        for (id, _) in &snapshots {
            let cleaned = self.cleanup_project(id, true).await;
            if failure.is_none()
                && let Err(error) = cleaned
            {
                failure = Some(error);
            }
        }
        let preparing = self
            .runtime
            .lock()
            .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?
            .preparing
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        for id in preparing {
            let cleaned = async {
                let _life = self.lock_project(&id).await?;
                self.stop_preparation_locked(&id).await?;
                Ok::<(), GuardError>(())
            }
            .await;
            if failure.is_none()
                && let Err(error) = cleaned
            {
                failure = Some(error);
            }
        }
        let drained = self.drain_jobs(None).await;
        let lifecycle_drained = self.drain_lifecycles().await;
        if failure.is_none()
            && let Err(error) = lifecycle_drained
        {
            failure = Some(error);
        }
        let initial = snapshots
            .iter()
            .map(|(id, _)| id.as_str())
            .collect::<std::collections::BTreeSet<_>>();
        let late = {
            let all = self
                .runtime
                .lock()
                .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
            all.projects
                .iter()
                .filter(|(id, _)| !initial.contains(id.as_str()))
                .map(|(id, r)| (id.clone(), r.clone()))
                .collect::<Vec<_>>()
        };
        for (id, r) in late {
            let cleaned = async {
                if !r.stopped {
                    self.close_active
                        .lock()
                        .map_err(|_| {
                            GuardError::Conflict("guard shutdown intent lock failed".into())
                        })?
                        .insert(id.clone());
                }
                self.update_runtime(&id, |r| {
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
                {
                    let _tx = self.coordinator.lock().await;
                    let mut p = self.project(&id).await?;
                    p["resume_requested"] = json!(
                        p["resume_requested"] == true || !r.stopped && p["autostart"] == true
                    );
                    self.archive.put_monitoring("project", p).await?;
                }
                self.cleanup_project(&id, true).await?;
                Ok::<(), GuardError>(())
            }
            .await;
            if failure.is_none()
                && let Err(error) = cleaned
            {
                failure = Some(error);
            }
        }
        let late_preparations = {
            let all = self
                .runtime
                .lock()
                .map_err(|_| GuardError::Conflict("guard runtime lock failed".into()))?;
            all.preparing.keys().cloned().collect::<Vec<_>>()
        };
        for id in late_preparations {
            let cleaned = async {
                let _life = self.lock_project(&id).await?;
                self.stop_preparation_locked(&id).await?;
                Ok::<(), GuardError>(())
            }
            .await;
            if failure.is_none()
                && let Err(error) = cleaned
            {
                failure = Some(error);
            }
        }
        if let Some(error) = failure {
            return Err(error);
        }
        drained?;
        if snapshots.len() > MAX_ACTIVE_PROJECTS {
            return Err(GuardError::Conflict("guard state bound exceeded".into()));
        }
        Ok(())
    }
}

#[cfg(test)]
#[path = "supervisor_tests.rs"]
mod tests;
