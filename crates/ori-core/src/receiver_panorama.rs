//! Read-only projection and original point lookup for sequential AR-DV1 passes.
use crate::{
    Core,
    error::{ApiError, ApiResult},
};
use serde_json::Value;
use std::collections::BTreeMap;

enum Request {
    Panorama { pass: Option<u64>, columns: usize },
    Point { pass: u64, frequency: u64 },
}

impl Request {
    fn parse(path: &[&str], query: &BTreeMap<String, String>) -> ApiResult<Self> {
        let allowed: &[&str] = match path {
            [] => &["pass", "columns", "context_id"],
            ["point"] => &["pass", "frequency_hz", "context_id"],
            _ => return Err(ApiError::missing("not_found")),
        };
        if query.keys().any(|key| !allowed.contains(&key.as_str())) {
            return Err(ApiError::invalid(
                "Unknown receiver panorama query parameter",
            ));
        }
        if let Some(id) = query.get("context_id")
            && (id.len() != 64
                || !id
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)))
        {
            return Err(ApiError::invalid("Invalid receiver panorama context_id"));
        }
        let pass = query
            .get("pass")
            .map(|s| {
                s.parse::<u64>()
                    .ok()
                    .filter(|n| (1..=9_007_199_254_740_991).contains(n))
                    .ok_or_else(|| ApiError::invalid("pass must be a positive integer"))
            })
            .transpose()?;
        if path.is_empty() {
            let columns = query
                .get("columns")
                .map(|s| {
                    s.parse::<usize>()
                        .ok()
                        .filter(|n| (2..=1024).contains(n))
                        .ok_or_else(|| ApiError::invalid("columns must be an integer in 2..1024"))
                })
                .transpose()?
                .unwrap_or(512);
            Ok(Self::Panorama { pass, columns })
        } else {
            let pass = pass.ok_or_else(|| ApiError::invalid("point lookup requires pass"))?;
            let frequency = query
                .get("frequency_hz")
                .and_then(|s| s.parse::<u64>().ok())
                .filter(|n| (100_000..=1_300_000_000).contains(n))
                .ok_or_else(|| {
                    ApiError::invalid("frequency_hz must be an integer in 100000..1300000000")
                })?;
            Ok(Self::Point { pass, frequency })
        }
    }
}

pub(crate) async fn read(
    core: &Core,
    session: &str,
    stream: &str,
    path: &[&str],
    query: &BTreeMap<String, String>,
) -> ApiResult<Value> {
    let request = Request::parse(path, query)?;
    let context_id = query.get("context_id").map(String::as_str);
    crate::http::stream_context(core, session, stream).await?;
    let archive = core.archive()?;
    Ok(match request {
        Request::Panorama { pass, columns } => {
            archive
                .receiver_panorama_in_context(session, stream, pass, columns, context_id)
                .await?
        }
        Request::Point { pass, frequency } => {
            archive
                .receiver_panorama_point_in_context(session, stream, pass, frequency, context_id)
                .await?
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn point_requires_an_explicit_pass_and_exact_integer_frequency() {
        for q in [
            vec![("frequency_hz", "88000000")],
            vec![("pass", "0"), ("frequency_hz", "88000000")],
            vec![("pass", "2"), ("frequency_hz", "88000000.1")],
            vec![("pass", "2"), ("frequency_hz", "99999")],
            vec![("pass", "2"), ("frequency_hz", "1300000001")],
            vec![("pass", "2"), ("frequency_hz", "9007199254740992")],
        ] {
            let query = q
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .collect();
            assert!(Request::parse(&["point"], &query).is_err());
        }
        let query = BTreeMap::from([
            ("pass".into(), "2".into()),
            ("frequency_hz".into(), "88000000".into()),
        ]);
        assert!(matches!(
            Request::parse(&["point"], &query),
            Ok(Request::Point {
                pass: 2,
                frequency: 88_000_000
            })
        ));
    }
    #[test]
    fn panorama_bounds_columns_and_rejects_unknown_parameters() {
        for (key, value) in [
            ("columns", "1"),
            ("columns", "1025"),
            ("limit", "4"),
            ("pass", "-1"),
        ] {
            assert!(Request::parse(&[], &BTreeMap::from([(key.into(), value.into())])).is_err());
        }
        assert!(matches!(
            Request::parse(&[], &BTreeMap::new()),
            Ok(Request::Panorama { pass: None, .. })
        ));
    }

    #[tokio::test]
    async fn receiver_get_routes_return_the_committed_observation_and_exact_gap() {
        use axum::{
            body::{Body, to_bytes},
            extract::{ConnectInfo, State},
            http::Request,
        };
        use serde_json::json;
        let temp = tempfile::tempdir().unwrap();
        let core = Core::open(crate::CoreConfig::replay(temp.path()))
            .await
            .unwrap();
        let archive = core.archive().unwrap();
        archive.create_session(json!({"id":"scope","name":"Synthetic RX","created_at":"2026-11-11T00:00:00Z",
            "streams":[{"id":"rx","device":"Test receiver","role":"receiver","level_unit":"s_meter_count"}]})).await.unwrap();
        let first = json!({"id":"raw-one","session_id":"scope","stream_id":"rx",
            "interval":{"started_at":"2026-11-11T00:00:00Z","ended_at":"2026-11-11T00:00:00Z"},
            "frequency_hz":100000000,"mode":"FM","level_unit":"s_meter_count","level":135,
            "metadata":{"adapter":"ardv1-serial","receiver_profile":"ardv1-monitor-v1","receiver_firmware":"2504A",
                "scan_plan_key":"fixture","scan_strategy_requested":"rf","scope_source":"sequential_receiver_readback",
                "scan_pass":1,"scan_index":0,"scan_total":2,"scan_start_hz":100000000,"scan_stop_hz":100010000,
                "scan_step_hz":10000,"scan_segment_index":0,"settle_s":0.025,"frequency_readback":true,"simulated":false,
                "observed_frequency_hz":100000000,"requested_frequency_hz":100000000,"observed_mode":"FM","requested_mode":"FM",
                "frequency_source":"receiver_RX","mode_source":"receiver_RX","raw_receiver_status":"RX VFA RF0100.00000 ST010.00 MD0F0 LM1350"}
        });
        archive
            .append_observations(vec![first.clone()])
            .await
            .unwrap();
        let root = "/api/v1/sessions/scope/streams/rx/receiver-panorama";
        let get = |tail: &str| {
            let req = Request::builder()
                .uri(format!("{root}{tail}"))
                .header("Host", "localhost")
                .body(Body::empty())
                .unwrap();
            crate::http::handle(
                State(core.clone()),
                ConnectInfo("127.0.0.1:19876".parse().unwrap()),
                req,
            )
        };
        let summary = get("?columns=2").await;
        assert_eq!(summary.status(), 200);
        let read = |response: axum::response::Response| async {
            serde_json::from_slice::<Value>(&to_bytes(response.into_body(), 65536).await.unwrap())
                .unwrap()
        };
        let summary = read(summary).await;
        assert_eq!(summary["data"]["measured_points"], 1);
        assert_eq!(summary["data"]["bins"][0]["observation_id"], "raw-one");
        let original = get("/point?pass=1&frequency_hz=100000000").await;
        assert_eq!(original.status(), 200);
        assert_eq!(read(original).await["data"]["id"], "raw-one");
        assert_eq!(
            get("/point?pass=1&frequency_hz=100010000").await.status(),
            404
        );
        // A changed plan restarts pass numbering at one in the same stream.
        let first_context = ori_archive::verified_receiver_scan_context(&first).unwrap();
        let mut other = first;
        other["metadata"]["scan_plan_key"] = json!("second-plan");
        other["id"] = json!("raw-two");
        let second_context = ori_archive::verified_receiver_scan_context(&other).unwrap();
        archive.append_observations(vec![other]).await.unwrap();
        let old_query = format!("?pass=1&context_id={first_context}");
        let old_pass = get(&old_query).await;
        assert_eq!(old_pass.status(), 200);
        let old_pass = read(old_pass).await;
        assert_eq!(old_pass["data"]["context_id"], first_context);
        assert_eq!(old_pass["data"]["bins"][0]["observation_id"], "raw-one");
        let point = read(get(&format!("/point{old_query}&frequency_hz=100000000")).await).await;
        assert_eq!(point["data"]["id"], "raw-one");
        assert_eq!(
            read(get("?pass=1").await).await["data"]["context_id"],
            second_context
        );
        assert_eq!(get("?pass=2&context_id=bad").await.status(), 400);
        assert_eq!(
            get(&format!("?pass=2&context_id={first_context}"))
                .await
                .status(),
            404
        );
        assert_eq!(
            get(&format!("?context_id={}", "0".repeat(64)))
                .await
                .status(),
            404
        );
        core.close().await.unwrap();
    }
}
