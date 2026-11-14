//! Owned localhost / duplex fixtures only. No physical device is opened.
use ori_contracts::{parse_json_value, to_json_vec};
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
    async fn start(config: CoreConfig) -> Self {
        let core = Core::open(config).await.unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let owned = core.clone();
        let task = tokio::spawn(async move { owned.serve(listener).await });
        Self { core, url, task }
    }
    async fn request(&self, method: &str, path: &str, body: Option<&Value>) -> (u16, Value) {
        let mut request =
            reqwest::Client::new().request(method.parse().unwrap(), format!("{}{path}", self.url));
        if let Some(body) = body {
            request = request
                .header("Content-Type", "application/json")
                .body(to_json_vec(body).unwrap());
        }
        let response = request.send().await.unwrap();
        let status = response.status().as_u16();
        (
            status,
            parse_json_value(&response.bytes().await.unwrap(), 16 * 1024 * 1024).unwrap(),
        )
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
fn fixture(id: &str) -> Value {
    json!({"id":id,"name":"owned worker fixture","streams":[
        {"id":"analyzer","device":"Synthetic tinySA","role":"spectrum_analyzer","level_unit":"dBm","settings":{"adapter":"tinysa-sim","start_hz":100,"stop_hz":110,"points":3,"noise_sigma_db":0,"emitters":[],"period_s":0.01}},
        {"id":"receiver","device":"Synthetic AR-DV1","role":"receiver","level_unit":"s_meter_count","settings":{"adapter":"ardv1-sim","period_s":0.01}}
    ]})
}
async fn setup(server: &Server, value: Value) {
    let id = value["id"].as_str().unwrap().to_owned();
    assert_eq!(
        server
            .request("POST", "/api/v1/sessions", Some(&value))
            .await
            .0,
        201
    );
    assert_eq!(
        server
            .request(
                "POST",
                &format!("/api/v1/sessions/{id}/start"),
                Some(&json!({}))
            )
            .await
            .0,
        200
    );
}
async fn wait_measurements(server: &Server, id: &str, stream: &str) -> Value {
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let (status, result) = server
                .request("GET", &format!("/api/v1/sessions/{id}/workers"), None)
                .await;
            assert_eq!(status, 200, "{result}");
            if let Some(worker) =
                result["data"].as_array().unwrap().iter().find(|w| {
                    w["stream_id"] == stream && w["measurements"].as_u64().unwrap_or(0) > 0
                })
            {
                return worker.clone();
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap()
}

#[tokio::test]
async fn explicit_simulators_commit_raw_measurements_and_tune_before_acknowledgement() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(CoreConfig::replay(directory.path())).await;
    setup(&server, fixture("workers")).await;
    let (status, idle) = server
        .request("GET", "/api/v1/sessions/workers/workers", None)
        .await;
    assert_eq!(status, 200, "{idle}");
    assert!(
        idle["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["alive"] == false)
    );
    let (status, running) = server
        .request(
            "POST",
            "/api/v1/sessions/workers/workers/start",
            Some(&json!({})),
        )
        .await;
    assert_eq!(status, 200, "{running}");
    assert!(
        running["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["alive"] == true)
    );
    wait_measurements(&server, "workers", "analyzer").await;
    wait_measurements(&server, "workers", "receiver").await;
    let (_, sweeps) = server
        .request(
            "GET",
            "/api/v1/sessions/workers/sweeps?stream_id=analyzer&limit=1",
            None,
        )
        .await;
    assert_eq!(sweeps["data"][0]["frequencies_hz"], json!([100, 105, 110]));
    assert_eq!(sweeps["data"][0]["levels"], json!([-100.0, -100.0, -100.0]));
    assert_eq!(sweeps["data"][0]["settings"]["generator_output"], "off");
    let (status, tuned) = server
        .request(
            "POST",
            "/api/v1/sessions/workers/streams/receiver/tune",
            Some(&json!({"frequency_hz":145_600_000,"mode":"DMR"})),
        )
        .await;
    assert_eq!(status, 200, "{tuned}");
    assert_eq!(tuned["data"]["tuned"]["frequency_hz"], 145_600_000);
    assert_eq!(tuned["data"]["worker"]["current"]["mode"], "DMR");
    let (_, events) = server
        .request("GET", "/api/v1/events/history?limit=100", None)
        .await;
    assert!(
        events["data"]
            .as_array()
            .unwrap()
            .iter()
            .any(|event| event["event_type"] == "device.tuned")
    );
    let (status, stopped) = server
        .request("POST", "/api/v1/sessions/workers/stop", Some(&json!({})))
        .await;
    assert_eq!(status, 200, "{stopped}");
    let (_, workers) = server
        .request("GET", "/api/v1/sessions/workers/workers", None)
        .await;
    assert!(
        workers["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["alive"] == false)
    );
    let (_, before) = server
        .request("GET", "/api/v1/events/history?limit=100", None)
        .await;
    tokio::time::sleep(Duration::from_millis(30)).await;
    let (_, after) = server
        .request("GET", "/api/v1/events/history?limit=100", None)
        .await;
    assert_eq!(
        before, after,
        "no owner may append after session-stop acknowledgement"
    );
    server.stop().await;
}

#[tokio::test]
async fn implicit_or_disabled_physical_adapters_never_open_at_session_start() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(CoreConfig::replay(directory.path())).await;
    let mut value = fixture("disabled");
    value["streams"][0]["settings"]["adapter"] = json!("tinysa-serial");
    value["streams"][0]["port"] = json!("/never-open-this-owned-test-name");
    setup(&server, value).await;
    let (status, result) = server
        .request(
            "POST",
            "/api/v1/sessions/disabled/streams/analyzer/start",
            Some(&json!({})),
        )
        .await;
    assert_eq!(status, 400, "{result}");
    assert_eq!(result["error"]["code"], "unsupported_adapter");
    let (status, workers) = server
        .request("GET", "/api/v1/sessions/disabled/workers", None)
        .await;
    assert_eq!(status, 200);
    assert!(
        workers["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|worker| worker["alive"] == false)
    );
    server.stop().await;
}
