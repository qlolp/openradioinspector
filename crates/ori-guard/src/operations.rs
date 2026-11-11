use crate::{
    GuardError, GuardService, Result, WebhookRequest,
    history::{cursor_decode, cursor_encode},
    util::*,
};
use ori_archive::{Mutation, NotificationClaim, NotificationCompletion, NotificationRow};
use serde_json::{Value, json};
use uuid::Uuid;

pub(crate) fn plus_seconds(stamp: &ori_contracts::UtcTimestamp, seconds: i64) -> Result<String> {
    let time = chrono::DateTime::<chrono::Utc>::from_timestamp_micros(stamp.unix_micros())
        .and_then(|t| t.checked_add_signed(chrono::TimeDelta::seconds(seconds)))
        .ok_or_else(|| invalid("timestamp range"))?;
    Ok(time.to_rfc3339_opts(chrono::SecondsFormat::Micros, true))
}
fn archive_defaults() -> Value {
    json!({"minimum_free_bytes":268435456,"minimum_retention_seconds":3600})
}
impl GuardService {
    pub async fn configure(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &["audio", "receiver_mode"], false)?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let mut p = self.project(id).await?;
        if self.runtime_snapshot(id)?.is_some() {
            return Err(invalid("Сначала снимите проект с охраны"));
        }
        if let Some(mode) = payload.get("receiver_mode") {
            if !mode
                .as_str()
                .is_some_and(|s| crate::preflight::MONITOR_MODES.contains(&s))
            {
                return Err(invalid("Режим недоступен; TETRA не входит в AUTO"));
            }
            p["receiver_mode"] = mode.clone();
        }
        if let Some(audio) = payload.get("audio") {
            if audio.is_null() {
                p["audio"] = Value::Null;
            } else {
                fields(audio, &["device", "name", "confirm"], true)?;
                if audio["confirm"] != true {
                    return Err(invalid(
                        "Выберите и подтвердите аналоговый аудиовход AR-DV1",
                    ));
                }
                let device = integer(audio, "device")?;
                let name = s(audio, "name")?;
                let inputs = self.control.audio_inputs().await?;
                if inputs.available != Some(true) {
                    return Err(invalid("Аудиовходы недоступны/не подтверждены"));
                }
                let found = inputs
                    .devices
                    .iter()
                    .find(|d| u64::from(d.index) == device && d.name == name)
                    .ok_or_else(|| invalid("Выбранный аудиовход отсутствует"))?;
                p["audio"] = json!({"device":found.index,"name":found.name});
            }
        }
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        if self.runtime_snapshot(id)?.is_some() {
            return Err(GuardError::Conflict("Проект уже владеет приборами".into()));
        }
        let mut current = self.project(id).await?;
        for key in ["audio", "receiver_mode"] {
            if payload.get(key).is_some() {
                current[key] = p[key].clone();
            }
        }
        self.archive
            .put_monitoring("project", current.clone())
            .await?;
        Ok(current)
    }
    pub async fn audio_inputs(&self) -> Result<Value> {
        match self.control.audio_inputs().await {
            Ok(input) => Ok(
                json!({"available":input.available,"devices":value(&input.devices)?,"error":input.error}),
            ),
            Err(error) => Ok(json!({"available":false,"devices":[],"error":error.to_string()})),
        }
    }
    pub async fn settings(&self, id: &str) -> Result<Value> {
        let p = self.project(id).await?;
        let hook = self.archive.webhook_config(id).await?;
        Ok(
            json!({"autostart":p.get("autostart").cloned().unwrap_or(json!(false)),"archive":p.get("archive").cloned().unwrap_or_else(archive_defaults),"detector":p["parameters"],"webhook":{"enabled":p.get("webhook_enabled").cloned().unwrap_or(json!(false)),"url":hook.as_ref().map(|h|h.url()).unwrap_or(""),"secret_configured":hook.as_ref().is_some_and(|h|!h.secret().is_empty())}}),
        )
    }
    pub async fn update_settings(&self, id: &str, payload: Value) -> Result<Value> {
        fields(
            &payload,
            &["autostart", "archive", "webhook", "detector"],
            false,
        )?;
        let _life = self.lock_project(id).await?;
        self.ensure_open()?;
        let mut p = self.project(id).await?;
        let settings = self.settings(id).await?;
        let mut hook_mutation = None;
        if let Some(auto) = payload.get("autostart") {
            if !auto.is_boolean() {
                return Err(invalid("autostart должен быть boolean"));
            }
            p["autostart"] = auto.clone();
        }
        if let Some(config) = payload.get("archive") {
            fields(
                config,
                &["minimum_free_bytes", "minimum_retention_seconds"],
                false,
            )?;
            let mut merged = settings["archive"].clone();
            merged
                .as_object_mut()
                .ok_or_else(|| invalid("archive settings"))?
                .extend(
                    config
                        .as_object()
                        .ok_or_else(|| invalid("archive settings"))?
                        .clone(),
                );
            let free = integer(&merged, "minimum_free_bytes")?;
            let retention = integer(&merged, "minimum_retention_seconds")?;
            if !(1048576..=1_000_000_000_000_000).contains(&free)
                || !(60..=31536000).contains(&retention)
            {
                return Err(invalid("Недопустимый резерв/прогноз хранения архива"));
            }
            p["archive"] = merged;
        }
        if let Some(patch) = payload.get("detector") {
            if self.runtime_snapshot(id)?.is_some() {
                return Err(invalid("Снимите охрану перед изменением детектора"));
            }
            let old = p["parameters"]
                .as_object()
                .ok_or_else(|| invalid("detector parameters"))?;
            let changes = patch
                .as_object()
                .ok_or_else(|| invalid("detector settings"))?;
            if changes.keys().any(|k| !old.contains_key(k)) {
                return Err(invalid("Неизвестный параметр детектора"));
            }
            let mut parameters = p["parameters"].clone();
            parameters
                .as_object_mut()
                .ok_or_else(|| invalid("detector parameters"))?
                .extend(changes.clone());
            for (key, low, high) in [
                ("margin_db", 1., 60.),
                ("impulse_db", 1., 120.),
                ("hysteresis_db", 0., 20.),
                ("dwell_seconds", 1., 30.),
            ] {
                let x = number(&parameters, key)?;
                if x < low || x > high {
                    return Err(invalid(format!("Недопустимый параметр {key}")));
                }
            }
            let window = integer(&parameters, "window_sweeps")?;
            let confirm = integer(&parameters, "confirmations")?;
            if !(1..=15).contains(&window) || confirm == 0 || confirm > window {
                return Err(invalid("Неверное окно подтверждения"));
            }
            if number(&parameters, "impulse_db")? < number(&parameters, "margin_db")?
                || number(&parameters, "hysteresis_db")? > number(&parameters, "margin_db")?
            {
                return Err(invalid("Импульс/гистерезис не согласованы с порогом"));
            }
            p["parameters"] = parameters;
            p["detector_state"] = Value::Null;
        }
        if let Some(config) = payload.get("webhook") {
            fields(config, &["enabled", "url", "secret"], false)?;
            let enabled = config
                .get("enabled")
                .unwrap_or(&settings["webhook"]["enabled"]);
            if !enabled.is_boolean() {
                return Err(invalid("enabled должен быть boolean"));
            }
            let url = config
                .get("url")
                .unwrap_or(&settings["webhook"]["url"])
                .as_str()
                .filter(|s| s.chars().count() <= 2048)
                .ok_or_else(|| invalid("Неверный URL webhook"))?;
            if !url.is_empty() {
                let parsed = url::Url::parse(url)
                    .map_err(|_| invalid("Требуется HTTP(S) URL без учётных данных и fragment"))?;
                if !["http", "https"].contains(&parsed.scheme())
                    || parsed.host_str().is_none()
                    || !parsed.username().is_empty()
                    || parsed.password().is_some_and(|s| !s.is_empty())
                    || parsed.fragment().is_some_and(|s| !s.is_empty())
                {
                    return Err(invalid(
                        "Требуется HTTP(S) URL без учётных данных и fragment",
                    ));
                }
            }
            let old = self.archive.webhook_config(id).await?;
            let secret = config
                .get("secret")
                .map(|v| v.as_str().ok_or_else(|| invalid("Неверный секрет webhook")))
                .transpose()?
                .unwrap_or_else(|| old.as_ref().map(|v| v.secret()).unwrap_or(""));
            if secret.chars().count() > 1024
                || enabled == true && (url.is_empty() || secret.is_empty())
            {
                return Err(invalid("Для webhook нужны адрес и секрет"));
            }
            hook_mutation = Some(Mutation::Webhook {
                project_id: id.into(),
                url: url.into(),
                secret: secret.into(),
            });
            p["webhook_enabled"] = enabled.clone();
        }
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        if payload.get("detector").is_some() && self.runtime_snapshot(id)?.is_some() {
            return Err(GuardError::Conflict("Проект уже на охране".into()));
        }
        let mut current = self.project(id).await?;
        for (input, output) in [
            ("autostart", "autostart"),
            ("archive", "archive"),
            ("webhook", "webhook_enabled"),
            ("detector", "parameters"),
        ] {
            if payload.get(input).is_some() {
                current[output] = p[output].clone();
            }
        }
        if payload.get("detector").is_some() {
            current["detector_state"] = Value::Null;
        }
        let mut mutations = vec![];
        if let Some(h) = hook_mutation {
            mutations.push(h);
        }
        mutations.push(Self::mutation("project", current));
        self.archive.apply(mutations).await?;
        drop(_tx);
        self.settings(id).await
    }
    pub(crate) fn notification(
        &self,
        p: &Value,
        source: &str,
        kind: &str,
        severity: &str,
        message: &str,
        event_id: Option<&str>,
    ) -> Result<NotificationRow> {
        let project = s(p, "id")?;
        let id = Uuid::new_v5(
            &Uuid::NAMESPACE_URL,
            format!("{project}:{source}:{kind}").as_bytes(),
        )
        .simple()
        .to_string();
        let now = self.clock.now().as_str().to_owned();
        Ok(NotificationRow {
            id: id.clone(),
            project_id: project.into(),
            created_at: now.clone(),
            payload: json!({"notification_id":id,"project_id":project,"event_id":event_id,"kind":kind,"severity":severity,"message":message,"occurred_at":now}),
            state: if p["webhook_enabled"] == true {
                "pending".into()
            } else {
                "disabled".into()
            },
            attempts: 0,
            next_at: Some(now),
            error: None,
            lease_until: None,
            acknowledged_at: None,
        })
    }
    pub(crate) fn event_notification(&self, p: &Value, event: &Value) -> Result<NotificationRow> {
        self.notification(
            p,
            s(event, "id")?,
            s(event, "kind")?,
            "warning",
            &format!(
                "Обнаружено излучение / отклонение: {} Гц",
                integer(event, "center_frequency_hz")?
            ),
            Some(s(event, "id")?),
        )
    }
    pub async fn notify_event(&self, p: &Value, event: &Value) -> Result<String> {
        let row = self.event_notification(p, event)?;
        let id = row.id.clone();
        self.archive.enqueue_notification(row).await?;
        Ok(id)
    }
    pub async fn notify_error(&self, p: &Value, message: &str, kind: &str) -> Result<String> {
        let row = self.notification(
            p,
            s(p, "active_session_id")?,
            kind,
            "critical",
            message,
            None,
        )?;
        let id = row.id.clone();
        self.archive.enqueue_notification(row).await?;
        Ok(id)
    }
    pub(crate) fn volatile_error(&self, p: &Value, message: &str) -> Result<Value> {
        let row = self.notification(
            p,
            s(p, "active_session_id")?,
            "coverage_error",
            "critical",
            message,
            None,
        )?;
        let mut v = row.payload;
        v["id"] = json!(row.id);
        v["created_at"] = json!(row.created_at);
        v["acknowledged_at"] = Value::Null;
        v["volatile"] = json!(true);
        v["delivery"] = json!({"state":"unavailable","attempts":0,"next_attempt_at":null,"last_error":"archive_unavailable"});
        Ok(v)
    }
    pub async fn alerts(&self, id: &str, after: Option<&str>, limit: usize) -> Result<Value> {
        self.project(id).await?;
        if !(1..=100).contains(&limit) {
            return Err(invalid("limit должен быть 1..100"));
        }
        let cursor = after
            .filter(|s| !s.is_empty())
            .map(cursor_decode)
            .transpose()?;
        let page = self.archive.notification_page(id, cursor, limit).await?;
        let mut items=page.items.into_iter().map(|row|{let mut v=row.payload;v["id"]=json!(row.id);v["created_at"]=json!(row.created_at);v["acknowledged_at"]=json!(row.acknowledged_at);v["delivery"]=json!({"state":row.state,"attempts":row.attempts,"next_attempt_at":row.next_at,"last_error":row.error});v}).collect::<Vec<_>>();
        if after.is_none_or(str::is_empty)
            && let Some(v) = self.volatile_alert(id)?
            && v["volatile"] == true
            && !items.iter().any(|a| a["id"] == v["id"])
        {
            items.push(v);
        }
        items.sort_by(|a, b| {
            (&b["created_at"].as_str(), &b["id"].as_str())
                .cmp(&(&a["created_at"].as_str(), &a["id"].as_str()))
        });
        let extra = items.len() > limit;
        items.truncate(limit);
        let next = if extra {
            items
                .last()
                .map(|v| cursor_encode(s(v, "created_at")?, s(v, "id")?))
                .transpose()?
        } else {
            page.next_cursor
                .map(|c| cursor_encode(&c.started_at, &c.id))
                .transpose()?
        };
        Ok(json!({"items":items,"next_cursor":next}))
    }
    pub async fn acknowledge_alert(&self, id: &str, alert: &str, payload: Value) -> Result<Value> {
        fields(&payload, &[], true)
            .map_err(|_| invalid("Подтверждение принимает пустой объект"))?;
        self.project(id).await?;
        if self.acknowledge_volatile(id, alert)? {
            return Ok(json!({"id":alert,"acknowledged":true,"volatile":true}));
        }
        if !self
            .archive
            .acknowledge_notification(id, alert, self.clock.now().as_str())
            .await?
        {
            return Err(GuardError::NotFoundContext(
                "Тревога другого проекта/отсутствует",
            ));
        }
        Ok(json!({"id":alert,"acknowledged":true}))
    }
    pub async fn deliver_one(&self) -> Result<bool> {
        let now = self.clock.now();
        let claim = self
            .archive
            .claim_notification(now.as_str(), &plus_seconds(&now, 30)?)
            .await?;
        let claimed = match claim {
            NotificationClaim::Empty => return Ok(false),
            NotificationClaim::Disabled { .. } => return Ok(true),
            NotificationClaim::Deliver(c) => c,
        };
        let result = self
            .control
            .deliver_webhook(WebhookRequest {
                notification_id: claimed.row.id.clone(),
                url: claimed.config.url().into(),
                secret: claimed.config.secret().into(),
                body: claimed.body().to_vec(),
            })
            .await;
        let error = match result {
            Ok(receipt)
                if receipt.delivered == Some(true)
                    && receipt.status.is_some_and(|s| (200..300).contains(&s)) =>
            {
                None
            }
            Ok(receipt) => Some(
                receipt
                    .status
                    .map(|s| format!("HTTP {s}"))
                    .unwrap_or_else(|| "network_error".into()),
            ),
            Err(_) => Some("network_error".into()),
        };
        let completion = match error {
            None => NotificationCompletion::Sent,
            Some(error) if claimed.row.attempts >= 5 => NotificationCompletion::Failed { error },
            Some(error) => NotificationCompletion::Retry {
                next_at: plus_seconds(&now, (5i64 << claimed.row.attempts).min(300))?,
                error,
            },
        };
        self.archive
            .finish_notification(
                &claimed.row.id,
                claimed.row.attempts,
                claimed
                    .row
                    .lease_until
                    .as_deref()
                    .ok_or_else(|| invalid("notification lease"))?,
                completion,
            )
            .await?;
        Ok(true)
    }
}
