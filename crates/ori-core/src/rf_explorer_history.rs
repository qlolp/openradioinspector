//! Read-only views of committed survey windows. Capture and display have independent lifetimes.
use crate::{
    Core,
    error::{ApiError, ApiResult},
};
use axum::http::StatusCode;
use ori_archive::ArchiveError;
use serde_json::Value;
use std::collections::BTreeMap;

type Query = BTreeMap<String, String>;

fn only(query: &Query, allowed: &[&str]) -> ApiResult<()> {
    if query.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err(ApiError::invalid(
            "Unknown RF Explorer survey query parameter",
        ));
    }
    Ok(())
}

fn number(query: &Query, key: &str, default: usize, min: usize, max: usize) -> ApiResult<usize> {
    match query.get(key) {
        None => Ok(default),
        Some(raw) => raw
            .parse::<usize>()
            .ok()
            .filter(|n| (min..=max).contains(n))
            .ok_or_else(|| ApiError::invalid(format!("{key} must be in {min}..{max}"))),
    }
}

fn survey_error(error: ArchiveError) -> ApiError {
    if matches!(error, ArchiveError::Conflict) {
        ApiError::new(
            StatusCode::CONFLICT,
            "incompatible_survey_windows",
            "The archived windows have incompatible survey readback or acquisition order",
        )
    } else {
        error.into()
    }
}

fn hold_since(query: &Query) -> ApiResult<Option<String>> {
    query
        .get("hold_since")
        .map(|raw| {
            ori_contracts::normalize_utc(raw)
                .map_err(|_| ApiError::invalid("hold_since requires a UTC timestamp"))
        })
        .transpose()
}

pub(crate) async fn read(
    core: &Core,
    id: &str,
    stream: &str,
    path: &[&str],
    query: &Query,
) -> ApiResult<Value> {
    super::http::stream_context(core, id, stream).await?;
    let archive = core.archive()?;
    let value = match path {
        [] => {
            only(query, &["limit", "before"])?;
            let limit = number(query, "limit", 20, 1, 100)?;
            let before = query.get("before").cloned();
            if before
                .as_ref()
                .is_some_and(|s| s.is_empty() || s.len() > 2048)
            {
                return Err(ApiError::invalid("Invalid pass list cursor"));
            }
            archive.rf_explorer_passes(id, stream, limit, before).await
        }
        [pass] => {
            only(query, &["columns", "hold_since"])?;
            let columns = number(query, "columns", 512, 1, 2048)?;
            archive
                .rf_explorer_pass(id, stream, pass, columns, hold_since(query)?)
                .await
        }
        [pass, "point"] => {
            only(query, &["frequency_hz", "hold_since"])?;
            let frequency = query
                .get("frequency_hz")
                .and_then(|raw| raw.parse::<u64>().ok())
                .filter(|&value| value <= 9_007_199_254_740_991)
                .ok_or_else(|| {
                    ApiError::invalid(
                        "frequency_hz requires a nonnegative integer in the supported range",
                    )
                })?;
            archive
                .rf_explorer_point(id, stream, pass, frequency, hold_since(query)?)
                .await
        }
        [pass, "windows"] => {
            only(query, &["after_tile", "limit"])?;
            let after = number(query, "after_tile", 0, 0, 2398)?;
            let limit = number(query, "limit", 100, 1, 256)?;
            archive
                .rf_explorer_pass_windows(id, stream, pass, after, limit)
                .await
        }
        _ => return Err(ApiError::missing("not_found")),
    };
    value.map_err(survey_error)
}
