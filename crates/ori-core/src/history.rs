//! Opaque summary cursors compatible with the Python filter/snapshot contract.
use crate::{
    Core,
    error::{ApiError, ApiResult},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ori_archive::{MeasurementCursor, SweepQuery};
use ori_contracts::{normalize_utc, parse_json_value, to_json_vec};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
pub(crate) async fn summaries(
    core: &Core,
    session: &str,
    query: &BTreeMap<String, String>,
) -> ApiResult<Value> {
    core.session(session).await?;
    if query.keys().any(|key| {
        ![
            "stream_id",
            "limit",
            "after",
            "start",
            "end",
            "min_hz",
            "max_hz",
        ]
        .contains(&key.as_str())
    }) {
        return Err(ApiError::invalid(
            "Unknown or repeated summary query parameter",
        ));
    }
    let mut filter = SweepQuery::session(session);
    filter.limit = query
        .get("limit")
        .map(|v| v.parse::<usize>().ok())
        .unwrap_or(Some(100))
        .filter(|v| (1..=100).contains(v))
        .ok_or_else(|| ApiError::invalid("Summary limit must be 1..100"))?;
    filter.stream_id = query.get("stream_id").cloned();
    let time = |name: &str| {
        query
            .get(name)
            .map(|v| normalize_utc(v).map_err(|_| ApiError::invalid("Invalid summary timestamp")))
            .transpose()
    };
    filter.start = time("start")?;
    filter.end = time("end")?;
    if matches!((&filter.start,&filter.end),(Some(a),Some(b))if b<=a) {
        return Err(ApiError::invalid("Summary end must follow start"));
    }
    let hz = |name: &str| {
        query
            .get(name)
            .map(|v| {
                v.parse::<i64>().ok().filter(|v| *v >= 0).ok_or_else(|| {
                    ApiError::invalid("Summary frequencies must be nonnegative integer Hz")
                })
            })
            .transpose()
    };
    filter.min_hz = hz("min_hz")?;
    filter.max_hz = hz("max_hz")?;
    if matches!((filter.min_hz,filter.max_hz),(Some(a),Some(b))if b<a) {
        return Err(ApiError::invalid("Summary frequency interval is reversed"));
    }
    let filters = BTreeMap::from([
        ("session_id", json!(session)),
        ("stream_id", json!(filter.stream_id)),
        ("start", json!(filter.start)),
        ("end", json!(filter.end)),
        ("min_hz", json!(filter.min_hz)),
        ("max_hz", json!(filter.max_hz)),
    ]);
    let digest = Sha256::digest(
        to_json_vec(&filters).map_err(|_| ApiError::invalid("Summary filter encoding"))?,
    )
    .iter()
    .map(|byte| format!("{byte:02x}"))
    .collect::<String>();
    let current = core.archive()?.sweep_ingestion_boundary(session).await?;
    let mut boundary = current;
    if let Some(after) = query.get("after") {
        let invalid = || ApiError::invalid("Invalid summary cursor or changed filters");
        if after.is_empty() || after.len() > 16384 {
            return Err(invalid());
        }
        let raw = URL_SAFE_NO_PAD
            .decode(after.trim_end_matches('='))
            .map_err(|_| invalid())?;
        let cursor = parse_json_value(&raw, 16384).map_err(|_| invalid())?;
        if cursor.as_object().is_none_or(|v| {
            v.len() != 5
                || !["v", "filter", "boundary", "time", "id"]
                    .iter()
                    .all(|key| v.contains_key(*key))
        }) || cursor["v"].as_u64() != Some(1)
            || cursor["filter"] != digest
        {
            return Err(invalid());
        }
        boundary = cursor["boundary"].as_u64().ok_or_else(invalid)?;
        let stamp = cursor["time"].as_str().ok_or_else(invalid)?;
        let id = cursor["id"]
            .as_str()
            .filter(|id| !id.is_empty())
            .ok_or_else(invalid)?;
        if normalize_utc(stamp).map_err(|_| invalid())? != stamp {
            return Err(invalid());
        }
        filter.after = Some(MeasurementCursor {
            started_at: stamp.into(),
            id: id.into(),
        });
    }
    if boundary > current {
        return Err(ApiError::invalid(
            "Summary cursor belongs to a different archive state",
        ));
    }
    let page = core.archive()?.sweep_summaries(filter, boundary).await?;
    let next=page.next_cursor.map(|cursor|to_json_vec(&json!({"v":1,"filter":digest,"boundary":boundary,"time":cursor.started_at,"id":cursor.id})).map(|raw|URL_SAFE_NO_PAD.encode(raw))).transpose().map_err(|_|ApiError::invalid("Summary cursor encoding"))?;
    Ok(json!({"items":page.items,"next_cursor":next}))
}
