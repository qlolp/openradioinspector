//! Legacy software TaskBook facade. Noop callbacks never operate an instrument.
use crate::{
    Core, CoreError,
    error::{ApiError, ApiResult},
    http::envelope,
    security::AuthReceipt,
    task_execution::{TaskAuthority, TaskExecution},
};
use axum::{http::StatusCode, response::Response};
use ori_tasks::{TaskResult, TaskScheduler, TaskSchedulerConfig};
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex, OnceLock,
    atomic::{AtomicBool, Ordering},
};
use tokio::sync::watch;
#[cfg(test)]
#[path = "task_facade_tests.rs"]
mod facade_tests;

pub(crate) struct Tasks {
    scheduler: Arc<TaskScheduler>,
    execution: Option<Arc<TaskExecution>>,
    configuring: tokio::sync::Mutex<()>,
    closing: AtomicBool,
    stage: Mutex<Option<tempfile::TempDir>>,
    runtime: tokio::runtime::Handle,
    shutdown: OnceLock<watch::Receiver<Option<TaskResult<()>>>>,
}
pub(crate) fn initialize(core: &Arc<Core>) -> crate::Result<()> {
    if core.archive.is_none() {
        return Ok(());
    }
    let stage = if core.config.task_book.is_none() {
        let mut builder = tempfile::Builder::new();
        builder.prefix(".ori-tasks-");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            builder.permissions(std::fs::Permissions::from_mode(0o700));
        }
        Some(builder.tempdir_in(&core.config.data_directory)?)
    } else {
        None
    };
    let path = core.config.task_book.clone().unwrap_or_else(|| {
        stage
            .as_ref()
            .expect("ephemeral task stage")
            .path()
            .join("tasks.json")
    });
    let config = TaskSchedulerConfig::new(path);
    let execution = core
        .config
        .task_execution
        .clone()
        .map(TaskExecution::new)
        .transpose()?;
    let effects: Arc<dyn ori_tasks::TaskEffects> = match &execution {
        Some(execution) => execution.clone(),
        None => Arc::new(ori_tasks::NoopTaskEffects),
    };
    // Startup only, before Guard can start acquisition. The library creates its
    // owned thread here; subsequent file IO/fsync never runs on device loops.
    let scheduler = if let Some(sources) = &core.config.domain_sources {
        let clock = sources.clock.clone();
        TaskScheduler::with_clock(
            config,
            effects,
            Arc::new(move || {
                chrono::DateTime::from_timestamp_micros(clock.now().unix_micros())
                    .expect("domain UTC")
            }),
        )?
    } else {
        TaskScheduler::open(config, effects)?
    };
    core.tasks
        .set(Arc::new(Tasks {
            scheduler,
            execution,
            configuring: tokio::sync::Mutex::new(()),
            closing: AtomicBool::new(false),
            stage: Mutex::new(stage),
            runtime: tokio::runtime::Handle::current(),
            shutdown: OnceLock::new(),
        }))
        .map_err(|_| CoreError::Invalid("task facade already initialized"))?;
    Ok(())
}
impl Tasks {
    pub(crate) fn execution_available(&self) -> bool {
        self.execution.is_some()
    }
    pub(crate) fn begin_shutdown(self: &Arc<Self>) -> watch::Receiver<Option<TaskResult<()>>> {
        self.closing.store(true, Ordering::Release);
        if let Some(execution) = &self.execution {
            execution.begin_shutdown();
        }
        self.shutdown
            .get_or_init(|| {
                let (completed, receiver) = watch::channel(None);
                let own = self.clone();
                self.runtime.spawn(async move {
                    let outcome = async {
                        let _configured = own.configuring.lock().await;
                        if let Some(execution) = &own.execution {
                            execution.begin_shutdown().wait_joined().await?;
                        }
                        own.scheduler.close().await?;
                        let stage = own
                            .stage
                            .lock()
                            .map_err(|_| {
                                ori_tasks::TaskError::new(
                                    503,
                                    "task_cleanup_failed",
                                    "Task storage cleanup could not be confirmed",
                                )
                            })?
                            .take();
                        if let Some(stage) = stage {
                            tokio::task::spawn_blocking(move || stage.close())
                                .await
                                .map_err(|_| {
                                    ori_tasks::TaskError::new(
                                        503,
                                        "task_cleanup_failed",
                                        "Task storage cleanup could not be confirmed",
                                    )
                                })?
                                .map_err(|_| {
                                    ori_tasks::TaskError::new(
                                        503,
                                        "task_cleanup_failed",
                                        "Task storage cleanup could not be confirmed",
                                    )
                                })?;
                        }
                        Ok(())
                    }
                    .await;
                    completed.send_replace(Some(outcome));
                });
                receiver
            })
            .clone()
    }
    pub(crate) async fn close(self: &Arc<Self>) -> crate::Result<()> {
        let mut completion = self.begin_shutdown();
        loop {
            if let Some(result) = completion.borrow().clone() {
                return result.map_err(Into::into);
            }
            completion
                .changed()
                .await
                .map_err(|_| CoreError::Invalid("task cleanup completion was lost"))?;
        }
    }
    async fn configure(self: &Arc<Self>, payload: Value, auth: AuthReceipt) -> ApiResult<Value> {
        let object = payload
            .as_object()
            .filter(|object| object.len() == 1 && object.contains_key("enabled"))
            .ok_or_else(|| ori_tasks::TaskError::invalid("Разрешено только поле enabled"))?;
        let enabled = object["enabled"]
            .as_bool()
            .ok_or_else(|| ori_tasks::TaskError::invalid("enabled должен быть JSON boolean"))?;
        let (sent, receiver) = tokio::sync::oneshot::channel();
        let own = self.clone();
        self.runtime.spawn(async move {
            let _order = own.configuring.lock().await;
            let result = async {
                if own.closing.load(Ordering::Acquire) {
                    return Err(ApiError::unavailable("task_scheduler_unavailable"));
                }
                auth.revalidate()?;
                if let Some(execution) = &own.execution {
                    if enabled {
                        let tasks = own.scheduler.list_tasks().await?;
                        execution.admit_enable(
                            tasks.as_array().ok_or_else(|| {
                                ApiError::unavailable("task_scheduler_unavailable")
                            })?,
                            Arc::new(Authority(auth.clone())),
                        )?;
                    } else {
                        execution.revoke();
                        execution.begin_drain().wait_joined().await?;
                    }
                }
                if own.closing.load(Ordering::Acquire) {
                    return Err(ApiError::unavailable("task_scheduler_unavailable"));
                }
                auth.revalidate()?;
                match own.scheduler.configure(payload).await {
                    Ok(value) => Ok(planning_status(value, own.execution_available())),
                    Err(error) => {
                        if let Some(execution) = &own.execution {
                            execution.revoke();
                            let _ = execution.begin_drain().wait_joined().await;
                        }
                        Err(error.into())
                    }
                }
            }
            .await;
            let _ = sent.send(result);
        });
        receiver
            .await
            .map_err(|_| ApiError::unavailable("task_configuration_completion_lost"))?
    }
}
struct Authority(AuthReceipt);
impl TaskAuthority for Authority {
    fn revalidate(&self) -> TaskResult<()> {
        self.0.revalidate().map_err(|_| {
            ori_tasks::TaskError::new(
                409,
                "task_execution_authority_changed",
                "Original task execution authority changed",
            )
        })
    }
    fn revoked(&self) -> ori_tasks::TaskFuture<'_, ()> {
        Box::pin(async move {
            self.0.revoked().await;
            Ok(())
        })
    }
}
impl Drop for Tasks {
    fn drop(&mut self) {
        // An abandoned Core cannot confirm that the actor joined. Do not
        // remove its owned directory while unfinished file work can survive.
        if let Ok(stage) = self.stage.get_mut()
            && let Some(stage) = stage.take()
        {
            std::mem::forget(stage);
        }
    }
}
fn scheduler(core: &Core) -> ApiResult<&Arc<TaskScheduler>> {
    core.archive()?;
    core.tasks
        .get()
        .map(|tasks| &tasks.scheduler)
        .ok_or_else(|| ApiError::unavailable("task_scheduler_unavailable"))
}
pub(crate) fn handles(parts: &[String]) -> bool {
    parts.len() >= 3 && parts[..3] == ["api", "v1", "task-scheduler"]
}
pub(crate) async fn get(core: &Core, parts: &[String]) -> ApiResult<Option<Response>> {
    if !handles(parts) {
        return Ok(None);
    }
    let scheduler = scheduler(core)?;
    let data = match &parts[3..] {
        [] => planning_status(
            scheduler.status().await?,
            core.tasks
                .get()
                .is_some_and(|tasks| tasks.execution_available()),
        ),
        [operation] if operation == "tasks" => {
            let items = scheduler.list_tasks().await?;
            json!({"count":items.as_array().map_or(0,Vec::len),"items":items})
        }
        _ => {
            return Err(ApiError::new(
                StatusCode::NOT_FOUND,
                "not_found",
                "Маршрут API v1 не найден",
            ));
        }
    };
    Ok(Some(envelope(StatusCode::OK, data)))
}
pub(crate) async fn post(
    core: &Core,
    parts: &[String],
    payload: Value,
    auth: AuthReceipt,
) -> ApiResult<Response> {
    let scheduler = scheduler(core)?;
    let (status, data) = match &parts[3..] {
        [] => (
            StatusCode::OK,
            core.tasks
                .get()
                .ok_or_else(|| ApiError::unavailable("task_scheduler_unavailable"))?
                .configure(payload, auth)
                .await?,
        ),
        [operation] if operation == "tasks" => {
            if let Some(execution) = core.tasks.get().and_then(|tasks| tasks.execution.as_ref()) {
                execution.validate_task(&payload)?;
            }
            (StatusCode::CREATED, scheduler.create_task(payload).await?)
        }
        [operation, name, action] if operation == "tasks" && action == "cancel" => {
            (StatusCode::OK, scheduler.cancel_task(name).await?)
        }
        _ => {
            return Err(ApiError::new(
                StatusCode::NOT_FOUND,
                "not_found",
                "Маршрут API v1 не найден",
            ));
        }
    };
    Ok(envelope(status, data))
}
fn planning_status(mut value: Value, available: bool) -> Value {
    value["execution_available"] = json!(available);
    value
}
