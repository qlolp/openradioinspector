use crate::{
    preflight::{MONITOR_MODES, receiver_frequency},
    runtime::{JobKind, ManualLease, ProjectRuntime},
    util::*,
    *,
};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
impl GuardService {
    pub(crate) async fn lease(
        &self,
        id: &str,
        token: &Value,
        current: bool,
    ) -> Result<(ProjectRuntime, String, ManualLease)> {
        let r = self
            .runtime_snapshot(id)?
            .filter(|r| !r.stopped)
            .ok_or_else(|| {
                GuardError::Conflict("Охрана не активна; снимите её для сброса владения".into())
            })?;
        let (stream, lease) = r
            .manual
            .iter()
            .find(|(_, m)| token.as_str() == Some(m.token.as_str()))
            .map(|(s, m)| (s.clone(), m.clone()))
            .ok_or_else(|| GuardError::Conflict("Неверный token ручного владельца".into()))?;
        if current {
            if lease.returning || self.clock.monotonic() >= lease.expires {
                return Err(GuardError::Conflict(
                    "Ручная аренда истекла; обзор/очередь остаётся на паузе до явного возврата"
                        .into(),
                ));
            }
            if lease.role == "receiver" {
                let workers = self.control.list_workers(&r.sid).await?;
                let worker = workers.iter().find(|w| w.stream_id == stream);
                if lease.worker_instance.is_none()
                    || worker.is_none_or(|w| w.worker_instance != lease.worker_instance)
                {
                    return Err(GuardError::Conflict("Соединение приёмника изменилось; требуется явный возврат под управление охраны".into()));
                }
            }
        }
        Ok((r, stream, lease))
    }
    pub async fn manual_acquire(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &["role"], true)
            .map_err(|_| invalid("Выберите роль analyzer/receiver"))?;
        let role = payload["role"]
            .as_str()
            .filter(|role| ["analyzer", "receiver"].contains(role))
            .ok_or_else(|| invalid("Выберите роль analyzer/receiver"))?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let r = self
            .runtime_snapshot(id)?
            .filter(|r| !r.stopped)
            .ok_or_else(|| GuardError::Conflict("Нужна работающая охрана".into()))?;
        let stream = if role == "analyzer" {
            r.analyzer.clone()
        } else {
            r.receiver.clone()
        };
        let workers = self.control.list_workers(&r.sid).await?;
        let instance = workers
            .iter()
            .find(|w| w.session_id == r.sid && w.stream_id == stream)
            .and_then(|w| w.worker_instance.clone());
        if role == "receiver" && instance.is_none() {
            return Err(ControlError::Unavailable("receiver_worker_identity").into());
        }
        let token = self.ids.next_id(IdKind::Owner);
        let manual_control = Arc::new(crate::grants::ManualControlState::new(
            role,
            instance.clone(),
        ));
        {
            let _tx = self.coordinator.lock().await;
            self.ensure_open()?;
            self.project(id).await?;
            self.update_runtime(id, |runtime| {
                if runtime.stopped
                    || runtime.token != r.token
                    || runtime.manual.contains_key(&stream)
                {
                    return Err(GuardError::Conflict(
                        "Прибор уже передан вручную/охрана остановлена".into(),
                    ));
                }
                if role == "receiver" {
                    runtime.epoch = runtime
                        .epoch
                        .checked_add(1)
                        .ok_or_else(|| invalid("control epoch"))?;
                } else {
                    runtime.analyzer_epoch = runtime
                        .analyzer_epoch
                        .checked_add(1)
                        .ok_or_else(|| invalid("control epoch"))?;
                }
                runtime.manual.insert(
                    stream.clone(),
                    ManualLease {
                        token: token.clone(),
                        role: role.into(),
                        expires: self.clock.monotonic() + Duration::from_secs(30),
                        returning: false,
                        worker_instance: instance,
                        control: manual_control,
                    },
                );
                Ok(())
            })?;
        }
        self.wake.notify_waiters();
        if role == "analyzer" {
            let target = Self::target(&r, &stream);
            let original = self.pending_manual_control(&target, &token)?;
            let stopped = self.control.stop_stream(&target).await?;
            self.confirm_manual_acquire_stop(&original, &stopped)?;
            self.confirm_instrument_coverage_locked(&original).await?;
        }
        Ok(json!({"token":token,"role":role,"expires_seconds":30}))
    }
    pub async fn manual_renew(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &["token"], true).map_err(|_| invalid("Нужен только token"))?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let (r, stream, lease) = self.lease(id, &payload["token"], true).await?;
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        self.update_runtime(id, |current| {
            if current.stopped || current.token != r.token {
                return Err(ControlError::Denied.into());
            }
            let m = current
                .manual
                .get_mut(&stream)
                .filter(|m| {
                    m.token == lease.token && !m.returning && self.clock.monotonic() < m.expires
                })
                .ok_or(ControlError::Denied)?;
            m.expires = self.clock.monotonic() + Duration::from_secs(30);
            Ok(())
        })?;
        Ok(json!({"role":lease.role,"expires_seconds":30}))
    }
    pub async fn manual_tune(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &["token", "frequency_hz", "mode"], true)
            .map_err(|_| invalid("Нужны token, frequency_hz и mode"))?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let (r, stream, lease) = self.lease(id, &payload["token"], true).await?;
        if lease.role != "receiver" {
            return Err(invalid("Перестройка относится к receiver"));
        }
        self.project(id).await?;
        let frequency = receiver_frequency(&payload["frequency_hz"])?;
        let mode = payload["mode"]
            .as_str()
            .filter(|mode| MONITOR_MODES.contains(mode))
            .ok_or_else(|| invalid("Недоступный режим приёма"))?;
        let permit = ControlPermit {
            project_id: id.into(),
            token: lease.token,
            epoch: r.epoch,
            expected_worker_instance: lease.worker_instance,
        };
        let target = Self::target(&r, &stream);
        self.authorize_control(&target, Some(&permit))?;
        let reply = self
            .control
            .tune(TuneRequest {
                target: target.clone(),
                permit: permit.clone(),
                frequency_hz: frequency,
                mode: mode.into(),
            })
            .await?;
        let _tx = self.coordinator.lock().await;
        self.authorize_control(&target, Some(&permit))?;
        if reply.acknowledged != Some(true) {
            return Err(ControlError::Unavailable("acknowledged_tune").into());
        }
        self.control.validate_tune(&reply)?;
        Ok(json!({"tuned":value(&reply.tuned)?,"worker":value(&reply.worker)?}))
    }
    pub async fn manual_return(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &["token"], true).map_err(|_| invalid("Нужен только token"))?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let (r, stream, lease) = self.lease(id, &payload["token"], false).await?;
        if lease.returning {
            return Ok(json!({"state":"restoring","role":lease.role}));
        }
        {
            let _tx = self.coordinator.lock().await;
            self.ensure_open()?;
            self.update_runtime(id, |current| {
                if current.stopped || current.token != r.token {
                    return Err(ControlError::Denied.into());
                }
                if lease.role == "receiver" {
                    current.epoch = current
                        .epoch
                        .checked_add(1)
                        .ok_or_else(|| invalid("control epoch"))?;
                } else {
                    current.analyzer_epoch = current
                        .analyzer_epoch
                        .checked_add(1)
                        .ok_or_else(|| invalid("control epoch"))?;
                }
                current
                    .manual
                    .get_mut(&stream)
                    .filter(|m| m.token == lease.token)
                    .ok_or(ControlError::Denied)?
                    .returning = true;
                Ok(())
            })?;
            if lease.role == "analyzer" {
                let mut p = self.project(id).await?;
                p["coverage"] = json!("restoring");
                self.archive.put_monitoring("project", p).await?;
            }
        }
        self.wake.notify_waiters();
        if lease.role == "analyzer" {
            let owned = self.weak.upgrade().ok_or(ControlError::Denied)?;
            let id = id.to_owned();
            let task_id = id.clone();
            let token = r.token.clone();
            let stream = stream.clone();
            self.spawn_job(&id, JobKind::Restore, async move {
                let result = owned.restore_analyzer(&task_id, &token, &stream).await;
                if let Err(error) = &result {
                    if owned
                        .runtime_snapshot(&task_id)?
                        .is_none_or(|r| r.stopped || r.token != token)
                    {
                        return Ok(());
                    }
                    owned.fail_runtime(&task_id, &error.to_string()).await?;
                }
                result
            })?;
            return Ok(json!({"state":"restoring","role":"analyzer"}));
        }
        let result = async {
            let current = self.runtime_snapshot(id)?.ok_or(ControlError::Denied)?;
            let session = self
                .archive
                .get_session(&current.sid)
                .await?
                .ok_or(GuardError::NotFound)?;
            let workers = self.control.list_workers(&current.sid).await?;
            let worker = workers.iter().find(|w| w.stream_id == stream);
            if worker
                .is_none_or(|w| !w.alive || w.current.get("restart_required") == Some(&json!(true)))
            {
                self.control
                    .stop_stream(&Self::target(&current, &stream))
                    .await?;
                self.control
                    .start_stream(
                        &decode::<ori_contracts::Session>(&session)?,
                        &stream,
                        self.permit(id, &current),
                    )
                    .await?;
            }
            let p = self.project(id).await?;
            let frequency = receiver_frequency(
                p["profile"]["receiver"]["settings"]
                    .get("frequency_hz")
                    .unwrap_or(&json!(145500000)),
            )?;
            let mode = p
                .get("receiver_mode")
                .and_then(Value::as_str)
                .unwrap_or("AUTO");
            let permit = self.permit(id, &current);
            let target = Self::target(&current, &stream);
            let reply = self
                .control
                .tune(TuneRequest {
                    target: target.clone(),
                    permit: permit.clone(),
                    frequency_hz: frequency,
                    mode: mode.into(),
                })
                .await?;
            let _tx = self.coordinator.lock().await;
            self.authorize_control(&target, Some(&permit))?;
            if reply.acknowledged != Some(true) {
                return Err(ControlError::Unavailable("acknowledged_tune").into());
            }
            self.control.validate_tune(&reply)?;
            self.update_runtime(id, |runtime| {
                if runtime.token != current.token || runtime.epoch != current.epoch {
                    return Err(ControlError::Denied.into());
                }
                runtime.manual.remove(&stream);
                Ok(())
            })?;
            Ok::<(), GuardError>(())
        }
        .await;
        if let Err(error) = result {
            self.update_runtime(id, |current| {
                if let Some(m) = current
                    .manual
                    .get_mut(&stream)
                    .filter(|m| m.token == lease.token)
                {
                    m.returning = false;
                }
                Ok(())
            })?;
            return Err(error);
        }
        Ok(json!({"state":"returned","role":"receiver"}))
    }
    pub(crate) async fn restore_analyzer(
        self: &Arc<Self>,
        id: &str,
        token: &str,
        stream: &str,
    ) -> Result<()> {
        let _life = self.lock_project(id).await?;
        let r = self
            .runtime_snapshot(id)?
            .filter(|r| r.token == token && !r.stopped)
            .ok_or(ControlError::Denied)?;
        if let Some(search) = &r.search {
            let sid = s(search, "session_id")?;
            self.control.stop_session(sid).await?;
            self.stop_archived_session(sid).await?;
            while self.search_tick(id).await? {}
            self.update_runtime(id, |runtime| {
                runtime
                    .claims
                    .remove(&(sid.into(), s(search, "stream_id")?.into()));
                Ok(())
            })?;
        }
        let target = Self::target(&r, stream);
        self.control.stop_stream(&target).await?;
        let started = self.clock.now();
        let current = self
            .runtime_snapshot(id)?
            .filter(|r| r.token == token && !r.stopped)
            .ok_or(ControlError::Denied)?;
        let session = self
            .archive
            .get_session(&current.sid)
            .await?
            .ok_or(GuardError::NotFound)?;
        self.control
            .start_stream(
                &decode::<ori_contracts::Session>(&session)?,
                stream,
                self.analyzer_permit(id, &current),
            )
            .await?;
        let deadline = self.clock.monotonic() + Duration::from_secs(100);
        loop {
            let current = self
                .runtime_snapshot(id)?
                .filter(|r| r.token == token && !r.stopped)
                .ok_or(ControlError::Denied)?;
            if self.closed.load(std::sync::atomic::Ordering::Acquire) {
                return Err(ControlError::Denied.into());
            }
            let sweeps = self
                .archive
                .list_sweeps(&current.sid, Some(stream), 1)
                .await?;
            if let Some(raw) = sweeps.last() {
                let sweep: ori_contracts::Sweep = decode(raw)?;
                if sweep.interval.started_at >= started {
                    let p = self.project(id).await?;
                    let b = self
                        .get_record("baseline", s(&p, "baseline_id")?, Some(id))
                        .await?;
                    if ori_monitoring::fingerprint(&sweep)? != s(&b, "fingerprint")? {
                        return Err(invalid("Профиль широкого обзора не совпал с эталоном"));
                    }
                    let _tx = self.coordinator.lock().await;
                    let mut p = self.project(id).await?;
                    let current = self.runtime_snapshot(id)?.ok_or(ControlError::Denied)?;
                    if current.token != token
                        || current.stopped
                        || !current.manual.get(stream).is_some_and(|m| m.returning)
                    {
                        return Err(ControlError::Denied.into());
                    }
                    p["state"] = json!("armed");
                    p["coverage"] = json!("wide");
                    p["detector_state"] = Value::Null;
                    p["last_error"] = Value::Null;
                    self.archive.put_monitoring("project", p.clone()).await?;
                    self.update_runtime(id, |runtime| {
                        if runtime.token != token
                            || runtime.stopped
                            || !runtime.manual.get(stream).is_some_and(|m| m.returning)
                        {
                            return Err(ControlError::Denied.into());
                        }
                        runtime.manual.remove(stream);
                        runtime.search = None;
                        runtime.search_error = None;
                        runtime.cached_project = p.clone();
                        Ok(())
                    })?;
                    return Ok(());
                }
            }
            if self.clock.monotonic() >= deadline {
                return Err(GuardError::Conflict(
                    "Не получен свежий полный широкий свип; охрана остаётся остановленной".into(),
                ));
            }
            tokio::select! {_ = tokio::time::sleep(Duration::from_millis(50))=>{},_ = self.wake.notified()=>{}}
        }
    }
}
