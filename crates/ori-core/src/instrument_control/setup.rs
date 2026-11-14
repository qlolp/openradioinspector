//! Bounded owned setup, with an original target epoch through ready/bind.
use super::*;
use axum::http::StatusCode;
use serde_json::Value;

pub(super) async fn start(
    core: &Arc<Core>,
    target: &Target,
    auth: AuthReceipt,
    grant: InstrumentControlGrant,
) -> ApiResult<Value> {
    let service = core
        .instrument_control
        .get()
        .ok_or_else(|| ApiError::unavailable("instrument_owner"))?
        .clone();
    let cell = service.target_cell(target).map_err(super::http::api)?;
    let generation = cell.generation.load(Ordering::Acquire);
    let source = core.clone();
    let target = target.clone();
    let authority = Arc::downgrade(&service);
    let received = service.spawn_setup(async move {
        let service = authority
            .upgrade()
            .ok_or_else(|| ApiError::unavailable("instrument_closed"))?;
        let target_guard = cell.lock.clone().lock_owned().await;
        cell.check(generation)?;
        auth.revalidate()?;
        grant
            .check()
            .map_err(|_| super::http::api(ControlError::Denied))?;
        if service.closed.load(Ordering::Acquire) {
            return Err(ApiError::unavailable("instrument_closed"));
        }
        if grant.manual_role().is_some() && grant.expected_worker_instance().is_some() {
            return Err(super::http::api(ControlError::Denied));
        }
        let manager = source
            .devices
            .as_ref()
            .ok_or_else(|| ApiError::unavailable("instrument_owner"))?
            .clone();
        let device_target = devices::Target::new(&target.session_id, &target.stream_id);
        let original = gate(auth.clone(), grant.clone(), None);
        let original_cell = cell.clone();
        let live = Arc::downgrade(&service);
        let check = devices::ExecutionGate::new(
            move || {
                let service = live.upgrade().ok_or(devices::DeviceError::Closed)?;
                if service.closed.load(Ordering::Acquire) {
                    return Err(devices::DeviceError::Closed);
                }
                original_cell.check(generation)?;
                original.authorize()
            },
            None,
        );
        let (status, retained) = manager
            .start_instrument(
                &device_target,
                check.clone(),
                grant.manual_role() == Some("analyzer"),
            )
            .await?;
        #[cfg(test)]
        service.before_bind().await;
        let bound = (|| {
            cell.check(generation)?;
            retained.check()?;
            auth.revalidate()
                .map_err(|_| devices::DeviceError::Denied)?;
            grant.check().map_err(|_| devices::DeviceError::Denied)?;
            if grant.manual_role().is_some() {
                source
                    .domains
                    .get()
                    .ok_or(devices::DeviceError::Closed)?
                    .guard
                    .bind_manual_worker(&grant, &super::http::guard_worker(status.clone()))
                    .map_err(|_| devices::DeviceError::Denied)?;
            }
            Ok::<(), devices::DeviceError>(())
        })();
        // Never hold target exclusion while awaiting Guard's project lifecycle.
        drop(target_guard);
        let result = async {
            bound?;
            if grant.manual_role().is_some() {
                source
                    .domain()?
                    .guard
                    .confirm_instrument_coverage(&grant)
                    .await?;
            }
            retained.check()?;
            cell.check(generation)?;
            auth.revalidate()?;
            if service.closed.load(Ordering::Acquire) {
                return Err(ApiError::unavailable("instrument_closed"));
            }
            if status.adapter.as_deref() == Some("tinysa-serial") {
                let device = manager
                    .capture_instrument(&device_target, retained.execution_gate())
                    .map_err(super::http::api)?;
                service
                    .remember_connection(
                        target,
                        Arc::new(Scope {
                            device,
                            auth,
                            grant,
                        }),
                    )
                    .map_err(super::http::api)?;
            }
            status.value().map_err(ApiError::from)
        }
        .await;
        if result.is_err() && manager.stop_retained_worker(&retained).await.is_err() {
            service.setup_failed.store(true, Ordering::Release);
            return Err(ApiError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "instrument_cleanup_unconfirmed",
                "Исходный транспорт не завершил очистку; владение сохранено",
            ));
        }
        result
    })?;
    received
        .await
        .map_err(|_| ApiError::unavailable("instrument_setup_task"))?
}
