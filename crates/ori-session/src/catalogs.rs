use crate::validation::*;
use crate::{SessionError, SessionResult, SessionService};
use ori_archive::Mutation;
use serde_json::{Value, json};
use std::collections::HashSet;

const SAFE_HZ: u64 = 9_007_199_254_740_991;
fn catalog() -> Value {
    serde_json::from_str(include_str!("catalog.json")).expect("authority catalog JSON")
}
fn result_payload(payload: &Value) -> SessionResult<Value> {
    if !payload.is_object() {
        return Err(SessionError::invalid(
            "Тело запроса должно быть JSON-объектом",
        ));
    }
    if !exact_keys(
        payload,
        &[
            "methodology",
            "result_code",
            "frequency_hz",
            "interval",
            "values",
        ],
    ) {
        return Err(SessionError::invalid(
            "Разрешены только поля methodology, result_code, frequency_hz, interval и values",
        ));
    }
    let all = catalog();
    let specifications = all.as_array().expect("catalog");
    let specification = specifications
        .iter()
        .find(|spec| spec["methodology"] == payload["methodology"]);
    let Some(specification) = specification else {
        return Err(SessionError::invalid(format!(
            "Методика должна быть одной из: {}",
            specifications
                .iter()
                .map(|spec| text(&spec["methodology"]))
                .collect::<Vec<_>>()
                .join(", ")
        )));
    };
    let code = text(&payload["result_code"]);
    if !["norm", "not_norm", "not_measured"].contains(&code) {
        return Err(SessionError::invalid(
            "result_code должен быть одним из: norm, not_norm, not_measured",
        ));
    }
    let hz = &payload["frequency_hz"];
    if !integer(hz) {
        return Err(SessionError::invalid(
            "frequency_hz должен быть целым числом Гц",
        ));
    }
    if hz.as_u64().is_none_or(|hz| hz > SAFE_HZ) {
        return Err(SessionError::invalid(
            "frequency_hz вне диапазона безопасных целых Hz",
        ));
    }
    let span = &payload["interval"];
    if !exact_keys(span, &["started_at", "ended_at"]) {
        return Err(SessionError::invalid(
            "interval должен содержать только started_at и ended_at",
        ));
    }
    for key in ["started_at", "ended_at"] {
        if span[key].as_str().is_none_or(str::is_empty) {
            return Err(SessionError::invalid(format!(
                "{key} должен быть непустой строкой UTC"
            )));
        }
    }
    let span = interval(span)?;
    let values = &payload["values"];
    let fields = specification["fields"]
        .as_array()
        .expect("catalog fields")
        .iter()
        .map(text)
        .collect::<Vec<_>>();
    if code == "not_measured" {
        if !values.as_object().is_some_and(|v| v.is_empty()) {
            return Err(SessionError::invalid(
                "Код not_measured не допускает значений результата",
            ));
        }
    } else {
        if !exact_keys(values, &fields) {
            return Err(SessionError::invalid(format!(
                "Методика {} требует ровно поля: {}",
                text(&payload["methodology"]),
                fields.join(", ")
            )));
        }
        for name in fields {
            let value = &values[name];
            if !value.is_number()
                || (!integer(value) && !value.as_f64().is_some_and(f64::is_finite))
            {
                return Err(SessionError::invalid(format!(
                    "Значение {name} должно быть конечным числом"
                )));
            }
        }
    }
    Ok(
        json!({"methodology":payload["methodology"],"result_code":code,"frequency_hz":hz,"interval":span,"values":values}),
    )
}
fn safe_entry_hz(value: &Value, bandwidth: bool) -> SessionResult<()> {
    if bandwidth && value.is_null() {
        return Ok(());
    }
    if !integer(value) || value.as_u64().is_none_or(|v| v > SAFE_HZ) {
        return Err(SessionError::invalid(if bandwidth {
            "bandwidth_hz записи должен быть целым числом Hz или null"
        } else {
            "frequency_hz записи должен быть целым числом в диапазоне безопасных Hz"
        }));
    }
    Ok(())
}
fn signal_entry(entry: &Value) -> SessionResult<Value> {
    if !exact_keys(
        entry,
        &[
            "frequency_hz",
            "bandwidth_hz",
            "interval",
            "level_unit",
            "peak_level",
        ],
    ) {
        return Err(SessionError::invalid(
            "Запись требует только frequency_hz, bandwidth_hz, interval, level_unit и peak_level",
        ));
    }
    safe_entry_hz(&entry["frequency_hz"], false)?;
    safe_entry_hz(&entry["bandwidth_hz"], true)?;
    let span = &entry["interval"];
    if !exact_keys(span, &["started_at", "ended_at"]) {
        return Err(SessionError::invalid(
            "interval записи должен содержать только started_at и ended_at",
        ));
    }
    for key in ["started_at", "ended_at"] {
        if span[key].as_str().is_none_or(str::is_empty) {
            return Err(SessionError::invalid(format!(
                "{key} записи должен быть непустой строкой UTC"
            )));
        }
    }
    let span = interval(span)?;
    if !["dBm", "s_meter_count"].contains(&text(&entry["level_unit"])) {
        return Err(SessionError::invalid(
            "level_unit записи должен быть dBm или s_meter_count",
        ));
    }
    let peak = &entry["peak_level"];
    if !peak.is_null() && !peak.is_number() {
        return Err(SessionError::invalid(
            "peak_level записи должен быть числом или null",
        ));
    }
    if peak.is_number() && !integer(peak) && !peak.as_f64().is_some_and(f64::is_finite) {
        return Err(SessionError::invalid(
            "peak_level записи должен быть конечным числом",
        ));
    }
    let mut value = entry.clone();
    value["interval"] = span;
    Ok(value)
}
fn signal_list(document: &Value) -> SessionResult<Value> {
    if !exact_keys(
        document,
        &[
            "format",
            "format_version",
            "name",
            "frequency_unit",
            "entries",
        ],
    ) {
        return Err(SessionError::invalid("Некорректные поля списка сигналов"));
    }
    if document["format"] != "openradioinspector-signal-list"
        || !integer(&document["format_version"])
        || document["format_version"].as_u64() != Some(1)
    {
        return Err(SessionError::invalid(
            "Неподдерживаемый формат или версия списка сигналов",
        ));
    }
    if document["frequency_unit"] != "Hz" {
        return Err(SessionError::invalid("Список сигналов требует единицу Hz"));
    }
    let name = name(&document["name"])?;
    let entries = document["entries"]
        .as_array()
        .filter(|items| (1..=256).contains(&items.len()))
        .ok_or_else(|| {
            SessionError::invalid("Список сигналов должен содержать от 1 до 256 записей")
        })?;
    let entries = entries
        .iter()
        .map(signal_entry)
        .collect::<SessionResult<Vec<_>>>()?;
    Ok(
        json!({"format":"openradioinspector-signal-list","format_version":1,"name":name,"frequency_unit":"Hz","entries":entries}),
    )
}
fn parse_list(value: &Value) -> SessionResult<Value> {
    let raw = value
        .as_str()
        .ok_or_else(|| SessionError::invalid("list_json должен быть строкой JSON UTF-8"))?;
    if raw.len() > 262_144 {
        return Err(SessionError::invalid(
            "Список сигналов должен занимать не более 262144 байт UTF-8",
        ));
    }
    let parsed = crate::signal_json::parse(raw)?;
    signal_list(&parsed)
}

impl SessionService {
    pub async fn list_sweep_references(
        &self,
        session: &str,
        stream: Option<&str>,
    ) -> SessionResult<Value> {
        let owner = self.session(session).await?;
        if let Some(stream) = stream {
            Self::stream(&owner, stream)?;
        }
        Ok(
            json!({"session_id":session,"stream_id":stream,"storage_mode":"sqlite","persistent":true,"limit":100,
            "items":self.archive.list_references(session,stream,100).await?}),
        )
    }
    pub async fn create_sweep_reference(
        &self,
        session: &str,
        payload: Value,
    ) -> SessionResult<Value> {
        if !exact_keys(&payload, &["name", "sweep_id"]) {
            return Err(SessionError::invalid("Нужны только поля name и sweep_id"));
        }
        let name = name(&payload["name"])?;
        let sweep_id = payload["sweep_id"]
            .as_str()
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| SessionError::invalid("sweep_id должен быть непустым ID свипа"))?;
        let (owner, sweep) = {
            let coordinator = self.archive.domain_coordinator();
            let _guard = coordinator.lock().await;
            let owner = self.session(session).await?;
            let sweep = self
                .archive
                .get_sweep(sweep_id)
                .await?
                .filter(|value| value["session_id"] == session)
                .ok_or_else(|| SessionError::missing("sweep_not_found", sweep_id))?;
            (owner, sweep)
        };
        self.effects
            .validate_reference(owner, sweep.clone())
            .await?;
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        self.session(session).await?;
        let current = self
            .archive
            .get_sweep(sweep_id)
            .await?
            .filter(|value| value["session_id"] == session)
            .ok_or_else(|| SessionError::missing("sweep_not_found", sweep_id))?;
        if current != sweep {
            return Err(SessionError::new(
                409,
                "comparison_incompatible",
                "Исходный свип изменился во время проверки совместимости",
            ));
        }
        let references = self.archive.list_references(session, None, 100).await?;
        if let Some(existing) = references.iter().find(|value| value["name"] == name) {
            if existing["sweep_id"] != sweep_id {
                return Err(SessionError::new(
                    409,
                    "reference_name_conflict",
                    "Это имя эталона уже используется для другого свипа",
                ));
            }
            return Ok(json!({"reference":existing,"created":false}));
        }
        if references.len() >= 100 {
            return Err(SessionError::new(
                409,
                "reference_limit_reached",
                "В сеансе уже сохранено 100 эталонов",
            ));
        }
        let reference = json!({"id":(self.ids)(),"session_id":session,"stream_id":sweep["stream_id"],"sweep_id":sweep_id,
            "name":name,"created_at":stamp((self.clock)()),"interval":sweep["interval"],"level_unit":sweep["level_unit"],
            "points":sweep["frequencies_hz"].as_array().map_or(0,Vec::len)});
        let result = self
            .archive
            .save_reference(
                reference.clone(),
                self.event(
                    "sweep_reference.created",
                    session,
                    json!({"reference":reference}),
                ),
            )
            .await?;
        Ok(
            json!({"reference":result.items.first().unwrap_or(&reference),"created":!result.events.is_empty()}),
        )
    }
    pub async fn measurement_catalog(&self) -> SessionResult<Value> {
        Ok(catalog())
    }
    pub async fn save_measurement_result(
        &self,
        session: &str,
        payload: Value,
    ) -> SessionResult<Value> {
        let stream = payload
            .get("stream_id")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| SessionError::invalid("stream_id должен быть непустым ID потока"))?;
        let mut body = payload.clone();
        body.as_object_mut()
            .expect("stream owner object")
            .remove("stream_id");
        let id = (self.ids)();
        let mut record = result_payload(&body)?;
        record["id"] = json!(id);
        record["session_id"] = json!(session);
        record["stream_id"] = json!(stream);
        record["created_at"] = json!(stamp((self.clock)()));
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        let owner = self.session(session).await?;
        Self::stream(&owner, stream)?;
        self.archive
            .save_measurement_result(
                record.clone(),
                Some(self.event(
                    "measurement_result.saved",
                    session,
                    json!({"measurement_result":record}),
                )),
            )
            .await?;
        Ok(record)
    }
    pub async fn list_measurement_results(
        &self,
        session: &str,
        methodology: Option<&str>,
        limit: Option<usize>,
    ) -> SessionResult<Value> {
        if let Some(methodology) = methodology
            && !catalog()
                .as_array()
                .expect("catalog")
                .iter()
                .any(|value| value["methodology"] == methodology)
        {
            return Err(SessionError::invalid(format!(
                "Неизвестная методика: {methodology}"
            )));
        }
        if limit.is_some_and(|limit| !(1..=1000).contains(&limit)) {
            return Err(SessionError::invalid(
                "limit должен быть целым от 1 до 1000",
            ));
        }
        self.session(session).await?;
        Ok(json!(
            self.archive
                .session_measurement_results(session, methodology, limit)
                .await?
        ))
    }
    pub async fn signal_list_export(
        &self,
        session: &str,
        label: &str,
        stream: Option<&str>,
    ) -> SessionResult<Value> {
        let owner = self.session(session).await?;
        if let Some(stream) = stream {
            Self::stream(&owner, stream)?;
        }
        let candidates = self.archive.session_candidates(session, None).await?;
        let mut entries = Vec::new();
        for candidate in candidates
            .iter()
            .filter(|value| stream.is_none_or(|stream| value["source_stream_id"] == stream))
        {
            if !["dBm", "s_meter_count"].contains(&text(&candidate["level_unit"])) {
                return Err(SessionError::new(
                    409,
                    "signal_list_error",
                    format!(
                        "Неизвестная единица кандидата: {}",
                        text(&candidate["level_unit"])
                    ),
                ));
            }
            entries.push(json!({"frequency_hz":candidate["center_frequency_hz"],"bandwidth_hz":candidate["bandwidth_hz"],
                "interval":candidate["interval"],"level_unit":candidate["level_unit"],"peak_level":candidate["peak_level"]}));
        }
        if entries.is_empty() {
            return Err(SessionError::new(
                409,
                "signal_list_error",
                "В сеансе нет кандидатов для экспорта",
            ));
        }
        let artifact = signal_list(
            &json!({"format":"openradioinspector-signal-list","format_version":1,
            "name":name(&json!(label))?,"frequency_unit":"Hz","entries":entries}),
        )?;
        let raw = serde_json::to_vec(&artifact)
            .map_err(|_| SessionError::invalid("Некорректный JSON UTF-8"))?;
        if raw.len() > 262_144 {
            return Err(SessionError::invalid(
                "Список сигналов должен занимать не более 262144 байт UTF-8",
            ));
        }
        Ok(
            json!({"session_id":session,"exported":artifact["entries"].as_array().expect("entries").len(),"list":artifact}),
        )
    }
    pub async fn signal_list_import(&self, session: &str, payload: Value) -> SessionResult<Value> {
        if !exact_keys(&payload, &["stream_id", "list_json"]) {
            return Err(SessionError::invalid(
                "Разрешены только поля stream_id и list_json",
            ));
        }
        let stream = payload["stream_id"]
            .as_str()
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| SessionError::invalid("stream_id должен быть непустым ID потока"))?;
        let artifact = parse_list(&payload["list_json"])?;
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        let owner = self.session(session).await?;
        let target = Self::stream(&owner, stream)?;
        let entries = artifact["entries"].as_array().expect("validated entries");
        for entry in entries {
            if entry["level_unit"] != target["level_unit"] {
                return Err(SessionError::new(
                    409,
                    "signal_list_error",
                    format!(
                        "Единица записи {} не совпадает с потоком {}; автопреобразование запрещено",
                        text(&entry["level_unit"]),
                        text(&target["level_unit"])
                    ),
                ));
            }
        }
        let candidates=entries.iter().map(|entry|self.candidate_value(session,&json!({"source_stream_id":stream,"interval":entry["interval"],
            "center_frequency_hz":entry["frequency_hz"],"bandwidth_hz":entry["bandwidth_hz"],"peak_level":entry["peak_level"],"level_unit":entry["level_unit"]}))).collect::<SessionResult<Vec<_>>>()?;
        let mut ids = HashSet::new();
        for candidate in &candidates {
            let id = text(&candidate["id"]);
            if !ids.insert(id.to_owned()) || self.archive.get_candidate(id).await?.is_some() {
                return Err(SessionError::invalid("Повторяющийся ID кандидата"));
            }
        }
        let created = candidates
            .iter()
            .map(|candidate| candidate["id"].clone())
            .collect::<Vec<_>>();
        let mutations = candidates
            .into_iter()
            .map(|candidate| Mutation::Candidate {
                event: Some(self.event(
                    "candidate.created",
                    session,
                    json!({"candidate":candidate}),
                )),
                value: candidate,
            })
            .collect();
        self.archive.apply(mutations).await?;
        Ok(
            json!({"session_id":session,"stream_id":stream,"imported":created.len(),"candidate_ids":created,"name":artifact["name"]}),
        )
    }
}
