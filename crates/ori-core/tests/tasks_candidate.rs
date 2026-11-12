//! Actual HTTP with an owned TaskBook; default callbacks cannot touch hardware.
use ori_contracts::{encode_path_segment, parse_json_value, to_json_vec};
use ori_core::{Core, CoreConfig};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use tokio::{net::TcpListener, task::JoinHandle};
struct Server {
    core: Arc<Core>,
    url: String,
    task: Option<JoinHandle<ori_core::Result<()>>>,
}
impl Server {
    async fn start(config: CoreConfig) -> Self {
        let core = Core::open(config).await.unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let own = core.clone();
        let task = tokio::spawn(async move { own.serve(listener).await });
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
            request = request
                .header("Content-Type", "application/json")
                .body(to_json_vec(payload).unwrap());
        }
        let response = request.send().await.unwrap();
        let status = response.status().as_u16();
        (
            status,
            parse_json_value(&response.bytes().await.unwrap(), 16 * 1024 * 1024).unwrap(),
        )
    }
    async fn stop(mut self) {
        self.core.request_shutdown();
        tokio::time::timeout(Duration::from_secs(4), self.task.take().unwrap())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
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
#[tokio::test]
async fn task_http_is_runtime_opt_in_private_path_free_and_does_not_create_measurements() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(CoreConfig::replay(directory.path())).await;
    let (_, cap) = server.request("GET", "/api/v1/capabilities", None).await;
    let (status, initial) = server.request("GET", "/api/v1/task-scheduler", None).await;
    assert_eq!(status, 200, "{initial}");
    assert_eq!(initial["data"]["enabled"], false);
    let file = directory.path().join("synthetic.task");
    std::fs::write(&file, b"not interpreted by NoopTaskEffects").unwrap();
    let name = "planned/µ-e\u{301}";
    let payload = json!({"name":name,"start_at":(chrono::Utc::now()+chrono::Duration::hours(1)).to_rfc3339_opts(chrono::SecondsFormat::Micros,true),"duration_s":1,"task_file":file});
    let (status, created) = server
        .request("POST", "/api/v1/task-scheduler/tasks", Some(&payload))
        .await;
    assert_eq!(status, 201, "{created}");
    assert_eq!(created["data"]["name"], name);
    assert!(created["data"].get("task_file").is_none());
    assert!(created["data"].get("panorama_file").is_none());
    let (_, tasks) = server
        .request("GET", "/api/v1/task-scheduler/tasks", None)
        .await;
    assert_eq!(tasks["data"]["count"], 1);
    assert_eq!(tasks["data"]["items"][0], created["data"]);
    let (status, invalid) = server
        .request(
            "POST",
            "/api/v1/task-scheduler",
            Some(&json!({"enabled":true,"extra":false})),
        )
        .await;
    assert_eq!(status, 400, "{invalid}");
    let (enable_status, enabled) = server
        .request(
            "POST",
            "/api/v1/task-scheduler",
            Some(&json!({"enabled":true})),
        )
        .await;
    let (_, after_enable) = server.request("GET", "/api/v1/task-scheduler", None).await;
    let (status, cancelled) = server
        .request(
            "POST",
            &format!(
                "/api/v1/task-scheduler/tasks/{}/cancel",
                encode_path_segment(name)
            ),
            Some(&json!({})),
        )
        .await;
    assert_eq!(status, 200, "{cancelled}");
    let (_, events) = server
        .request("GET", "/api/v1/events/history?limit=100", None)
        .await;
    assert_eq!(
        events["data"],
        json!([]),
        "software task lifecycle invented raw measurements/events"
    );
    server.stop().await;
    assert_eq!(cap["data"]["features"]["control_task_planning"], true);
    assert_eq!(cap["data"]["features"]["control_task_scheduler"], true);
    assert_eq!(cap["data"]["features"]["control_task_execution"], false);
    assert_eq!(enable_status, 200, "{enabled}");
    assert_eq!(enabled["data"]["enabled"], true);
    assert_eq!(after_enable["data"]["enabled"], true);
    assert_eq!(after_enable["data"]["execution_available"], false);
    let server = Server::start(CoreConfig::replay(directory.path())).await;
    let (_, tasks) = server
        .request("GET", "/api/v1/task-scheduler/tasks", None)
        .await;
    assert_eq!(
        tasks["data"]["count"], 0,
        "default ephemeral task book unexpectedly persisted"
    );
    server.stop().await;
}
#[tokio::test]
async fn explicit_taskbook_survives_core_restart_but_scheduler_remains_disabled() {
    let directory = tempfile::tempdir().unwrap();
    let book = directory.path().join("operator-tasks.json");
    let mut config = CoreConfig::replay(directory.path());
    config.task_book = Some(book.clone());
    let server = Server::start(config).await;
    let input = directory.path().join("persisted.task");
    std::fs::write(&input, b"owned unused input").unwrap();
    let payload = json!({"name":"persisted","start_at":(chrono::Utc::now()+chrono::Duration::hours(1)).to_rfc3339_opts(chrono::SecondsFormat::Micros,true),"duration_s":1,"task_file":input});
    let (status, created) = server
        .request("POST", "/api/v1/task-scheduler/tasks", Some(&payload))
        .await;
    assert_eq!(status, 201, "{created}");
    let (status, disabled) = server
        .request(
            "POST",
            "/api/v1/task-scheduler",
            Some(&json!({"enabled":false})),
        )
        .await;
    assert_eq!(status, 200, "{disabled}");
    server.stop().await;
    let _lease = ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap();
    drop(_lease);
    let mut config = CoreConfig::replay(directory.path());
    config.task_book = Some(book.clone());
    let server = Server::start(config).await;
    let (_, status) = server.request("GET", "/api/v1/task-scheduler", None).await;
    assert_eq!(status["data"]["enabled"], false);
    let (_, tasks) = server
        .request("GET", "/api/v1/task-scheduler/tasks", None)
        .await;
    assert_eq!(tasks["data"]["count"], 1);
    assert_eq!(tasks["data"]["items"][0]["name"], "persisted");
    server.stop().await;
}
