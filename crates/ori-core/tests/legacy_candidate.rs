//! Actual localhost HTTP, owned worker cancellation and temporary archives.
use futures_util::StreamExt;
use ori_contracts::{parse_json_value, to_json_vec};
use ori_core::{Core, CoreConfig};
use ori_runtime::worker::WorkerSpec;
use serde_json::{Value, json};
use std::{path::Path, sync::Arc, time::Duration};
use tokio::{net::TcpListener, task::JoinHandle};

struct Server {
    core: Arc<Core>,
    url: String,
    task: Option<JoinHandle<ori_core::Result<()>>>,
}
impl Server {
    async fn start(path: &Path, worker: Option<(&str, &Path)>) -> Self {
        let mut config = CoreConfig::replay(path);
        config.legacy.library = Some(path.to_owned());
        if let Some((mode, marker)) = worker {
            let program = std::env::var_os("ORI_TEST_PYTHON")
                .map(Into::into)
                .unwrap_or_else(|| "/usr/bin/python3".into());
            let spec = WorkerSpec {
                program,
                args: vec![
                    format!("{}/tests/legacy_echo.py", env!("CARGO_MANIFEST_DIR")),
                    mode.into(),
                    marker.to_string_lossy().into_owned(),
                ],
                environment: Default::default(),
                deadline_ms: 30000,
                require_hard_memory: false,
                linux_cgroup: None,
            };
            config.legacy.worker = Some(spec.clone());
            config.analysis_worker = Some(spec);
        }
        let core = Core::open(config).await.unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let owned = core.clone();
        let task = tokio::spawn(async move { owned.serve(listener).await });
        Self {
            core,
            url,
            task: Some(task),
        }
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
    async fn stop(&mut self) {
        self.core.request_shutdown();
        if let Some(task) = self.task.take() {
            tokio::time::timeout(Duration::from_secs(5), task)
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
fn empty() -> Value {
    json!({"version":0,"database":null,"signals":[],"threshold":null,"panoramas":0,"current":null,"max":null,"min":null,"average":null,"waterfall":[],"points":[],"settings":null})
}

#[tokio::test]
async fn legacy_cold_routes_return_unwrapped_state_and_authoritative_validation() {
    let dir = tempfile::tempdir().unwrap();
    let mut server = Server::start(dir.path(), None).await;
    for (route, wanted) in [
        ("/api/state", empty()),
        ("/api/signals", json!({"signals":[]})),
        ("/api/coverage", json!({})),
        ("/api/occupancy", json!({})),
        ("/api/efficiency", json!({})),
        ("/api/measure?start=100&stop=200", json!({})),
    ] {
        let (status, body) = server.request("GET", route, None).await;
        assert_eq!((status, body), (200, wanted), "{route}");
    }
    for (method, route, payload, error) in [
        ("POST", "/api/database", Some(json!({})), "не указан путь"),
        (
            "POST",
            "/api/threshold",
            Some(json!({})),
            "нет активной линии порога",
        ),
        (
            "GET",
            "/api/coverage?rows=0",
            None,
            "Размеры сетки должны быть от 1 до 100",
        ),
        (
            "GET",
            "/api/occupancy?threshold=NaN",
            None,
            "Число должно быть конечным",
        ),
        (
            "GET",
            "/api/measure?start=200&stop=100",
            None,
            "Конец диапазона должен быть больше начала",
        ),
    ] {
        let (status, body) = server.request(method, route, payload).await;
        assert_eq!(
            (status, body),
            (400, json!({"ok":false,"error":error})),
            "{route}"
        );
    }
    let (status, body) = server.request("GET", "/api/keywords", None).await;
    assert_eq!(status, 503);
    assert_eq!(body["ok"], false);
    assert!(body.get("contract").is_none());
    server.stop().await;
    assert!(ori_runtime::lease::WriterLease::acquire(dir.path()).is_ok());
}

#[tokio::test]
async fn legacy_stream_sends_initial_plain_snapshot_and_drops_before_cleanup() {
    let dir = tempfile::tempdir().unwrap();
    let mut server = Server::start(dir.path(), None).await;
    let response = reqwest::Client::new()
        .get(format!("{}/api/stream", server.url))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    assert!(
        response.headers()["content-type"]
            .to_str()
            .unwrap()
            .starts_with("text/event-stream")
    );
    let mut frames = response.bytes_stream();
    let chunk = tokio::time::timeout(Duration::from_secs(2), frames.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let text = std::str::from_utf8(&chunk).unwrap();
    let data = text.strip_prefix("data: ").unwrap().trim();
    assert_eq!(parse_json_value(data.as_bytes(), 65536).unwrap(), empty());
    drop(frames);
    server.stop().await;
}

#[cfg(unix)]
#[tokio::test]
async fn synthetic_keywords_return_complete_worker_envelope_then_use_bounded_cache() {
    let dir = tempfile::tempdir().unwrap();
    let marker = dir.path().join("completed.pid");
    let mut server = Server::start(dir.path(), Some(("ok", &marker))).await;
    let wanted = (200, json!({"keywords": [], "count": 0}));
    assert_eq!(server.request("GET", "/api/keywords", None).await, wanted);
    let original = std::fs::read_to_string(&marker).unwrap();
    assert_eq!(server.request("GET", "/api/keywords", None).await, wanted);
    assert_eq!(std::fs::read_to_string(&marker).unwrap(), original);
    server.stop().await;
}

#[cfg(unix)]
#[tokio::test]
async fn legacy_owned_worker_shares_research_pool_and_is_reaped_after_http_cancellation() {
    let dir = tempfile::tempdir().unwrap();
    let marker = dir.path().join("owned.pid");
    let mut server = Server::start(dir.path(), Some(("wait", &marker))).await;
    let url = format!("{}/api/keywords", server.url);
    let waiter = tokio::spawn(async move { reqwest::get(url).await });
    tokio::time::timeout(Duration::from_secs(5), async {
        while !marker.exists() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let pid = std::fs::read_to_string(&marker)
        .unwrap()
        .parse::<i32>()
        .unwrap();
    waiter.abort();
    let _ = waiter.await;
    let (status, error) = server
        .request(
            "POST",
            "/api/v1/sessions/missing/dvb-evaluation",
            Some(json!({})),
        )
        .await;
    assert_eq!(status, 503, "{error}");
    assert_eq!(error["error"]["code"], "research_busy");
    server.stop().await;
    // Signal0 is only an existence probe of this fixture's own child PID.
    assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::ESRCH)
    );
    assert!(ori_runtime::lease::WriterLease::acquire(dir.path()).is_ok());
}
