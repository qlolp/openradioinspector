//! Actual public automation routes, original receipts, and owned effects.
use ori_contracts::{parse_json_value, to_json_vec};
use ori_core::{Core, CoreConfig};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::{net::TcpListener, task::JoinHandle};

fn python_program(preferred: Option<std::ffi::OsString>) -> PathBuf {
    let requested = PathBuf::from(
        preferred
            .or_else(|| std::env::var_os("ORI_TEST_PYTHON"))
            .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into()),
    );
    if requested.is_absolute() {
        return requested;
    }
    for directory in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        let Ok(directory) = directory.canonicalize() else {
            continue;
        };
        let candidate = directory.join(&requested);
        if candidate.is_file() {
            return candidate;
        }
        if cfg!(windows) {
            let candidate = candidate.with_extension("exe");
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    panic!("A trusted absolute Python executable is required for the owned scientific fixture");
}

struct Server {
    core: Arc<Core>,
    client: reqwest::Client,
    url: String,
    task: Option<JoinHandle<ori_core::Result<()>>>,
}
impl Server {
    async fn start(directory: &Path) -> Self {
        Self::configured(directory, false).await
    }
    async fn configured(directory: &Path, science: bool) -> Self {
        let mut config = CoreConfig::replay(directory);
        if science {
            let installed = std::env::var_os("ORI_RESEARCH_PYTHON");
            let program = python_program(installed.clone());
            let (args, environment) = if installed.is_some() {
                (
                    vec![
                        "-I".into(),
                        "-m".into(),
                        "radioinspector.research_rpc".into(),
                    ],
                    Default::default(),
                )
            } else {
                (
                    vec![
                        Path::new(env!("CARGO_MANIFEST_DIR"))
                            .join("tests/automation_detection_child.py")
                            .to_str()
                            .unwrap()
                            .into(),
                    ],
                    Default::default(),
                )
            };
            config.analysis_worker = Some(ori_runtime::worker::WorkerSpec {
                program,
                args,
                environment,
                deadline_ms: 5000,
                require_hard_memory: false,
                linux_cgroup: None,
            });
        }
        let core = Core::open(config).await.unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let serving = core.clone();
        let task = tokio::spawn(async move { serving.serve(listener).await });
        Self {
            core,
            client: reqwest::Client::new(),
            url,
            task: Some(task),
        }
    }
    async fn request(&self, method: &str, path: &str, payload: Option<&Value>) -> Value {
        self.request_as(method, path, payload, None).await
    }
    async fn request_as(
        &self,
        method: &str,
        path: &str,
        payload: Option<&Value>,
        token: Option<&str>,
    ) -> Value {
        let mut request = self
            .client
            .request(method.parse().unwrap(), format!("{}{path}", self.url));
        if let Some(token) = token {
            request = request.bearer_auth(token);
        }
        if method != "GET" && self.url.starts_with("https:") {
            request = request.header("Origin", &self.url);
        }
        if let Some(payload) = payload {
            request = request
                .header("Content-Type", "application/json")
                .body(to_json_vec(payload).unwrap());
        }
        let response = request.send().await.unwrap();
        let status = response.status().as_u16();
        let body = parse_json_value(&response.bytes().await.unwrap(), 1_048_576).unwrap();
        json!({"status": status, "body": body})
    }
    async fn seed(&self) {
        let created = self.request("POST", "/api/v1/sessions", Some(&json!({
            "id":"automation-http-oracle", "name":"Owned automation HTTP oracle",
            "streams":[
                {"id":"spectrum","device":"Owned synthetic analyzer","role":"spectrum_analyzer","level_unit":"dBm","settings":{"adapter":"tinysa-sim"}},
                {"id":"receiver","device":"Owned synthetic receiver","role":"receiver","level_unit":"s_meter_count","settings":{"adapter":"ardv1-sim"}}
            ]
        }))).await;
        assert_eq!(created["status"], 201, "{created}");
    }
    async fn stop(mut self) {
        self.core.request_shutdown();
        tokio::time::timeout(Duration::from_secs(5), self.task.take().unwrap())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
    }
    async fn tls(directory: &Path, marker: &Path) -> Self {
        let security = directory.join("security");
        std::fs::create_dir(&security).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&security, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let identity =
            ori_runtime::security::TlsIdentity::create(&security, vec!["127.0.0.1".into()])
                .unwrap();
        let client = reqwest::Client::builder()
            .tls_certs_only([reqwest::Certificate::from_pem(
                identity.trust_anchor_pem().as_bytes(),
            )
            .unwrap()])
            .build()
            .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let authority = listener.local_addr().unwrap().to_string();
        let url = format!("https://{authority}");
        let mut config = CoreConfig::replay(directory);
        config.lan_security = Some(ori_core::LanSecurityConfig {
            directory: security,
            hosts: vec![authority],
            origins: vec![url.clone()],
        });
        config.analysis_worker = Some(ori_runtime::worker::WorkerSpec {
            program: python_program(std::env::var_os("ORI_RESEARCH_PYTHON")),
            args: vec![
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("tests/automation_detection_child.py")
                    .to_str()
                    .unwrap()
                    .into(),
                marker.to_str().unwrap().into(),
            ],
            environment: Default::default(),
            deadline_ms: 5000,
            require_hard_memory: false,
            linux_cgroup: None,
        });
        let core = Core::open(config).await.unwrap();
        let serving = core.clone();
        let task = tokio::spawn(async move { serving.serve_tls(listener).await });
        Self {
            core,
            client,
            url,
            task: Some(task),
        }
    }
    async fn pair(&self) -> String {
        let code = self.core.begin_pairing_window(60, 3).unwrap();
        let result=self.request("POST","/api/v1/security/pair",Some(&json!({"pairing_code":code,"label":"owned automation fixture","client":"native"}))).await;
        assert_eq!(result["status"], 200, "{result}");
        result["body"]["data"]["token"].as_str().unwrap().into()
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.core.request_shutdown();
    }
}
fn oracle() -> Value {
    parse_json_value(
        include_bytes!("fixtures/automation-http-python.json"),
        65_536,
    )
    .unwrap()
}

#[tokio::test]
async fn automation_http_defaults_match_actual_python_and_start_no_worker() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(directory.path()).await;
    server.seed().await;
    let mut results = serde_json::Map::new();
    for kind in ["detection", "handoff", "completion"] {
        let path = format!("/api/v1/sessions/automation-http-oracle/automatic-{kind}");
        results.insert(kind.into(), server.request("GET", &path, None).await);
    }
    let workers = server
        .request(
            "GET",
            "/api/v1/sessions/automation-http-oracle/workers",
            None,
        )
        .await;
    server.stop().await;
    assert_eq!(Value::Object(results), oracle()["defaults"]);
    assert_eq!(workers["status"], 200, "{workers}");
    let workers = workers["body"]["data"].as_array().unwrap();
    assert_eq!(workers.len(), 2);
    assert!(workers.iter().all(|worker| worker["alive"] == false));
}

#[tokio::test]
async fn automation_http_policy_errors_keep_original_python_messages() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(directory.path()).await;
    server.seed().await;
    let mut results = serde_json::Map::new();
    for kind in ["detection", "handoff", "completion"] {
        let path = format!("/api/v1/sessions/automation-http-oracle/automatic-{kind}");
        results.insert(
            kind.into(),
            server
                .request("POST", &path, Some(&json!({"unknown":true})))
                .await,
        );
    }
    server.stop().await;
    assert_eq!(Value::Object(results), oracle()["errors"]);
}

#[tokio::test]
async fn policy_validation_precedes_manual_authority_and_never_echoes_its_secret() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(directory.path()).await;
    server.seed().await;
    let mut errors = serde_json::Map::new();
    let mut defaults = serde_json::Map::new();
    for kind in ["detection", "handoff", "completion"] {
        let path = format!("/api/v1/sessions/automation-http-oracle/automatic-{kind}");
        errors.insert(
            kind.into(),
            server
                .request(
                    "POST",
                    &path,
                    Some(&json!({
                        "unknown":true,"control_owner":"synthetic-foreign-manual-secret"
                    })),
                )
                .await,
        );
        defaults.insert(kind.into(), server.request("GET", &path, None).await);
    }
    let events = server
        .request("GET", "/api/v1/events/history?limit=100", None)
        .await;
    let workers = server
        .request(
            "GET",
            "/api/v1/sessions/automation-http-oracle/workers",
            None,
        )
        .await;
    server.stop().await;
    assert_eq!(Value::Object(errors), oracle()["errors"]);
    assert_eq!(Value::Object(defaults), oracle()["defaults"]);
    assert!(
        !to_json_vec(&events)
            .unwrap()
            .windows(b"synthetic-foreign-manual-secret".len())
            .any(|window| window == b"synthetic-foreign-manual-secret")
    );
    assert!(
        !events["body"]["data"]
            .as_array()
            .unwrap()
            .iter()
            .any(|event| event["event_type"]
                .as_str()
                .unwrap_or("")
                .contains("policy_changed"))
    );
    assert!(
        workers["body"]["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|worker| worker["alive"] == false)
    );
}

#[tokio::test]
async fn core_restart_requires_new_explicit_policy_opt_in_for_every_kind() {
    let directory = tempfile::tempdir().unwrap();
    let first = Server::start(directory.path()).await;
    first.seed().await;
    let mut configured = Vec::new();
    for kind in ["detection", "handoff", "completion"] {
        let path = format!("/api/v1/sessions/automation-http-oracle/automatic-{kind}");
        configured.push(
            first
                .request("POST", &path, Some(&json!({"enabled":true})))
                .await,
        );
    }
    first.stop().await;
    let second = Server::start(directory.path()).await;
    let mut defaults = serde_json::Map::new();
    for kind in ["detection", "handoff", "completion"] {
        let path = format!("/api/v1/sessions/automation-http-oracle/automatic-{kind}");
        defaults.insert(kind.into(), second.request("GET", &path, None).await);
    }
    let workers = second
        .request(
            "GET",
            "/api/v1/sessions/automation-http-oracle/workers",
            None,
        )
        .await;
    second.stop().await;
    assert!(
        configured.iter().all(|result| result["status"] == 200),
        "{configured:?}"
    );
    assert_eq!(Value::Object(defaults), oracle()["defaults"]);
    assert!(
        workers["body"]["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|worker| worker["alive"] == false)
    );
}

#[tokio::test]
async fn enabling_detection_does_not_start_unrelated_policy_schedulers() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(directory.path()).await;
    server.seed().await;
    let base = "/api/v1/sessions/automation-http-oracle/automatic-";
    let configured = server
        .request(
            "POST",
            &format!("{base}detection"),
            Some(&json!({"enabled":true})),
        )
        .await;
    assert_eq!(configured["status"], 200, "{configured}");
    let mut unrelated = serde_json::Map::new();
    for kind in ["handoff", "completion"] {
        unrelated.insert(
            kind.into(),
            server.request("GET", &format!("{base}{kind}"), None).await,
        );
    }
    server.stop().await;
    assert_eq!(Value::Object(unrelated), oracle()["after_detection_opt_in"]);
}

#[tokio::test]
async fn draft_opt_in_uses_original_detection_or_explicit_worker_unavailability() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::configured(directory.path(), true).await;
    server.seed().await;
    let base = "/api/v1/sessions/automation-http-oracle";
    let enabled = server
        .request(
            "POST",
            &format!("{base}/automatic-detection"),
            Some(&json!({
                "enabled":true,"interval_seconds":0.1,"threshold_dbm":-60.0
            })),
        )
        .await;
    assert_eq!(enabled["status"], 200, "{enabled}");
    let draft = server.request("GET", base, None).await;
    assert_eq!(draft["body"]["data"]["state"], "draft");
    let before = server
        .request("GET", &format!("{base}/candidates"), None)
        .await;
    assert_eq!(before["body"]["data"], json!([]));
    let started = server
        .request("POST", &format!("{base}/start"), Some(&json!({})))
        .await;
    assert_eq!(started["status"], 200, "{started}");
    let source = oracle()["draft_opt_in"]["sweep"].clone();
    let ingested = server
        .request("POST", &format!("{base}/sweeps"), Some(&source))
        .await;
    assert_eq!(ingested["status"], 201, "{ingested}");
    if cfg!(windows) {
        // Runtime's existing Windows child containment is deliberately absent.
        // Exercise the actual fail-closed route instead of skipping this test
        // or manufacturing scientific results on that platform.
        let failed = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let status = server
                    .request("GET", &format!("{base}/automatic-detection"), None)
                    .await;
                if status["body"]["data"]["runtime"]["last_result"] == "scheduler_error" {
                    break status;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let candidates = server
            .request("GET", &format!("{base}/candidates"), None)
            .await;
        let workers = server
            .request("GET", &format!("{base}/workers"), None)
            .await;
        server.stop().await;
        assert_eq!(
            failed["body"]["data"]["runtime"]["last_error"],
            "Ресурс временно недоступен"
        );
        assert_eq!(candidates["body"]["data"], json!([]));
        assert!(
            workers["body"]["data"]
                .as_array()
                .unwrap()
                .iter()
                .all(|worker| worker["alive"] == false)
        );
        return;
    }
    let candidates = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let value = server
                .request("GET", &format!("{base}/candidates"), None)
                .await;
            assert_eq!(value["status"], 200, "{value}");
            if value["body"]["data"].as_array().unwrap().len() == 2 {
                break value["body"]["data"].clone();
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    let candidates = match candidates {
        Ok(candidates) => candidates,
        Err(error) => {
            let status = server
                .request("GET", &format!("{base}/automatic-detection"), None)
                .await;
            server.stop().await;
            panic!("Original detector did not produce candidates: {error}; {status}");
        }
    };
    let mut selected = candidates
        .as_array()
        .unwrap()
        .iter()
        .map(|candidate| {
            let fields = [
                "source_stream_id",
                "interval",
                "center_frequency_hz",
                "bandwidth_hz",
                "peak_level",
                "score",
                "level_unit",
                "state",
            ];
            Value::Object(
                fields
                    .into_iter()
                    .map(|key| (key.into(), candidate[key].clone()))
                    .collect(),
            )
        })
        .collect::<Vec<_>>();
    selected.sort_by_key(|candidate| candidate["center_frequency_hz"].as_u64().unwrap());
    let disabled = server
        .request(
            "POST",
            &format!("{base}/automatic-detection"),
            Some(&json!({"enabled":false})),
        )
        .await;
    assert_eq!(disabled["status"], 200, "{disabled}");
    let investigations = server
        .request("GET", &format!("{base}/investigations"), None)
        .await;
    let workers = server
        .request("GET", &format!("{base}/workers"), None)
        .await;
    server.stop().await;
    assert_eq!(json!(selected), oracle()["draft_opt_in"]["candidates"]);
    assert_eq!(investigations["body"]["data"], json!([]));
    assert!(
        workers["body"]["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|worker| worker["alive"] == false)
    );
}

#[tokio::test]
async fn automation_auth_uses_original_tls_authority_or_explicit_platform_denial() {
    let directory = tempfile::tempdir().unwrap();
    if cfg!(windows) {
        let result = ori_runtime::security::TlsIdentity::create(
            &directory.path().join("security"),
            vec!["127.0.0.1".into()],
        );
        assert!(
            matches!(result,Err(ori_runtime::Error::Unsafe(message)) if message.contains("verified owner-only Windows DACL"))
        );
        assert!(!directory.path().join("sessions.sqlite3").exists());
        return;
    }
    let marker = directory.path().join("owned-child.json");
    let server = Server::tls(directory.path(), &marker).await;
    let first = server.pair().await;
    let second = server.pair().await;
    for (id, token) in [("first", first.as_str()), ("second", second.as_str())] {
        let base = format!("/api/v1/sessions/{id}");
        let created=server.request_as("POST","/api/v1/sessions",Some(&json!({"id":id,"name":"Owned TLS automation","streams":[
            {"id":format!("spectrum-{id}"),"device":"Owned source","role":"spectrum_analyzer","level_unit":"dBm","settings":{"adapter":"tinysa-sim"}},
            {"id":format!("receiver-{id}"),"device":"Owned receiver","role":"receiver","level_unit":"s_meter_count","settings":{"adapter":"ardv1-sim"}}
        ]})),Some(token)).await;
        assert_eq!(created["status"], 201, "{created}");
        let started = server
            .request_as(
                "POST",
                &format!("{base}/start"),
                Some(&json!({})),
                Some(token),
            )
            .await;
        assert_eq!(started["status"], 200, "{started}");
        let configured = server
            .request_as(
                "POST",
                &format!("{base}/automatic-detection"),
                Some(&json!({"enabled":true,"interval_seconds":0.1,"threshold_dbm":-60.0})),
                Some(token),
            )
            .await;
        assert_eq!(configured["status"], 200, "{configured}");
    }
    let mut source = oracle()["draft_opt_in"]["sweep"].clone();
    source["id"] = json!("first-blocked");
    source["session_id"] = json!("first");
    source["stream_id"] = json!("spectrum-first");
    let ingested = server
        .request_as(
            "POST",
            "/api/v1/sessions/first/sweeps",
            Some(&source),
            Some(&first),
        )
        .await;
    assert_eq!(ingested["status"], 201, "{ingested}");
    let owned: Value = match tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if let Ok(bytes) = std::fs::read(&marker)
                && let Ok(value) = serde_json::from_slice(&bytes)
            {
                break value;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    {
        Ok(owned) => owned,
        Err(error) => {
            let status = server
                .request_as(
                    "GET",
                    "/api/v1/sessions/first/automatic-detection",
                    None,
                    Some(&second),
                )
                .await;
            server.stop().await;
            panic!("Owned detector child was not started: {error}; {status}");
        }
    };
    assert_eq!(owned["sweep_id"], "first-blocked");
    let revoked = server
        .request_as(
            "POST",
            "/api/v1/security/credentials/current/revoke",
            Some(&json!({})),
            Some(&first),
        )
        .await;
    assert_eq!(revoked["status"], 200, "{revoked}");
    // A different authenticated client can observe the original policy, but
    // that observation must never lend the old pass a replacement credential.
    let observed = server
        .request_as(
            "GET",
            "/api/v1/sessions/first/automatic-detection",
            None,
            Some(&second),
        )
        .await;
    assert_eq!(observed["status"], 200, "{observed}");
    #[cfg(target_os = "linux")]
    {
        let original_process = format!("/proc/{}", owned["pid"].as_u64().unwrap());
        let joined = tokio::time::timeout(Duration::from_secs(2), async {
            while Path::new(&original_process).exists() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await;
        if joined.is_err() {
            server.stop().await;
            panic!(
                "Original credential revocation did not reap its owned child before the worker deadline"
            );
        }
    }
    source["id"] = json!("second-actual");
    source["session_id"] = json!("second");
    source["stream_id"] = json!("spectrum-second");
    let ingested = server
        .request_as(
            "POST",
            "/api/v1/sessions/second/sweeps",
            Some(&source),
            Some(&second),
        )
        .await;
    assert_eq!(ingested["status"], 201, "{ingested}");
    let independent = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let response = server
                .request_as(
                    "GET",
                    "/api/v1/sessions/second/candidates",
                    None,
                    Some(&second),
                )
                .await;
            if response["body"]["data"]
                .as_array()
                .is_some_and(|items| items.len() == 2)
            {
                break response;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let original = server
        .request_as(
            "GET",
            "/api/v1/sessions/first/candidates",
            None,
            Some(&second),
        )
        .await;
    let workers = server
        .request_as("GET", "/api/v1/sessions/first/workers", None, Some(&second))
        .await;
    server.stop().await;
    assert_eq!(original["body"]["data"], json!([]));
    assert_eq!(independent["body"]["data"].as_array().unwrap().len(), 2);
    assert!(
        workers["body"]["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|worker| worker["alive"] == false)
    );
    #[cfg(target_os = "linux")]
    assert!(
        !Path::new(&format!("/proc/{}", owned["pid"].as_u64().unwrap())).exists(),
        "The original owned child must be reaped before another child borrows the global reservation"
    );
}

#[tokio::test]
async fn handoff_uses_explicit_owned_receiver_and_completion_uses_canonical_evidence_after_stop() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(directory.path()).await;
    server.seed().await;
    let base = "/api/v1/sessions/automation-http-oracle";
    let started = server
        .request("POST", &format!("{base}/start"), Some(&json!({})))
        .await;
    assert_eq!(started["status"], 200, "{started}");
    let connected = server
        .request(
            "POST",
            &format!("{base}/streams/receiver/start"),
            Some(&json!({})),
        )
        .await;
    assert_eq!(connected["status"], 200, "{connected}");
    let candidate=server.request("POST",&format!("{base}/candidates"),Some(&json!({
        "id":"owned-handoff-candidate","source_stream_id":"spectrum",
        "interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:01.123456Z"},
        "center_frequency_hz":100200000,"bandwidth_hz":100000,"peak_level":-0.0,"score":1.0000000000000002,"level_unit":"dBm"
    }))).await;
    assert_eq!(candidate["status"], 201, "{candidate}");
    let queued = server
        .request(
            "POST",
            &format!("{base}/candidates/owned-handoff-candidate/investigations"),
            Some(&json!({"id":"owned-handoff-job","target_stream_id":"receiver","mode":"FM"})),
        )
        .await;
    assert_eq!(queued["status"], 201, "{queued}");
    let enabled = server
        .request(
            "POST",
            &format!("{base}/automatic-handoff"),
            Some(&json!({"enabled":true,"interval_seconds":0.1,"receiver_stream_id":"receiver"})),
        )
        .await;
    assert_eq!(enabled["status"], 200, "{enabled}");
    let listening = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let response = server
                .request(
                    "GET",
                    &format!("{base}/investigations/owned-handoff-job"),
                    None,
                )
                .await;
            if response["body"]["data"]["state"] == "listening" {
                break response["body"]["data"].clone();
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let disabled = server
        .request(
            "POST",
            &format!("{base}/automatic-handoff"),
            Some(&json!({"enabled":false})),
        )
        .await;
    assert_eq!(disabled["status"], 200, "{disabled}");
    let stamp = listening["listening_started_at"].as_str().unwrap();
    let source = json!({"id":"owned-handoff-evidence","stream_id":"receiver","interval":{"started_at":stamp,"ended_at":stamp},"frequency_hz":100200000,"mode":"FM","level":-0.0,"level_unit":"s_meter_count","squelch_open":true,"decoded_mode":null,"metadata":{"calibrated":false}});
    let observed = server
        .request("POST", &format!("{base}/observations"), Some(&source))
        .await;
    assert_eq!(observed["status"], 201, "{observed}");
    let stopped = server
        .request(
            "POST",
            &format!("{base}/streams/receiver/stop"),
            Some(&json!({})),
        )
        .await;
    assert_eq!(stopped["status"], 200, "{stopped}");
    let enabled = server
        .request(
            "POST",
            &format!("{base}/automatic-completion"),
            Some(&json!({"enabled":true,"interval_seconds":0.1,"receiver_stream_id":"receiver"})),
        )
        .await;
    assert_eq!(enabled["status"], 200, "{enabled}");
    let completed = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let response = server
                .request(
                    "GET",
                    &format!("{base}/investigations/owned-handoff-job"),
                    None,
                )
                .await;
            if response["body"]["data"]["state"] == "completed" {
                break response["body"]["data"].clone();
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let workers = server
        .request("GET", &format!("{base}/workers"), None)
        .await;
    let evidence = server
        .request(
            "GET",
            &format!("{base}/observations?stream_id=receiver&limit=100"),
            None,
        )
        .await;
    let candidate = server
        .request(
            "GET",
            &format!("{base}/candidates/owned-handoff-candidate"),
            None,
        )
        .await;
    server.stop().await;
    assert!(
        completed["result_observation_ids"]
            .as_array()
            .unwrap()
            .iter()
            .any(|id| id == "owned-handoff-evidence")
    );
    assert_eq!(candidate["body"]["data"]["state"], "resolved");
    assert_eq!(
        candidate["body"]["data"]["score"]
            .as_f64()
            .unwrap()
            .to_bits(),
        1.0000000000000002f64.to_bits()
    );
    let evidence = evidence["body"]["data"]
        .as_array()
        .unwrap()
        .iter()
        .find(|value| value["id"] == "owned-handoff-evidence")
        .unwrap();
    assert_eq!(evidence["level_unit"], "s_meter_count");
    assert_eq!(
        evidence["level"].as_f64().unwrap().to_bits(),
        (-0.0f64).to_bits()
    );
    assert!(
        workers["body"]["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|worker| worker["alive"] == false)
    );
}
