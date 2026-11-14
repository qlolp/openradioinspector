use crate::{
    Core,
    error::{ApiError, ApiResult},
    new_id, utc_now,
};
use axum::{
    body::{Body, to_bytes},
    extract::{ConnectInfo, Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{
        IntoResponse, Response,
        sse::{Event, KeepAlive, Sse},
    },
};
use ori_contracts::{Envelope, parse_json_value, to_json_vec};
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet},
    convert::Infallible,
    net::SocketAddr,
    sync::Arc,
    time::Duration,
};
use subtle::ConstantTimeEq;

#[cfg(all(test, not(windows)))]
#[path = "http_manual_tests.rs"]
mod manual_tests;

pub(crate) fn json_response(status: StatusCode, value: &impl Serialize) -> Response {
    match to_json_vec(value) {
        Ok(bytes) if bytes.len()<=16*1024*1024=>Response::builder().status(status).header(header::CONTENT_TYPE,"application/json; charset=utf-8").header(header::CACHE_CONTROL,"no-store").header(header::CONTENT_LENGTH,bytes.len()).body(Body::from(bytes)).expect("static response headers"),
        _=>Response::builder().status(StatusCode::SERVICE_UNAVAILABLE).header(header::CONTENT_TYPE,"application/json").body(Body::from(r#"{"contract":"openradioinspector-platform","contract_version":1,"error":{"code":"response_budget","message":"Response exceeds bounded serialization"}}"#)).expect("static response"),
    }
}
pub(crate) fn envelope(status: StatusCode, data: Value) -> Response {
    json_response(status, &Envelope::new(data))
}
fn scoped_parts(path: &str) -> ApiResult<(Vec<String>, Option<String>)> {
    let mut parts = path_parts(path)?;
    if parts.len() >= 3 && parts[..3] == ["api", "v1", "instances"] {
        let owner = parts
            .get(3)
            .filter(|value| {
                !value.is_empty()
                    && value.len() <= 128
                    && value
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
            })
            .ok_or_else(|| ApiError::invalid("Invalid runtime instance scope"))?
            .clone();
        if parts.len() < 5 {
            return Err(ApiError::invalid("Missing scoped API resource"));
        }
        parts.drain(2..4);
        Ok((parts, Some(owner)))
    } else {
        Ok((parts, None))
    }
}

pub(crate) fn request_limit(path: &str) -> usize {
    if path == "/internal/runtime/v1/shutdown" {
        return 4096;
    }
    let Ok((parts, _)) = scoped_parts(path) else {
        return 65536;
    };
    let route = parts.iter().map(String::as_str).collect::<Vec<_>>();
    if route.starts_with(&["api", "v1", "security"]) {
        return 4096;
    }
    match route.as_slice() {
        [
            "api",
            "v1",
            "sessions",
            _,
            "sweeps" | "observations" | "audio-segments",
        ]
        | [
            "api",
            "v1",
            "sessions",
            _,
            "sweeps" | "observations",
            "batch",
        ]
        | ["api", "v1", "sessions", _, "phonogram", "frames"] => 16 * 1024 * 1024,
        [
            "api",
            "v1",
            "sessions",
            _,
            "sweep-mask-analysis" | "candidate-frequency-rounding" | "frequency-rounding",
        ]
        | ["api", "v1", "sessions", _, "signal-list", "import"] => 524288,
        ["api", "v1", "sessions", _, "measurement-map"] => 262144,
        [
            "api",
            "v1",
            "sessions",
            _,
            "sweep-average-comparison"
            | "sweep-average-change-detection"
            | "sweep-three-thresholds"
            | "sweep-context"
            | "sweep-occupancy"
            | "sweep-quality"
            | "frequency-counter"
            | "dvb-evaluation",
        ] => 8192,
        _ => 65536,
    }
}
pub(crate) async fn handle(
    State(core): State<Arc<Core>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    request: Request,
) -> Response {
    let private = request.uri().path().starts_with("/internal/runtime/");
    let legacy =
        request.uri().path().starts_with("/api/") && !request.uri().path().starts_with("/api/v1/");
    let slots = if private {
        core.private_requests.clone()
    } else {
        core.requests.clone()
    };
    let Ok(_slot) = slots.try_acquire_owned() else {
        let error = ApiError::unavailable("request_queue_full");
        return close_http(if legacy {
            crate::legacy::plain_error(error)
        } else {
            error.into_response()
        });
    };
    let response = match dispatch(core, peer, request, private).await {
        Ok(response) => response,
        Err(error) => {
            if legacy {
                crate::legacy::plain_error(error)
            } else {
                error.into_response()
            }
        }
    };
    close_http(response)
}
fn close_http(mut response: Response) -> Response {
    // Hyper's global keep_alive(false) adds "close" even to101 and breaks
    // WebSocket negotiation. Only upgrades keep their connection; ordinary
    // HTTP retains exactly one request and the pre-Hyper framing boundary.
    if response.status() != StatusCode::SWITCHING_PROTOCOLS {
        response.headers_mut().insert(
            header::CONNECTION,
            header::HeaderValue::from_static("close"),
        );
    }
    response
}
async fn dispatch(
    core: Arc<Core>,
    peer: SocketAddr,
    request: Request,
    private: bool,
) -> ApiResult<Response> {
    let role = request
        .extensions()
        .get::<crate::security::ListenerRole>()
        .copied()
        .unwrap_or(crate::security::ListenerRole::Plain);
    let method = request.method().clone();
    let uri = request.uri().clone();
    let headers = request.headers().clone();
    if role == crate::security::ListenerRole::Control && !private {
        return Err(ApiError::missing("not_found"));
    }
    if role == crate::security::ListenerRole::Tls && private {
        return Err(ApiError::missing("not_found"));
    }
    if private {
        return control(
            core,
            peer,
            method,
            uri.path(),
            uri.query(),
            headers,
            request.into_body(),
        )
        .await;
    }
    let (parts, runtime_owner) = scoped_parts(uri.path())?;
    let security_path = if runtime_owner.is_some() {
        format!(
            "/{}",
            parts
                .iter()
                .map(|part| ori_contracts::encode_path_segment(part))
                .collect::<Vec<_>>()
                .join("/")
        )
    } else {
        uri.path().to_owned()
    };
    let authentication = core
        .security
        .check(&method, &security_path, &headers, role)
        .await?;
    if let Some(expected) = runtime_owner
        && !core
            .config
            .identity
            .as_ref()
            .is_some_and(|actual| actual.instance_id == expected)
    {
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            "runtime_owner_changed",
            "The privately verified runtime owner has changed; no command was executed",
        ));
    }
    let scientific = parts
        .get(4)
        .and_then(|name| crate::analysis::method(name))
        .filter(|_| parts.len() == 5 && parts[..3] == ["api", "v1", "sessions"]);
    let references = parts.len() == 5
        && parts[..3] == ["api", "v1", "sessions"]
        && parts[4] == "sweep-references";
    if references && method == Method::POST && uri.query().is_some_and(|query| !query.is_empty()) {
        return Err(ApiError::invalid(
            "POST эталона не принимает параметры запроса",
        ));
    }
    if method == Method::POST && scientific.is_some() && uri.query().is_some() {
        return Err(ApiError::invalid("Анализ не принимает параметры запроса"));
    }
    let query = if parts.len() == 2 && parts[0] == "api" {
        let mut values = BTreeMap::new();
        // Existing legacy parse_qs keeps the first nonempty value and ignores
        // blank values; its numeric grammar is separate from v1's strict IDs.
        for (key, value) in url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        {
            if !value.is_empty() {
                values.entry(key.into_owned()).or_insert(value.into_owned());
            }
        }
        values
    } else if references && method == Method::GET {
        crate::analysis::query(uri.query(), "references")?
    } else if method == Method::GET
        && matches!(scientific, Some("compare_sweeps" | "sweep_threshold"))
    {
        crate::analysis::query(uri.query(), scientific.expect("matched analysis GET"))?
    } else {
        query(uri.query())?
    };
    if method == Method::POST && crate::history_jobs::handles(&parts) && !query.is_empty() {
        return Err(ApiError::invalid(
            "History operation accepts no query parameters",
        ));
    }
    if method == Method::GET && parts == ["api", "v1", "events", "ws"] {
        return crate::websocket::upgrade(core, request, query, authentication).await;
    }
    if security_path == "/api/v1/security/status" && method == Method::GET {
        return Ok(envelope(
            StatusCode::OK,
            core.security.status(&authentication),
        ));
    }
    if security_path.starts_with("/api/v1/security/") {
        let (payload, _budget) = read_json(
            &core,
            &headers,
            request.into_body(),
            4096,
            method == Method::DELETE,
            false,
        )
        .await?;
        let _admitted = core.admission.enter()?;
        authentication.receipt().revalidate()?;
        return match (method.as_str(), security_path.as_str()) {
            ("POST", "/api/v1/security/pair") => core.security.pair(payload, &headers).await,
            ("POST", "/api/v1/security/browser-session") => {
                core.security.browser_session(&authentication, &payload)
            }
            ("DELETE", "/api/v1/security/credentials/current")
            | ("POST", "/api/v1/security/credentials/current/revoke") => {
                core.security.revoke(&authentication, &payload).await
            }
            _ => Err(ApiError::missing("not_found")),
        };
    }
    if method == Method::GET {
        if !uri.path().starts_with("/api/") {
            return crate::assets::response(uri.path())
                .ok_or_else(|| ApiError::missing("not_found"));
        }
        if crate::instrument_control::handles(&method, &parts)
            && let Some(response) = crate::instrument_control::dispatch(
                &core,
                &method,
                &parts,
                &query,
                &headers,
                authentication.receipt(),
                None,
            )
            .await?
        {
            return Ok(response);
        }
        return get(core, parts, query, headers, authentication).await;
    }
    if method != Method::POST {
        return Err(ApiError::missing("not_found"));
    }
    let measurement = parts
        .get(4)
        .is_some_and(|part| matches!(part.as_str(), "sweeps" | "observations" | "audio-segments"));
    let maximum = request_limit(uri.path());
    let (payload, _budget) = read_json(
        &core,
        &headers,
        request.into_body(),
        maximum,
        !measurement && scientific.is_none() && !(parts.len() == 2 && parts[0] == "api"),
        false,
    )
    .await?;
    let admitted = core.admission.enter()?;
    authentication.receipt().revalidate()?;
    if crate::audio_http::handles(&Method::POST, &parts) {
        return crate::audio_http::post(&core, &parts, payload, authentication.receipt())
            .await?
            .ok_or_else(|| ApiError::missing("not_found"));
    }
    if crate::manual_requests::handles(&parts) {
        return crate::manual_requests::post(
            core,
            &parts,
            payload,
            authentication.receipt(),
            admitted,
        )
        .await;
    }
    if crate::instrument_control::handles(&method, &parts)
        && let Some(response) = crate::instrument_control::dispatch(
            &core,
            &method,
            &parts,
            &query,
            &headers,
            authentication.receipt(),
            Some(payload.clone()),
        )
        .await?
    {
        return Ok(response);
    }
    if crate::tasks::handles(&parts) {
        return crate::tasks::post(&core, &parts, payload, authentication.receipt()).await;
    }
    if crate::automation_binding::handles(&method, &parts) {
        return crate::automation_binding::post(&core, &parts, payload, authentication.receipt())
            .await?
            .ok_or_else(|| ApiError::missing("not_found"));
    }
    post(&core, &parts, payload).await
}
async fn read_json(
    core: &Core,
    headers: &HeaderMap,
    body: Body,
    maximum: usize,
    allow_empty: bool,
    private: bool,
) -> ApiResult<(Value, Option<tokio::sync::OwnedSemaphorePermit>)> {
    let lengths: Vec<_> = headers.get_all(header::CONTENT_LENGTH).iter().collect();
    if headers.contains_key(header::TRANSFER_ENCODING)
        || lengths.len() > 1
        || (!allow_empty && lengths.is_empty())
    {
        return Err(ApiError::invalid(
            "Нужен один Content-Length без Transfer-Encoding",
        ));
    }
    let raw = lengths
        .first()
        .map(|value| value.to_str().unwrap_or("invalid"))
        .unwrap_or("0");
    if raw.is_empty() || !raw.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(ApiError::invalid("Некорректный Content-Length"));
    }
    let normalized = raw.trim_start_matches('0');
    let digits = if normalized.is_empty() {
        "0"
    } else {
        normalized
    };
    let max = maximum.to_string();
    if digits.len() > max.len() || (digits.len() == max.len() && digits > max.as_str()) {
        return Err(ApiError::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "request_too_large",
            "Тело запроса превышает предел",
        ));
    }
    let length = digits
        .parse::<usize>()
        .map_err(|_| ApiError::invalid("Некорректный Content-Length"))?;
    let budget = if private {
        None
    } else {
        Some(
            core.body_bytes
                .clone()
                .try_acquire_many_owned(length as u32)
                .map_err(|_| ApiError::unavailable("request_byte_budget"))?,
        )
    };
    let bytes = tokio::time::timeout(core.config.body_timeout, to_bytes(body, maximum))
        .await
        .map_err(|_| {
            ApiError::new(
                StatusCode::REQUEST_TIMEOUT,
                "request_timeout",
                "Истёк срок чтения тела запроса",
            )
        })?
        .map_err(|_| ApiError::invalid("Неполное или слишком большое тело запроса"))?;
    if bytes.len() != length {
        return Err(ApiError::invalid(
            "Длина тела не совпадает с Content-Length",
        ));
    }
    if bytes.is_empty() && allow_empty {
        return Ok((json!({}), budget));
    }
    let value = parse_json_value(&bytes, maximum)
        .map_err(|_| ApiError::invalid("Ожидается строгий UTF-8 JSON"))?;
    if !value.is_object() {
        return Err(ApiError::invalid("Ожидается объект JSON"));
    }
    Ok((value, budget))
}
pub(crate) fn path_parts(path: &str) -> ApiResult<Vec<String>> {
    path.trim_matches('/')
        .split('/')
        .map(|part| {
            let decoded = percent_encoding::percent_decode_str(part)
                .decode_utf8()
                .map_err(|_| ApiError::invalid("Некорректный UTF-8 ID"))?;
            if decoded.len() > 4096 || decoded.contains('\0') {
                return Err(ApiError::invalid("Некорректный ID"));
            }
            Ok(decoded.into_owned())
        })
        .collect()
}
fn query(raw: Option<&str>) -> ApiResult<BTreeMap<String, String>> {
    let mut output = BTreeMap::new();
    for (key, value) in url::form_urlencoded::parse(raw.unwrap_or_default().as_bytes()) {
        if output
            .insert(key.into_owned(), value.into_owned())
            .is_some()
        {
            return Err(ApiError::invalid("Параметры запроса не должны повторяться"));
        }
    }
    Ok(output)
}
fn limit(query: &BTreeMap<String, String>, default: usize, maximum: usize) -> ApiResult<usize> {
    let result = query
        .get("limit")
        .map(|s| {
            s.parse::<usize>()
                .map_err(|_| ApiError::invalid("Неверный limit"))
        })
        .transpose()?
        .unwrap_or(default);
    if result == 0 || result > maximum {
        return Err(ApiError::invalid("limit вне допустимого диапазона"));
    }
    Ok(result)
}
fn after(query: &BTreeMap<String, String>) -> ApiResult<u64> {
    query
        .get("after")
        .map(|s| {
            s.parse::<u64>()
                .map_err(|_| ApiError::invalid("Неверный after"))
        })
        .transpose()
        .map(|v| v.unwrap_or(0))
}

async fn get(
    core: Arc<Core>,
    parts: Vec<String>,
    query: BTreeMap<String, String>,
    headers: HeaderMap,
    authentication: crate::security::AuthContext,
) -> ApiResult<Response> {
    if let Some(response) =
        crate::audio_http::get(&core, &parts, &query, authentication.receipt()).await?
    {
        return Ok(response);
    }
    if let Some(response) =
        crate::legacy::get(&core, &parts, &query, authentication.clone()).await?
    {
        return Ok(response);
    }
    if let Some(response) = crate::tasks::get(&core, &parts).await? {
        return Ok(response);
    }
    if let Some(response) =
        crate::history_jobs::get(&core, &parts, &query, authentication.receipt()).await?
    {
        return Ok(response);
    }
    if let Some(response) =
        crate::raw_export::get(&core, &parts, &query, authentication.receipt()).await?
    {
        return Ok(response);
    }
    if let Some(response) =
        crate::automation_binding::get(&core, &parts, &query, authentication.receipt()).await?
    {
        return Ok(response);
    }
    if let Some(response) = crate::domain_http::get(&core, &parts, &query).await? {
        return Ok(response);
    }
    let route = parts.iter().map(String::as_str).collect::<Vec<_>>();
    let data = match route.as_slice() {
        ["api", "v1", "capabilities"] => capabilities(&core),
        ["api", "v1", "health"] => {
            if let Some(archive) = &core.archive {
                let mut counts = archive.registry_counts().await?;
                counts["status"] = json!("ok");
                counts["api_version"] = json!("v1");
                counts["contract_version"] = json!(1);
                counts["backend"] = json!("rust-candidate");
                if let Some(identity) = &core.config.identity {
                    counts["runtime_instance_id"] = json!(identity.instance_id);
                }
                counts
            } else {
                json!({"status":"ok","api_version":"v1","contract_version":1,"backend":"rust-candidate","mode":"shadow","registry_available":false})
            }
        }
        ["api", "v1", "archive", "capacity"] => core.archive()?.capacity().await?,
        ["api", "v1", "archive", "status"] => core.archive()?.status().await?,
        ["api", "v1", "workers"] => {
            if let Some(devices) = &core.devices {
                json!(devices.list(None).await?)
            } else {
                json!([])
            }
        }
        ["api", "v1", "sessions", id, "workers"] => {
            core.session(id).await?;
            json!(
                core.devices
                    .as_ref()
                    .expect("replay device manager")
                    .list(Some(id))
                    .await?
            )
        }
        ["api", "v1", "sessions"] => {
            let page = core
                .archive()?
                .list_sessions(limit(&query, 1000, 1000)?, None)
                .await?;
            if page.next_cursor.is_some() {
                return Err(ApiError::unavailable("session_list_requires_pagination"));
            }
            json!(page.items)
        }
        ["api", "v1", "sessions", id] => core.session(id).await?,
        ["api", "v1", "sessions", id, "streams"] => core.session(id).await?["streams"].clone(),
        ["api", "v1", "sessions", id, "streams", stream] => core.session(id).await?["streams"]
            .as_array()
            .and_then(|items| items.iter().find(|item| item["id"] == *stream))
            .cloned()
            .ok_or_else(|| {
                ApiError::new(
                    StatusCode::NOT_FOUND,
                    "stream_not_found",
                    "Stream not found in session",
                )
            })?,
        [
            "api",
            "v1",
            "sessions",
            id,
            operation @ ("sweep-comparison" | "sweep-threshold"),
        ] => {
            let method = crate::analysis::method(operation).expect("static analysis method");
            let payload = crate::analysis::get_payload(method, &query)?;
            return crate::analysis::response(&core, id, method, payload).await;
        }
        ["api", "v1", "sessions", id, "sweep-summaries"] => {
            crate::history::summaries(&core, id, &query).await?
        }
        ["api", "v1", "sessions", id, "analytics", "summary"] => {
            core.session(id).await?;
            core.archive()?
                .analytics_summary(id, query.get("stream_id").map(String::as_str))
                .await?
        }
        [
            "api",
            "v1",
            "sessions",
            id,
            "streams",
            stream,
            "instrument",
            "artifacts",
        ] => {
            stream_context(&core, id, stream).await?;
            let offset = query
                .get("offset")
                .map(|v| v.parse::<usize>().ok())
                .unwrap_or(Some(0))
                .filter(|v| *v <= 10_000_000)
                .ok_or_else(|| ApiError::invalid("Artifact offset must be 0..10000000"))?;
            json!({"items":core.archive()?.list_artifacts(id,stream,limit(&query,100,100)?,offset).await?})
        }
        [
            "api",
            "v1",
            "sessions",
            id,
            "streams",
            stream,
            "instrument",
            "artifacts",
            artifact,
        ] => {
            stream_context(&core, id, stream).await?;
            let item = core
                .archive()?
                .get_artifact(id, stream, artifact)
                .await?
                .ok_or_else(|| ApiError::missing("instrument_not_found"))?;
            crate::media::summary(&item)
        }
        [
            "api",
            "v1",
            "sessions",
            id,
            "streams",
            stream,
            "instrument",
            "artifacts",
            artifact,
            "content",
        ] => {
            stream_context(&core, id, stream).await?;
            let item = core
                .archive()?
                .get_artifact(id, stream, artifact)
                .await?
                .ok_or_else(|| ApiError::missing("instrument_not_found"))?;
            let disposition = if item.mime_type == "image/png" {
                "inline"
            } else {
                "attachment"
            };
            return Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, item.mime_type)
                .header(header::CONTENT_LENGTH, item.payload.len())
                .header(
                    header::CONTENT_DISPOSITION,
                    format!("{disposition}; filename=\"{}\"", item.filename),
                )
                .header(header::CACHE_CONTROL, "no-store")
                .header("X-Content-Type-Options", "nosniff")
                .body(Body::from(item.payload))
                .map_err(|_| ApiError::unavailable("artifact_response"));
        }
        ["api", "v1", "sessions", id, "sweeps"] | ["api", "v1", "sessions", id, "observations"] => {
            core.session(id).await?;
            let stream = query.get("stream_id").map(String::as_str);
            let maximum = limit(&query, 100, 1000)?;
            if route[4] == "sweeps" {
                json!(core.archive()?.list_sweeps(id, stream, maximum).await?)
            } else {
                json!(
                    core.archive()?
                        .list_observations(id, stream, maximum)
                        .await?
                )
            }
        }
        ["api", "v1", "sessions", id, "sweeps", measurement] => {
            let value = core
                .archive()?
                .get_sweep(measurement)
                .await?
                .ok_or_else(|| ApiError::missing("sweep_not_found"))?;
            if value["session_id"] != *id {
                return Err(ApiError::missing("sweep_not_found"));
            }
            value
        }
        ["api", "v1", "sessions", id, "observations", measurement] => {
            let value = core
                .archive()?
                .get_observation(measurement)
                .await?
                .ok_or_else(|| ApiError::missing("observation_not_found"))?;
            if value["session_id"] != *id {
                return Err(ApiError::missing("observation_not_found"));
            }
            value
        }
        ["api", "v1", "sessions", id, "candidates"] => {
            core.session(id).await?;
            let page = core
                .archive()?
                .list_candidates(
                    id,
                    query.get("state").map(String::as_str),
                    limit(&query, 1000, 1000)?,
                    None,
                )
                .await?;
            if page.next_cursor.is_some() {
                return Err(ApiError::unavailable("candidate_list_requires_pagination"));
            }
            json!(page.items)
        }
        ["api", "v1", "sessions", id, "investigations"] => {
            core.session(id).await?;
            let page = core
                .archive()?
                .list_investigations(
                    Some(id),
                    query.get("state").map(String::as_str),
                    limit(&query, 1000, 1000)?,
                    None,
                )
                .await?;
            if page.next_cursor.is_some() {
                return Err(ApiError::unavailable(
                    "investigation_list_requires_pagination",
                ));
            }
            json!(page.items)
        }
        ["api", "v1", "sessions", id, "candidates", candidate] => {
            let value = core
                .archive()?
                .get_candidate(candidate)
                .await?
                .ok_or_else(|| ApiError::missing("candidate_not_found"))?;
            if value["session_id"] != *id {
                return Err(ApiError::missing("candidate_not_found"));
            }
            value
        }
        ["api", "v1", "sessions", id, "investigations", investigation] => {
            let (owner, value) = core
                .archive()?
                .get_investigation(investigation)
                .await?
                .ok_or_else(|| ApiError::missing("investigation_not_found"))?;
            if owner != *id {
                return Err(ApiError::missing("investigation_not_found"));
            }
            value
        }
        ["api", "v1", "events", "history"] => json!(
            core.archive()?
                .events(after(&query)?, limit(&query, 100, 1000)?)
                .await?
        ),
        ["api", "v1", "events"] => {
            let cursor = if let Some(value) = headers.get("last-event-id") {
                value
                    .to_str()
                    .map_err(|_| ApiError::invalid("Last-Event-ID"))?
                    .parse()
                    .map_err(|_| ApiError::invalid("Last-Event-ID"))?
            } else {
                after(&query)?
            };
            return events(core, cursor, authentication).await;
        }
        ["api", "v1", "openapi.json"] => return Ok(json_response(StatusCode::OK, openapi())),
        _ => {
            return Err(ApiError::new(
                StatusCode::NOT_IMPLEMENTED,
                "candidate_route_unavailable",
                "Маршрут ещё не прошёл Rust/Python parity; кандидат не готов к замене ядра",
            ));
        }
    };
    Ok(envelope(StatusCode::OK, data))
}
fn capabilities(core: &Core) -> Value {
    let archived = core.mode().writes_archive();
    let domains = core.domains.get().is_some();
    let scientific = archived && core.config.analysis_worker.is_some();
    let mut value = json!({"api_version":"v1","contract_version":1,"hardware_ownership":"core","session_storage":if archived{"sqlite"}else{"shadow"},"storage_mode":if archived{"sqlite"}else{"shadow"},"persistent_archive":archived,"clients":["raspberry-pi-webui","windows-gui","macos-swiftui"],"features":{"sessions":archived,"monitoring_sessions":archived,"session_lifecycle":archived,"session_event_history":archived,"sse_replay":archived,"persistent_archive":archived,"measurement_ingest":archived,"measurement_archive":archived,"measurement_batch_ingest":archived,"measurement_batch_atomic":archived,"measurement_batch_limit":100,"measurement_batch_max_request_bytes":16777216,"sweep_summaries":archived,"audio_segment_ingest":archived,"event_stream":archived,"hardware_control":false,"monitoring_projects":false,"monitoring_guard":false,"session_recovery":archived,"session_analytics":archived,"session_analytics_archive_wide":archived,"session_archive_export":false,"archive_capacity_guard":archived,"archive_automatic_deletion":false,"control_task_scheduler":false,"automatic_investigation_completion":false},"candidate":{"mode":core.mode().as_str(),"compatibility_complete":false,"release_accepted":false,"hardware_validation":"not_run","unsupported_features":["legacy_panorama_workspace","guard_orchestration","instrument_workers","audio_capture_and_live_stream","research_http","tasks","advanced_analysis","archive_export"],"resource_limits":core.resource_capabilities()}});
    for name in [
        "sweep_comparison",
        "adaptive_sweep_threshold",
        "three_sweep_thresholds",
        "sweep_context",
        "sweep_occupancy",
        "spectral_mask_analysis",
        "averaged_sweep_reference",
        "averaged_sweep_change_detection",
        "frequency_raster_rounding",
        "candidate_frequency_rounding",
        "sweep_quality",
        "frequency_counter",
        "field_strength_rms_v2",
        "dvb_t_evaluation",
        "measurement_map",
    ] {
        value["features"][name] = Value::Bool(scientific);
    }
    for name in [
        "simulated_device_workers",
        "independent_worker_commands",
        "continuous_simulated_monitoring",
    ] {
        value["features"][name] = Value::Bool(archived);
    }
    value["features"]["explicit_serial_adapters"] =
        Value::Bool(archived && core.config.devices.transport.is_some());
    let instrument_control = core.instrument_control.get().is_some();
    value["features"]["runtime_owner_scoped_requests"] =
        Value::Bool(core.config.identity.is_some());
    value["features"]["hardware_control"] = Value::Bool(instrument_control);
    value["features"]["tinysa_ultra_instrument_control"] = Value::Bool(instrument_control);
    value["features"]["tinysa_ultra_async_operations"] = Value::Bool(instrument_control);
    value["features"]["tinysa_ultra_hardware_validated"] = Value::Bool(false);
    value["candidate"]["native_connector_enabled"] =
        Value::Bool(archived && core.config.devices.transport.is_some());
    value["features"]["hardware_validation_not_run"] = Value::Bool(true);
    value["features"]["control_task_planning"] = Value::Bool(core.tasks.get().is_some());
    value["features"]["control_task_execution"] = Value::Bool(
        core.tasks
            .get()
            .is_some_and(|tasks| tasks.execution_available()),
    );
    value["features"]["control_task_scheduler"] = Value::Bool(core.tasks.get().is_some());
    for feature in [
        "control_task_scheduler_task_list",
        "control_task_scheduler_task_create",
        "control_task_scheduler_task_cancel",
    ] {
        value["features"][feature] = Value::Bool(core.tasks.get().is_some());
    }
    value["features"]["control_task_scheduler_opt_in_required"] = Value::Bool(true);
    value["features"]["control_task_scheduler_policy_persistent"] = Value::Bool(false);
    value["features"]["openapi_v1"] = Value::Bool(true);
    value["features"]["monitoring_receiver_profile"] = json!("ardv1-monitor-v1");
    value["features"]["monitoring_backup_reserve"] = Value::Bool(archived);
    value["features"]["guard_original_tune_audio_capture"] = Value::Bool(core.audio.is_some());
    value["features"]["analog_audio_inputs"] = Value::Bool(core.audio.is_some());
    value["features"]["legacy_workspace_state"] = Value::Bool(core.legacy.get().is_some());
    value["features"]["legacy_panorama_workspace"] = Value::Bool(
        cfg!(unix)
            && core.legacy.get().is_some()
            && (core.config.legacy.worker.is_some() || core.config.analysis_worker.is_some()),
    );
    value["candidate"]["legacy_aggregate_background_io"] = Value::Bool(false);
    value["features"]["derived_history_jobs"] = Value::Bool(archived);
    value["features"]["derived_history_viewport"] = Value::Bool(archived);
    for name in [
        "session_archive_export",
        "session_archive_export_archive_wide",
        "session_archive_export_ndjson",
        "session_archive_export_streaming",
    ] {
        value["features"][name] = Value::Bool(archived);
    }
    if let Some(items) = value["candidate"]["unsupported_features"].as_array_mut() {
        items.retain(|item| item != "archive_export");
    }
    value["candidate"]["raw_export_limits"] = json!({"live_readers":2,"max_record_bytes":16*1024*1024,"read_timeout_ms":5000,"whole_database_copy":false,"heavy_research_permit":false,"native_source_io_accounted":false});
    value["candidate"]["history_worker_configured"] = Value::Bool(
        core.history_jobs
            .get()
            .is_some_and(|history| history.available()),
    );
    value["candidate"]["aggregate_background_io_complete"] = Value::Bool(false);
    if let Some(observer) = core.acquisition_pressure.get() {
        let status = observer.status();
        value["features"]["acquisition_background_pressure"] = Value::Bool(status.observer_running);
        value["candidate"]["acquisition_pressure"] = json!(status);
    } else {
        value["features"]["acquisition_background_pressure"] = Value::Bool(false);
    }
    if let Some(background) = &core.background {
        value["candidate"]["background_qos"] = json!(background.qos.status());
    }
    value["features"]["measurement_binary_stream"] = if archived {
        ori_stream::capability()
    } else {
        Value::Bool(false)
    };
    for name in [
        "independent_device_streams",
        "monitoring_projects",
        "monitoring_guard",
        "candidate_investigations",
        "manual_investigation_handoff",
        "semi_automatic_handoff",
        "measurement_results",
        "measurement_methodology_catalog",
        "signal_list_exchange",
    ] {
        value["features"][name] = Value::Bool(domains);
    }
    value["features"]["named_sweep_references"] = Value::Bool(domains && scientific);
    for name in [
        "automatic_handoff",
        "automatic_candidate_detection",
        "automatic_investigation_completion",
    ] {
        value["features"][name] = Value::Bool(core.automation.get().is_some());
    }
    // Audit events persist, but each process starts with disabled policies.
    for feature in [
        "automatic_candidate_detection",
        "automatic_handoff",
        "automatic_investigation_completion",
    ] {
        value["features"][format!("{feature}_opt_in_required")] = Value::Bool(true);
        value["features"][format!("{feature}_policy_persistent")] = Value::Bool(false);
    }
    let uploaded_audio = archived && core.audio_http.get().is_some();
    value["features"]["phonogram_pre_history"] = Value::Bool(uploaded_audio);
    value["features"]["remote_audio_stream"] = Value::Bool(uploaded_audio);
    if uploaded_audio {
        let unsupported = value["candidate"]["unsupported_features"]
            .as_array_mut()
            .expect("static capability list");
        unsupported.retain(|feature| feature != "audio_capture_and_live_stream");
        if core.audio.is_none() {
            unsupported.push(json!("host_audio_capture"));
        }
    }
    value["candidate"]["unsupported_features"]
        .as_array_mut()
        .expect("static capability list")
        .retain(|value| {
            !(archived && value == "instrument_workers"
                || domains && value == "guard_orchestration"
                || core.tasks.get().is_some() && value == "tasks")
        });
    if scientific {
        value["candidate"]["unsupported_features"]
            .as_array_mut()
            .expect("static capability list")
            .retain(|v| v != "advanced_analysis" && v != "research_http");
    }
    if value["features"]["legacy_panorama_workspace"] == true {
        value["candidate"]["unsupported_features"]
            .as_array_mut()
            .expect("static capability list")
            .retain(|value| value != "legacy_panorama_workspace");
    }
    value
}
fn openapi() -> &'static Value {
    static DOCUMENT: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    DOCUMENT.get_or_init(|| {
        parse_json_value(
            ori_contracts::RUNTIME_OPENAPI_V1_JSON.as_bytes(),
            16 * 1024 * 1024,
        )
        .expect("generated runtime OpenAPI must be strict JSON")
    })
}

async fn post(core: &Arc<Core>, parts: &[String], payload: Value) -> ApiResult<Response> {
    if parts.len() == 2 && parts[0] == "api" {
        return crate::legacy::post(core, parts, payload)
            .await?
            .ok_or_else(|| ApiError::missing("not_found"));
    }
    core.archive()?;
    if crate::history_jobs::handles(parts) {
        return crate::history_jobs::post(core, parts, payload).await;
    }
    if crate::domain_http::handles_post(parts) {
        return crate::domain_http::post(core, parts, payload)
            .await?
            .ok_or_else(|| ApiError::missing("domain_route_not_found"));
    }
    let route = parts.iter().map(String::as_str).collect::<Vec<_>>();
    if let ["api", "v1", "sessions", id, operation] = route.as_slice()
        && !matches!(*operation, "sweep-comparison" | "sweep-threshold")
        && let Some(method) = crate::analysis::method(operation)
    {
        return crate::analysis::response(core, id, method, payload).await;
    }
    match route.as_slice() {
        [
            "api",
            "v1",
            "sessions",
            id,
            "workers",
            operation @ ("start" | "stop"),
        ] => {
            core.session(id).await?;
            let devices = core.devices.as_ref().expect("replay device manager");
            let workers = if *operation == "start" {
                devices.start_session(id).await?
            } else {
                devices.authorize_public_session(id).await?;
                devices.stop_session(id).await?
            };
            Ok(envelope(StatusCode::OK, json!(workers)))
        }
        [
            "api",
            "v1",
            "sessions",
            id,
            "streams",
            stream,
            operation @ ("start" | "stop" | "tune"),
        ] => {
            core.session(id).await?;
            let devices = core.devices.as_ref().expect("replay device manager");
            let target = crate::devices::Target::new(id, stream);
            let gate = devices.public_gate(target.clone());
            let result = match *operation {
                "start" => devices.start_stream(id, stream, gate).await?.value()?,
                "stop" => {
                    gate.authorize()?;
                    devices.stop_stream(id, stream).await?.value()?
                }
                _ => {
                    let frequency = payload
                        .get("frequency_hz")
                        .and_then(Value::as_u64)
                        .ok_or_else(|| {
                            ApiError::invalid("frequency_hz должно быть целым числом")
                        })?;
                    let mode = payload
                        .get("mode")
                        .filter(|v| !v.is_null())
                        .map(|v| {
                            v.as_str()
                                .map(str::to_owned)
                                .ok_or_else(|| ApiError::invalid("mode должно быть строкой"))
                        })
                        .transpose()?;
                    devices.tune(target, frequency, mode, gate).await?.value()?
                }
            };
            Ok(envelope(StatusCode::OK, result))
        }
        ["api", "v1", "sessions", id, "audio-segments"] => {
            let item = crate::media::audio(id, payload)?;
            let _order = core.mutation_order.lock().await;
            let stream = stream_context(core, id, &item.stream_id).await?;
            if stream["role"] != "receiver" {
                return Err(ApiError::invalid(
                    "Audio segment must belong to a receiver stream",
                ));
            }
            let summary = crate::media::summary(&item);
            let event = ori_archive::EventDraft::now(
                "audio_segment.received",
                Some((*id).into()),
                json!({"artifact":summary}),
            );
            let saved=core.archive()?.apply(vec![ori_archive::Mutation::Artifact(item),ori_archive::Mutation::Event(event)]).await.map_err(|error|{
                if matches!(&error,ori_archive::ArchiveError::Conflict)||matches!(&error,ori_archive::ArchiveError::Sql(error)if error.sqlite_error_code()==Some(rusqlite::ErrorCode::ConstraintViolation)){ApiError::new(StatusCode::CONFLICT,"audio_segment_conflict","Audio segment ID already exists or violates archive ownership")}else{error.into()}
            })?;
            Ok(envelope(
                StatusCode::CREATED,
                saved.items.into_iter().next().expect("committed artifact"),
            ))
        }
        ["api", "v1", "sessions"] => {
            let _order = core.mutation_order.lock().await;
            let session = new_session(payload)?;
            let saved = core.archive()?.create_session(session).await?;
            Ok(envelope(
                StatusCode::CREATED,
                saved.items.into_iter().next().expect("created session"),
            ))
        }
        ["api", "v1", "sessions", id, "start"] | ["api", "v1", "sessions", id, "stop"] => {
            Ok(envelope(
                StatusCode::OK,
                core.transition(id, route[4] == "start").await?,
            ))
        }
        ["api", "v1", "sessions", id, "sweeps"]
        | ["api", "v1", "sessions", id, "observations"]
        | ["api", "v1", "sessions", id, "sweeps", "batch"]
        | ["api", "v1", "sessions", id, "observations", "batch"] => {
            let batch = route.len() == 6;
            let mut values = if batch {
                payload
                    .get("items")
                    .and_then(Value::as_array)
                    .cloned()
                    .ok_or_else(|| ApiError::invalid("Поле items должно быть JSON-массивом"))?
            } else {
                vec![payload]
            };
            // The compatibility HTTP boundary checks every batch object's
            // framing/owner before DTO construction and session lookup.
            if batch {
                for (index, value) in values.iter_mut().enumerate() {
                    if !value.is_object() {
                        return Err(ApiError::invalid(format!(
                            "Элемент пакета {index} должен быть JSON-объектом"
                        )));
                    }
                    if value
                        .get("session_id")
                        .is_some_and(|value| !value.is_null() && value != id)
                    {
                        return Err(ApiError::invalid(format!(
                            "session_id элемента {index} не совпадает с маршрутом"
                        )));
                    }
                    value["session_id"] = json!(id);
                }
            }
            if values.is_empty() || values.len() > 100 {
                return Err(ApiError::invalid(
                    "Пакет должен содержать от 1 до 100 измерений",
                ));
            }
            let sweeps = route[4] == "sweeps";
            let values = values
                .into_iter()
                .map(|value| measurement(value, id, sweeps))
                .collect::<ApiResult<Vec<_>>>()?;
            core.session(id).await?;
            let committed = if sweeps {
                core.archive()?.append_sweeps(values).await?
            } else {
                core.archive()?.append_observations(values).await?
            };
            let data = if batch {
                json!({"count":committed.items.len(),"items":committed.items})
            } else {
                committed.items.into_iter().next().expect("one source")
            };
            Ok(envelope(StatusCode::CREATED, data))
        }
        _ => Err(ApiError::new(
            StatusCode::NOT_IMPLEMENTED,
            "candidate_route_unavailable",
            "Маршрут ещё не прошёл Rust/Python parity",
        )),
    }
}
async fn stream_context(core: &Core, session: &str, stream: &str) -> ApiResult<Value> {
    core.session(session).await?["streams"]
        .as_array()
        .and_then(|items| items.iter().find(|item| item["id"] == stream))
        .cloned()
        .ok_or_else(|| ApiError::missing("stream_not_found"))
}
fn required(value: &Value, key: &str) -> ApiResult<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .map(str::to_owned)
        .ok_or_else(|| ApiError::invalid(format!("Требуется поле {key}")))
}
fn new_session(payload: Value) -> ApiResult<Value> {
    if payload.get("state").is_some_and(|s| s != "draft") {
        return Err(ApiError::invalid(
            "Новый сеанс должен иметь состояние draft",
        ));
    }
    if ["created_at", "started_at", "ended_at"]
        .iter()
        .any(|key| payload.get(key).is_some_and(|v| !v.is_null()))
    {
        return Err(ApiError::invalid(
            "Жизненный цикл нового сеанса назначает служба",
        ));
    }
    let id = payload
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(new_id);
    let streams = payload
        .get("streams")
        .filter(|v| !v.is_null())
        .map(|v| {
            v.as_array()
                .cloned()
                .ok_or_else(|| ApiError::invalid("streams должен быть массивом"))
        })
        .transpose()?
        .unwrap_or_default();
    let mut normalized = Vec::new();
    let mut ids = HashSet::new();
    let mut ports = HashSet::new();
    for mut stream in streams {
        if !stream.is_object() {
            return Err(ApiError::invalid("Поток должен быть объектом"));
        }
        let stream_id = stream
            .get("id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(new_id);
        if !ids.insert(stream_id.clone()) {
            return Err(ApiError::invalid(
                "Идентификаторы потоков должны быть уникальны",
            ));
        }
        if let Some(port) = stream
            .get("port")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            && !ports.insert(port.to_owned())
        {
            return Err(ApiError::invalid("Один порт нельзя назначить двум потокам"));
        }
        stream["id"] = json!(stream_id);
        stream["session_id"] = json!(id);
        normalized.push(stream);
    }
    Ok(
        json!({"id":id,"name":required(&payload,"name")?,"state":"draft","created_at":utc_now(),"started_at":null,"ended_at":null,"contract_version":payload.get("contract_version").cloned().unwrap_or(json!(1)),"metadata":payload.get("metadata").filter(|v|!v.is_null()).cloned().unwrap_or(json!({})),"streams":normalized}),
    )
}
fn measurement(mut value: Value, session: &str, sweep: bool) -> ApiResult<Value> {
    if !value.is_object() {
        return Err(ApiError::invalid("Измерение должно быть объектом"));
    }
    if value
        .get("session_id")
        .is_some_and(|v| !v.is_null() && v != session)
    {
        return Err(ApiError::invalid(
            "session_id в теле не совпадает с маршрутом",
        ));
    }
    value["session_id"] = json!(session);
    let missing = |field: &str| {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "missing_field",
            format!("Отсутствует поле: {field}"),
        )
    };
    for field in ["stream_id", "interval"] {
        if value.get(field).is_none() {
            return Err(missing(field));
        }
    }
    if let Some(interval) = value["interval"].as_object() {
        for field in ["started_at", "ended_at"] {
            if !interval.contains_key(field) {
                return Err(missing(field));
            }
        }
    }
    let fields: &[&str] = if sweep {
        &["frequencies_hz", "levels", "level_unit"]
    } else {
        &["frequency_hz", "mode"]
    };
    for field in fields {
        if value.get(*field).is_none() {
            return Err(missing(field));
        }
    }
    if value.get("id").is_none_or(|v| v.is_null() || v == "") {
        value["id"] = json!(new_id());
    }
    if !sweep && value.get("level_unit").is_none() {
        value["level_unit"] = json!("s_meter_count");
    }
    for field in if sweep { ["settings"] } else { ["metadata"] } {
        if value.get(field).is_none_or(Value::is_null) {
            value[field] = json!({});
        }
    }
    Ok(value)
}
async fn control(
    core: Arc<Core>,
    peer: SocketAddr,
    method: Method,
    path: &str,
    query: Option<&str>,
    headers: HeaderMap,
    body: Body,
) -> ApiResult<Response> {
    let identity = core
        .config
        .identity
        .as_ref()
        .ok_or_else(|| ApiError::missing("not_found"))?;
    let loopback = peer.ip().is_loopback()
        || match peer.ip() {
            std::net::IpAddr::V6(ip) => ip.to_ipv4_mapped().is_some_and(|ip| ip.is_loopback()),
            _ => false,
        };
    if !loopback {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "runtime_loopback_required",
            "Loopback required",
        ));
    }
    let auth: Vec<_> = headers.get_all(header::AUTHORIZATION).iter().collect();
    let authorized = auth.len() == 1
        && auth[0]
            .to_str()
            .ok()
            .and_then(|s| s.strip_prefix("Bearer "))
            .is_some_and(|candidate| {
                bool::from(candidate.as_bytes().ct_eq(identity.token.as_bytes()))
            });
    if !authorized {
        return Err(ApiError::new(
            StatusCode::UNAUTHORIZED,
            "runtime_unauthorized",
            "Unauthorized",
        ));
    }
    if query.is_some() {
        return Err(ApiError::missing("runtime_route_not_found"));
    }
    if method == Method::GET && path == "/internal/runtime/v1/status" {
        return Ok(json_response(StatusCode::OK, &core.runtime_status()));
    }
    if method != Method::POST || path != "/internal/runtime/v1/shutdown" {
        return Err(ApiError::missing("runtime_route_not_found"));
    }
    let (payload, _budget) = read_json(&core, &headers, body, 4096, false, true).await?;
    let allowed = [
        "version",
        "preserve_resume_requested",
        "reason",
        "acknowledge_coverage_gap",
    ];
    if payload
        .as_object()
        .expect("validated object")
        .keys()
        .any(|key| !allowed.contains(&key.as_str()))
        || payload["version"].as_u64() != Some(1)
        || payload["preserve_resume_requested"] != true
        || !payload["reason"]
            .as_str()
            .is_some_and(|s| ["shutdown", "cutover", "rollback"].contains(&s))
        || payload
            .get("acknowledge_coverage_gap")
            .is_some_and(|v| !v.is_boolean())
    {
        return Err(ApiError::invalid("Invalid runtime shutdown envelope"));
    }
    // Claim publication uses this same brief coordinator. Either shutdown
    // revokes admission first, or the newly armed owner requires explicit gap
    // acknowledgement; an admitted arm cannot slip between check and revoke.
    let _boundary = core.mutation_order.lock().await;
    if payload["reason"] == "cutover"
        && core.runtime_status().guard_armed
        && payload["acknowledge_coverage_gap"] != true
    {
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            "runtime_guard_armed",
            "Explicit coverage-gap acknowledgement is required while guard ownership remains active",
        ));
    }
    core.request_shutdown();
    Ok(json_response(
        StatusCode::ACCEPTED,
        &json!({"version":1,"accepted":true}),
    ))
}
async fn events(
    core: Arc<Core>,
    mut cursor: u64,
    authentication: crate::security::AuthContext,
) -> ApiResult<Response> {
    let permit = core
        .subscribers
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::unavailable("subscriber_limit"))?;
    let archive = core.archive()?.clone();
    let mut notices = archive.subscribe_commits();
    let mut shutdown = core.shutdown.subscribe();
    let security = core.security.clone();
    let mut revocations = security.revocations.subscribe();
    let receipt = authentication.receipt();
    let stream = async_stream::stream! {
        let _permit=permit;
        loop {
            if *shutdown.borrow()||security.validate(&authentication).await.is_err(){break;}
            match archive.events(cursor,8).await {
                Ok(items) if !items.is_empty()=>{for event in items{let Some(sequence)=event["sequence"].as_u64()else{break;};if sequence<=cursor{continue;}let event_type=event["event_type"].as_str().unwrap_or("unknown");match to_json_vec(&Envelope::new(&event)){Ok(data)=>{cursor=sequence;yield Ok::<Event,Infallible>(Event::default().id(sequence.to_string()).event(event_type).data(String::from_utf8(data).expect("JSON UTF8")));},Err(_)=>break,}}continue;},
                Err(_)=>{yield Ok(Event::default().event("stream.gap").data(format!("{{\"after\":{cursor},\"history_available\":true}}")));break;},
                _=>{},
            }
            tokio::select! {_=shutdown.changed()=>{},_=receipt.revoked()=>{break;},_=revocations.changed()=>{if security.validate(&authentication).await.is_err(){break;}},notice=notices.recv()=>{if notice.is_err(){continue;}},_=tokio::time::sleep(Duration::from_secs(15))=>{yield Ok(Event::default().comment("keep-alive"));}}
        }
    };
    Ok(Sse::new(stream)
        .keep_alive(
            KeepAlive::new()
                .interval(Duration::from_secs(15))
                .text("keep-alive"),
        )
        .into_response())
}
