#![cfg(unix)]
#[path = "support/bounded_http.rs"]
mod bounded_http;
use ori_archive::{Archive, ArchiveConfig};
use ori_contracts::{encode_path_segment, parse_json_value, to_json_vec};
use ori_core::{Core, CoreConfig};
use ori_runtime::worker::WorkerSpec;
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, sync::Arc, time::Duration};
use tokio::{net::TcpListener, task::JoinHandle};
struct Server {
    core: Arc<Core>,
    url: String,
    task: Option<JoinHandle<ori_core::Result<()>>>,
}
impl Server {
    async fn start(data: &Path, mode: &str, marker: Option<&Path>) -> Self {
        let mut config = CoreConfig::replay(data);
        let mut args = vec![
            format!("{}/tests/analysis_echo.py", env!("CARGO_MANIFEST_DIR")),
            mode.into(),
        ];
        if let Some(marker) = marker {
            args.push(marker.to_str().unwrap().into());
        }
        config.analysis_worker = Some(WorkerSpec {
            program: std::env::var_os("ORI_TEST_PYTHON")
                .map(Into::into)
                .unwrap_or_else(|| "/usr/bin/python3".into()),
            args,
            environment: BTreeMap::new(),
            deadline_ms: 5000,
            require_hard_memory: false,
            linux_cgroup: None,
        });
        Self::with_config(config).await
    }
    async fn with_config(config: CoreConfig) -> Self {
        let core = Core::open(config).await.unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let serving = core.clone();
        let task = tokio::spawn(async move { serving.serve(listener).await });
        Self {
            core,
            url,
            task: Some(task),
        }
    }
    async fn request(&self, method: &str, path: &str, payload: Option<&Value>) -> (u16, Value) {
        let mut request =
            reqwest::Client::new().request(method.parse().unwrap(), format!("{}{path}", self.url));
        if let Some(payload) = payload {
            request = request.body(to_json_vec(payload).unwrap());
        }
        let response = request.send().await.unwrap();
        let status = response.status().as_u16();
        let bytes = response.bytes().await.unwrap();
        (status, parse_json_value(&bytes, 16 * 1024 * 1024).unwrap())
    }
    async fn reject_declared_length(&self, path: &str, length: u64) -> (u16, Value) {
        use tokio::io::AsyncWriteExt;
        tokio::time::timeout(Duration::from_secs(5), async {
            let authority = self.url.strip_prefix("http://").unwrap();
            let mut stream = tokio::net::TcpStream::connect(authority).await.unwrap();
            stream.write_all(format!("POST {path} HTTP/1.1\r\nHost: {authority}\r\nContent-Type: application/json\r\nContent-Length: {length}\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
            // The server rejects the declared length before reading a body.
            // Sending a huge body with reqwest can lose this valid response to
            // BrokenPipe while Hyper is still uploading on Darwin.
            let response = bounded_http::read_closed_response(&mut stream).await.unwrap();
            let split = response.windows(4).position(|v| v == b"\r\n\r\n").unwrap();
            let status = std::str::from_utf8(&response[..split]).unwrap().split_whitespace().nth(1).unwrap().parse().unwrap();
            (status, parse_json_value(&response[split+4..],65536).unwrap())
        }).await.expect("oversized header must be rejected before any body read")
    }
    async fn stop(&mut self) {
        self.core.request_shutdown();
        if let Some(task) = self.task.take() {
            tokio::time::timeout(Duration::from_secs(3), task)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
        }
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.core.request_shutdown();
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}
fn fixture() -> Value {
    parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        65536,
    )
    .unwrap()
}
fn route(tail: &str) -> String {
    format!(
        "/api/v1/sessions/{}{tail}",
        encode_path_segment("fixture-сессия")
    )
}
async fn seed(data: &Path) {
    let archive = Archive::open(ArchiveConfig::new(data.join("sessions.sqlite3"))).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T00:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    let mut sweeps = Vec::new();
    for number in 0..72 {
        let mut sweep = fixture()["sweep"].clone();
        sweep["id"] = json!(format!("exact-source-{number:03}"));
        sweep["interval"] = json!({"started_at":format!("2026-11-11T12:00:{:02}.123456Z",number%60),"ended_at":format!("2026-11-11T12:00:{:02}.223456Z",number%60)});
        sweep["settings"]["opaque"] = parse_json_value(
            br#"{"literal":{"$serde_json::private::Number":"123"},"big":18446744073709551616}"#,
            4096,
        )
        .unwrap();
        sweeps.push(sweep);
    }
    archive.append_sweeps(sweeps).await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn scientific_http_selects_only_exact_immutable_ids_and_preserves_raw_ipc_metadata() {
    let directory = tempfile::tempdir().unwrap();
    seed(directory.path()).await;
    let mut server = Server::start(directory.path(), "echo", None).await;
    let path = route("/sweep-comparison?reference_id=exact-source-000&target_id=exact-source-071");
    let (status, result) = server.request("GET", &path, None).await;
    assert_eq!(status, 200, "{result}");
    let data = &result["data"];
    assert_eq!(data["method"], "compare_sweeps");
    assert_eq!(data["sweeps"].as_array().unwrap().len(), 2);
    assert_eq!(data["sweeps"][0]["id"], "exact-source-000");
    assert_eq!(data["sweeps"][1]["id"], "exact-source-071");
    assert_eq!(
        data["sweeps"][0]["levels"][1].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        data["sweeps"][0]["settings"]["opaque"]["big"].to_string(),
        "18446744073709551616"
    );
    assert_eq!(
        data["sweeps"][0]["settings"]["opaque"]["literal"]["$serde_json::private::Number"],
        "123"
    );
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=100", None)
        .await;
    assert_eq!(events["data"].as_array().unwrap().len(), 73);
    server.stop().await;
}

#[tokio::test]
async fn candidate_openapi_declares_scientific_routes_and_their_actual_body_budgets() {
    let directory = tempfile::tempdir().unwrap();
    seed(directory.path()).await;
    let mut server = Server::start(directory.path(), "echo", None).await;
    let (_, spec) = server.request("GET", "/api/v1/openapi.json", None).await;
    for (operation, verb, budget) in [
        ("sweep-comparison", "get", 0),
        ("sweep-threshold", "get", 0),
        ("sweep-context", "post", 8192),
        ("sweep-mask-analysis", "post", 524288),
        ("measurement-map", "post", 262144),
        ("field-strength-rms-v2", "post", 65536),
    ] {
        let path = format!("/api/v1/sessions/{{id}}/{operation}");
        let entry = &spec["paths"][&path][verb];
        assert!(entry.is_object(), "missing {verb} {path}");
        if budget > 0 {
            assert_eq!(entry["x-ori-max-request-bytes"], budget);
        }
    }
    server.stop().await;
}

#[tokio::test]
async fn calibrated_field_route_selects_one_source_and_keeps_the_control_body_limit() {
    let directory = tempfile::tempdir().unwrap();
    seed(directory.path()).await;
    let mut server = Server::start(directory.path(), "echo", None).await;
    let before = server
        .request("GET", "/api/v1/events/history?after=0&limit=100", None)
        .await;
    let payload = json!({"sweep_id":"exact-source-001","frequency_hz":100000001,"calibration":{}});
    let (status, response) = server
        .request("POST", &route("/field-strength-rms-v2"), Some(&payload))
        .await;
    let (_, capabilities) = server.request("GET", "/api/v1/capabilities", None).await;
    let mut oversized = payload.clone();
    oversized["calibration"]["description"] = Value::String("x".repeat(65536));
    let (large_status, _) = server
        .reject_declared_length(
            &route("/field-strength-rms-v2"),
            to_json_vec(&oversized).unwrap().len() as u64,
        )
        .await;
    let after = server
        .request("GET", "/api/v1/events/history?after=0&limit=100", None)
        .await;
    server.stop().await;
    assert_eq!(status, 200, "{response}");
    assert_eq!(response["data"]["method"], "field_strength_rms_v2");
    assert_eq!(response["data"]["sweeps"].as_array().unwrap().len(), 1);
    assert_eq!(response["data"]["sweeps"][0]["id"], "exact-source-001");
    assert_eq!(response["data"]["payload"], payload);
    assert_eq!(
        capabilities["data"]["features"]["field_strength_rms_v2"],
        true
    );
    assert_eq!(large_status, 413);
    assert_eq!(after, before);
}

#[tokio::test]
async fn analysis_http_retains_structured_nonzero_worker_errors_and_refuses_wrong_operation() {
    let directory = tempfile::tempdir().unwrap();
    seed(directory.path()).await;
    let mut server = Server::start(directory.path(), "error", None).await;
    let (status, error) = server
        .request(
            "POST",
            &route("/sweep-quality"),
            Some(&json!({"sweep_id":"exact-source-001","edge_margin_db":10,"high_level_dbm":-20})),
        )
        .await;
    assert_eq!(status, 400);
    assert_eq!(error["error"]["code"], "invalid_request");
    assert_eq!(error["error"]["message"], "Synthetic exact error 😀");
    server.stop().await;
    let mut server = Server::start(directory.path(), "bad-operation", None).await;
    let (status, error) = server
        .request(
            "POST",
            &route("/sweep-quality"),
            Some(&json!({"sweep_id":"exact-source-001","edge_margin_db":10,"high_level_dbm":-20})),
        )
        .await;
    assert_eq!(status, 502);
    assert_eq!(error["error"]["code"], "research_protocol_error");
    server.stop().await;
}

#[tokio::test]
async fn source_selection_and_child_execution_share_one_admission_and_shutdown_reaps_the_owned_child()
 {
    let directory = tempfile::tempdir().unwrap();
    seed(directory.path()).await;
    let marker = directory.path().join("owned-child.pid");
    let mut server = Server::start(directory.path(), "slow", Some(&marker)).await;
    let url = format!(
        "{}{}",
        server.url,
        route("/sweep-comparison?reference_id=exact-source-000&target_id=exact-source-071")
    );
    let request = tokio::spawn(async move { reqwest::get(url).await.unwrap().status() });
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !marker.exists() {
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let (status, value) = server
        .request(
            "GET",
            &route("/sweep-comparison?reference_id=exact-source-001&target_id=exact-source-002"),
            None,
        )
        .await;
    assert_eq!(status, 503);
    assert_eq!(value["error"]["code"], "research_busy");
    assert_eq!(server.request("GET", "/api/v1/health", None).await.0, 200);
    let pid = std::fs::read_to_string(&marker).unwrap();
    assert!(pid.parse::<u32>().is_ok_and(|id| id > 0));
    server.stop().await;
    assert_eq!(request.await.unwrap(), 503);
    #[cfg(target_os = "linux")]
    assert!(
        !Path::new(&format!("/proc/{pid}")).exists(),
        "owned worker survived shutdown"
    );
}

#[tokio::test]
async fn analysis_body_limits_match_the_existing_per_method_contract_and_keep_artifacts_as_strings()
{
    let directory = tempfile::tempdir().unwrap();
    seed(directory.path()).await;
    let mut server = Server::start(directory.path(), "echo", None).await;
    let artifact = "x".repeat(100000);
    let (status, value) = server
        .request(
            "POST",
            &route("/sweep-mask-analysis"),
            Some(&json!({"sweep_id":"exact-source-000","mask_json":artifact})),
        )
        .await;
    assert_eq!(status, 200, "{value}");
    assert_eq!(value["data"]["payload"]["mask_json"], artifact);
    let before = server
        .request("GET", "/api/v1/events/history?after=0&limit=100", None)
        .await;
    assert_eq!(
        server
            .reject_declared_length(&route("/sweep-mask-analysis"), u64::MAX)
            .await
            .0,
        413
    );
    let payload = json!({"sweep_id":"exact-source-000","mask_json":"x".repeat(524288)});
    assert_eq!(
        server
            .reject_declared_length(
                &route("/sweep-mask-analysis"),
                to_json_vec(&payload).unwrap().len() as u64
            )
            .await
            .0,
        413
    );
    let payload = json!({"sweep_id":"exact-source-000","padding":"x".repeat(8192)});
    assert_eq!(
        server
            .reject_declared_length(
                &route("/sweep-quality"),
                to_json_vec(&payload).unwrap().len() as u64
            )
            .await
            .0,
        413
    );
    assert_eq!(
        server
            .request(
                "POST",
                &route("/sweep-quality?unexpected=1"),
                Some(&json!({}))
            )
            .await
            .0,
        400
    );
    let after = server
        .request("GET", "/api/v1/events/history?after=0&limit=100", None)
        .await;
    server.stop().await;
    assert_eq!(
        before, after,
        "rejected headers must not publish archive/state changes"
    );
}

#[tokio::test]
async fn scientific_get_queries_reject_malformed_percent_encoding_utf8_and_duplicate_fields() {
    let directory = tempfile::tempdir().unwrap();
    seed(directory.path()).await;
    let mut server = Server::start(directory.path(), "echo", None).await;
    for suffix in [
        "?reference_id=%GG&target_id=exact-source-001",
        "?reference_id=%FF&target_id=exact-source-001",
        "?reference_id=exact-source-000&target_id=exact-source-001&target_id=exact-source-002",
    ] {
        let (status, value) = server
            .request("GET", &route(&format!("/sweep-comparison{suffix}")), None)
            .await;
        assert_eq!(status, 400, "{suffix}: {value}");
    }
    server.stop().await;
}

#[tokio::test]
async fn configured_scientific_http_does_not_report_research_http_as_unsupported() {
    let directory = tempfile::tempdir().unwrap();
    seed(directory.path()).await;
    let mut server = Server::start(directory.path(), "echo", None).await;
    let path = route("/sweep-comparison?reference_id=exact-source-000&target_id=exact-source-071");
    let (result_status, result) = server.request("GET", &path, None).await;
    let (cap_status, cap) = server.request("GET", "/api/v1/capabilities", None).await;
    server.stop().await;
    assert_eq!(result_status, 200, "{result}");
    assert_eq!(result["data"]["method"], "compare_sweeps");
    assert_eq!(cap_status, 200);
    assert_eq!(cap["data"]["features"]["sweep_comparison"], true);
    assert!(
        !cap["data"]["candidate"]["unsupported_features"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item == "research_http"),
        "a successful configured HTTP operation is still reported as unsupported"
    );
}

#[tokio::test]
async fn absent_scientific_worker_stays_unavailable_and_retains_research_http_diagnostic() {
    let directory = tempfile::tempdir().unwrap();
    seed(directory.path()).await;
    let mut server = Server::with_config(CoreConfig::replay(directory.path())).await;
    let (result_status, result) = server
        .request(
            "POST",
            &route("/sweep-quality"),
            Some(&json!({"sweep_id":"exact-source-001","edge_margin_db":10,"high_level_dbm":-20})),
        )
        .await;
    let (cap_status, cap) = server.request("GET", "/api/v1/capabilities", None).await;
    server.stop().await;
    assert_eq!(result_status, 503, "{result}");
    assert_eq!(result["error"]["code"], "research_unavailable");
    assert_eq!(cap_status, 200);
    assert_eq!(cap["data"]["features"]["sweep_comparison"], false);
    assert_eq!(cap["data"]["features"]["field_strength_rms_v2"], false);
    assert!(
        cap["data"]["candidate"]["unsupported_features"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item == "research_http")
    );
}
