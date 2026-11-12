//! Real HTTP, temporary canonical sources and the installed isolated worker.
#![cfg(unix)]
use ori_contracts::{encode_path_segment, parse_json_value, to_json_vec};
use ori_core::{Core, CoreConfig, HistoryOptions};
use ori_runtime::worker::WorkerSpec;
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, sync::Arc, time::Duration};
use tokio::{net::TcpListener, task::JoinHandle};

struct Server {
    core: Arc<Core>,
    url: String,
    task: JoinHandle<ori_core::Result<()>>,
}
impl Server {
    async fn start(data: &Path, worker: bool) -> Self {
        let mut config = CoreConfig::replay(data);
        if worker {
            config.history = HistoryOptions {
                worker: Some(WorkerSpec {
                    program: std::env::var_os("ORI_RESEARCH_PYTHON")
                        .expect("Set ORI_RESEARCH_PYTHON to the installed isolated research interpreter")
                        .into(),
                    args: vec!["-I".into(), "-m".into(), "ori_research".into()],
                    environment: BTreeMap::new(),
                    deadline_ms: 10_000,
                    require_hard_memory: false,
                    linux_cgroup: None,
                }),
                ..HistoryOptions::default()
            };
        }
        let core = Core::open(config).await.unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let own = core.clone();
        let task = tokio::spawn(async move { own.serve(listener).await });
        Self { core, url, task }
    }
    async fn request(&self, method: &str, path: &str, body: Option<Value>) -> (u16, Value) {
        let mut request =
            reqwest::Client::new().request(method.parse().unwrap(), format!("{}{path}", self.url));
        if let Some(body) = body {
            request = request
                .header("Content-Type", "application/json")
                .body(to_json_vec(&body).unwrap());
        }
        let response = request.send().await.unwrap();
        let status = response.status().as_u16();
        (
            status,
            parse_json_value(&response.bytes().await.unwrap(), 16 * 1024 * 1024).unwrap(),
        )
    }
    async fn job(&self, body: Value) -> (String, Value) {
        let (status, response) = self
            .request("POST", "/api/v1/history/jobs", Some(body))
            .await;
        assert_eq!(status, 202, "{response}");
        let id = response["data"]["id"].as_str().unwrap().to_owned();
        let result = tokio::time::timeout(Duration::from_secs(15), async {
            loop {
                let (status, view) = self
                    .request("GET", &format!("/api/v1/history/jobs/{id}"), None)
                    .await;
                assert_eq!(status, 200);
                match view["data"]["phase"].as_str() {
                    Some("completed") => return view["data"].clone(),
                    Some("failed" | "cancelled") => panic!("{view}"),
                    _ => tokio::time::sleep(Duration::from_millis(10)).await,
                }
            }
        })
        .await
        .unwrap();
        (id, result)
    }
    async fn stop(self) {
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
fn session(tail: &str) -> String {
    format!(
        "/api/v1/sessions/{}{tail}",
        encode_path_segment("fixture-сессия")
    )
}
async fn seed(server: &Server) {
    let f = fixture();
    assert_eq!(
        server
            .request("POST", "/api/v1/sessions", Some(f["session"].clone()))
            .await
            .0,
        201
    );
    for kind in ["sweep", "observation"] {
        assert_eq!(
            server
                .request(
                    "POST",
                    &session(&format!("/{kind}s")),
                    Some(f[kind].clone())
                )
                .await
                .0,
            201
        );
    }
}

#[tokio::test]
async fn missing_history_worker_is_explicit_and_does_not_prevent_cold_readiness() {
    let root = tempfile::tempdir().unwrap();
    let server = Server::start(root.path(), false).await;
    let health = server.request("GET", "/api/v1/health", None).await;
    let jobs = server
        .request(
            "POST",
            "/api/v1/history/jobs",
            Some(json!({"operation":"export"})),
        )
        .await;
    server.stop().await;
    assert_eq!(health.0, 200);
    assert_eq!(jobs.0, 503);
    assert_eq!(jobs.1["error"]["code"], "history_worker_unavailable");
}

#[tokio::test]
async fn installed_history_jobs_stream_fixed_parquet_and_keep_raw_sources_unchanged() {
    let root = tempfile::tempdir().unwrap();
    let server = Server::start(root.path(), true).await;
    seed(&server).await;
    let before = server.request("GET", &session("/sweeps"), None).await.1;
    for operation in ["summary", "occupancy", "export"] {
        let mut request = json!({"operation":operation,"filters":{"session_id":"fixture-сессия"}});
        if operation == "occupancy" {
            request["threshold"] = json!({"value":-50.0,"unit":"dBm"});
        }
        let (id, view) = server.job(request).await;
        assert_eq!(view["source_boundary"], 3);
        let (status, output) = server
            .request("GET", &format!("/api/v1/history/jobs/{id}/result"), None)
            .await;
        assert_eq!(status, 200, "{output}");
        assert_eq!(output["data"]["enforcement"]["hard_memory"], false);
        assert!(
            !to_json_vec(&output)
                .unwrap()
                .windows(root.path().as_os_str().len())
                .any(|w| w == root.path().to_str().unwrap().as_bytes())
        );
        if operation == "export" {
            let response = reqwest::get(format!(
                "{}/api/v1/history/jobs/{id}/exports/sweep_points",
                server.url
            ))
            .await
            .unwrap();
            assert_eq!(response.status(), 200);
            assert_eq!(
                response.headers()["content-type"],
                "application/vnd.apache.parquet"
            );
            let bytes = response.bytes().await.unwrap();
            assert!(bytes.starts_with(b"PAR1") && bytes.ends_with(b"PAR1"));
        }
    }
    let after = server.request("GET", &session("/sweeps"), None).await.1;
    server.stop().await;
    assert_eq!(to_json_vec(&before).unwrap(), to_json_vec(&after).unwrap());
    assert!(!std::fs::read_dir(root.path()).unwrap().any(|p| {
        p.unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".ori-background-")
    }));
}

#[tokio::test]
async fn history_viewport_keeps_original_signed_zero_frequency_and_point_identity() {
    let root = tempfile::tempdir().unwrap();
    let server = Server::start(root.path(), true).await;
    seed(&server).await;
    let path = "/api/v1/history/waterfall?session_id=fixture-%D1%81%D0%B5%D1%81%D1%81%D0%B8%D1%8F&stream_id=spectrum%2Fone&min_frequency_hz=100000001&max_frequency_hz=100000004&columns=4&rows=1";
    let (status, response) = server.request("GET", path, None).await;
    server.stop().await;
    assert_eq!(status, 200, "{response}");
    assert_eq!(response["data"]["derived"], true);
    let row = &response["data"]["rows"][0];
    assert_eq!(row["measurement_id"], "fixture-sweep-α");
    let cell = row["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["point_index"] == 1)
        .unwrap();
    assert_eq!(cell["frequency_hz"], 100000002);
    assert_eq!(
        cell["level"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
}
