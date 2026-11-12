//! Optional derived jobs; immutable canonical reads and one shared heavy pool.
use crate::{
    Core, CoreError,
    error::{ApiError, ApiResult},
    security::AuthReceipt,
};
use axum::{
    body::{Body, Bytes},
    http::{StatusCode, header},
    response::Response,
};
use ori_contracts::Envelope;
use ori_history::{
    ExportTable, HistoryConfig, HistoryError, HistoryRequest, HistoryService, ViewportQuery,
};
use ori_runtime::worker::WorkerSpec;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{Arc, OnceLock},
    time::Duration,
};
use tokio::sync::watch;

/// Operator settings only. HTTP accepts no executable, path, SQL or quota.
#[derive(Clone)]
pub struct HistoryOptions {
    pub worker: Option<WorkerSpec>,
    pub max_source_bytes: u64,
    pub max_cache_bytes: u64,
}
impl Default for HistoryOptions {
    fn default() -> Self {
        Self {
            worker: None,
            max_source_bytes: 256 * 1024 * 1024,
            max_cache_bytes: 10 * 1024 * 1024 * 1024,
        }
    }
}
type Completion = watch::Receiver<Option<ori_history::Result<()>>>;
pub(crate) struct HistoryJobs {
    service: Option<Arc<HistoryService>>,
    runtime: tokio::runtime::Handle,
    shutdown: OnceLock<Completion>,
}
pub(crate) async fn initialize(core: &Arc<Core>) -> crate::Result<()> {
    let Some(archive) = &core.archive else {
        return Ok(());
    };
    let options = &core.config.history;
    if options.max_cache_bytes == 0
        || options.max_cache_bytes > 10 * 1024 * 1024 * 1024
        || options.max_source_bytes == 0
        || options.max_source_bytes > options.max_cache_bytes / 2
    {
        return Err(CoreError::Invalid(
            "history storage budgets exceed the supported policy",
        ));
    }
    let worker = options.worker.clone().or_else(|| {
        core.config.analysis_worker.clone().map(|mut worker| {
            worker.args = vec!["-I".into(), "-B".into(), "-m".into(), "ori_research".into()];
            worker
        })
    });
    let background = core.background.as_ref().expect("replay background owner");
    let service = if let Some(worker) = worker {
        let mut config = HistoryConfig::new(
            background.root.join("cache"),
            background.root.join("snapshots"),
        );
        config.qos = background.qos.clone();
        config.max_cache_bytes = options.max_cache_bytes;
        config.max_source_bytes = options.max_source_bytes;
        match HistoryService::new(archive.clone(), core.research_pool.clone(), worker, config).await
        {
            Ok(service) => Some(Arc::new(service)),
            // Optional unavailable containment cannot prevent a cold node's UI
            // and actual project preflight. Requests report explicit 503.
            Err(HistoryError::WorkerUnavailable) => None,
            Err(error) => return Err(error.into()),
        }
    } else {
        None
    };
    core.history_jobs
        .set(Arc::new(HistoryJobs {
            service,
            runtime: tokio::runtime::Handle::current(),
            shutdown: OnceLock::new(),
        }))
        .map_err(|_| CoreError::Invalid("history facade already initialized"))?;
    Ok(())
}
impl HistoryJobs {
    pub fn available(&self) -> bool {
        self.service.is_some()
    }
    pub fn begin_shutdown(self: &Arc<Self>) -> Completion {
        self.shutdown
            .get_or_init(|| {
                let (sent, receiver) = watch::channel(None);
                let own = self.clone();
                self.runtime.spawn(async move {
                    let result = if let Some(service) = &own.service {
                        service.close().await
                    } else {
                        Ok(())
                    };
                    sent.send_replace(Some(result));
                });
                receiver
            })
            .clone()
    }
    pub async fn close(self: &Arc<Self>) -> crate::Result<()> {
        let mut completion = self.begin_shutdown();
        loop {
            if let Some(outcome) = completion.borrow().clone() {
                return outcome.map_err(Into::into);
            }
            completion
                .changed()
                .await
                .map_err(|_| CoreError::Invalid("history cleanup completion lost"))?;
        }
    }
}
fn service(core: &Core) -> ApiResult<&Arc<HistoryService>> {
    core.archive()?;
    if core.admission.is_quiescing() {
        return Err(ApiError::unavailable("runtime_quiescing"));
    }
    core.history_jobs
        .get()
        .and_then(|h| h.service.as_ref())
        .ok_or_else(|| ApiError::unavailable("history_worker_unavailable"))
}
fn failed(error: HistoryError) -> ApiError {
    let (status, code) = match &error {
        HistoryError::Invalid(_) => (StatusCode::BAD_REQUEST, "invalid_request"),
        HistoryError::NotFound => (StatusCode::NOT_FOUND, "history_not_found"),
        HistoryError::Busy => (StatusCode::SERVICE_UNAVAILABLE, "history_busy"),
        HistoryError::Pressure => (StatusCode::SERVICE_UNAVAILABLE, "acquisition_pressure"),
        HistoryError::Cancelled => (StatusCode::SERVICE_UNAVAILABLE, "history_cancelled"),
        HistoryError::Closed => (StatusCode::SERVICE_UNAVAILABLE, "history_closed"),
        HistoryError::WorkerUnavailable => (
            StatusCode::SERVICE_UNAVAILABLE,
            "history_worker_unavailable",
        ),
        HistoryError::ResourceLimit | HistoryError::SourceLimit => {
            (StatusCode::PAYLOAD_TOO_LARGE, "history_resource_limit")
        }
        HistoryError::UnsafeStorage => (StatusCode::SERVICE_UNAVAILABLE, "history_unsafe_storage"),
        HistoryError::Worker { .. } | HistoryError::Internal => {
            (StatusCode::BAD_GATEWAY, "history_failed")
        }
    };
    ApiError::new(status, code, error.to_string())
}
pub(crate) fn handles(parts: &[String]) -> bool {
    parts.len() >= 3 && parts[..3] == ["api", "v1", "history"]
}
fn no_query(query: &BTreeMap<String, String>) -> ApiResult<()> {
    if !query.is_empty() {
        return Err(ApiError::invalid(
            "History operation accepts no query parameters",
        ));
    }
    Ok(())
}
pub(crate) async fn post(core: &Core, parts: &[String], payload: Value) -> ApiResult<Response> {
    let history = service(core)?;
    match &parts[3..] {
        [jobs] if jobs == "jobs" => {
            let request: HistoryRequest = serde_json::from_value(payload)
                .map_err(|_| ApiError::invalid("Invalid typed history request"))?;
            request.validate().map_err(failed)?;
            let id = history.submit(request).await.map_err(failed)?;
            let view = history.status(&id).await.map_err(failed)?;
            Ok(crate::http::json_response(
                StatusCode::ACCEPTED,
                &Envelope::new(view),
            ))
        }
        [jobs, id, action] if jobs == "jobs" && action == "cancel" => {
            if payload.as_object().is_none_or(|v| !v.is_empty()) {
                return Err(ApiError::invalid(
                    "Cancellation body must be an empty object",
                ));
            }
            history.cancel(id).await.map_err(failed)?;
            Ok(crate::http::envelope(
                StatusCode::OK,
                json!({"cancelled":true}),
            ))
        }
        [jobs, id, action] if jobs == "jobs" && action == "pages" => {
            let object = payload
                .as_object()
                .filter(|o| o.len() <= 1 && o.keys().all(|k| k == "cursor"))
                .ok_or_else(|| ApiError::invalid("Only an optional cursor is accepted"))?;
            let cursor = match object.get("cursor") {
                None | Some(Value::Null) => None,
                Some(Value::String(value)) if value.len() <= 4096 => Some(value.clone()),
                _ => return Err(ApiError::invalid("Invalid history cursor")),
            };
            let next = history.replay(id, cursor).await.map_err(failed)?;
            let view = history.status(&next).await.map_err(failed)?;
            Ok(crate::http::json_response(
                StatusCode::ACCEPTED,
                &Envelope::new(view),
            ))
        }
        _ => Err(ApiError::missing("not_found")),
    }
}
pub(crate) async fn get(
    core: &Core,
    parts: &[String],
    query: &BTreeMap<String, String>,
    auth: AuthReceipt,
) -> ApiResult<Option<Response>> {
    if !handles(parts) {
        return Ok(None);
    }
    let history = service(core)?;
    if !matches!(&parts[3..], [waterfall] if waterfall == "waterfall") {
        no_query(query)?;
    }
    let response = match &parts[3..] {
        [jobs] if jobs == "jobs" => {
            let items = history.jobs().await.map_err(failed)?;
            crate::http::envelope(StatusCode::OK, json!({"count":items.len(),"items":items}))
        }
        [jobs, id] if jobs == "jobs" => crate::http::json_response(
            StatusCode::OK,
            &Envelope::new(history.status(id).await.map_err(failed)?),
        ),
        [jobs, id, result] if jobs == "jobs" && result == "result" => {
            let output = history.result(id).await.map_err(failed)?;
            crate::http::json_response(StatusCode::OK, &Envelope::new(output.as_ref()))
        }
        [jobs, id, exports, table] if jobs == "jobs" && exports == "exports" => {
            let table = match table.as_str() {
                "streams" => ExportTable::Streams,
                "sweeps" => ExportTable::Sweeps,
                "sweep_points" => ExportTable::SweepPoints,
                "observations" => ExportTable::Observations,
                _ => return Err(ApiError::missing("history_export_not_found")),
            };
            let reader = history.export_part(id, table).await.map_err(failed)?;
            return Ok(Some(export(reader, core.shutdown.subscribe(), auth)?));
        }
        [waterfall] if waterfall == "waterfall" => {
            let query = viewport(query)?;
            let page = history.viewport(query).await.map_err(failed)?;
            crate::http::json_response(StatusCode::OK, &Envelope::new(page))
        }
        _ => return Err(ApiError::missing("not_found")),
    };
    auth.revalidate()?;
    Ok(Some(response))
}
fn viewport(query: &BTreeMap<String, String>) -> ApiResult<ViewportQuery> {
    let mut values = serde_json::Map::new();
    for (key, text) in query {
        let value = match key.as_str() {
            "session_id" | "stream_id" => Value::String(text.clone()),
            "min_frequency_hz" | "max_frequency_hz" | "columns" | "rows" | "start_us"
            | "end_us" => {
                let number = text
                    .parse::<i64>()
                    .map_err(|_| ApiError::invalid("Viewport values must be exact integers"))?;
                json!(number)
            }
            "cursor" if text.len() <= 4096 => {
                ori_contracts::parse_json_value(text.as_bytes(), 4096)
                    .map_err(|_| ApiError::invalid("Invalid viewport cursor"))?
            }
            _ => return Err(ApiError::invalid("Unknown viewport parameter")),
        };
        values.insert(key.clone(), value);
    }
    let query: ViewportQuery = serde_json::from_value(Value::Object(values))
        .map_err(|_| ApiError::invalid("Invalid typed viewport query"))?;
    query.validate().map_err(failed)?;
    Ok(query)
}
fn export(
    mut reader: ori_history::ExportReader,
    mut shutdown: watch::Receiver<bool>,
    auth: AuthReceipt,
) -> ApiResult<Response> {
    auth.revalidate()?;
    let bytes = reader.info().bytes;
    let sha256 = reader.info().sha256.clone();
    let stream: std::pin::Pin<Box<dyn futures_util::Stream<Item = std::io::Result<Bytes>> + Send>> =
        Box::pin(async_stream::try_stream! {
            loop {
                let next = async {
                    loop {
                        match reader.read_chunk().await {
                            Err(HistoryError::Busy) => tokio::time::sleep(Duration::from_millis(10)).await,
                            result => return result,
                        }
                    }
                };
                let chunk = tokio::select! {
                    biased;
                    _=auth.revoked()=>Err(std::io::Error::other("request authority revoked")),
                    _=async { while !*shutdown.borrow() { if shutdown.changed().await.is_err() { break; } } }=>Err(std::io::Error::other("core shutting down")),
                    result=tokio::time::timeout(Duration::from_secs(30),next)=>match result {
                        Ok(Ok(chunk))=>Ok(chunk),
                        Ok(Err(_))=>Err(std::io::Error::other("derivative export read failed")),
                        Err(_)=>Err(std::io::Error::other("derivative export admission deadline")),
                    },
                }?;
                auth.revalidate().map_err(|_| std::io::Error::other("request authority revoked"))?;
                if chunk.is_empty() { break; }
                yield Bytes::from(chunk);
            }
        });
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/vnd.apache.parquet")
        .header(header::CONTENT_LENGTH, bytes)
        .header(header::CACHE_CONTROL, "no-store")
        .header("X-Content-SHA256", sha256)
        .body(Body::from_stream(stream))
        .map_err(|_| ApiError::unavailable("history_export_unavailable"))
}
