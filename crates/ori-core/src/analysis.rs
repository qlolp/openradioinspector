//! One read-only scientific child, over a bounded consistent source snapshot.
use crate::{
    Core,
    error::{ApiError, ApiResult},
};
use axum::{http::StatusCode, response::Response};
use ori_archive::ArchiveError;
use ori_runtime::worker::Cancellation;
use serde_json::{Map, Value};
use std::{
    collections::{BTreeMap, HashSet},
    sync::Arc,
};

pub(crate) fn method(route: &str) -> Option<&'static str> {
    Some(match route {
        "sweep-comparison" => "compare_sweeps",
        "sweep-threshold" => "sweep_threshold",
        "sweep-average-comparison" => "sweep_average_comparison",
        "sweep-average-change-detection" => "sweep_average_change_detection",
        "sweep-three-thresholds" => "sweep_three_thresholds",
        "sweep-context" => "sweep_context",
        "sweep-occupancy" => "sweep_occupancy",
        "sweep-mask-analysis" => "sweep_mask_analysis",
        "candidate-frequency-rounding" => "candidate_frequency_rounding",
        "frequency-rounding" => "frequency_rounding",
        "sweep-quality" => "sweep_quality",
        "frequency-counter" => "frequency_counter",
        "field-strength-rms-v2" => "field_strength_rms_v2",
        "dvb-evaluation" => "dvb_evaluation",
        "measurement-map" => "measurement_map",
        _ => return None,
    })
}
pub(crate) fn query(raw: Option<&str>, method: &str) -> ApiResult<BTreeMap<String, String>> {
    let raw = raw.unwrap_or_default();
    let bytes = raw.as_bytes();
    for (i, byte) in bytes.iter().enumerate() {
        if *byte == b'%'
            && (bytes.get(i + 1).is_none_or(|b| !b.is_ascii_hexdigit())
                || bytes.get(i + 2).is_none_or(|b| !b.is_ascii_hexdigit()))
        {
            return Err(ApiError::invalid(
                "Некорректное percent-кодирование параметров",
            ));
        }
    }
    let mut values = BTreeMap::new();
    if raw.is_empty() {
        return Ok(values);
    }
    let required = if method == "references" {
        "Разрешён только один непустой stream_id"
    } else if method == "compare_sweeps" {
        "Нужны ровно по одному reference_id и target_id"
    } else {
        "Нужны ровно по одному sweep_id, window_points и margin_db"
    };
    let decode = |value: &str| {
        percent_encoding::percent_decode_str(&value.replace('+', " "))
            .decode_utf8()
            .map(|v| v.into_owned())
            .map_err(|_| ApiError::invalid("Некорректный UTF-8 параметров"))
    };
    for field in raw.split('&') {
        let (key, value) = field
            .split_once('=')
            .ok_or_else(|| ApiError::invalid(required))?;
        if values.insert(decode(key)?, decode(value)?).is_some() {
            return Err(ApiError::invalid(required));
        }
    }
    Ok(values)
}
pub(crate) fn get_payload(method: &str, query: &BTreeMap<String, String>) -> ApiResult<Value> {
    let mut payload = Map::new();
    if method == "compare_sweeps" {
        if query.len() != 2
            || ["reference_id", "target_id"]
                .iter()
                .any(|key| query.get(*key).is_none_or(|v| v.trim().is_empty()))
        {
            return Err(ApiError::invalid(
                "Нужны ровно по одному reference_id и target_id",
            ));
        }
        for key in ["reference_id", "target_id"] {
            payload.insert(key.into(), Value::String(query[key].clone()));
        }
    } else {
        if query.len() != 3
            || ["sweep_id", "window_points", "margin_db"]
                .iter()
                .any(|key| query.get(*key).is_none_or(|v| v.trim().is_empty()))
        {
            return Err(ApiError::invalid(
                "Нужны ровно по одному sweep_id, window_points и margin_db",
            ));
        }
        let raw = &query["window_points"];
        if !raw.bytes().all(|v| v.is_ascii_digit()) {
            return Err(ApiError::invalid(
                "window_points должен быть нечётным целым от 3 до 101",
            ));
        }
        let window = raw.parse::<u64>().map_err(|_| {
            ApiError::invalid("window_points должен быть нечётным целым от 3 до 101")
        })?;
        let raw = &query["margin_db"];
        let margin = if decimal_float(raw) {
            raw.parse::<f64>().ok().filter(|v| v.is_finite())
        } else {
            None
        }
        .ok_or_else(|| ApiError::invalid("margin_db должен быть конечным числом от 0 до 120 dB"))?;
        payload.insert("sweep_id".into(), Value::String(query["sweep_id"].clone()));
        payload.insert("window_points".into(), window.into());
        payload.insert(
            "margin_db".into(),
            serde_json::Number::from_f64(margin)
                .expect("finite margin")
                .into(),
        );
    }
    Ok(Value::Object(payload))
}
fn decimal_float(value: &str) -> bool {
    let bytes = value.as_bytes();
    let mut i = usize::from(matches!(bytes.first(), Some(b'+' | b'-')));
    let mut digits = 0;
    while bytes.get(i).is_some_and(u8::is_ascii_digit) {
        i += 1;
        digits += 1;
    }
    if bytes.get(i) == Some(&b'.') {
        i += 1;
        while bytes.get(i).is_some_and(u8::is_ascii_digit) {
            i += 1;
            digits += 1;
        }
    }
    if digits == 0 {
        return false;
    }
    if matches!(bytes.get(i), Some(b'e' | b'E')) {
        i += 1;
        if matches!(bytes.get(i), Some(b'+' | b'-')) {
            i += 1;
        }
        let start = i;
        while bytes.get(i).is_some_and(u8::is_ascii_digit) {
            i += 1;
        }
        if i == start {
            return false;
        }
    }
    i == bytes.len()
}
fn id(value: &Value) -> Option<String> {
    value
        .as_str()
        .filter(|v| !v.trim().is_empty())
        .map(str::to_owned)
}
fn ids(method: &str, payload: &Value) -> (Vec<String>, Vec<String>) {
    let mut sweeps = Vec::new();
    let mut candidates = Vec::new();
    match method {
        "compare_sweeps" => {
            sweeps.extend(id(&payload["reference_id"]));
            sweeps.extend(id(&payload["target_id"]));
        }
        "sweep_average_comparison" | "sweep_average_change_detection" => {
            if let Some(values) = payload["reference_ids"]
                .as_array()
                .filter(|values| (2..=16).contains(&values.len()))
            {
                sweeps.extend(values.iter().filter_map(id));
                sweeps.extend(id(&payload["target_id"]));
            }
        }
        "candidate_frequency_rounding" => candidates.extend(id(&payload["candidate_id"])),
        "frequency_rounding" => {
            if let Some(values) = payload["candidate_ids"]
                .as_array()
                .filter(|v| (1..=256).contains(&v.len()))
            {
                candidates.extend(values.iter().filter_map(id));
            }
        }
        "dvb_evaluation" | "measurement_map" => {}
        _ => sweeps.extend(id(&payload["sweep_id"])),
    }
    let mut seen = HashSet::new();
    sweeps.retain(|id| seen.insert(id.clone()));
    seen.clear();
    candidates.retain(|id| seen.insert(id.clone()));
    (sweeps, candidates)
}
async fn sources(
    core: &Core,
    session: &str,
    method: &str,
    payload: &Value,
) -> ApiResult<(Value, Vec<Value>, Vec<Value>)> {
    let archive = core.archive()?;
    let context = if matches!(method, "sweep_context" | "sweep_occupancy") {
        id(&payload["sweep_id"])
            .filter(|id| id.chars().count() <= 128)
            .zip(payload["before"].as_u64().filter(|v| *v <= 16))
            .zip(payload["after"].as_u64().filter(|v| *v <= 16))
    } else {
        None
    };
    let selected = if let Some(((id, before), after)) = context {
        match archive
            .analysis_context(session, &id, before as usize, after as usize)
            .await
        {
            Ok(Some((session, context))) => {
                let mut sweeps = context.before;
                sweeps.push(context.anchor);
                sweeps.extend(context.after);
                Ok((session, sweeps, Vec::new()))
            }
            Ok(None) => {
                archive
                    .analysis_inputs(session, Vec::new(), Vec::new())
                    .await
            }
            Err(error) => Err(error),
        }
    } else {
        let (sweeps, candidates) = ids(method, payload);
        archive.analysis_inputs(session, sweeps, candidates).await
    };
    match selected {
        Ok(value) => Ok(value),
        Err(ArchiveError::NotFound) => Ok((Value::Null, Vec::new(), Vec::new())),
        Err(error) => Err(error.into()),
    }
}
fn unavailable() -> ApiError {
    ApiError::unavailable("research_unavailable")
}
fn protocol() -> ApiError {
    ApiError::new(
        StatusCode::BAD_GATEWAY,
        "research_protocol_error",
        "Научный обработчик вернул некорректный ответ",
    )
}
fn execution_error(error: ori_runtime::Error) -> ApiError {
    use ori_runtime::Error::*;
    match error {
        Protocol(message) if message == "private request byte limit" => ApiError::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "resource_limit",
            "Request byte limit exceeded",
        ),
        Busy => ApiError::unavailable("research_busy"),
        Cancelled => ApiError::unavailable("research_cancelled"),
        Deadline => ApiError::new(
            StatusCode::GATEWAY_TIMEOUT,
            "research_timeout",
            "Истёк срок научной обработки",
        ),
        ResourceLimit => ApiError::unavailable("research_resource_limit"),
        OutputLimit | Protocol(_) | Json(_) => protocol(),
        _ => unavailable(),
    }
}
fn worker_result(value: Value, request_id: &str) -> ApiResult<Value> {
    let valid = value.as_object().is_some_and(|v| v.len() == 5)
        && value["version"].as_u64() == Some(1)
        && value["request_id"] == request_id
        && value["operation"] == "analysis";
    if !valid {
        return Err(protocol());
    }
    match value["ok"].as_bool() {
        Some(true) if value.get("result").is_some() => Ok(value["result"].clone()),
        Some(false) if value.get("error").is_some() => {
            let error = &value["error"];
            let code = match error["api_code"].as_str() {
                Some("invalid_request") => "invalid_request",
                Some("missing_field") => "missing_field",
                Some("protocol_error") => "protocol_error",
                Some("resource_limit") => "resource_limit",
                Some("shared_io_unavailable") => "shared_io_unavailable",
                Some("internal_error") => "internal_error",
                Some("session_not_found") => "session_not_found",
                Some("sweep_not_found") => "sweep_not_found",
                Some("candidate_not_found") => "candidate_not_found",
                Some("comparison_incompatible") => "comparison_incompatible",
                Some("average_reference_incompatible") => "average_reference_incompatible",
                Some("threshold_incompatible") => "threshold_incompatible",
                Some("three_thresholds_incompatible") => "three_thresholds_incompatible",
                Some("context_incompatible") => "context_incompatible",
                Some("frequency_rounding_incompatible") => "frequency_rounding_incompatible",
                Some("quality_incompatible") => "quality_incompatible",
                Some("counter_incompatible") => "counter_incompatible",
                Some("field_strength_unavailable") => "field_strength_unavailable",
                Some("dvb_evaluation_error") => "dvb_evaluation_error",
                Some("measurement_map_error") => "measurement_map_error",
                Some("mask_incompatible") => "mask_incompatible",
                _ => return Err(protocol()),
            };
            let status = match error["http_status"].as_u64() {
                Some(400) => StatusCode::BAD_REQUEST,
                Some(404) => StatusCode::NOT_FOUND,
                Some(409) => StatusCode::CONFLICT,
                Some(413) => StatusCode::PAYLOAD_TOO_LARGE,
                Some(500) => StatusCode::INTERNAL_SERVER_ERROR,
                Some(503) if code == "shared_io_unavailable" => StatusCode::SERVICE_UNAVAILABLE,
                _ => return Err(protocol()),
            };
            let message = error["api_message"].as_str().ok_or_else(protocol)?;
            Err(ApiError::new(status, code, message))
        }
        _ => Err(protocol()),
    }
}
async fn execute(
    core: &Core,
    session_id: &str,
    method: &'static str,
    sources: (Value, Vec<Value>, Vec<Value>),
    payload: Value,
    permit: ori_runtime::worker::ResearchPermit,
    cancel: Cancellation,
) -> ApiResult<Value> {
    let (session, sweeps, candidates) = sources;
    let spec = core
        .config
        .analysis_worker
        .clone()
        .ok_or_else(unavailable)?;
    if *core.shutdown.borrow() {
        return Err(ApiError::unavailable("research_cancelled"));
    }
    let request_id = crate::new_id();
    let mut request = Map::new();
    request.insert("version".into(), 1.into());
    request.insert("request_id".into(), Value::String(request_id.clone()));
    request.insert("operation".into(), Value::String("analysis".into()));
    request.insert("method".into(), Value::String(method.into()));
    if session.is_null() {
        request.insert("session_id".into(), Value::String(session_id.into()));
    }
    request.insert("session".into(), session);
    request.insert("sweeps".into(), Value::Array(sweeps));
    request.insert("candidates".into(), Value::Array(candidates));
    request.insert("payload".into(), payload);
    let request = Value::Object(request);
    let response = crate::private_worker::run(
        core,
        permit,
        spec,
        request,
        cancel,
        None,
        crate::private_worker::IoClass::MemoryOnly,
    )
    .await
    .map_err(execution_error)?;
    worker_result(response.response, &request_id)
}
pub(crate) async fn response(
    core: &Arc<Core>,
    session: &str,
    method: &'static str,
    payload: Value,
) -> ApiResult<Response> {
    core.archive()?;
    if core.config.analysis_worker.is_none() {
        return Err(unavailable());
    }
    let result = owned(core, session, method, payload, None).await?;
    Ok(crate::http::envelope(StatusCode::OK, result))
}
pub(crate) async fn validate_reference(
    core: &Arc<Core>,
    session: Value,
    sweep: Value,
) -> ApiResult<()> {
    if core.config.analysis_worker.is_none() {
        return Err(unavailable());
    }
    let session_id = session["id"].as_str().ok_or_else(protocol)?.to_owned();
    let sweep_id = sweep["id"].as_str().ok_or_else(protocol)?.to_owned();
    let mut payload = Map::new();
    payload.insert("reference_id".into(), sweep_id.clone().into());
    payload.insert("target_id".into(), sweep_id.into());
    owned(
        core,
        &session_id,
        "compare_sweeps",
        Value::Object(payload),
        Some((session, vec![sweep], Vec::new())),
    )
    .await?;
    Ok(())
}

struct CancelWaiter(Option<Cancellation>);
impl Drop for CancelWaiter {
    fn drop(&mut self) {
        if let Some(cancel) = &self.0 {
            cancel.cancel();
        }
    }
}
/// Own source selection too: a disconnected waiter cannot release global
/// admission while its already admitted Archive read is still running.
async fn owned(
    core: &Arc<Core>,
    session: &str,
    method: &'static str,
    payload: Value,
    supplied: Option<(Value, Vec<Value>, Vec<Value>)>,
) -> ApiResult<Value> {
    let permit = core.research_pool.try_reserve().map_err(execution_error)?;
    let cancel = Cancellation::default();
    let guard = core
        .background
        .as_ref()
        .ok_or_else(unavailable)?
        .qos
        .register(cancel.clone())
        .map_err(|_| ApiError::unavailable("research_paused"))?;
    let mut waiter = CancelWaiter(Some(cancel.clone()));
    let (sent, receiver) = tokio::sync::oneshot::channel();
    let own = core.clone();
    let session = session.to_owned();
    tokio::spawn(async move {
        let _guard = guard;
        let outcome = async {
            if cancel.is_cancelled() || *own.shutdown.borrow() {
                return Err(ApiError::unavailable("research_cancelled"));
            }
            let selected = match supplied {
                Some(value) => value,
                None => sources(&own, &session, method, &payload).await?,
            };
            if cancel.is_cancelled() || *own.shutdown.borrow() {
                return Err(ApiError::unavailable("research_cancelled"));
            }
            execute(&own, &session, method, selected, payload, permit, cancel).await
        }
        .await;
        let _ = sent.send(outcome);
    });
    let outcome = receiver
        .await
        .map_err(|_| ApiError::unavailable("research_completion_lost"))?;
    waiter.0 = None;
    outcome
}
