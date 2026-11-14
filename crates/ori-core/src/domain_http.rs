//! Existing public paths/envelopes over the shared Session and Guard domains.
use crate::{
    Core,
    error::{ApiError, ApiResult},
    http::envelope,
};
use axum::{
    body::Body,
    http::{StatusCode, header},
    response::Response,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;
type Query = BTreeMap<String, String>;
fn count(query: &Query, name: &str, default: usize, maximum: usize) -> ApiResult<usize> {
    query
        .get(name)
        .map(|value| {
            value
                .parse::<usize>()
                .ok()
                .filter(|v| *v > 0 && *v <= maximum)
                .ok_or_else(|| ApiError::invalid(format!("{name} вне допустимого диапазона")))
        })
        .unwrap_or(Ok(default))
}
fn number(query: &Query, name: &str, default: u64) -> ApiResult<u64> {
    query
        .get(name)
        .map(|value| {
            value
                .parse()
                .map_err(|_| ApiError::invalid(format!("{name} должно быть неотрицательным целым")))
        })
        .unwrap_or(Ok(default))
}
fn optional<'a>(query: &'a Query, name: &str) -> Option<&'a str> {
    query.get(name).map(String::as_str)
}
pub(crate) fn required<'a>(value: &'a Value, key: &str) -> ApiResult<&'a str> {
    value.get(key).and_then(Value::as_str).ok_or_else(|| {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "missing_field",
            format!("Отсутствует поле: {key}"),
        )
    })
}
pub(crate) fn handles_post(parts: &[String]) -> bool {
    parts.len() >= 3 && parts[..3] == ["api", "v1", "monitoring-projects"]
        || parts.len() >= 5
            && parts[..3] == ["api", "v1", "sessions"]
            && matches!(
                parts[4].as_str(),
                "candidates"
                    | "investigations"
                    | "measurement-results"
                    | "sweep-references"
                    | "signal-list"
            )
}
pub(crate) async fn get(
    core: &Core,
    parts: &[String],
    query: &Query,
) -> ApiResult<Option<Response>> {
    let route = parts.iter().map(String::as_str).collect::<Vec<_>>();
    if route.starts_with(&["api", "v1", "monitoring-projects"]) {
        return guard_get(core, &route[3..], query).await.map(Some);
    }
    let data = match route.as_slice() {
        ["api", "v1", "measurement-catalog"] => {
            core.domain()?.session.measurement_catalog().await?
        }
        ["api", "v1", "sessions", session, "candidates"] => {
            core.domain()?
                .session
                .list_candidates(session, optional(query, "state"))
                .await?
        }
        ["api", "v1", "sessions", session, "candidates", "by-id", id]
        | ["api", "v1", "sessions", session, "candidates", id] => {
            core.domain()?.session.get_candidate(session, id).await?
        }
        ["api", "v1", "sessions", session, "investigations"] => {
            core.domain()?
                .session
                .list_investigations(session, optional(query, "state"))
                .await?
        }
        ["api", "v1", "sessions", session, "investigations", "next"] => {
            core.domain()?.session.next_investigation(session).await?
        }
        [
            "api",
            "v1",
            "sessions",
            session,
            "investigations",
            "by-id",
            id,
        ]
        | ["api", "v1", "sessions", session, "investigations", id] => {
            core.domain()?
                .session
                .get_investigation(session, id)
                .await?
        }
        ["api", "v1", "sessions", session, "measurement-results"] => {
            core.domain()?
                .session
                .list_measurement_results(
                    session,
                    optional(query, "methodology"),
                    Some(count(query, "limit", 100, 1000)?),
                )
                .await?
        }
        ["api", "v1", "sessions", session, "sweep-references"] => {
            if query.keys().any(|key| key != "stream_id")
                || query
                    .get("stream_id")
                    .is_some_and(|value| value.trim().is_empty())
            {
                return Err(ApiError::invalid("Разрешён только один непустой stream_id"));
            }
            core.domain()?
                .session
                .list_sweep_references(session, optional(query, "stream_id"))
                .await?
        }
        ["api", "v1", "sessions", session, "signal-list", "export"] => {
            core.domain()?
                .session
                .signal_list_export(
                    session,
                    optional(query, "name").unwrap_or("Список сигналов"),
                    optional(query, "stream_id"),
                )
                .await?
        }
        _ => return Ok(None),
    };
    Ok(Some(envelope(StatusCode::OK, data)))
}
pub(crate) async fn post(
    core: &Core,
    parts: &[String],
    payload: Value,
) -> ApiResult<Option<Response>> {
    let route = parts.iter().map(String::as_str).collect::<Vec<_>>();
    if route.starts_with(&["api", "v1", "monitoring-projects"]) {
        return guard_post(core, &route[3..], payload).await.map(Some);
    }
    let (status, data) = match route.as_slice() {
        ["api", "v1", "sessions", session, "candidates"] => (
            StatusCode::CREATED,
            core.domain()?
                .session
                .create_candidate(session, payload)
                .await?,
        ),
        [
            "api",
            "v1",
            "sessions",
            session,
            "candidates",
            id,
            "investigations",
        ] => (
            StatusCode::CREATED,
            core.domain()?
                .session
                .enqueue_investigation(session, id, payload)
                .await?,
        ),
        [
            "api",
            "v1",
            "sessions",
            session,
            "candidates",
            id,
            "dismiss",
        ] => (
            StatusCode::OK,
            core.domain()?
                .session
                .dismiss_candidate(session, id)
                .await?,
        ),
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
            (
                StatusCode::OK,
                core.domain()?
                    .session
                    .start_next_investigation(session, required(&payload, "investigation_id")?)
                    .await?,
            )
        }
        [
            "api",
            "v1",
            "sessions",
            session,
            "investigations",
            id,
            "start",
        ] => (
            StatusCode::OK,
            core.domain()?
                .session
                .start_investigation(session, id)
                .await?,
        ),
        [
            "api",
            "v1",
            "sessions",
            session,
            "investigations",
            id,
            "complete",
        ] => (
            StatusCode::OK,
            core.domain()?
                .session
                .complete_investigation(session, id)
                .await?,
        ),
        [
            "api",
            "v1",
            "sessions",
            session,
            "investigations",
            id,
            "cancel",
        ] => (
            StatusCode::OK,
            core.domain()?
                .session
                .cancel_investigation(session, id)
                .await?,
        ),
        [
            "api",
            "v1",
            "sessions",
            session,
            "investigations",
            id,
            "fail",
        ] => (
            StatusCode::OK,
            core.domain()?
                .session
                .fail_investigation(session, id, payload["error"].as_str().unwrap_or(""))
                .await?,
        ),
        ["api", "v1", "sessions", session, "measurement-results"] => (
            StatusCode::CREATED,
            core.domain()?
                .session
                .save_measurement_result(session, payload)
                .await?,
        ),
        ["api", "v1", "sessions", session, "sweep-references"] => {
            let result = core
                .domain()?
                .session
                .create_sweep_reference(session, payload)
                .await?;
            (
                if result["created"] == true {
                    StatusCode::CREATED
                } else {
                    StatusCode::OK
                },
                result["reference"].clone(),
            )
        }
        ["api", "v1", "sessions", session, "signal-list", "import"] => (
            StatusCode::CREATED,
            core.domain()?
                .session
                .signal_list_import(session, payload)
                .await?,
        ),
        _ => return Ok(None),
    };
    Ok(Some(envelope(status, data)))
}
async fn guard_get(core: &Core, tail: &[&str], query: &Query) -> ApiResult<Response> {
    let guard = &core.domain()?.guard;
    let limit = || count(query, "limit", 200, 1000);
    let after = || optional(query, "after").unwrap_or("");
    let data = match tail {
        [] => {
            guard
                .list_records("project", None, after(), limit()?, false)
                .await?
        }
        ["audio-inputs"] => guard.audio_inputs().await?,
        [id] => guard.project(id).await?,
        [id, "preflight"] => guard.preflight(id, false).await?,
        [id, "settings"] => guard.settings(id).await?,
        [id, "storage"] => guard.storage_report(id, true).await?,
        [id, "alerts"] => {
            if query
                .keys()
                .any(|key| !matches!(key.as_str(), "limit" | "after"))
            {
                return Err(
                    ori_guard::GuardError::Invalid("Неверная пагинация тревог".into()).into(),
                );
            }
            guard
                .alerts(
                    id,
                    optional(query, "after"),
                    count(query, "limit", 50, 100)?,
                )
                .await?
        }
        [id, "status"] => guard.guard_status(id).await?,
        [id, "search", "status"] => guard.search_status(id).await?,
        [id, "baselines"] => {
            guard
                .list_records("baseline", Some(id), after(), limit()?, true)
                .await?
        }
        [id, "allowed"] => {
            guard
                .list_records("allowed", Some(id), after(), limit()?, false)
                .await?
        }
        [id, "events"] => {
            guard
                .list_records("event", Some(id), after(), limit()?, true)
                .await?
        }
        [id, "events", event, "sources"] => {
            if query
                .keys()
                .any(|key| !matches!(key.as_str(), "limit" | "after"))
            {
                return Err(
                    ori_guard::GuardError::Invalid("Неверная пагинация источников".into()).into(),
                );
            }
            guard
                .event_sources(id, event, after(), count(query, "limit", 64, 64)?)
                .await?
        }
        [id, "events", event, "audio"] => {
            let artifact = guard.event_audio(id, event).await?;
            return Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "audio/wav")
                .header(header::CONTENT_LENGTH, artifact.payload.len())
                .header(header::CACHE_CONTROL, "no-store")
                .body(Body::from(artifact.payload))
                .map_err(|_| ApiError::unavailable("audio_response"));
        }
        [id, "events", event] => guard.get_record("event", event, Some(id)).await?,
        [id, "baselines", baseline] => guard.get_record("baseline", baseline, Some(id)).await?,
        [id, "history"] => {
            if query.keys().any(|key| {
                !matches!(
                    key.as_str(),
                    "limit" | "after" | "start" | "end" | "min_hz" | "max_hz" | "event_id"
                )
            }) {
                return Err(
                    ori_guard::GuardError::Invalid("Неверные фильтры истории".into()).into(),
                );
            }
            guard
                .history(
                    id,
                    ori_guard::HistoryQuery {
                        limit: count(query, "limit", 32, 64)?,
                        after: optional(query, "after").map(str::to_owned),
                        start: optional(query, "start").map(str::to_owned),
                        end: optional(query, "end").map(str::to_owned),
                        min_hz: number(query, "min_hz", 0)?,
                        max_hz: number(query, "max_hz", 20_000_000_000)?,
                        event_id: optional(query, "event_id").map(str::to_owned),
                    },
                )
                .await?
        }
        [id, "history", source] => guard.history_original(id, source).await?,
        [id, "history", source, "comparison"] => guard.history_comparison(id, source).await?,
        _ => {
            return Err(ApiError::new(
                StatusCode::NOT_FOUND,
                "monitoring_route_not_found",
                "Маршрут проекта не найден",
            ));
        }
    };
    Ok(envelope(StatusCode::OK, data))
}
async fn guard_post(core: &Core, tail: &[&str], payload: Value) -> ApiResult<Response> {
    let guard = &core.domain()?.guard;
    let (status, data) = match tail {
        [] => (StatusCode::CREATED, guard.create(payload).await?),
        [id, "preparation"] | [id, "preparation", "save"] => {
            (StatusCode::OK, guard.save_preparation(id, payload).await?)
        }
        [id, "preparation", "start"] => {
            (StatusCode::OK, guard.start_preparation(id, payload).await?)
        }
        [id, "preparation", "stop"] => (StatusCode::OK, guard.stop_preparation(id, payload).await?),
        [id, "settings"] => (StatusCode::OK, guard.update_settings(id, payload).await?),
        [id, "configure"] => (StatusCode::OK, guard.configure(id, payload).await?),
        [id, "arm"] => (StatusCode::OK, guard.arm(id, payload).await?),
        [id, "disarm"] => (StatusCode::OK, guard.disarm(id, payload).await?),
        [id, "baselines"] => (
            StatusCode::CREATED,
            guard.create_baseline(id, payload).await?,
        ),
        [id, "allowed"] => (StatusCode::CREATED, guard.add_allowed(id, payload).await?),
        [id, "analyze"] => {
            if payload != json!({}) {
                return Err(ori_guard::GuardError::Invalid(
                    "Анализ новых свипов принимает пустой объект".into(),
                )
                .into());
            }
            (StatusCode::OK, guard.analyze(id).await?)
        }
        [id, "events", event, "review"] => (
            StatusCode::OK,
            guard.review_event(id, event, payload).await?,
        ),
        [id, "alerts", alert, "ack"] => (
            StatusCode::OK,
            guard.acknowledge_alert(id, alert, payload).await?,
        ),
        [id, "manual", "acquire"] => (StatusCode::OK, guard.manual_acquire(id, payload).await?),
        [id, "manual", "renew"] => (StatusCode::OK, guard.manual_renew(id, payload).await?),
        [id, "manual", "return"] => (StatusCode::OK, guard.manual_return(id, payload).await?),
        [id, "manual", "tune"] => (StatusCode::OK, guard.manual_tune(id, payload).await?),
        [id, "search", "start"] => (StatusCode::OK, guard.search_start(id, payload).await?),
        [id, "search", "mark"] => (StatusCode::OK, guard.search_mark(id, payload).await?),
        _ => {
            return Err(ApiError::new(
                StatusCode::NOT_FOUND,
                "monitoring_route_not_found",
                "Маршрут проекта не найден",
            ));
        }
    };
    Ok(envelope(status, data))
}
