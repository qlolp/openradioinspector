//! Frozen live raw exports without a whole-database disposable copy.
use ori_contracts::{encode_path_segment, parse_json_value, to_json_vec};
use ori_core::{Core, CoreConfig};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use tokio::{net::TcpListener, task::JoinHandle};
struct Server {
    core: Arc<Core>,
    url: String,
    task: JoinHandle<ori_core::Result<()>>,
}
impl Server {
    async fn start(path: &std::path::Path) -> Self {
        let core = Core::open(CoreConfig::replay(path)).await.unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let own = core.clone();
        let task = tokio::spawn(async move { own.serve(listener).await });
        Self { core, url, task }
    }
    async fn post(&self, path: &str, value: &Value) {
        let result = reqwest::Client::new()
            .post(format!("{}{path}", self.url))
            .header("Content-Type", "application/json")
            .body(to_json_vec(value).unwrap())
            .send()
            .await
            .unwrap();
        assert_eq!(result.status(), 201, "{}", result.text().await.unwrap());
    }
    async fn export(&self, tail: &str) -> reqwest::Response {
        reqwest::get(format!("{}{tail}", self.url)).await.unwrap()
    }
    async fn close(self) {
        self.core.request_shutdown();
        tokio::time::timeout(Duration::from_secs(5), self.task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
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

#[tokio::test]
async fn live_ndjson_freezes_counts_and_exact_raw_records_while_commits_continue() {
    let data = tempfile::tempdir().unwrap();
    let server = Server::start(data.path()).await;
    let fixture = fixture();
    server.post("/api/v1/sessions", &fixture["session"]).await;
    let mut sweeps = Vec::new();
    for id in ["sweep-é", "sweep-e\u{301}"] {
        let mut sweep = fixture["sweep"].clone();
        sweep["id"] = json!(id);
        sweep["settings"]["literal"] = parse_json_value(
            br#"{"$serde_json::private::Number":"1","big":18446744073709551616}"#,
            4096,
        )
        .unwrap();
        server.post(&route("/sweeps"), &sweep).await;
        sweeps.push(sweep);
    }
    server
        .post(&route("/observations"), &fixture["observation"])
        .await;
    let response = server.export(&route("/archive/export.ndjson")).await;
    let status = response.status();
    let headers = response.headers().clone();
    let mut late = fixture["sweep"].clone();
    late["id"] = json!("late");
    server.post(&route("/sweeps"), &late).await;
    let bytes = response.bytes().await.unwrap();
    server.close().await;
    assert_eq!(status, 200, "{}", String::from_utf8_lossy(&bytes));
    assert!(
        headers["content-type"]
            .to_str()
            .unwrap()
            .starts_with("application/x-ndjson")
    );
    assert_eq!(headers["x-archive-sweeps"], "2");
    assert_eq!(headers["x-archive-observations"], "1");
    let records = bytes
        .split(|b| *b == b'\n')
        .filter(|s| !s.is_empty())
        .map(|s| parse_json_value(s, 16 * 1024 * 1024).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(records.len(), 4);
    assert_eq!(
        records[0],
        json!({"format":"openradioinspector-archive-ndjson","format_version":1,"session_id":"fixture-сессия"})
    );
    assert_eq!(records[1]["kind"], "sweep");
    assert_eq!(records[1]["measurement"]["id"], "sweep-e\u{301}");
    assert_eq!(records[2]["measurement"]["id"], "sweep-é");
    assert_eq!(records[3]["kind"], "observation");
    assert_eq!(records[3]["measurement"], fixture["observation"]);
    assert_eq!(
        records[1]["measurement"]["levels"][1]
            .as_f64()
            .unwrap()
            .to_bits(),
        (-0.0_f64).to_bits()
    );
    let mut canonical = sweeps[1].clone();
    canonical["interval"] = json!({"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.223456Z"});
    assert_eq!(records[1]["measurement"], canonical);
    let bits = |value: &Value| {
        value["levels"]
            .as_array()
            .unwrap()
            .iter()
            .map(|level| level.as_f64().unwrap().to_bits())
            .collect::<Vec<_>>()
    };
    assert_eq!(bits(&records[1]["measurement"]), bits(&canonical));
}

#[tokio::test]
async fn live_json_export_uses_the_same_frozen_raw_document_and_stream_filter() {
    let data = tempfile::tempdir().unwrap();
    let server = Server::start(data.path()).await;
    let fixture = fixture();
    server.post("/api/v1/sessions", &fixture["session"]).await;
    server.post(&route("/sweeps"), &fixture["sweep"]).await;
    server
        .post(&route("/observations"), &fixture["observation"])
        .await;
    let response = server
        .export(&route("/archive/export?stream_id=receiver%2Bone"))
        .await;
    let status = response.status();
    let bytes = response.bytes().await.unwrap();
    server.close().await;
    assert_eq!(status, 200, "{}", String::from_utf8_lossy(&bytes));
    let body = parse_json_value(&bytes, 16 * 1024 * 1024).unwrap();
    assert_eq!(body["contract"], "openradioinspector-platform");
    assert_eq!(
        body["data"],
        json!({"format":"openradioinspector-archive","format_version":1,"sweeps":[],"observations":[fixture["observation"]]})
    );
    assert!(!std::fs::read_dir(data.path()).unwrap().any(|p| {
        p.unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".ori-background-")
    }));
}

#[tokio::test]
async fn raw_export_stream_validation_preserves_the_existing_public_error() {
    let data = tempfile::tempdir().unwrap();
    let server = Server::start(data.path()).await;
    server.post("/api/v1/sessions", &fixture()["session"]).await;
    let mut results = Vec::new();
    for format in ["export", "export.ndjson"] {
        let response = server
            .export(&route(&format!(
                "/archive/{format}?stream_id=missing-%C3%A9"
            )))
            .await;
        let status = response.status();
        let value = parse_json_value(&response.bytes().await.unwrap(), 65536).unwrap();
        results.push((status, value));
    }
    server.close().await;
    for (status, value) in results {
        assert_eq!(status, 400, "{value}");
        assert_eq!(value["error"]["code"], "invalid_request");
        assert_eq!(
            value["error"]["message"],
            "Поток не принадлежит сеансу: missing-é"
        );
    }
}
