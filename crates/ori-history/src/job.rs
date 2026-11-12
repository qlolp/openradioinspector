//! One sealed source and one fixed installed-worker operation.
use crate::{
    HistoryConfig, HistoryError, HistoryRequest, JobOutput, JobPhase, JobView, Operation, Result,
    storage::{Roots, SealedSnapshot},
    types::MAX_RESULT_BYTES,
};
use ori_archive::Archive;
use ori_runtime::worker::{Cancellation, ResearchPermit, WorkerSpec};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};

pub(crate) struct Record {
    pub(crate) view: JobView,
    pub(crate) request: HistoryRequest,
    pub(crate) snapshot: Option<Arc<SealedSnapshot>>,
    pub(crate) output: Option<Arc<JobOutput>>,
    pub(crate) bytes: usize,
    pub(crate) permit: Option<ResearchPermit>,
}
pub(crate) struct JobWork {
    pub(crate) archive: Archive,
    pub(crate) roots: Arc<Roots>,
    pub(crate) config: HistoryConfig,
    pub(crate) worker: WorkerSpec,
    pub(crate) request: HistoryRequest,
    pub(crate) replay: Option<(Arc<SealedSnapshot>, Option<String>)>,
    pub(crate) permit: Option<ResearchPermit>,
    pub(crate) cancel: Cancellation,
    pub(crate) current: Arc<Mutex<JobView>>,
}
pub(crate) fn progress(value: &Arc<Mutex<JobView>>) -> Result<JobView> {
    value
        .lock()
        .map(|v| v.clone())
        .map_err(|_| HistoryError::Internal)
}
pub(crate) async fn run(mut work: JobWork) -> Record {
    let mut snapshot = None;
    let mut result = execute(&mut work, &mut snapshot).await;
    if result.is_err()
        && let Some(retired) = snapshot.take()
        && (tokio::task::spawn_blocking(move || drop(retired))
            .await
            .is_err()
            || work.roots.cleanup_failed())
    {
        result = Err(HistoryError::Internal);
    }
    let mut view = progress(&work.current)
        .unwrap_or_else(|_| JobView::new(String::new(), work.request.operation));
    let (output, bytes) = match result {
        Ok((output, bytes)) => {
            view.phase = JobPhase::Completed;
            view.result_available = true;
            view.enforcement = Some(output.enforcement.clone());
            (Some(output), bytes)
        }
        Err(error) => {
            view.phase = if matches!(error, HistoryError::Cancelled) {
                JobPhase::Cancelled
            } else {
                JobPhase::Failed
            };
            view.failure = Some(error.failure());
            snapshot = None;
            (None, 0)
        }
    };
    Record {
        view,
        request: work.request,
        snapshot,
        output,
        bytes,
        permit: work.permit.take(),
    }
}

async fn execute(
    work: &mut JobWork,
    snapshot: &mut Option<Arc<SealedSnapshot>>,
) -> Result<(Arc<JobOutput>, usize)> {
    if work.cancel.is_cancelled() {
        return Err(HistoryError::Cancelled);
    }
    let (sealed, cursor) = if let Some(replay) = work.replay.take() {
        replay
    } else {
        (
            crate::snapshot::build(
                work.archive.clone(),
                work.roots.clone(),
                &work.config,
                &work.request.filters,
                &work.cancel,
            )
            .await?,
            None,
        )
    };
    *snapshot = Some(sealed.clone());
    {
        let mut view = work.current.lock().map_err(|_| HistoryError::Internal)?;
        view.phase = JobPhase::Running;
        view.source_boundary = Some(sealed.boundary);
        view.source_sha256 = Some(sealed.sha256.clone());
    }
    let roots = work.roots.clone();
    let (cache_budget, temp_budget) = tokio::task::spawn_blocking(move || roots.worker_budget())
        .await
        .map_err(|_| HistoryError::Internal)??;
    let mut input = json!({
        "version": 1, "request_id": progress(&work.current)?.id,
        "operation": work.request.operation.name(),
        "snapshot_path": sealed.directory.join("records.ndjson"),
        "cache_directory": work.roots.worker, "filters": work.request.filters,
        "page_size": work.request.page_size,
        "limits": {"cache_bytes": cache_budget, "temp_bytes": temp_budget,
                   "worker_rss_bytes": work.config.worker_rss_bytes}
    });
    if let Some(capability) = work.config.qos.worker_io() {
        input["shared_io"] =
            serde_json::to_value(capability).map_err(|_| HistoryError::Internal)?;
    }
    if let Some(threshold) = &work.request.threshold {
        input["threshold"] = json!(threshold);
    }
    if let Some(cursor) = cursor {
        input["cursor"] = json!(cursor);
    }
    let bytes = ori_contracts::to_json_vec(&input).map_err(|_| HistoryError::Internal)?;
    if bytes.len() > 65536 {
        return Err(HistoryError::ResourceLimit);
    }
    let permit = work.permit.take().ok_or(HistoryError::Internal)?;
    let (permit, result) = permit
        .run_retained(work.worker.clone(), bytes, work.cancel.clone())
        .await;
    work.permit = Some(permit);
    let result = result?;
    if work.cancel.is_cancelled() {
        return Err(HistoryError::Cancelled);
    }
    if result.response["ok"] != true {
        let code = result.response["error"]["code"]
            .as_str()
            .unwrap_or("worker_failed");
        let allowed = [
            "invalid_request",
            "invalid_filter",
            "invalid_cursor",
            "unsupported_schema",
            "source_record_invalid",
            "source_record_too_large",
            "snapshot_invalid",
            "unsealed_snapshot",
            "cache_busy",
            "cache_quota_exceeded",
            "temp_quota_exceeded",
            "rss_limit_exceeded",
            "response_too_large",
            "internal_error",
            "shared_io_unavailable",
        ];
        return Err(HistoryError::Worker {
            code: if allowed.contains(&code) {
                code.into()
            } else {
                "worker_failed".into()
            },
            retryable: result.response["error"]["retryable"]
                .as_bool()
                .unwrap_or(false),
        });
    }
    let allowed = if work.request.operation == Operation::Export {
        &[
            "cache_key",
            "cache_hit",
            "cache_rebuilt",
            "manifest",
            "manifest_sha256",
        ][..]
    } else {
        &[
            "rows",
            "next_cursor",
            "cache_key",
            "event_watermark",
            "occupancy_basis",
            "threshold",
            "cache_hit",
            "cache_rebuilt",
            "manifest_sha256",
        ][..]
    };
    let source = result.response["result"]
        .as_object()
        .ok_or(HistoryError::Internal)?;
    if result.response["operation"] != work.request.operation.name()
        || !source
            .get("cache_key")
            .and_then(Value::as_str)
            .is_some_and(crate::viewport::hex_key)
    {
        return Err(HistoryError::Internal);
    }
    let data = Value::Object(
        source
            .iter()
            .filter(|(key, _)| allowed.contains(&key.as_str()))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect(),
    );
    let output = Arc::new(JobOutput {
        data,
        metrics: result.response["metrics"].clone(),
        enforcement: result.enforcement,
    });
    let bytes = ori_contracts::to_json_vec(output.as_ref())
        .map_err(|_| HistoryError::Internal)?
        .len();
    if bytes > MAX_RESULT_BYTES {
        return Err(HistoryError::ResourceLimit);
    }
    let roots = work.roots.clone();
    tokio::task::spawn_blocking(move || roots.total())
        .await
        .map_err(|_| HistoryError::Internal)??;
    Ok((output, bytes))
}

#[cfg(all(test, unix))]
#[path = "job_tests.rs"]
mod tests;
