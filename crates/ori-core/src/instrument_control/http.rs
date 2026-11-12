use super::*;
use crate::{
    error::{ApiError, ApiResult},
    http::envelope,
};
use axum::{
    body::Body,
    http::{HeaderMap, Method, StatusCode, header},
    response::Response,
};
use ori_contracts::to_json_vec;
use serde_json::{Value, json};

pub(crate) fn handles(method: &Method, parts: &[String]) -> bool {
    let p = parts.iter().map(String::as_str).collect::<Vec<_>>();
    matches!(
        p.as_slice(),
        ["api", "v1", "sessions", _, "streams", _, "instrument", ..]
    ) || (*method == Method::POST
        && matches!(
            p.as_slice(),
            [
                "api",
                "v1",
                "sessions",
                _,
                "streams",
                _,
                "start" | "stop" | "tune"
            ]
        ))
}
pub(super) fn api(error: ControlError) -> ApiError {
    match error {
        ControlError::Invalid(message) => {
            ApiError::new(StatusCode::BAD_REQUEST, "instrument_invalid", message)
        }
        ControlError::Conflict(message) => {
            ApiError::new(StatusCode::CONFLICT, "instrument_conflict", message)
        }
        ControlError::NotFound => missing("Operation not found"),
        ControlError::Persistence(_) => ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "instrument_persistence_unavailable",
            "Результат операции не записан; управление закрыто",
        ),
        ControlError::Closed => ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "instrument_unavailable",
            "Управление прибором закрыто",
        ),
        error => ApiError::new(
            StatusCode::CONFLICT,
            "instrument_conflict",
            error.to_string(),
        ),
    }
}
pub(super) fn quoted(message: &str) -> String {
    // Python KeyError's public message includes its quoted original argument.
    let quote = if message.contains('\'') && !message.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut text = String::new();
    text.push(quote);
    for c in message.chars() {
        match c {
            '\\' => text.push_str("\\\\"),
            '\n' => text.push_str("\\n"),
            '\r' => text.push_str("\\r"),
            '\t' => text.push_str("\\t"),
            c if c == quote => {
                text.push('\\');
                text.push(c);
            }
            c if c != ' '
                && matches!(
                    unicode_general_category::get_general_category(c),
                    unicode_general_category::GeneralCategory::Control
                        | unicode_general_category::GeneralCategory::Format
                        | unicode_general_category::GeneralCategory::LineSeparator
                        | unicode_general_category::GeneralCategory::ParagraphSeparator
                        | unicode_general_category::GeneralCategory::PrivateUse
                        | unicode_general_category::GeneralCategory::SpaceSeparator
                        | unicode_general_category::GeneralCategory::Surrogate
                        | unicode_general_category::GeneralCategory::Unassigned
                ) =>
            {
                use std::fmt::Write;
                let value = c as u32;
                if value <= 0xff {
                    write!(&mut text, "\\x{value:02x}").expect("string write");
                } else if value <= 0xffff {
                    write!(&mut text, "\\u{value:04x}").expect("string write");
                } else {
                    write!(&mut text, "\\U{value:08x}").expect("string write");
                }
            }
            c => text.push(c),
        }
    }
    text.push(quote);
    text
}
fn missing(message: &str) -> ApiError {
    ApiError::new(
        StatusCode::NOT_FOUND,
        "instrument_not_found",
        quoted(message),
    )
}
async fn scope(core: &Core, target: &Target, artifacts: bool) -> ApiResult<Value> {
    let session = core
        .archive()?
        .get_session(&target.session_id)
        .await?
        .ok_or_else(|| missing(&format!("Сеанс не найден: {}", target.session_id)))?;
    let stream = session["streams"]
        .as_array()
        .and_then(|items| items.iter().find(|item| item["id"] == target.stream_id))
        .cloned()
        .ok_or_else(|| missing("Stream not found in session"))?;
    if !artifacts && stream["settings"]["adapter"] != "tinysa-serial" {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "instrument_invalid",
            "Stream is not tinySA serial",
        ));
    }
    Ok(stream)
}
fn service(core: &Core) -> ApiResult<&Arc<InstrumentControl>> {
    core.instrument_control
        .get()
        .ok_or_else(|| ApiError::unavailable("instrument_unavailable"))
}
fn client(headers: &HeaderMap) -> ApiResult<Option<&str>> {
    headers
        .get("X-Instrument-Client")
        .map(|value| {
            value.to_str().map_err(|_| {
                ApiError::new(
                    StatusCode::BAD_REQUEST,
                    "instrument_invalid",
                    "Invalid X-Instrument-Client capability",
                )
            })
        })
        .transpose()
}
fn count(
    query: &BTreeMap<String, String>,
    key: &str,
    default: usize,
    min: usize,
    max: usize,
) -> ApiResult<usize> {
    match query.get(key) {
        Some(raw) => super::numeric::count(raw, key, min, max),
        None => Ok(default),
    }
}
fn control_owner(payload: &mut Value) -> ApiResult<Option<String>> {
    let value = payload
        .as_object_mut()
        .ok_or_else(|| ApiError::invalid("Ожидается объект JSON"))?
        .remove("control_owner");
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if !value.is_empty() && value.len() <= 256 => Ok(Some(value)),
        _ => Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "instrument_invalid",
            "Invalid original manual control_owner",
        )),
    }
}
pub(crate) async fn dispatch(
    core: &Arc<Core>,
    method: &Method,
    parts: &[String],
    query: &BTreeMap<String, String>,
    headers: &HeaderMap,
    auth: AuthReceipt,
    payload: Option<Value>,
) -> ApiResult<Option<Response>> {
    if !handles(method, parts) {
        return Ok(None);
    }
    auth.revalidate()?;
    let target = Target {
        session_id: parts[3].clone(),
        stream_id: parts[5].clone(),
    };
    if parts[6] != "instrument" {
        return connect(
            core,
            &target,
            &parts[6],
            auth,
            payload.unwrap_or_else(|| json!({})),
        )
        .await
        .map(Some);
    }
    let tail = parts[7..].iter().map(String::as_str).collect::<Vec<_>>();
    let artifacts = *method == Method::GET && tail.first() == Some(&"artifacts");
    scope(core, &target, artifacts).await?;
    if artifacts {
        return artifact(core, &target, &tail, query).await.map(Some);
    }
    let service = service(core)?;
    if *method == Method::GET {
        let response = match tail.as_slice() {
            ["capabilities"] => envelope(
                StatusCode::OK,
                service.coordinator.capabilities(&target).map_err(api)?,
            ),
            ["settings"] | ["control-lease"] => envelope(
                StatusCode::OK,
                service.coordinator.status(&target).map_err(api)?,
            ),
            ["operations"] => envelope(
                StatusCode::OK,
                json!({"items":service.coordinator.list(&target).map_err(api)?}),
            ),
            ["operations", id] => envelope(
                StatusCode::OK,
                service
                    .coordinator
                    .get(&target, id, client(headers)?)
                    .map_err(api)?,
            ),
            ["screen"] => {
                let _admission = core.admission.enter()?;
                let raw = br#"{"operation":"capture","parameters":{"save":false}}"#;
                envelope(
                    StatusCode::ACCEPTED,
                    service
                        .submit(&target, raw, None, auth, None)
                        .await
                        .map_err(api)?,
                )
            }
            _ => return Err(missing("Instrument route not found")),
        };
        return Ok(Some(response));
    }
    if *method != Method::POST {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "instrument_invalid",
            "Unsupported method",
        ));
    }
    let mut payload = payload.ok_or_else(|| {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "instrument_invalid",
            "Invalid instrument request",
        )
    })?;
    let control_owner = control_owner(&mut payload)?;
    match tail.as_slice() {
        ["operations", id, "cancel"] => Ok(Some(envelope(
            StatusCode::OK,
            service.coordinator.cancel(&target, id).await.map_err(api)?,
        ))),
        ["operations"] | ["settings"] | ["control-lease"] => {
            if tail[0] == "settings" {
                payload["operation"] = json!("setting");
            }
            if tail[0] == "control-lease" {
                let action = payload
                    .as_object_mut()
                    .expect("bounded HTTP object")
                    .remove("action")
                    .unwrap_or_else(|| json!("acquire"));
                payload["operation"] = json!(match action.as_str() {
                    Some("acquire") => "lease.acquire",
                    Some("renew") => "lease.renew",
                    Some("release") => "lease.release",
                    Some("resume") => "monitor.resume",
                    _ =>
                        return Err(ApiError::new(
                            StatusCode::BAD_REQUEST,
                            "instrument_invalid",
                            "Unknown lease action"
                        )),
                });
            }
            let raw = to_json_vec(&payload).map_err(|_| {
                ApiError::new(
                    StatusCode::BAD_REQUEST,
                    "instrument_invalid",
                    "Invalid finite instrument request",
                )
            })?;
            Ok(Some(envelope(
                StatusCode::ACCEPTED,
                service
                    .submit(
                        &target,
                        &raw,
                        client(headers)?,
                        auth,
                        control_owner.as_deref(),
                    )
                    .await
                    .map_err(api)?,
            )))
        }
        _ => Err(missing("Instrument route not found")),
    }
}
async fn artifact(
    core: &Core,
    target: &Target,
    tail: &[&str],
    query: &BTreeMap<String, String>,
) -> ApiResult<Response> {
    let archive = core.archive()?;
    if tail == ["artifacts"] {
        return Ok(envelope(
            StatusCode::OK,
            json!({"items":archive.list_artifacts(&target.session_id,&target.stream_id,count(query,"limit",100,1,100)?,count(query,"offset",0,0,10_000_000)?).await?}),
        ));
    }
    if !(tail.len() == 2 || tail.len() == 3 && tail[2] == "content") {
        return Err(missing("Artifact route not found"));
    }
    let item = archive
        .get_artifact(&target.session_id, &target.stream_id, tail[1])
        .await?
        .ok_or_else(|| missing("Artifact not found"))?;
    if tail.len() == 2 {
        return Ok(envelope(StatusCode::OK, crate::media::summary(&item)));
    }
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, item.mime_type.clone())
        .header(header::CONTENT_LENGTH, item.payload.len())
        .header(
            header::CONTENT_DISPOSITION,
            format!(
                "{}; filename=\"{}\"",
                if item.mime_type == "image/png" {
                    "inline"
                } else {
                    "attachment"
                },
                item.filename
            ),
        )
        .header(header::CACHE_CONTROL, "no-store")
        .header("X-Content-Type-Options", "nosniff")
        .body(Body::from(item.payload))
        .map_err(|_| ApiError::unavailable("instrument_artifact_header"))
}
pub(super) fn guard_worker(status: devices::WorkerStatus) -> ori_guard::WorkerStatus {
    let mut extra = status
        .value()
        .expect("fixed worker state")
        .as_object()
        .expect("worker object")
        .clone();
    for key in ["session_id", "stream_id", "state", "alive", "current"] {
        extra.remove(key);
    }
    ori_guard::WorkerStatus {
        session_id: status.session_id,
        stream_id: status.stream_id,
        state: status.state,
        alive: status.alive,
        current: status
            .current
            .as_object()
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .collect(),
        extra: extra.into_iter().collect(),
        worker_instance: status.worker_instance,
    }
}
async fn connect(
    core: &Arc<Core>,
    target: &Target,
    operation: &str,
    auth: AuthReceipt,
    mut payload: Value,
) -> ApiResult<Response> {
    let control_owner = control_owner(&mut payload)?;
    core.session(&target.session_id).await?;
    let guard = core.domain()?.guard.clone();
    auth.revalidate()?;
    let grant = guard
        .capture_instrument_control(
            &ori_guard::ControlTarget {
                session_id: target.session_id.clone(),
                stream_id: target.stream_id.clone(),
            },
            control_owner.as_deref(),
        )
        .map_err(|_| api(ControlError::Denied))?;
    let manager = core
        .devices
        .as_ref()
        .ok_or_else(|| ApiError::unavailable("instrument_owner"))?
        .clone();
    let device_target = devices::Target::new(&target.session_id, &target.stream_id);
    let value = match operation {
        "start" => super::setup::start(core, target, auth, grant).await?,
        "stop" => {
            grant.check().map_err(|_| api(ControlError::Denied))?;
            let captured = manager
                .capture_worker(
                    &device_target,
                    gate(
                        auth.clone(),
                        grant.clone(),
                        grant.expected_worker_instance().map(str::to_owned),
                    ),
                )
                .ok();
            let status = if let Some(captured) = captured {
                service(core)?.close_retained(&captured).await?
            } else {
                auth.revalidate()?;
                grant.check().map_err(|_| api(ControlError::Denied))?;
                service(core)?.close_target(&device_target).await?;
                manager
                    .stop_stream(&target.session_id, &target.stream_id)
                    .await?
            };
            if grant.manual_role().is_some() && grant.stopped_worker_instance().is_none() {
                let mut stopped = guard_worker(status.clone());
                if !stopped.alive {
                    stopped.state = "stopped".into();
                }
                guard
                    .confirm_manual_worker_stopped(&grant, &stopped)
                    .map_err(|_| api(ControlError::Denied))?;
            }
            status.value()?
        }
        "tune" => {
            let frequency = payload["frequency_hz"]
                .as_u64()
                .ok_or_else(|| ApiError::invalid("frequency_hz должно быть целым числом"))?;
            let mode = payload
                .get("mode")
                .filter(|v| !v.is_null())
                .map(|v| {
                    v.as_str()
                        .map(str::to_owned)
                        .ok_or_else(|| ApiError::invalid("mode должно быть строкой"))
                })
                .transpose()?;
            let retained = manager.capture_worker(
                &device_target,
                gate(
                    auth,
                    grant.clone(),
                    grant.expected_worker_instance().map(str::to_owned),
                ),
            )?;
            manager
                .tune(device_target, frequency, mode, retained.execution_gate())
                .await?
                .value()?
        }
        _ => return Err(ApiError::invalid("Unsupported device operation")),
    };
    Ok(envelope(StatusCode::OK, value))
}
