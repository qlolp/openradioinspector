use crate::validation::*;
use crate::{SessionEffects, SessionError, SessionResult, TuneCompletionCheck, TuneRequest};
use chrono::{DateTime, Utc};
use ori_archive::{Archive, EventDraft, Mutation};
use rand::RngCore;
use serde_json::{Value, json};
use std::sync::Arc;

pub(crate) const OPEN: &[&str] = &["queued", "tuning", "listening", "decoding"];
const CANDIDATE_STATES: &[&str] = &["new", "queued", "investigating", "resolved", "dismissed"];
const INVESTIGATION_STATES: &[&str] = &[
    "queued",
    "tuning",
    "listening",
    "decoding",
    "completed",
    "failed",
    "cancelled",
];
struct EligibleStart {
    check: Arc<dyn TuneCompletionCheck>,
    minimum_priority: u64,
    receiver: Option<String>,
}
pub struct SessionService {
    pub(crate) archive: Archive,
    pub(crate) effects: Arc<dyn SessionEffects>,
    pub(crate) clock: Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>,
    pub(crate) ids: Arc<dyn Fn() -> String + Send + Sync>,
}
fn uuid() -> String {
    let mut bytes = [0_u8; 16];
    rand::thread_rng().fill_bytes(&mut bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let mut value = String::with_capacity(36);
    for (index, byte) in bytes.into_iter().enumerate() {
        if [4, 6, 8, 10].contains(&index) {
            value.push('-');
        }
        use std::fmt::Write;
        write!(&mut value, "{byte:02x}").expect("String formatting");
    }
    value
}
impl SessionService {
    pub fn new(archive: Archive, effects: Arc<dyn SessionEffects>) -> Arc<Self> {
        Self::with_sources(archive, effects, Arc::new(Utc::now), Arc::new(uuid))
    }
    pub fn with_sources(
        archive: Archive,
        effects: Arc<dyn SessionEffects>,
        clock: Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>,
        ids: Arc<dyn Fn() -> String + Send + Sync>,
    ) -> Arc<Self> {
        Arc::new(Self {
            archive,
            effects,
            clock,
            ids,
        })
    }
    pub(crate) fn event(&self, kind: &str, session: &str, payload: Value) -> EventDraft {
        EventDraft {
            event_type: kind.into(),
            occurred_at: stamp((self.clock)()),
            session_id: Some(session.into()),
            payload,
        }
    }
    pub(crate) async fn session(&self, id: &str) -> SessionResult<Value> {
        self.archive
            .get_session(id)
            .await?
            .ok_or_else(|| SessionError::missing("session_not_found", id))
    }
    pub async fn get_session(&self, id: &str) -> SessionResult<Value> {
        self.session(id).await
    }
    pub(crate) fn stream<'a>(session: &'a Value, id: &str) -> SessionResult<&'a Value> {
        session["streams"]
            .as_array()
            .and_then(|streams| streams.iter().find(|stream| stream["id"] == id))
            .ok_or_else(|| SessionError::invalid(format!("Поток не принадлежит сеансу: {id}")))
    }
    pub(crate) fn units(session: &Value, stream: &str, unit: &str) -> SessionResult<()> {
        let target = Self::stream(session, stream)?;
        if target["level_unit"] != unit {
            return Err(SessionError::invalid(format!(
                "Единица измерения '{unit}' не совпадает с потоком '{}'; автоматическое преобразование запрещено",
                text(&target["level_unit"])
            )));
        }
        Ok(())
    }
    async fn candidate(&self, session: &str, id: &str) -> SessionResult<Value> {
        self.session(session).await?;
        self.archive
            .get_candidate(id)
            .await?
            .filter(|value| value["session_id"] == session)
            .ok_or_else(|| SessionError::missing("candidate_not_found", id))
    }
    async fn investigation(&self, session: &str, id: &str) -> SessionResult<Value> {
        self.session(session).await?;
        self.archive
            .get_investigation(id)
            .await?
            .filter(|(owner, _)| owner == session)
            .map(|(_, value)| value)
            .ok_or_else(|| SessionError::missing("investigation_not_found", id))
    }
    pub async fn get_candidate(&self, session: &str, id: &str) -> SessionResult<Value> {
        self.candidate(session, id).await
    }
    pub async fn get_investigation(&self, session: &str, id: &str) -> SessionResult<Value> {
        self.investigation(session, id).await
    }
    pub(crate) fn candidate_value(&self, session: &str, payload: &Value) -> SessionResult<Value> {
        if !payload.is_object() {
            return Err(SessionError::invalid(
                "Тело запроса должно быть JSON-объектом",
            ));
        }
        if payload.get("state").is_some_and(|value| value != "new") {
            return Err(SessionError::invalid(
                "Новый кандидат должен иметь состояние new",
            ));
        }
        if payload
            .get("session_id")
            .is_some_and(|value| !value.is_null() && value != session)
        {
            return Err(SessionError::invalid(
                "session_id в теле не совпадает с маршрутом",
            ));
        }
        let source = py_string(field(payload, "source_stream_id")?);
        let observed = interval(field(payload, "interval")?)?;
        let hz = field(payload, "center_frequency_hz")?;
        let unit = py_string(field(payload, "level_unit")?);
        let id = payload
            .get("id")
            .filter(|value| truthy(value))
            .map(py_string)
            .unwrap_or_else(|| (self.ids)());
        let owner = required(session, "session_id")?;
        let source = required(&source, "source_stream_id")?;
        let hz = frequency(hz, "center_frequency_hz")?;
        let unit = required(&unit, "level_unit")?;
        let bandwidth = match payload.get("bandwidth_hz").filter(|value| !value.is_null()) {
            Some(value) => json!(frequency(value, "bandwidth_hz")?),
            None => Value::Null,
        };
        let peak = finite(&payload["peak_level"], "peak_level")?;
        let score = finite(&payload["score"], "score")?;
        let observations = match payload.get("observation_ids").filter(|value| truthy(value)) {
            None => Vec::new(),
            Some(Value::Array(items)) => items.iter().map(py_string).collect(),
            Some(Value::String(items)) => items.chars().map(|item| item.to_string()).collect(),
            Some(Value::Object(items)) => items.keys().cloned().collect(),
            _ => {
                return Err(SessionError::invalid(
                    "observation_ids должно быть последовательностью",
                ));
            }
        };
        if owner != session {
            return Err(SessionError::invalid("Кандидат принадлежит другому сеансу"));
        }
        Ok(
            json!({"id":id,"session_id":owner,"source_stream_id":source,"interval":observed,
            "center_frequency_hz":hz,"bandwidth_hz":bandwidth,"peak_level":peak,"score":score,
            "level_unit":unit,"observation_ids":observations,"state":"new"}),
        )
    }
    pub async fn create_candidate(&self, session: &str, payload: Value) -> SessionResult<Value> {
        self.create_candidate_checked(session, payload, None).await
    }
    /// Revalidate cached original authorization under the domain coordinator.
    /// The check performs no I/O and must not obtain a new permit.
    pub async fn create_candidate_with_check(
        &self,
        session: &str,
        payload: Value,
        check: Arc<dyn TuneCompletionCheck>,
    ) -> SessionResult<Value> {
        self.create_candidate_checked(session, payload, Some(check))
            .await
    }
    async fn create_candidate_checked(
        &self,
        session: &str,
        payload: Value,
        check: Option<Arc<dyn TuneCompletionCheck>>,
    ) -> SessionResult<Value> {
        let value = self.candidate_value(session, &payload)?;
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        let owner = self.session(session).await?;
        Self::units(
            &owner,
            text(&value["source_stream_id"]),
            text(&value["level_unit"]),
        )?;
        let ids = value["observation_ids"].as_array().expect("candidate IDs");
        if ids.len() > 1000 || ids.iter().any(|id| text(id).is_empty()) {
            return Err(SessionError::invalid(
                "Допустимо до 1000 непустых ID исходных измерений",
            ));
        }
        if !ids.is_empty() {
            let ids = ids.iter().map(|id| text(id).to_owned()).collect::<Vec<_>>();
            let owners = self.archive.measurement_owners(ids.clone()).await?;
            if ids.iter().any(|id| {
                owners.get(id).is_none_or(|owners| {
                    owners.len() != 1
                        || owners[0] != (session.into(), text(&value["source_stream_id"]).into())
                })
            }) {
                return Err(SessionError::invalid(
                    "Исходное измерение отсутствует или принадлежит другому сеансу/потоку",
                ));
            }
        }
        if self
            .archive
            .get_candidate(text(&value["id"]))
            .await?
            .is_some()
        {
            return Err(SessionError::invalid(format!(
                "Кандидат уже существует: {}",
                text(&value["id"])
            )));
        }
        if let Some(check) = check {
            check.revalidate()?;
            if owner["state"] != "running" {
                return Err(SessionError::invalid("Сначала запустите сеанс мониторинга"));
            }
        }
        self.save_candidate(session, value, "candidate.created")
            .await
    }
    pub(crate) async fn save_candidate(
        &self,
        session: &str,
        value: Value,
        kind: &str,
    ) -> SessionResult<Value> {
        self.archive
            .apply(vec![Mutation::Candidate {
                event: Some(self.event(kind, session, json!({"candidate":value}))),
                value: value.clone(),
            }])
            .await?;
        Ok(value)
    }
    async fn save_job(
        &self,
        session: &str,
        job: Value,
        candidate: Value,
        kind: &str,
    ) -> SessionResult<Value> {
        self.archive
            .apply(vec![Mutation::Investigation {
                session_id: session.into(),
                value: job.clone(),
                event: Some(self.event(
                    kind,
                    session,
                    json!({"investigation":job,"candidate":candidate}),
                )),
                candidate,
            }])
            .await?;
        Ok(job)
    }
    pub async fn list_candidates(
        &self,
        session: &str,
        state: Option<&str>,
    ) -> SessionResult<Value> {
        if let Some(state) = state
            && !CANDIDATE_STATES.contains(&state)
        {
            return Err(SessionError::invalid(format!(
                "'{state}' is not a valid CandidateState"
            )));
        }
        self.session(session).await?;
        Ok(json!(
            self.archive.session_candidates(session, state).await?
        ))
    }
    pub async fn list_investigations(
        &self,
        session: &str,
        state: Option<&str>,
    ) -> SessionResult<Value> {
        if let Some(state) = state
            && !INVESTIGATION_STATES.contains(&state)
        {
            return Err(SessionError::invalid(format!(
                "'{state}' is not a valid InvestigationState"
            )));
        }
        self.session(session).await?;
        Ok(json!(
            self.archive.session_investigations(session, state).await?
        ))
    }
    pub async fn dismiss_candidate(&self, session: &str, id: &str) -> SessionResult<Value> {
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        let mut candidate = self.candidate(session, id).await?;
        if !["new", "queued"].contains(&text(&candidate["state"])) {
            return Err(SessionError::invalid(format!(
                "Нельзя отклонить кандидата из состояния {}",
                text(&candidate["state"])
            )));
        }
        if self
            .archive
            .candidate_has_open_investigation(session, id)
            .await?
        {
            return Err(SessionError::invalid(
                "Сначала завершите или отмените открытое исследование",
            ));
        }
        candidate["state"] = json!("dismissed");
        self.save_candidate(session, candidate, "candidate.dismissed")
            .await
    }
    pub async fn enqueue_investigation(
        &self,
        session: &str,
        id: &str,
        payload: Value,
    ) -> SessionResult<Value> {
        if !payload.is_object() {
            return Err(SessionError::invalid(
                "Тело запроса должно быть JSON-объектом",
            ));
        }
        for key in [
            "state",
            "created_at",
            "started_at",
            "ended_at",
            "result_observation_ids",
            "error",
        ] {
            if payload.get(key).is_some_and(|value| !value.is_null()) {
                return Err(SessionError::invalid(format!(
                    "{key} назначается службой жизненного цикла"
                )));
            }
        }
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        let owner = self.session(session).await?;
        if ["stopped", "failed"].contains(&text(&owner["state"])) {
            return Err(SessionError::invalid(
                "Завершённый сеанс не принимает новые исследования",
            ));
        }
        let mut candidate = self.candidate(session, id).await?;
        if !["new", "queued"].contains(&text(&candidate["state"])) {
            return Err(SessionError::invalid(format!(
                "Нельзя поставить кандидата из состояния {}",
                text(&candidate["state"])
            )));
        }
        if self
            .archive
            .candidate_has_open_investigation(session, id)
            .await?
        {
            return Err(SessionError::invalid(
                "Для кандидата уже существует открытое исследование",
            ));
        }
        let stream = py_string(field(&payload, "target_stream_id")?);
        let target = Self::stream(&owner, &stream)?;
        if target["role"] != "receiver" {
            return Err(SessionError::invalid(
                "Исследование может настраивать только поток receiver",
            ));
        }
        let job_id = payload
            .get("id")
            .filter(|value| truthy(value))
            .map(py_string)
            .unwrap_or_else(|| (self.ids)());
        let target_frequency = frequency(
            payload
                .get("target_frequency_hz")
                .unwrap_or(&candidate["center_frequency_hz"]),
            "target_frequency_hz",
        )?;
        let priority = payload.get("priority").cloned().unwrap_or(json!(0));
        if !integer(&priority)
            || priority
                .as_u64()
                .is_none_or(|value| value > i64::MAX as u64)
        {
            return Err(SessionError::invalid(
                "priority должно быть неотрицательным целым",
            ));
        }
        if self.archive.get_investigation(&job_id).await?.is_some() {
            return Err(SessionError::invalid(format!(
                "Исследование уже существует: {job_id}"
            )));
        }
        let job = json!({"id":job_id,"candidate_id":id,"target_stream_id":stream,"target_frequency_hz":target_frequency,
            "state":"queued","priority":priority,"mode":payload.get("mode").cloned().unwrap_or(Value::Null),
            "created_at":stamp((self.clock)()),"started_at":null,"ended_at":null,"result_observation_ids":[],"error":null,"listening_started_at":null});
        candidate["state"] = json!("queued");
        self.save_job(session, job, candidate, "investigation.queued")
            .await
    }
    pub async fn next_investigation(&self, session: &str) -> SessionResult<Value> {
        self.session(session).await?;
        Ok(self
            .archive
            .next_queued_investigation(session)
            .await?
            .unwrap_or(Value::Null))
    }
    pub async fn start_investigation(&self, session: &str, id: &str) -> SessionResult<Value> {
        self.start_job(session, id, self.effects.clone(), false, None, None)
            .await
    }
    pub async fn start_next_investigation(&self, session: &str, id: &str) -> SessionResult<Value> {
        self.start_next_investigation_with_effects(session, id, self.effects.clone())
            .await
    }
    pub async fn start_investigation_with_effects(
        &self,
        session: &str,
        id: &str,
        effects: Arc<dyn SessionEffects>,
    ) -> SessionResult<Value> {
        self.start_job(session, id, effects, false, None, None)
            .await
    }
    pub async fn start_next_investigation_with_effects(
        &self,
        session: &str,
        id: &str,
        effects: Arc<dyn SessionEffects>,
    ) -> SessionResult<Value> {
        if id.trim().is_empty() {
            return Err(SessionError::invalid(
                "Для подтверждения требуется investigation_id",
            ));
        }
        self.start_job(session, id, effects, true, None, None).await
    }
    /// Keep the original cache-only caller check across both manual commits.
    /// The HTTP/effect owner remains responsible for the operation lifetime.
    pub async fn start_investigation_with_effects_and_check(
        &self,
        session: &str,
        id: &str,
        effects: Arc<dyn SessionEffects>,
        check: Arc<dyn TuneCompletionCheck>,
    ) -> SessionResult<Value> {
        self.start_job(session, id, effects, false, None, Some(check))
            .await
    }
    /// Preserve explicit confirmed-next selection with the same original check.
    pub async fn start_next_investigation_with_effects_and_check(
        &self,
        session: &str,
        id: &str,
        effects: Arc<dyn SessionEffects>,
        check: Arc<dyn TuneCompletionCheck>,
    ) -> SessionResult<Value> {
        if id.trim().is_empty() {
            return Err(SessionError::invalid(
                "Для подтверждения требуется investigation_id",
            ));
        }
        self.start_job(session, id, effects, true, None, Some(check))
            .await
    }
    /// Legacy automatic selection is re-read under the shared coordinator.
    /// Busy receivers do not prevent another receiver's eligible handoff.
    pub async fn start_eligible_investigation_with_effects(
        &self,
        session: &str,
        id: &str,
        effects: Arc<dyn SessionEffects>,
        check: Arc<dyn TuneCompletionCheck>,
        minimum_priority: u64,
        receiver: Option<&str>,
    ) -> SessionResult<Value> {
        self.start_job(
            session,
            id,
            effects,
            false,
            Some(EligibleStart {
                check,
                minimum_priority,
                receiver: receiver.map(str::to_owned),
            }),
            None,
        )
        .await
    }
    async fn start_job(
        &self,
        session: &str,
        id: &str,
        effects: Arc<dyn SessionEffects>,
        require_next: bool,
        eligible: Option<EligibleStart>,
        original: Option<Arc<dyn TuneCompletionCheck>>,
    ) -> SessionResult<Value> {
        let (request, epoch) = {
            let coordinator = self.archive.domain_coordinator();
            let _guard = coordinator.lock().await;
            let owner = self.session(session).await?;
            if owner["state"] != "running" {
                return Err(SessionError::invalid("Сначала запустите сеанс мониторинга"));
            }
            let mut job = self.investigation(session, id).await?;
            if job["state"] != "queued" {
                return Err(SessionError::invalid(format!(
                    "Нельзя запустить исследование из состояния {}",
                    text(&job["state"])
                )));
            }
            if require_next {
                let next = self
                    .archive
                    .next_queued_investigation(session)
                    .await?
                    .ok_or_else(|| SessionError::invalid("Очередь исследований пуста"))?;
                if next["id"] != id {
                    return Err(SessionError::invalid(
                        "Очередь изменилась: запросите и подтвердите актуальное следующее исследование",
                    ));
                }
            }
            if let Some(admission) = &eligible {
                admission.check.revalidate()?;
                let jobs = self.archive.session_investigations(session, None).await?;
                let busy = jobs
                    .iter()
                    .filter(|job| {
                        ["tuning", "listening", "decoding"].contains(&text(&job["state"]))
                    })
                    .map(|job| text(&job["target_stream_id"]))
                    .collect::<std::collections::HashSet<_>>();
                let next = jobs.iter().find(|job| {
                    job["state"] == "queued"
                        && job["priority"].as_u64().unwrap_or(0) >= admission.minimum_priority
                        && admission
                            .receiver
                            .as_deref()
                            .is_none_or(|receiver| job["target_stream_id"] == receiver)
                        && !busy.contains(text(&job["target_stream_id"]))
                });
                if next.is_none_or(|next| next["id"] != id) {
                    return Err(SessionError::invalid(
                        "Очередь изменилась: запросите и подтвердите актуальное следующее исследование",
                    ));
                }
            }
            if self
                .archive
                .receiver_has_runtime_investigation(
                    session,
                    text(&job["target_stream_id"]),
                    Some(id),
                )
                .await?
            {
                return Err(SessionError::invalid(
                    "Выбранный receiver уже выполняет другое исследование",
                ));
            }
            let candidate = self.candidate(session, text(&job["candidate_id"])).await?;
            if let Some(admission) = &eligible {
                admission.check.revalidate()?;
            }
            job["state"] = json!("tuning");
            job["started_at"] = json!(stamp((self.clock)()));
            job["ended_at"] = Value::Null;
            job["error"] = Value::Null;
            let request = TuneRequest {
                session_id: session.into(),
                stream_id: text(&job["target_stream_id"]).into(),
                frequency_hz: frequency(&job["target_frequency_hz"], "target_frequency_hz")?,
                mode: job["mode"].as_str().map(str::to_owned),
            };
            let epoch = parse_time(&job["started_at"], "started_at")?;
            if let Some(check) = &original {
                check.revalidate()?;
            }
            self.save_job(session, job, candidate, "investigation.tuning")
                .await?;
            (request, epoch)
        };
        // No coordinator (or archive transaction) survives an external callback.
        let effect = effects.tune(request).await;
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        let owner = self.session(session).await?;
        let mut job = self.investigation(session, id).await?;
        if job["state"] != "tuning" || owner["state"] != "running" {
            return Ok(job);
        }
        if parse_time(&job["started_at"], "started_at")? != epoch {
            return Ok(job);
        }
        let mut candidate = self.candidate(session, text(&job["candidate_id"])).await?;
        let effect = effect.and_then(|receipt| {
            if let Some(check) = &original {
                check.revalidate()?;
            }
            receipt.revalidate()
        });
        if let Err(error) = effect {
            job["state"] = json!("failed");
            job["ended_at"] = json!(stamp((self.clock)()));
            job["error"] = json!(if error.message.is_empty() {
                "SessionError".into()
            } else {
                error.message
            });
            candidate["state"] = json!("queued");
            return self
                .save_job(session, job, candidate, "investigation.failed")
                .await;
        }
        job["state"] = json!("listening");
        job["listening_started_at"] = json!(stamp((self.clock)()));
        candidate["state"] = json!("investigating");
        self.save_job(session, job, candidate, "investigation.listening")
            .await
    }
    async fn matching(
        &self,
        session: &str,
        job: &Value,
        limit: Option<usize>,
    ) -> SessionResult<Vec<String>> {
        if job["started_at"].is_null() {
            return Ok(Vec::new());
        }
        let mode = job["mode"].as_str().map(|mode| mode.trim().to_uppercase());
        Ok(self
            .archive
            .matching_observation_ids(
                session,
                text(&job["target_stream_id"]),
                frequency(&job["target_frequency_hz"], "target_frequency_hz")?,
                text(&job["started_at"]),
                mode.as_deref(),
                limit,
            )
            .await?)
    }
    pub async fn matching_observation_ids(
        &self,
        session: &str,
        id: &str,
        limit: Option<usize>,
    ) -> SessionResult<Value> {
        if limit == Some(0) {
            return Err(SessionError::invalid(
                "limit должен быть положительным целым или None",
            ));
        }
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        let job = self.investigation(session, id).await?;
        Ok(json!(self.matching(session, &job, limit).await?))
    }
    pub async fn complete_investigation(&self, session: &str, id: &str) -> SessionResult<Value> {
        self.complete_checked(session, id, None).await
    }
    pub async fn complete_investigation_with_check(
        &self,
        session: &str,
        id: &str,
        check: Arc<dyn TuneCompletionCheck>,
    ) -> SessionResult<Value> {
        self.complete_checked(session, id, Some(check)).await
    }
    async fn complete_checked(
        &self,
        session: &str,
        id: &str,
        check: Option<Arc<dyn TuneCompletionCheck>>,
    ) -> SessionResult<Value> {
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        let mut job = self.investigation(session, id).await?;
        if let Some(check) = check {
            check.revalidate()?;
        }
        if !["listening", "decoding"].contains(&text(&job["state"])) {
            return Err(SessionError::invalid(format!(
                "Нельзя завершить исследование из состояния {}",
                text(&job["state"])
            )));
        }
        if job["started_at"].is_null() {
            return Err(SessionError::invalid(
                "У активного исследования отсутствует started_at",
            ));
        }
        let observations = self.matching(session, &job, None).await?;
        if observations.is_empty() {
            return Err(SessionError::invalid(
                "Нет наблюдений целевого потока на выбранной частоте после начала исследования",
            ));
        }
        let mut candidate = self.candidate(session, text(&job["candidate_id"])).await?;
        job["state"] = json!("completed");
        job["ended_at"] = json!(stamp((self.clock)()));
        job["result_observation_ids"] = json!(observations);
        job["error"] = Value::Null;
        candidate["state"] = json!("resolved");
        self.save_job(session, job, candidate, "investigation.completed")
            .await
    }
    pub async fn fail_investigation(
        &self,
        session: &str,
        id: &str,
        error: &str,
    ) -> SessionResult<Value> {
        if error.trim().is_empty() {
            return Err(SessionError::invalid(
                "Для сбоя исследования требуется непустое описание error",
            ));
        }
        self.finish_job(session, id, Some(error.trim())).await
    }
    pub async fn cancel_investigation(&self, session: &str, id: &str) -> SessionResult<Value> {
        self.finish_job(session, id, None).await
    }
    async fn finish_job(
        &self,
        session: &str,
        id: &str,
        error: Option<&str>,
    ) -> SessionResult<Value> {
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        let mut job = self.investigation(session, id).await?;
        if !OPEN.contains(&text(&job["state"])) {
            return Err(SessionError::invalid(format!(
                "{} {}",
                if error.is_some() {
                    "Нельзя завершить сбоем состояние"
                } else {
                    "Нельзя отменить исследование из состояния"
                },
                text(&job["state"])
            )));
        }
        let mut candidate = self.candidate(session, text(&job["candidate_id"])).await?;
        job["state"] = json!(if error.is_some() {
            "failed"
        } else {
            "cancelled"
        });
        job["ended_at"] = json!(stamp((self.clock)()));
        job["error"] = json!(error);
        candidate["state"] = json!("queued");
        self.save_job(
            session,
            job,
            candidate,
            if error.is_some() {
                "investigation.failed"
            } else {
                "investigation.cancelled"
            },
        )
        .await
    }
}
