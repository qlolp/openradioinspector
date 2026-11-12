//! Plain legacy HTTP over an owned library workspace and isolated science.
mod numeric;
mod worker;
use crate::{
    Core, CoreError,
    error::{ApiError, ApiResult},
    security::AuthContext,
};
use axum::{
    body::{Body, Bytes},
    http::{StatusCode, header},
    response::{
        IntoResponse, Response,
        sse::{Event, Sse},
    },
};
use ori_legacy::{LegacyLimits, LegacyOptions, LegacyQuery, LegacyWorkspace};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    convert::Infallible,
    path::PathBuf,
    sync::{Arc, Mutex, OnceLock},
    time::Duration,
};
use tokio::sync::watch;

/// Operator-selected containment and bounded source policy. HTTP cannot choose
/// a worker executable, scratch directory, SQL, or private snapshot path.
#[derive(Clone, Default)]
pub struct LegacyConfig {
    pub library: Option<PathBuf>,
    pub limits: LegacyLimits,
    /// Trusted embedding override; CLI inherits the configured interpreter and
    /// fixes argv to the installed radioinspector.legacy_rpc module.
    pub worker: Option<ori_runtime::worker::WorkerSpec>,
}
type Completion = watch::Receiver<Option<Arc<ori_legacy::Result<()>>>>;
pub(crate) struct Legacy {
    workspace: LegacyWorkspace,
    stage: Mutex<Option<tempfile::TempDir>>,
    runtime: tokio::runtime::Handle,
    shutdown: OnceLock<Completion>,
}
pub(crate) async fn initialize(core: &Arc<Core>) -> crate::Result<()> {
    if core.archive.is_none() {
        return Ok(());
    }
    let mut builder = tempfile::Builder::new();
    builder.prefix(".ori-legacy-");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(std::fs::Permissions::from_mode(0o700));
    }
    let stage = builder.tempdir_in(&core.config.data_directory)?;
    let options = LegacyOptions {
        library: core.config.legacy.library.clone(),
        scratch_directory: stage.path().to_owned(),
        limits: core.config.legacy.limits.clone(),
        ..Default::default()
    };
    let workspace =
        LegacyWorkspace::new(options, Arc::new(worker::Science(Arc::downgrade(core)))).await?;
    core.legacy
        .set(Arc::new(Legacy {
            workspace,
            stage: Mutex::new(Some(stage)),
            runtime: tokio::runtime::Handle::current(),
            shutdown: OnceLock::new(),
        }))
        .map_err(|_| CoreError::Invalid("legacy facade already initialized"))?;
    Ok(())
}
impl Legacy {
    pub fn begin_shutdown(self: &Arc<Self>) -> Completion {
        self.shutdown
            .get_or_init(|| {
                let (sent, receiver) = watch::channel(None);
                let owned = self.clone();
                self.runtime.spawn(async move {
                    let result = async {
                        owned.workspace.close().await?;
                        let stage = owned
                            .stage
                            .lock()
                            .map_err(|_| ori_legacy::LegacyError::CleanupPending)?
                            .take();
                        if let Some(stage) = stage {
                            tokio::task::spawn_blocking(move || stage.close())
                                .await
                                .map_err(|_| ori_legacy::LegacyError::CleanupPending)?
                                .map_err(|_| ori_legacy::LegacyError::CleanupPending)?;
                        }
                        Ok(())
                    }
                    .await;
                    sent.send_replace(Some(Arc::new(result)));
                });
                receiver
            })
            .clone()
    }
    pub async fn close(self: &Arc<Self>) -> crate::Result<()> {
        let mut finished = self.begin_shutdown();
        loop {
            if let Some(outcome) = finished.borrow().clone() {
                return match outcome.as_ref() {
                    Ok(()) => Ok(()),
                    Err(_) => Err(CoreError::Invalid(
                        "legacy cleanup is not confirmed; writer ownership retained",
                    )),
                };
            }
            finished
                .changed()
                .await
                .map_err(|_| CoreError::Invalid("legacy cleanup completion was lost"))?;
        }
    }
}
impl Drop for Legacy {
    fn drop(&mut self) {
        if let Ok(stage) = self.stage.get_mut()
            && let Some(stage) = stage.take()
        {
            std::mem::forget(stage);
        }
    }
}
pub(crate) fn plain_error(error: ApiError) -> Response {
    crate::http::json_response(error.status, &json!({"ok":false,"error":error.message}))
}
fn failed(error: ori_legacy::LegacyError) -> Response {
    crate::http::json_response(
        StatusCode::from_u16(error.http_status()).unwrap_or(StatusCode::BAD_GATEWAY),
        &error.legacy_body(),
    )
}
fn workspace(core: &Core) -> ApiResult<&Arc<Legacy>> {
    core.archive()?;
    if core.admission.is_quiescing() {
        return Err(ApiError::unavailable("runtime_quiescing"));
    }
    core.legacy
        .get()
        .ok_or_else(|| ApiError::unavailable("legacy_workspace_unavailable"))
}
fn data(bytes: Arc<[u8]>) -> Response {
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/json; charset=utf-8")
        .header(header::CONTENT_LENGTH, bytes.len())
        .header(header::CACHE_CONTROL, "no-store")
        .body(Body::from(Bytes::from_owner(bytes)))
        .expect("static legacy response")
}
fn optional(query: &BTreeMap<String, String>, key: &str) -> ori_legacy::Result<Option<f64>> {
    query
        .get(key)
        .filter(|value| !value.is_empty())
        .map(|value| numeric::float(value))
        .transpose()
}
pub(crate) async fn get(
    core: &Arc<Core>,
    parts: &[String],
    query: &BTreeMap<String, String>,
    authentication: AuthContext,
) -> ApiResult<Option<Response>> {
    let [prefix, route] = parts else {
        return Ok(None);
    };
    if prefix != "api" {
        return Ok(None);
    }
    if route == "stream" {
        return stream(core.clone(), authentication).await.map(Some);
    }
    let operation = match route.as_str() {
        "state" => LegacyQuery::State {
            include_waterfall: true,
        },
        "signals" => LegacyQuery::Signals,
        "coverage" => match numeric::dimension(query.get("rows")).and_then(|rows| {
            numeric::dimension(query.get("cols")).map(|cols| LegacyQuery::Coverage { rows, cols })
        }) {
            Ok(query) => query,
            Err(error) => return Ok(Some(failed(error))),
        },
        "occupancy" | "efficiency" => match optional(query, "threshold") {
            Ok(threshold_db) => {
                if route == "occupancy" {
                    LegacyQuery::Occupancy { threshold_db }
                } else {
                    LegacyQuery::Efficiency { threshold_db }
                }
            }
            Err(error) => return Ok(Some(failed(error))),
        },
        "measure" => match (|| {
            let start_hz = numeric::float(query.get("start").map_or("0", String::as_str))?;
            let stop_hz = numeric::float(query.get("stop").map_or("0", String::as_str))?;
            if stop_hz <= start_hz {
                return Err(ori_legacy::LegacyError::Request(
                    "Конец диапазона должен быть больше начала".into(),
                ));
            }
            Ok(LegacyQuery::Measurements {
                start_hz,
                stop_hz,
                norm_hz: optional(query, "norm")?,
            })
        })() {
            Ok(query) => query,
            Err(error) => return Ok(Some(failed(error))),
        },
        "keywords" => LegacyQuery::Keywords,
        _ => return Ok(None),
    };
    Ok(Some(
        match workspace(core)?.workspace.query_json(operation).await {
            Ok(bytes) => data(bytes),
            Err(error) => failed(error),
        },
    ))
}
pub(crate) async fn post(
    core: &Core,
    parts: &[String],
    payload: Value,
) -> ApiResult<Option<Response>> {
    let route = parts.iter().map(String::as_str).collect::<Vec<_>>();
    let result = match route.as_slice() {
        ["api", "threshold"] => workspace(core)?.workspace.threshold(payload).await,
        ["api", "database"] => {
            let path = payload.get("path");
            if path.is_none_or(|value| {
                value.is_null()
                    || value == false
                    || value == 0
                    || value == ""
                    || value.as_array().is_some_and(Vec::is_empty)
                    || value.as_object().is_some_and(serde_json::Map::is_empty)
            }) {
                return Ok(Some(failed(ori_legacy::LegacyError::Request(
                    "не указан путь".into(),
                ))));
            }
            let path = path
                .and_then(Value::as_str)
                .ok_or_else(|| ApiError::invalid("expected str, bytes or os.PathLike object"))?;
            if !cfg!(unix) {
                return Ok(Some(failed(ori_legacy::LegacyError::Science {
                    status: 503,
                    code: "legacy_worker_unavailable".into(),
                    message:
                        "Owned scientific process-tree containment is unavailable on this platform"
                            .into(),
                })));
            }
            workspace(core)?
                .workspace
                .load_database(std::path::Path::new(path))
                .await
        }
        _ => return Ok(None),
    };
    Ok(Some(match result {
        Ok(value) => crate::http::json_response(StatusCode::OK, &value),
        Err(error) => failed(error),
    }))
}
async fn stream(core: Arc<Core>, authentication: AuthContext) -> ApiResult<Response> {
    let permit = core
        .subscribers
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::unavailable("subscriber_limit"))?;
    let legacy = workspace(&core)?.clone();
    let mut changes = legacy.workspace.subscribe();
    let mut shutdown = core.shutdown.subscribe();
    let security = core.security.clone();
    let mut revocations = security.revocations.subscribe();
    let events = async_stream::stream! {
        let _permit=permit;let mut last=None;
        loop{
            if *shutdown.borrow()||security.validate(&authentication).await.is_err(){break}
            let version=*changes.borrow_and_update();
            if last!=Some(version){
                let Ok(bytes)=legacy.workspace.query_json(LegacyQuery::State{include_waterfall:false}).await else{break};
                let Ok(text)=std::str::from_utf8(&bytes)else{break};
                last=Some(version);yield Ok::<_,Infallible>(Event::default().data(text));
            }
            tokio::select!{
                _=shutdown.changed()=>{},
                _=revocations.changed()=>{},
                result=changes.changed()=>{if result.is_err(){break}},
                _=tokio::time::sleep(Duration::from_secs(1))=>{yield Ok(Event::default().comment("keep-alive"));}
            }
        }
    };
    Ok(Sse::new(events).into_response())
}
