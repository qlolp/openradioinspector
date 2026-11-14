//! Only admitted manual investigation effects own a continuation past HTTP drop.
use crate::{
    Core,
    admission::MutationGuard,
    devices::{DeviceError, ExecutionGate, RetainedWorkerHandle, Target, TuneOutcome},
    error::{ApiError, ApiResult},
    http::envelope,
    security::AuthReceipt,
};
use axum::{http::StatusCode, response::Response};
use ori_guard::{ControlTarget, InstrumentControlGrant};
use ori_session::{SessionEffects, SessionError, SessionFuture, TuneCompletionCheck, TuneReceipt};
use serde_json::Value;
use std::sync::{Arc, Mutex, Weak};

enum Action {
    Start {
        session: String,
        id: String,
        next: bool,
    },
    Complete {
        session: String,
        id: String,
    },
}
pub(crate) fn handles(parts: &[String]) -> bool {
    let route = parts.iter().map(String::as_str).collect::<Vec<_>>();
    matches!(
        route.as_slice(),
        [
            "api",
            "v1",
            "sessions",
            _,
            "investigations",
            _,
            "start" | "complete"
        ] | ["api", "v1", "sessions", _, "investigations", "start-next"]
    )
}
fn action(parts: &[String], payload: &Value) -> ApiResult<Action> {
    let route = parts.iter().map(String::as_str).collect::<Vec<_>>();
    Ok(match route.as_slice() {
        [
            "api",
            "v1",
            "sessions",
            session,
            "investigations",
            id,
            "start",
        ] => Action::Start {
            session: (*session).into(),
            id: (*id).into(),
            next: false,
        },
        [
            "api",
            "v1",
            "sessions",
            session,
            "investigations",
            "start-next",
        ] => {
            if payload["confirm"] != true {
                return Err(ApiError::invalid(
                    "Полуавтоматический handoff требует явного confirm=true",
                ));
            }
            let id = crate::domain_http::required(payload, "investigation_id")?;
            if id.trim().is_empty() {
                return Err(ApiError::invalid(
                    "Для подтверждения требуется investigation_id",
                ));
            }
            Action::Start {
                session: (*session).into(),
                id: id.into(),
                next: true,
            }
        }
        [
            "api",
            "v1",
            "sessions",
            session,
            "investigations",
            id,
            "complete",
        ] => Action::Complete {
            session: (*session).into(),
            id: (*id).into(),
        },
        _ => return Err(ApiError::missing("not_found")),
    })
}
fn session_error(error: ApiError) -> SessionError {
    SessionError::new(error.status.as_u16(), &error.code, error.message)
}
fn device_error(error: DeviceError) -> SessionError {
    session_error(error.into())
}
struct Original {
    auth: AuthReceipt,
    session: ori_archive::SessionLifecycleReceipt,
    // Archive-only completion never asserts a connected receiver or RF grant.
    control: Option<Option<InstrumentControlGrant>>,
}
impl TuneCompletionCheck for Original {
    fn revalidate(&self) -> ori_session::SessionResult<()> {
        self.auth.revalidate().map_err(session_error)?;
        if !self.session.check() {
            return Err(SessionError::new(
                409,
                "session_lifecycle_changed",
                "Original session lifecycle changed",
            ));
        }
        if let Some(grant) = &self.control {
            grant
                .as_ref()
                .ok_or_else(|| device_error(DeviceError::Denied))?
                .check()
                .map_err(|_| device_error(DeviceError::Denied))?;
        }
        Ok(())
    }
}
struct Effects {
    core: Weak<Core>,
    original: Arc<Original>,
    worker: Mutex<Option<std::result::Result<RetainedWorkerHandle, DeviceError>>>,
}
struct Completion {
    original: Arc<Original>,
    outcome: TuneOutcome,
}
impl TuneCompletionCheck for Completion {
    fn revalidate(&self) -> ori_session::SessionResult<()> {
        self.original.revalidate()?;
        self.outcome.revalidate().map_err(device_error)
    }
}
impl SessionEffects for Effects {
    fn tune(&self, request: ori_session::TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            self.original.revalidate()?;
            let worker = self
                .worker
                .lock()
                .map_err(|_| device_error(DeviceError::Closed))?
                .take()
                .ok_or_else(|| device_error(DeviceError::Denied))?
                .map_err(device_error)?;
            worker.check().map_err(device_error)?;
            if worker.target() != &Target::new(&request.session_id, &request.stream_id) {
                return Err(device_error(DeviceError::Denied));
            }
            let core = self
                .core
                .upgrade()
                .ok_or_else(|| device_error(DeviceError::Closed))?;
            let outcome = core
                .devices
                .as_ref()
                .ok_or_else(|| device_error(DeviceError::Closed))?
                .tune(
                    worker.target().clone(),
                    request.frequency_hz,
                    request.mode,
                    worker.execution_gate(),
                )
                .await
                .map_err(device_error)?;
            let check = Arc::new(Completion {
                original: self.original.clone(),
                outcome,
            });
            check.revalidate()?;
            Ok(TuneReceipt::new(check))
        })
    }
    fn validate_reference(&self, _: Value, _: Value) -> SessionFuture<'_, ()> {
        Box::pin(async {
            Err(SessionError::new(
                503,
                "reference_unavailable",
                "Manual tune context has no reference callback",
            ))
        })
    }
}
async fn perform(core: &Arc<Core>, action: Action, auth: AuthReceipt) -> ApiResult<Response> {
    let (session, id, start) = match &action {
        Action::Start { session, id, .. } => (session, id, true),
        Action::Complete { session, id } => (session, id, false),
    };
    // Preserve missing-session/job and explicit-confirm errors before policy
    // capture. These reads never refresh the originating authentication.
    let domain = core.domain()?;
    let job = domain.session.get_investigation(session, id).await?;
    auth.revalidate()?;
    let lifecycle = core.archive()?.capture_session_lifecycle(session).await?;
    auth.revalidate()?;
    let target = Target::new(
        session,
        job["target_stream_id"]
            .as_str()
            .ok_or_else(|| ApiError::unavailable("archive_invalid_investigation"))?,
    );
    // A failed capture is retained as a permanent denial, not returned early:
    // domain state/confirmed-next validation retains its original precedence.
    let control = start.then(|| {
        domain
            .guard
            .capture_instrument_control(
                &ControlTarget {
                    session_id: target.session_id.clone(),
                    stream_id: target.stream_id.clone(),
                },
                None,
            )
            .ok()
    });
    let original = Arc::new(Original {
        auth: auth.clone(),
        session: lifecycle,
        control,
    });
    let value = match action {
        Action::Start { session, id, next } => {
            let check = original.clone();
            let gate = ExecutionGate::new(
                move || check.revalidate().map_err(|_| DeviceError::Denied),
                None,
            );
            let worker = core
                .devices
                .as_ref()
                .ok_or(DeviceError::Closed)?
                .capture_worker(&target, gate);
            let effects = Arc::new(Effects {
                core: Arc::downgrade(core),
                original: original.clone(),
                worker: Mutex::new(Some(worker)),
            });
            if next {
                domain
                    .session
                    .start_next_investigation_with_effects_and_check(
                        &session, &id, effects, original,
                    )
                    .await?
            } else {
                domain
                    .session
                    .start_investigation_with_effects_and_check(&session, &id, effects, original)
                    .await?
            }
        }
        Action::Complete { session, id } => {
            domain
                .session
                .complete_investigation_with_check(&session, &id, original)
                .await?
        }
    };
    auth.revalidate()?;
    Ok(envelope(StatusCode::OK, value))
}
pub(crate) async fn post(
    core: Arc<Core>,
    parts: &[String],
    payload: Value,
    auth: AuthReceipt,
    admitted: MutationGuard,
) -> ApiResult<Response> {
    let action = action(parts, &payload)?;
    let capacity = core
        .manual_requests
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::unavailable("manual_request_capacity"))?;
    auth.revalidate()?;
    let (sent, received) = tokio::sync::oneshot::channel();
    core.manual_runtime.clone().spawn(async move {
        let _capacity = capacity;
        let _admitted = admitted;
        let result = perform(&core, action, auth).await;
        let _ = sent.send(result);
    });
    received
        .await
        .map_err(|_| ApiError::unavailable("manual_request_completion_lost"))?
}
