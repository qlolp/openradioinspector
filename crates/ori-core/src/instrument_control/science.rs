//! Fixed pure-memory adapter to the already configured, bounded research owner.
use super::*;
use ori_contracts::to_json_vec;
use ori_control::{MeasurementOutcome, MeasurementRequest, raw_document};
use ori_runtime::worker::Cancellation;
use serde_json::json;

#[cfg(all(test, unix))]
#[path = "science_tests.rs"]
mod tests;

pub(super) async fn analyze(
    core: &Arc<Core>,
    request: MeasurementRequest,
) -> Result<MeasurementOutcome> {
    request.owner.revalidate()?;
    let spec = core
        .config
        .analysis_worker
        .clone()
        .ok_or(ControlError::Unavailable("scientific_worker"))?;
    let permit = core
        .research_pool
        .try_reserve()
        .map_err(|_| ControlError::Unavailable("scientific_worker_capacity"))?;
    let id = crate::new_id();
    let source = raw_document(&request.source)?;
    let payload = json!({"version":1,"request_id":id,"operation":"control_analysis","method":request.method.as_str(),"source":source,"rbw_hz":request.rbw_applied_hz});
    if to_json_vec(&payload)
        .map_err(|_| ControlError::Invalid("scientific source encoding"))?
        .len()
        > 16 * 1024 * 1024
    {
        return Err(ControlError::Invalid("scientific source byte budget"));
    }
    let cancel = Cancellation::default();
    let owner = request.owner.clone();
    let check: crate::private_worker::Check = Arc::new(move || {
        owner
            .revalidate()
            .map_err(|_| ori_runtime::Error::Cancelled)
    });
    let response = crate::private_worker::run(
        core,
        permit,
        spec,
        payload,
        cancel,
        Some(check),
        crate::private_worker::IoClass::MemoryOnly,
    )
    .await
    .map_err(|error| {
        if *core.shutdown.borrow() || request.owner.revalidate().is_err() {
            return ControlError::Denied;
        }
        match error {
            ori_runtime::Error::Cancelled => ControlError::Unavailable("acquisition_pressure"),
            ori_runtime::Error::Busy => ControlError::Unavailable("scientific_worker_capacity"),
            _ => ControlError::Backend("scientific worker did not complete".into()),
        }
    })?;
    request.owner.revalidate()?;
    let value = response.response;
    if value.as_object().is_none_or(|map| map.len() != 5)
        || value["version"] != 1
        || value["request_id"] != id
        || value["operation"] != "control_analysis"
    {
        return Err(ControlError::Backend("invalid scientific reply".into()));
    }
    if value["ok"] != true {
        return Err(ControlError::Backend("scientific validation failed".into()));
    }
    let result = value
        .get("result")
        .filter(|v| v.is_object())
        .cloned()
        .ok_or(ControlError::Backend("invalid scientific result".into()))?;
    Ok(MeasurementOutcome { result })
}
