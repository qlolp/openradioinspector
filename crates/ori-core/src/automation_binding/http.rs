use super::*;
use crate::{
    error::{ApiError, ApiResult},
    security::AuthReceipt,
};
use axum::{
    http::{Method, StatusCode},
    response::Response,
};
use std::collections::BTreeMap;

fn route(parts: &[String]) -> Option<(&str, PolicyKind)> {
    if parts.len() != 5 || parts[..3] != ["api", "v1", "sessions"] {
        return None;
    }
    let kind = match parts[4].as_str() {
        "automatic-detection" => PolicyKind::Detection,
        "automatic-handoff" => PolicyKind::Handoff,
        "automatic-completion" => PolicyKind::Completion,
        _ => return None,
    };
    Some((&parts[3], kind))
}
pub(crate) fn handles(method: &Method, parts: &[String]) -> bool {
    (*method == Method::GET || *method == Method::POST) && route(parts).is_some()
}
fn binding(core: &Core) -> ApiResult<&Arc<AutomationBinding>> {
    core.archive()?;
    core.automation
        .get()
        .ok_or_else(|| ApiError::unavailable("automation_unavailable"))
}
pub(crate) async fn get(
    core: &Core,
    parts: &[String],
    _query: &BTreeMap<String, String>,
    auth: AuthReceipt,
) -> ApiResult<Option<Response>> {
    let Some((session, kind)) = route(parts) else {
        return Ok(None);
    };
    auth.revalidate()?;
    let binding = binding(core)?;
    binding.check()?;
    let status = binding.service.status(kind, session).await?;
    auth.revalidate()?;
    Ok(Some(crate::http::envelope(StatusCode::OK, status)))
}
pub(crate) async fn post(
    core: &Core,
    parts: &[String],
    mut payload: Value,
    auth: AuthReceipt,
) -> ApiResult<Option<Response>> {
    let Some((session, kind)) = route(parts) else {
        return Ok(None);
    };
    auth.revalidate()?;
    let owner = payload
        .as_object_mut()
        .and_then(|object| object.remove("control_owner"));
    let owner = match owner {
        None | Some(Value::Null) => None,
        Some(Value::String(owner)) if !owner.is_empty() && owner.len() <= 256 => Some(owner),
        _ => {
            return Err(ApiError::invalid(
                "control_owner должно быть непустой строкой или null",
            ));
        }
    };
    let result = binding(core)?
        .configure(kind, session.into(), payload, auth, owner)
        .await?;
    Ok(Some(crate::http::envelope(StatusCode::OK, result)))
}
