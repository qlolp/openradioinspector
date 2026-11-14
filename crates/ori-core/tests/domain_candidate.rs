//! Public HTTP + actual temporary archive/owned simulator effects.
use ori_contracts::{parse_json_value, to_json_vec};
use ori_core::{Core, CoreConfig, RuntimeIdentity};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use tokio::{net::TcpListener, task::JoinHandle};
const TOKEN: &str = "synthetic-control-token-0123456789abcdef";
struct Server {
    core: Arc<Core>,
    url: String,
    task: JoinHandle<ori_core::Result<()>>,
}
impl Server {
    async fn start(path: &std::path::Path) -> Self {
        let mut config = CoreConfig::replay(path);
        config.identity = Some(RuntimeIdentity {
            token: TOKEN.into(),
            instance_id: "domain-fixture".into(),
        });
        let core = Core::open(config).await.unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let own = core.clone();
        let task = tokio::spawn(async move { own.serve(listener).await });
        Self { core, url, task }
    }
    async fn request(&self, method: &str, path: &str, payload: Option<&Value>) -> (u16, Value) {
        let mut request =
            reqwest::Client::new().request(method.parse().unwrap(), format!("{}{path}", self.url));
        if path.starts_with("/internal/") {
            request = request.bearer_auth(TOKEN);
        }
        if let Some(body) = payload {
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
fn fixture() -> Value {
    json!({"id":"domain","name":"Domain fixture","streams":[
        {"id":"spectrum","device":"Synthetic analyzer","role":"spectrum_analyzer","level_unit":"dBm","settings":{"adapter":"tinysa-sim","start_hz":100_000_000,"stop_hz":100_020_000,"points":3,"noise_sigma_db":0,"emitters":[],"period_s":0.01}},
        {"id":"receiver","device":"Synthetic receiver","role":"receiver","level_unit":"s_meter_count","settings":{"adapter":"ardv1-sim","period_s":0.01}}
    ]})
}
async fn setup(server: &Server) {
    let (status, value) = server
        .request("POST", "/api/v1/sessions", Some(&fixture()))
        .await;
    assert_eq!(status, 201, "{value}");
    assert_eq!(
        server
            .request("POST", "/api/v1/sessions/domain/start", Some(&json!({})))
            .await
            .0,
        200
    );
}
async fn project(server: &Server) -> String {
    let (status, value) = server
        .request(
            "POST",
            "/api/v1/monitoring-projects",
            Some(&json!({"name":"Synthetic project","site":"Fixture only","session_id":"domain"})),
        )
        .await;
    assert_eq!(status, 201, "{value}");
    value["data"]["id"].as_str().unwrap().to_owned()
}

#[tokio::test]
async fn session_domain_http_uses_owned_tune_and_actual_post_tune_observations_before_completion() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(directory.path()).await;
    setup(&server).await;
    let (status,candidate)=server.request("POST","/api/v1/sessions/domain/candidates",Some(&json!({"id":"candidate","source_stream_id":"spectrum","interval":{"started_at":"2026-11-11T00:00:00.000000Z","ended_at":"2026-11-11T00:00:01.000000Z"},"center_frequency_hz":100_010_000,"level_unit":"dBm","peak_level":-0.0,"score":1.0000000000000002}))).await;
    assert_eq!(status, 201, "{candidate}");
    assert_eq!(
        candidate["data"]["peak_level"].as_f64().unwrap().to_bits(),
        (-0.0f64).to_bits()
    );
    let (status, job) = server
        .request(
            "POST",
            "/api/v1/sessions/domain/candidates/candidate/investigations",
            Some(&json!({"id":"job","target_stream_id":"receiver","mode":"FM"})),
        )
        .await;
    assert_eq!(status, 201, "{job}");
    assert_eq!(job["data"]["state"], "queued");
    let (status, not_confirmed) = server
        .request(
            "POST",
            "/api/v1/sessions/domain/investigations/start-next",
            Some(&json!({"investigation_id":"job"})),
        )
        .await;
    assert_eq!(status, 400, "{not_confirmed}");
    assert_eq!(
        server
            .request(
                "POST",
                "/api/v1/sessions/domain/streams/receiver/start",
                Some(&json!({}))
            )
            .await
            .0,
        200
    );
    let (status, listening) = server
        .request(
            "POST",
            "/api/v1/sessions/domain/investigations/start-next",
            Some(&json!({"investigation_id":"job","confirm":true})),
        )
        .await;
    assert_eq!(status, 200, "{listening}");
    assert_eq!(listening["data"]["state"], "listening");
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let (_, observations) = server
                .request(
                    "GET",
                    "/api/v1/sessions/domain/observations?stream_id=receiver&limit=100",
                    None,
                )
                .await;
            if observations["data"]
                .as_array()
                .unwrap()
                .iter()
                .any(|source| {
                    source["frequency_hz"] == 100_010_000
                        && source["interval"]["started_at"].as_str().unwrap()
                            >= listening["data"]["listening_started_at"].as_str().unwrap()
                })
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let (status, completed) = server
        .request(
            "POST",
            "/api/v1/sessions/domain/investigations/job/complete",
            Some(&json!({})),
        )
        .await;
    assert_eq!(status, 200, "{completed}");
    assert_eq!(completed["data"]["state"], "completed");
    assert!(
        !completed["data"]["result_observation_ids"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let (_, resolved) = server
        .request("GET", "/api/v1/sessions/domain/candidates/candidate", None)
        .await;
    assert_eq!(resolved["data"]["state"], "resolved");
    let (_, events) = server
        .request("GET", "/api/v1/events/history?limit=100", None)
        .await;
    let values = events["data"].as_array().unwrap();
    for name in [
        "candidate.created",
        "investigation.queued",
        "investigation.tuning",
        "investigation.listening",
        "investigation.completed",
    ] {
        assert!(
            values.iter().any(|event| event["event_type"] == name),
            "{name}: {events}"
        );
    }
    server.stop().await;
}

#[tokio::test]
async fn cold_monitoring_ui_serves_truthful_preflight_and_scoped_crud_without_opening_devices() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(directory.path()).await;
    setup(&server).await;
    let (_, capabilities) = server.request("GET", "/api/v1/capabilities", None).await;
    for feature in [
        "monitoring_projects",
        "monitoring_guard",
        "candidate_investigations",
        "measurement_results",
        "signal_list_exchange",
    ] {
        assert_eq!(capabilities["data"]["features"][feature], true, "{feature}");
    }
    assert_eq!(
        capabilities["data"]["features"]["automatic_investigation_completion"],
        true
    );
    assert_eq!(
        capabilities["data"]["features"]["automatic_investigation_completion_opt_in_required"],
        true
    );
    let (status, completion) = server
        .request("GET", "/api/v1/sessions/domain/automatic-completion", None)
        .await;
    assert_eq!(status, 200, "{completion}");
    assert_eq!(completion["data"]["policy"]["enabled"], false);
    assert_eq!(completion["data"]["scheduler_alive"], false);
    assert_eq!(completion["data"]["runtime"]["last_result"], "disabled");
    assert_eq!(completion["data"]["policy_persistent"], false);
    assert_eq!(
        capabilities["data"]["features"]["named_sweep_references"], false,
        "creating a reference requires the configured scientific worker"
    );
    let (_, specification) = server.request("GET", "/api/v1/openapi.json", None).await;
    for (path, verb) in [
        ("/api/v1/monitoring-projects/{project_id}/arm", "post"),
        ("/api/v1/sessions/{id}/candidates", "post"),
        ("/api/v1/sessions/{id}/sweep-references", "get"),
    ] {
        assert!(
            specification["paths"][path][verb].is_object(),
            "{path}: {specification}"
        );
    }
    let id = project(&server).await;
    let (status, project) = server
        .request("GET", &format!("/api/v1/monitoring-projects/{id}"), None)
        .await;
    assert_eq!(status, 200);
    assert_eq!(project["data"]["baseline_id"], Value::Null);
    let (status, preflight) = server
        .request(
            "GET",
            &format!("/api/v1/monitoring-projects/{id}/preflight"),
            None,
        )
        .await;
    assert_eq!(status, 200, "{preflight}");
    assert_eq!(preflight["data"]["baseline_ready"], false);
    assert_eq!(preflight["data"]["hardware_validation"], "not_run");
    let (_, runtime) = server
        .request("GET", "/internal/runtime/v1/status", None)
        .await;
    assert_eq!(runtime["ready"], true);
    assert_eq!(runtime["preflight_complete"], true);
    assert_eq!(runtime["guard_armed"], false);
    let (_, workers) = server
        .request("GET", "/api/v1/sessions/domain/workers", None)
        .await;
    assert!(
        workers["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|worker| worker["alive"] == false)
    );
    let (status,saved)=server.request("POST",&format!("/api/v1/monitoring-projects/{id}/preparation"),Some(&json!({"antenna_notes":"Synthetic only","receive_path_confirmed":true,"location_confirmed":true}))).await;
    assert_eq!(status, 200, "{saved}");
    let (status, list) = server
        .request("GET", "/api/v1/monitoring-projects", None)
        .await;
    assert_eq!(status, 200, "{list}");
    assert_eq!(list["data"]["items"].as_array().unwrap().len(), 1);
    let (status, arm) = server
        .request(
            "POST",
            &format!("/api/v1/monitoring-projects/{id}/arm"),
            Some(&json!({})),
        )
        .await;
    assert_eq!(status, 400, "{arm}");
    server.stop().await;
}

#[tokio::test]
async fn armed_cutover_requires_ack_and_preserves_opt_in_intent_before_releasing_the_writer() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(directory.path()).await;
    setup(&server).await;
    let now = chrono::Utc::now();
    for index in 0..20 {
        let started = now - chrono::Duration::milliseconds(70_000 - index * 3_500);
        let ended = started + chrono::Duration::milliseconds(10);
        let source = json!({"id":format!("baseline-{index:02}"),"stream_id":"spectrum","interval":{"started_at":started.to_rfc3339_opts(chrono::SecondsFormat::Micros,true),"ended_at":ended.to_rfc3339_opts(chrono::SecondsFormat::Micros,true)},"frequencies_hz":[100_000_000,100_010_000,100_020_000],"levels":[-100.0,-100.0,-100.0],"level_unit":"dBm","settings":{"adapter":"tinysa-sim","rbw_hz":10_000,"sequence":index+1,"simulated":true,"generator_output":"off"}});
        let (status, value) = server
            .request("POST", "/api/v1/sessions/domain/sweeps", Some(&source))
            .await;
        assert_eq!(status, 201, "{value}");
    }
    let id = project(&server).await;
    let (status, baseline) = server
        .request(
            "POST",
            &format!("/api/v1/monitoring-projects/{id}/baselines"),
            Some(&json!({"name":"Synthetic baseline","location":"Fixture only","confirm":true})),
        )
        .await;
    assert_eq!(status, 201, "{baseline}");
    let (status, configured) = server
        .request(
            "POST",
            &format!("/api/v1/monitoring-projects/{id}/settings"),
            Some(&json!({"autostart":true})),
        )
        .await;
    assert_eq!(status, 200, "{configured}");
    let (status, armed) = server
        .request(
            "POST",
            &format!("/api/v1/monitoring-projects/{id}/arm"),
            Some(&json!({})),
        )
        .await;
    assert_eq!(status, 200, "{armed}");
    let session = armed["data"]["active_session_id"].as_str().unwrap();
    let stream = armed["data"]["receiver_stream_id"].as_str().unwrap();
    let (_, runtime) = server
        .request("GET", "/internal/runtime/v1/status", None)
        .await;
    assert_eq!(runtime["guard_armed"], true);
    let (status, denied) = server
        .request(
            "POST",
            &format!("/api/v1/sessions/{session}/streams/{stream}/tune"),
            Some(&json!({"frequency_hz":100_010_000})),
        )
        .await;
    assert_eq!(status, 409, "{denied}");
    let shutdown = json!({"version":1,"preserve_resume_requested":true,"reason":"cutover"});
    let (status, denied) = server
        .request("POST", "/internal/runtime/v1/shutdown", Some(&shutdown))
        .await;
    assert_eq!(status, 409, "{denied}");
    let (status,accepted)=server.request("POST","/internal/runtime/v1/shutdown",Some(&json!({"version":1,"preserve_resume_requested":true,"reason":"cutover","acknowledge_coverage_gap":true}))).await;
    assert_eq!(status, 202, "{accepted}");
    server.stop().await;
    let _lease = ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap();
    let archive = ori_archive::Archive::open(ori_archive::ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    let saved = archive.monitoring("project", &id).await.unwrap().unwrap();
    assert_eq!(saved["autostart"], true);
    assert_eq!(
        saved["resume_requested"], true,
        "controlled shutdown must preserve opt-in intent, never public disarm"
    );
    assert_eq!(
        archive
            .list_sweeps("domain", Some("spectrum"), 100)
            .await
            .unwrap()
            .len(),
        20
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn scan_plan_http_is_versioned_bounded_and_never_opens_devices() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(directory.path()).await;
    setup(&server).await;
    let id = project(&server).await;
    let route = format!("/api/v1/monitoring-projects/{id}/scan-plan");
    let config = json!({"name":"Survey","bands":[
        {"id":"fm","name":"FM","start_hz":88000000,"stop_hz":108000000,"points":201,"rbw_hz":10000,"max_revisit_seconds":15.0},
        {"id":"uhf","name":"UHF","start_hz":430000000,"stop_hz":440000000,"points":101,"rbw_hz":30000,"max_revisit_seconds":15.0}]});
    let (status, value) = server.request("POST", &route, Some(&config)).await;
    assert_eq!(status, 201, "{value}");
    assert_eq!(value["data"]["revision"], 1);
    let (_, stored) = server.request("GET", &route, None).await;
    assert_eq!(stored, value);
    let mut invalid = config.clone();
    invalid["bands"][0]["output"] = json!(true);
    assert_eq!(server.request("POST", &route, Some(&invalid)).await.0, 400);
    assert_eq!(server.request("GET", &route, None).await.1, stored);
    let (_, capabilities) = server.request("GET", "/api/v1/capabilities", None).await;
    assert_eq!(
        capabilities["data"]["features"]["monitoring_scan_plans"],
        true
    );
    let (worker_status, workers) = server
        .request("GET", "/api/v1/sessions/domain/workers", None)
        .await;
    assert_eq!(worker_status, 200, "{workers}");
    assert!(!workers.to_string().contains("\"alive\":true"));
    server.stop().await;
    let restarted = Server::start(directory.path()).await;
    let (status, recovered) = restarted.request("GET", &route, None).await;
    assert_eq!(status, 200, "{recovered}");
    assert_eq!(recovered, stored);
    let (status, workers) = restarted
        .request("GET", "/api/v1/sessions/domain/workers", None)
        .await;
    assert_eq!(status, 200, "{workers}");
    assert!(!workers.to_string().contains("\"alive\":true"));
    restarted.stop().await;
}
