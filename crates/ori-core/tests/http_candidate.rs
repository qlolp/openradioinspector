use ori_contracts::{encode_path_segment, parse_json_value, to_json_vec};
use ori_core::{Core, CoreConfig, RuntimeIdentity};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    task::JoinHandle,
};
struct Server {
    core: Arc<Core>,
    url: String,
    handle: Option<JoinHandle<ori_core::Result<()>>>,
}
impl Server {
    async fn start(config: CoreConfig) -> Self {
        let core = Core::open(config).await.unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let task = core.clone();
        let handle = tokio::spawn(async move { task.serve(listener).await });
        Self {
            core,
            url,
            handle: Some(handle),
        }
    }
    async fn stop(&mut self) {
        self.core.request_shutdown();
        if let Some(handle) = self.handle.take() {
            tokio::time::timeout(Duration::from_secs(3), handle)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
        }
    }
    async fn request(&self, method: &str, path: &str, body: Option<&Value>) -> (u16, Value) {
        let client = reqwest::Client::new();
        let mut request = client.request(method.parse().unwrap(), format!("{}{path}", self.url));
        if let Some(body) = body {
            request = request
                .header("Content-Type", "application/json")
                .body(to_json_vec(body).unwrap());
        }
        let response = request.send().await.unwrap();
        let status = response.status().as_u16();
        let bytes = response.bytes().await.unwrap();
        (status, parse_json_value(&bytes, 16 * 1024 * 1024).unwrap())
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.core.request_shutdown();
        if let Some(handle) = self.handle.take() {
            handle.abort();
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
fn route(id: &str, tail: &str) -> String {
    format!("/api/v1/sessions/{}{tail}", encode_path_segment(id))
}
async fn create(server: &Server) -> Value {
    let (status, value) = server
        .request("POST", "/api/v1/sessions", Some(&fixture()["session"]))
        .await;
    assert_eq!(status, 201);
    value["data"].clone()
}

#[tokio::test]
async fn public_session_state_and_single_stream_errors_keep_authority_messages() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    let invalid = server
        .request(
            "POST",
            "/api/v1/sessions",
            Some(&json!({"state":"running"})),
        )
        .await;
    let session = create(&server).await;
    let stream = &session["streams"][0];
    let found = server
        .request(
            "GET",
            &route(
                "fixture-сессия",
                &format!(
                    "/streams/{}",
                    encode_path_segment(stream["id"].as_str().unwrap())
                ),
            ),
            None,
        )
        .await;
    let missing_stream = server
        .request(
            "GET",
            &route("fixture-сессия", "/streams/missing-e%CC%81"),
            None,
        )
        .await;
    let missing_session = server
        .request("GET", &route("missing-e\u{301}", "/streams/missing"), None)
        .await;
    server.stop().await;
    assert_eq!(invalid.0, 400);
    assert_eq!(invalid.1["error"]["code"], "invalid_request");
    assert_eq!(
        invalid.1["error"]["message"],
        "Новый сеанс должен иметь состояние draft"
    );
    assert_eq!(found.0, 200);
    assert_eq!(&found.1["data"], stream);
    assert_eq!(missing_stream.0, 404);
    assert_eq!(missing_stream.1["error"]["code"], "stream_not_found");
    assert_eq!(
        missing_stream.1["error"]["message"],
        "Stream not found in session"
    );
    assert_eq!(missing_session.0, 404);
    assert_eq!(missing_session.1["error"]["code"], "session_not_found");
    assert_eq!(
        missing_session.1["error"]["message"],
        "Сеанс не найден: missing-e\u{301}"
    );
}

#[tokio::test]
async fn default_shadow_cannot_open_or_modify_a_live_archive() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("sessions.sqlite3");
    std::fs::write(&path, b"original-live-bytes").unwrap();
    let mut server = Server::start(CoreConfig::new(directory.path())).await;
    let (status, caps) = server.request("GET", "/api/v1/capabilities", None).await;
    assert_eq!(status, 200);
    assert_eq!(caps["data"]["candidate"]["mode"], "shadow");
    assert_eq!(
        server
            .request("POST", "/api/v1/sessions", Some(&fixture()["session"]))
            .await
            .0,
        409
    );
    server.stop().await;
    assert_eq!(std::fs::read(path).unwrap(), b"original-live-bytes");
    assert!(!directory.path().join(".ori-runtime").exists());
}

#[tokio::test]
async fn replay_sessions_preserve_wire_envelopes_ids_and_owned_lifecycle() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    let saved = create(&server).await;
    assert_eq!(saved["id"], "fixture-сессия");
    assert_eq!(saved["state"], "draft");
    assert!(saved["started_at"].is_null());
    assert_eq!(saved["streams"][0]["id"], "spectrum/one");
    assert_eq!(saved["streams"][0]["session_id"], saved["id"]);
    let (_, envelope) = server
        .request("GET", &route("fixture-сессия", ""), None)
        .await;
    assert_eq!(envelope["contract"], "openradioinspector-platform");
    assert_eq!(envelope["contract_version"], 1);
    assert_eq!(envelope["data"], saved);
    let (status, started) = server
        .request("POST", &route("fixture-сессия", "/start"), Some(&json!({})))
        .await;
    assert_eq!(status, 200);
    assert_eq!(started["data"]["state"], "running");
    assert_eq!(
        server
            .request("POST", &route("fixture-сессия", "/start"), Some(&json!({})))
            .await
            .0,
        400
    );
    let (status, stopped) = server
        .request("POST", &route("fixture-сессия", "/stop"), Some(&json!({})))
        .await;
    assert_eq!(status, 200);
    assert_eq!(stopped["data"]["state"], "stopped");
    server.stop().await;
}

#[tokio::test]
async fn raw_sources_and_batch_rollback_are_durable_before_event_publication() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    create(&server).await;
    server
        .request("POST", &route("fixture-сессия", "/start"), Some(&json!({})))
        .await;
    let mut first = fixture()["sweep"].clone();
    first["settings"]=parse_json_value(br#"{"$serde_json::private::Number":"opaque-historical-key","integer":184467440737095516160000}"#,65536).unwrap();
    let (status, saved) = server
        .request("POST", &route("fixture-сессия", "/sweeps"), Some(&first))
        .await;
    assert_eq!(status, 201);
    assert_eq!(
        saved["data"]["interval"]["started_at"],
        "2026-11-11T12:00:00.123456Z"
    );
    for (level, bits) in saved["data"]["levels"].as_array().unwrap().iter().zip([
        0xc059080000000000_u64,
        0x8000000000000000,
        0xbff0000000000001,
        0xc040600000000000,
    ]) {
        assert_eq!(level.as_f64().unwrap().to_bits(), bits);
    }
    assert_eq!(saved["data"]["settings"], first["settings"]);
    let mut valid = first.clone();
    valid["id"] = json!("rollback-valid");
    let mut foreign = valid.clone();
    foreign["id"] = json!("rollback-foreign");
    foreign["stream_id"] = json!("unknown");
    assert_eq!(
        server
            .request(
                "POST",
                &route("fixture-сессия", "/sweeps/batch"),
                Some(&json!({"items":[valid,foreign]}))
            )
            .await
            .0,
        400
    );
    let (status, items) = server
        .request("GET", &route("fixture-сессия", "/sweeps?limit=100"), None)
        .await;
    assert_eq!(status, 200);
    assert_eq!(items["data"].as_array().unwrap().len(), 1);
    assert_eq!(
        server
            .request(
                "POST",
                &route("fixture-сессия", "/observations"),
                Some(&fixture()["observation"])
            )
            .await
            .0,
        201
    );
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=100", None)
        .await;
    let events = events["data"].as_array().unwrap();
    assert_eq!(events.len(), 4);
    assert_eq!(events[2]["event_type"], "sweep.received");
    assert_eq!(events[3]["event_type"], "observation.received");
    server.stop().await;
    let mut reopened = Server::start(CoreConfig::replay(directory.path())).await;
    let (_, raw) = reopened
        .request("GET", &route("fixture-сессия", "/sweeps?limit=100"), None)
        .await;
    assert_eq!(raw["data"][0], saved["data"]);
    reopened.stop().await;
}

#[tokio::test]
async fn strict_duplicate_json_and_control_body_caps_reject_without_commits() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    let response = reqwest::Client::new()
        .post(format!("{}/api/v1/sessions", server.url))
        .body(r#"{"name":"first","name":"second"}"#)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 400);
    let response = reqwest::Client::new()
        .post(format!("{}/api/v1/sessions", server.url))
        .body(vec![b'x'; 65537])
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 413);
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=100", None)
        .await;
    assert_eq!(events["data"], json!([]));
    server.stop().await;
}

#[tokio::test]
async fn body_deadline_rejects_slow_upload_before_mutation_admission() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::replay(directory.path());
    config.body_timeout = Duration::from_millis(50);
    let mut server = Server::start(config).await;
    let address = server.url.strip_prefix("http://").unwrap();
    let mut socket = tokio::net::TcpStream::connect(address).await.unwrap();
    socket.write_all(b"POST /api/v1/sessions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 64\r\nConnection: close\r\n\r\n{\"name\":").await.unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), socket.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 408"));
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=100", None)
        .await;
    assert_eq!(events["data"], json!([]));
    server.stop().await;
}

async fn raw_request(server: &Server, request: &[u8], eof: bool) -> Vec<u8> {
    let mut socket = tokio::net::TcpStream::connect(server.url.trim_start_matches("http://"))
        .await
        .unwrap();
    socket.write_all(request).await.unwrap();
    if eof {
        socket.shutdown().await.unwrap();
    }
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), socket.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    response
}

#[tokio::test]
async fn raw_framing_rejects_arbitrary_precision_lengths_and_duplicate_equal_lengths_before_hyper()
{
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    let mut digits = vec![1u8];
    for _ in 0..10000 {
        let mut carry = 0;
        for digit in &mut digits {
            let n = *digit * 2 + carry;
            *digit = n % 10;
            carry = n / 10;
        }
        if carry > 0 {
            digits.push(carry);
        }
    }
    let exact: String = digits
        .iter()
        .rev()
        .map(|digit| (b'0' + digit) as char)
        .collect();
    assert_eq!(exact.len(), 3011);
    let huge = format!(
        "POST /api/v1/sessions HTTP/1.1\r\nHost: localhost\r\nContent-Length: {exact}\r\n\r\n"
    );
    let response = raw_request(&server, huge.as_bytes(), false).await;
    assert!(
        response.starts_with(b"HTTP/1.1 413"),
        "{}",
        String::from_utf8_lossy(&response)
    );
    for framing in [
        "Content-Length: 2\r\nContent-Length: 2\r\n",
        "Transfer-Encoding: chunked\r\n",
        "Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
    ] {
        let response = raw_request(
            &server,
            format!("POST /api/v1/sessions HTTP/1.1\r\nHost: localhost\r\n{framing}\r\n{{}}")
                .as_bytes(),
            false,
        )
        .await;
        assert!(
            response.starts_with(b"HTTP/1.1 400"),
            "{}",
            String::from_utf8_lossy(&response)
        );
    }
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=10", None)
        .await;
    assert_eq!(events["data"], json!([]));
    server.stop().await;
}

#[tokio::test]
async fn raw_connection_closes_after_one_request_and_premature_body_eof_is_not_admitted() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    let response=raw_request(&server,b"GET /api/v1/health HTTP/1.1\r\nHost: localhost\r\n\r\nGET /api/v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n",false).await;
    assert_eq!(
        response
            .windows(9)
            .filter(|window| *window == b"HTTP/1.1 ")
            .count(),
        1
    );
    assert!(
        String::from_utf8_lossy(&response)
            .to_ascii_lowercase()
            .contains("connection: close")
    );
    let response = raw_request(
        &server,
        b"POST /api/v1/sessions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 24\r\n\r\n{}",
        true,
    )
    .await;
    assert!(
        response.starts_with(b"HTTP/1.1 400"),
        "{}",
        String::from_utf8_lossy(&response)
    );
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=10", None)
        .await;
    assert_eq!(events["data"], json!([]));
    server.stop().await;
}

#[tokio::test]
async fn exclusive_writer_is_acquired_before_any_archive_open_or_migration() {
    let directory = tempfile::tempdir().unwrap();
    let _lease = ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap();
    assert!(
        Core::open(CoreConfig::replay(directory.path()))
            .await
            .is_err()
    );
    assert!(!directory.path().join("sessions.sqlite3").exists());
}

#[tokio::test]
async fn authenticated_private_shutdown_returns_identity_and_releases_only_after_close() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::replay(directory.path());
    config.identity = Some(RuntimeIdentity {
        token: "A".repeat(43),
        instance_id: "synthetic-instance".into(),
    });
    let mut server = Server::start(config).await;
    let client = reqwest::Client::new();
    let endpoint = format!("{}/internal/runtime/v1/status", server.url);
    assert_eq!(client.get(&endpoint).send().await.unwrap().status(), 401);
    let response = client
        .get(&endpoint)
        .bearer_auth("A".repeat(43))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let status: Value = response.json().await.unwrap();
    assert_eq!(status["instance_id"], "synthetic-instance");
    assert_eq!(status["pid"], std::process::id());
    assert_eq!(status["writer_owned"], true);
    assert_eq!(status["preflight_complete"], true);
    let response = client
        .post(format!("{}/internal/runtime/v1/shutdown", server.url))
        .bearer_auth("A".repeat(43))
        .json(&json!({"version":1,"preserve_resume_requested":false,"reason":"shutdown"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 400);
    let response = client
        .post(format!("{}/internal/runtime/v1/shutdown", server.url))
        .bearer_auth("A".repeat(43))
        .json(&json!({"version":1,"preserve_resume_requested":true,"reason":"shutdown"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 202);
    assert_eq!(response.json::<Value>().await.unwrap()["accepted"], true);
    server.stop().await;
    let _lease = ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap();
}

#[tokio::test]
async fn event_stream_replays_only_committed_events_and_closes_on_owned_shutdown() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::replay(directory.path());
    config.body_timeout = Duration::from_millis(50);
    let mut server = Server::start(config).await;
    create(&server).await;
    let mut response = reqwest::Client::new()
        .get(format!("{}/api/v1/events?after=0", server.url))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let first = tokio::time::timeout(Duration::from_secs(2), response.chunk())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let first = String::from_utf8(first.to_vec()).unwrap();
    assert!(first.contains("id: 1"));
    assert!(first.contains("event: session.created"));
    tokio::time::sleep(Duration::from_millis(100)).await;
    server
        .request(
            "POST",
            &route("fixture-сессия", "/sweeps"),
            Some(&fixture()["sweep"]),
        )
        .await;
    let second = tokio::time::timeout(Duration::from_secs(2), response.chunk())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let second = String::from_utf8(second.to_vec()).unwrap();
    assert!(second.contains("id: 2"));
    assert!(second.contains("event: sweep.received"));
    drop(response);
    server.stop().await;
}

#[tokio::test]
async fn shutdown_joins_resources_and_closes_a_backpressured_event_socket() {
    let directory = tempfile::tempdir().unwrap();
    let archive = ori_archive::Archive::open(ori_archive::ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T00:00:00.000000Z");
    session["metadata"]["synthetic_padding"] = json!("x".repeat(8 * 1024 * 1024));
    archive.create_session(session).await.unwrap();
    archive.close().await.unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    let socket = tokio::net::TcpSocket::new_v4().unwrap();
    socket.set_recv_buffer_size(1024).unwrap();
    let address = server.url.strip_prefix("http://").unwrap().parse().unwrap();
    let mut socket = socket.connect(address).await.unwrap();
    socket
        .write_all(b"GET /api/v1/events?after=0 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        .await
        .unwrap();
    let (headers, prefix) = tokio::time::timeout(Duration::from_secs(5), async {
        let mut headers = Vec::new();
        loop {
            headers.push(socket.read_u8().await.unwrap());
            if headers.ends_with(b"\r\n\r\n") {
                break;
            }
            assert!(headers.len() < 65536);
        }
        let mut prefix = [0; 128];
        socket.read_exact(&mut prefix).await.unwrap();
        (
            String::from_utf8(headers).unwrap(),
            String::from_utf8_lossy(&prefix).into_owned(),
        )
    })
    .await
    .unwrap();
    server.core.request_shutdown();
    let mut task = server.handle.take().unwrap();
    let outcome = tokio::time::timeout(Duration::from_secs(2), &mut task).await;
    let completed = outcome.is_ok();
    // Always release our owned client and reap the service, including the RED
    // path; no abandoned listener or writer is left by the regression.
    drop(socket);
    match outcome {
        Ok(result) => result.unwrap().unwrap(),
        Err(_) => tokio::time::timeout(Duration::from_secs(5), task)
            .await
            .unwrap()
            .unwrap()
            .unwrap(),
    }
    assert!(headers.starts_with("HTTP/1.1 200"));
    assert!(prefix.contains("event: session.created"));
    assert!(
        completed,
        "a client that stopped reading held service shutdown open"
    );
}

#[tokio::test]
async fn body_upload_uses_one_actual_ten_second_deadline_despite_continuing_progress() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    let socket = tokio::net::TcpStream::connect(server.url.trim_start_matches("http://"))
        .await
        .unwrap();
    let (mut reader, mut writer) = socket.into_split();
    writer
        .write_all(
            b"POST /api/v1/sessions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4096\r\n\r\n",
        )
        .await
        .unwrap();
    let began = tokio::time::Instant::now();
    let drip = tokio::spawn(async move {
        loop {
            if writer.write_all(b" ").await.is_err() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    });
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(16), reader.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    drip.abort();
    assert!(
        response.starts_with(b"HTTP/1.1 408"),
        "{}",
        String::from_utf8_lossy(&response)
    );
    assert!(began.elapsed() >= Duration::from_millis(9500));
    assert!(began.elapsed() < Duration::from_secs(16));
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=10", None)
        .await;
    assert_eq!(events["data"], json!([]));
    server.stop().await;
}

#[tokio::test]
async fn candidate_openapi_exposes_the_frozen_pairing_and_cookie_contract() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::new(directory.path())).await;
    let (status, value) = server.request("GET", "/api/v1/openapi.json", None).await;
    assert_eq!(status, 200);
    for (path, method) in [
        ("/api/v1/security/status", "get"),
        ("/api/v1/security/pair", "post"),
        ("/api/v1/security/browser-session", "post"),
        ("/api/v1/security/credentials/current/revoke", "post"),
        ("/api/v1/security/credentials/current", "delete"),
    ] {
        assert!(
            value["paths"][path][method].is_object(),
            "missing {method} {path}"
        );
    }
    assert_eq!(
        value["components"]["securitySchemes"]["browserSession"]["name"],
        "ori_session"
    );
    assert_eq!(
        value["components"]["schemas"]["PairingInput"]["properties"]["client"]["enum"],
        json!(["browser", "native"])
    );
    server.stop().await;
}

#[tokio::test]
async fn embedded_shared_program_assets_are_served_without_runtime_filesystem_traversal() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::new(directory.path())).await;
    let client = reqwest::Client::new();
    for (path, mime) in [
        ("/platform", "text/html"),
        ("/instrument", "text/html"),
        ("/static/platform.js", "text/javascript"),
        ("/static/instrument.css", "text/css"),
    ] {
        let response = client
            .get(format!("{}{path}", server.url))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 200, "{path}");
        assert!(
            response.headers()["Content-Type"]
                .to_str()
                .unwrap()
                .starts_with(mime)
        );
        assert!(!response.bytes().await.unwrap().is_empty());
    }
    for path in [
        "/static/%2e%2e/AGENTS.md",
        "/static/platform.js/extra",
        "/static/.ori-runtime/identity.json",
    ] {
        let response = client
            .get(format!("{}{path}", server.url))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 404, "{path}");
    }
    assert!(!directory.path().join(".ori-runtime").exists());
    server.stop().await;
}

fn pcm_wav(frames: u32, rate: u32) -> Vec<u8> {
    let mut raw = Vec::new();
    raw.extend_from_slice(b"RIFF");
    raw.extend_from_slice(&(36 + frames * 2).to_le_bytes());
    raw.extend_from_slice(b"WAVEfmt ");
    raw.extend_from_slice(&16u32.to_le_bytes());
    raw.extend_from_slice(&1u16.to_le_bytes());
    raw.extend_from_slice(&1u16.to_le_bytes());
    raw.extend_from_slice(&rate.to_le_bytes());
    raw.extend_from_slice(&(rate * 2).to_le_bytes());
    raw.extend_from_slice(&2u16.to_le_bytes());
    raw.extend_from_slice(&16u16.to_le_bytes());
    raw.extend_from_slice(b"data");
    raw.extend_from_slice(&(frames * 2).to_le_bytes());
    for index in 0..frames {
        raw.extend_from_slice(&(index as u16).wrapping_mul(277).to_le_bytes());
    }
    raw
}
fn audio_payload(id: &str, raw: &[u8], frames: u32, rate: u32) -> Value {
    use base64::Engine;
    use sha2::{Digest, Sha256};
    let start = chrono::DateTime::parse_from_rfc3339("2026-11-11T12:00:00.123456Z").unwrap();
    let end = start + chrono::Duration::microseconds(i64::from(frames) * 1000000 / i64::from(rate));
    let digest = Sha256::digest(raw)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    json!({"id":id,"stream_id":"receiver+one","frequency_hz":433075000,"mode":"FM","interval":{"started_at":start.to_rfc3339_opts(chrono::SecondsFormat::Micros,true),"ended_at":end.to_rfc3339_opts(chrono::SecondsFormat::Micros,true)},"data_base64":base64::engine::general_purpose::STANDARD.encode(raw),"sha256":digest})
}

#[tokio::test]
async fn receiver_wav_bytes_summary_and_event_commit_atomically_and_reopen_unchanged() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    create(&server).await;
    let raw = pcm_wav(524288, 8000);
    let payload = audio_payload("audio-原始", &raw, 524288, 8000);
    let (status, response) = server
        .request(
            "POST",
            &route("fixture-сессия", "/audio-segments"),
            Some(&payload),
        )
        .await;
    assert_eq!(status, 201, "{response}");
    let summary = response["data"].clone();
    assert_eq!(summary["size_bytes"], raw.len());
    assert_eq!(summary["metadata"]["frame_count"], 524288);
    assert_eq!(summary["metadata"]["sample_rate"], 8000);
    assert!(summary.get("payload").is_none());
    let stream = encode_path_segment("receiver+one");
    let artifact = encode_path_segment("audio-原始");
    let item = route(
        "fixture-сессия",
        &format!("/streams/{stream}/instrument/artifacts/{artifact}"),
    );
    let content = format!("{item}/content");
    assert_eq!(
        server.request("GET", &item, None).await,
        (
            200,
            json!({"contract":ori_contracts::CONTRACT_NAME,"contract_version":1,"data":summary})
        )
    );
    let response = reqwest::get(format!("{}{content}", server.url))
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers()["Content-Type"], "audio/wav");
    assert_eq!(response.bytes().await.unwrap().as_ref(), raw);
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=10", None)
        .await;
    assert_eq!(events["data"].as_array().unwrap().len(), 2);
    assert_eq!(events["data"][1]["event_type"], "audio_segment.received");
    assert_eq!(events["data"][1]["payload"]["artifact"], summary);
    assert!(to_json_vec(&events).unwrap().len() < 4096);
    let (status, error) = server
        .request(
            "POST",
            &route("fixture-сессия", "/audio-segments"),
            Some(&payload),
        )
        .await;
    assert_eq!(status, 409);
    assert_eq!(error["error"]["code"], "audio_segment_conflict");
    let listing = route(
        "fixture-сессия",
        &format!("/streams/{stream}/instrument/artifacts"),
    );
    assert_eq!(
        server.request("GET", &listing, None).await.1["data"]["items"],
        json!([summary])
    );
    server.stop().await;
    let mut reopened = Server::start(CoreConfig::replay(directory.path())).await;
    let response = reqwest::get(format!("{}{content}", reopened.url))
        .await
        .unwrap();
    assert_eq!(response.bytes().await.unwrap().as_ref(), raw);
    reopened.stop().await;
}

#[tokio::test]
async fn incomplete_riff_trailing_chunks_pcm_width_checksum_duration_and_stream_are_rejected_without_events()
 {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    create(&server).await;
    let raw = pcm_wav(800, 8000);
    let mut bad_tail = raw.clone();
    bad_tail.extend_from_slice(b"JUNK");
    bad_tail.extend_from_slice(&1000u32.to_le_bytes());
    bad_tail.push(1);
    let length = (bad_tail.len() - 8) as u32;
    bad_tail[4..8].copy_from_slice(&length.to_le_bytes());
    let mut narrow = raw.clone();
    narrow[34..36].copy_from_slice(&9u16.to_le_bytes());
    let mut alignment = raw.clone();
    alignment[32..34].copy_from_slice(&4u16.to_le_bytes());
    let mut byte_rate = raw.clone();
    byte_rate[28..32].copy_from_slice(&8000u32.to_le_bytes());
    let mut cases = vec![
        audio_payload("bad-tail", &bad_tail, 800, 8000),
        audio_payload("bad-width", &narrow, 800, 8000),
        audio_payload("bad-alignment", &alignment, 800, 8000),
        audio_payload("bad-byte-rate", &byte_rate, 800, 8000),
    ];
    let mut checksum = audio_payload("bad-checksum", &raw, 800, 8000);
    checksum["sha256"] = json!("00".repeat(32));
    cases.push(checksum);
    let mut duration = audio_payload("bad-duration", &raw, 800, 8000);
    duration["interval"]["ended_at"] = json!("2026-11-11T12:00:00.223459Z");
    cases.push(duration);
    let mut role = audio_payload("bad-role", &raw, 800, 8000);
    role["stream_id"] = json!("spectrum/one");
    cases.push(role);
    for payload in cases {
        let (status, error) = server
            .request(
                "POST",
                &route("fixture-сессия", "/audio-segments"),
                Some(&payload),
            )
            .await;
        assert_eq!(status, 400, "{error}");
    }
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=10", None)
        .await;
    assert_eq!(events["data"].as_array().unwrap().len(), 1);
    let mut complete_tail = raw.clone();
    complete_tail.extend_from_slice(b"JUNK");
    complete_tail.extend_from_slice(&3u32.to_le_bytes());
    complete_tail.extend_from_slice(b"abc\0");
    let size = (complete_tail.len() - 8) as u32;
    complete_tail[4..8].copy_from_slice(&size.to_le_bytes());
    let valid = audio_payload("complete-unknown-chunk", &complete_tail, 800, 8000);
    assert_eq!(
        server
            .request(
                "POST",
                &route("fixture-сессия", "/audio-segments"),
                Some(&valid)
            )
            .await
            .0,
        201
    );
    let mut extended = raw[..12].to_vec();
    extended.extend_from_slice(b"fmt ");
    extended.extend_from_slice(&40u32.to_le_bytes());
    extended.extend_from_slice(&0xfffeu16.to_le_bytes());
    extended.extend_from_slice(&raw[22..36]);
    extended.extend_from_slice(&22u16.to_le_bytes());
    extended.extend_from_slice(&16u16.to_le_bytes());
    extended.extend_from_slice(&1u32.to_le_bytes());
    extended.extend_from_slice(&[1, 0, 0, 0, 0, 0, 16, 0, 128, 0, 0, 170, 0, 56, 155, 113]);
    extended.extend_from_slice(&raw[36..]);
    let size = (extended.len() - 8) as u32;
    extended[4..8].copy_from_slice(&size.to_le_bytes());
    let valid = audio_payload("extensible-pcm", &extended, 800, 8000);
    assert_eq!(
        server
            .request(
                "POST",
                &route("fixture-сессия", "/audio-segments"),
                Some(&valid)
            )
            .await
            .0,
        201
    );
    server.stop().await;
}

#[tokio::test]
async fn summary_cursors_match_python_freeze_ingestion_and_preserve_utf8_order_and_full_sweep_peak()
{
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    create(&server).await;
    let mut sweeps = Vec::new();
    for id in ["a", "z", "я"] {
        let mut sweep = fixture()["sweep"].clone();
        sweep["id"] = json!(id);
        sweep["levels"] = json!([-1.0, -1.0, -2.0, -3.0]);
        sweeps.push(sweep);
    }
    assert_eq!(
        server
            .request(
                "POST",
                &route("fixture-сессия", "/sweeps/batch"),
                Some(&json!({"items":sweeps}))
            )
            .await
            .0,
        201
    );
    let base = route("fixture-сессия", "/sweep-summaries");
    let (status, first) = server
        .request("GET", &format!("{base}?limit=1"), None)
        .await;
    assert_eq!(status, 200, "{first}");
    let item = &first["data"]["items"][0];
    assert_eq!(item["id"], "я");
    assert_eq!(item["peak_frequency_hz"], 100000001);
    for field in ["levels", "frequencies_hz", "settings"] {
        assert!(item.get(field).is_none());
    }
    let cursor = first["data"]["next_cursor"].as_str().unwrap();
    let mut later = fixture()["sweep"].clone();
    later["id"] = json!("new-after-first-page");
    later["interval"] =
        json!({"started_at":"2026-11-11T11:00:00Z","ended_at":"2026-11-11T11:00:01Z"});
    assert_eq!(
        server
            .request("POST", &route("fixture-сессия", "/sweeps"), Some(&later))
            .await
            .0,
        201
    );
    let query = format!("{base}?limit=2&after={}", encode_path_segment(cursor));
    let second = server.request("GET", &query, None).await.1;
    assert_eq!(
        second["data"]["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["z", "a"]
    );
    assert!(second["data"]["next_cursor"].is_null());
    // Generated by Python json.dumps(ensure_ascii=False,separators=(',',':')).
    let python_cursor = "eyJ2IjoxLCJmaWx0ZXIiOiJjYTQ5NmNkY2I5MmRlM2Q3ODVmMzMwOWQwOGZiYWI1YmRmMTI5NTM4MGNiMjczZGI0YjdiNWVjMzY1MzI3ODVjIiwiYm91bmRhcnkiOjMsInRpbWUiOiIyMDI2LTExLTExVDEyOjAwOjAwLjEyMzQ1NloiLCJpZCI6ItGPIn0";
    let compatible = server
        .request(
            "GET",
            &format!("{base}?limit=2&after={python_cursor}"),
            None,
        )
        .await;
    assert_eq!(compatible.0, 200);
    assert_eq!(compatible.1, second);
    for query in [
        "limit=0".into(),
        "unknown=1".into(),
        "min_hz=-1".into(),
        "min_hz=20&max_hz=10".into(),
        format!("after={cursor}&stream_id=changed"),
        "start=2026-11-11T12%3A00%3A00Z&end=2026-11-11T12%3A00%3A00Z".into(),
    ] {
        assert_eq!(
            server
                .request("GET", &format!("{base}?{query}"), None)
                .await
                .0,
            400,
            "{query}"
        );
    }
    let filtered = server
        .request(
            "GET",
            &format!(
                "{base}?end=2026-11-11T12%3A00%3A00.123456Z&min_hz=100000002&max_hz=100000002"
            ),
            None,
        )
        .await;
    assert_eq!(filtered.1["data"]["items"].as_array().unwrap().len(), 1);
    assert_eq!(filtered.1["data"]["items"][0]["id"], "new-after-first-page");
    server.stop().await;
}

#[tokio::test]
async fn session_analytics_use_committed_archive_counts_and_keep_raw_receiver_units() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    create(&server).await;
    server
        .request(
            "POST",
            &route("fixture-сессия", "/sweeps"),
            Some(&fixture()["sweep"]),
        )
        .await;
    server
        .request(
            "POST",
            &route("fixture-сессия", "/observations"),
            Some(&fixture()["observation"]),
        )
        .await;
    let (status, value) = server
        .request("GET", &route("fixture-сессия", "/analytics/summary"), None)
        .await;
    assert_eq!(status, 200, "{value}");
    assert_eq!(value["data"]["sweeps"]["count"], 1);
    assert_eq!(value["data"]["observations"]["count"], 1);
    assert_eq!(
        value["data"]["observations"]["level_units"],
        json!(["s_meter_count"])
    );
    assert_eq!(value["data"]["observations"]["latest"]["level"], 137.0);
    server.stop().await;
}

#[tokio::test]
async fn capabilities_retain_public_client_fields_and_only_enable_implemented_features() {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    let (_, value) = server.request("GET", "/api/v1/capabilities", None).await;
    let caps = &value["data"];
    assert_eq!(caps["hardware_ownership"], "core");
    assert_eq!(caps["session_storage"], "sqlite");
    assert_eq!(caps["persistent_archive"], true);
    assert_eq!(
        caps["clients"],
        json!(["raspberry-pi-webui", "windows-gui", "macos-swiftui"])
    );
    for feature in [
        "sweep_summaries",
        "audio_segment_ingest",
        "session_analytics",
    ] {
        assert_eq!(caps["features"][feature], true);
    }
    assert_eq!(caps["features"]["hardware_control"], true);
    assert_eq!(caps["features"]["explicit_serial_adapters"], false);
    assert_eq!(caps["candidate"]["native_connector_enabled"], false);
    assert_eq!(caps["features"]["tinysa_ultra_hardware_validated"], false);
    assert_eq!(caps["candidate"]["compatibility_complete"], false);
    server.stop().await;
}

#[tokio::test]
async fn startup_resets_runtime_streams_and_interrupts_active_investigations_without_replaying_queued()
 {
    use ori_archive::{Archive, ArchiveConfig, Mutation};
    let directory = tempfile::tempdir().unwrap();
    let database = directory.path().join("sessions.sqlite3");
    let archive = Archive::open(ArchiveConfig::new(&database)).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    session["streams"][0]["state"] = json!("running");
    archive.create_session(session).await.unwrap();
    let candidate = json!({"id":"candidate-one","session_id":"fixture-сессия","source_stream_id":"spectrum/one","interval":{"started_at":"2026-11-11T12:00:01Z","ended_at":"2026-11-11T12:00:02Z"},"center_frequency_hz":100000003,"level_unit":"dBm","state":"investigating"});
    let investigation = |id, state| json!({"id":id,"candidate_id":"candidate-one","target_stream_id":"receiver+one","target_frequency_hz":100000003,"state":state,"priority":0,"created_at":"2026-11-11T12:00:03Z"});
    archive
        .apply(vec![
            Mutation::Candidate {
                value: candidate.clone(),
                event: None,
            },
            Mutation::Investigation {
                session_id: "fixture-сессия".into(),
                value: investigation("active", "listening"),
                candidate: candidate.clone(),
                event: None,
            },
            Mutation::Investigation {
                session_id: "fixture-сессия".into(),
                value: investigation("queued", "queued"),
                candidate,
                event: None,
            },
        ])
        .await
        .unwrap();
    archive.close().await.unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    let (_, session) = server
        .request("GET", &route("fixture-сессия", ""), None)
        .await;
    assert_eq!(session["data"]["streams"][0]["state"], "disconnected");
    let (_, active) = server
        .request(
            "GET",
            &route("fixture-сессия", "/investigations/active"),
            None,
        )
        .await;
    assert_eq!(active["data"]["state"], "failed");
    assert_eq!(
        active["data"]["error"],
        "Исследование прервано перезапуском службы"
    );
    let (_, queued) = server
        .request(
            "GET",
            &route("fixture-сессия", "/investigations/queued"),
            None,
        )
        .await;
    assert_eq!(queued["data"]["state"], "queued");
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=0&limit=100", None)
        .await;
    assert_eq!(
        events["data"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|e| e["event_type"] == "investigation.interrupted")
            .count(),
        1
    );
    server.stop().await;
}

#[tokio::test]
async fn stopping_a_session_atomically_cancels_queued_investigations_and_publishes_in_order() {
    use ori_archive::{Archive, ArchiveConfig, Mutation};
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2025-01-01T12:00:00Z");
    session["started_at"] = json!("2025-01-01T12:00:01Z");
    session["state"] = json!("running");
    archive.create_session(session).await.unwrap();
    let candidate = json!({"id":"queued-candidate","session_id":"fixture-сессия","source_stream_id":"spectrum/one","interval":{"started_at":"2025-01-01T12:00:01Z","ended_at":"2025-01-01T12:00:02Z"},"center_frequency_hz":100000003,"level_unit":"dBm","state":"queued"});
    let investigation = json!({"id":"queued-job","candidate_id":"queued-candidate","target_stream_id":"receiver+one","target_frequency_hz":100000003,"state":"queued","priority":0,"created_at":"2025-01-01T12:00:03Z"});
    archive
        .apply(vec![Mutation::Investigation {
            session_id: "fixture-сессия".into(),
            value: investigation,
            candidate,
            event: None,
        }])
        .await
        .unwrap();
    archive.close().await.unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    assert_eq!(
        server
            .request("POST", &route("fixture-сессия", "/stop"), Some(&json!({})))
            .await
            .0,
        200
    );
    let (_, job) = server
        .request(
            "GET",
            &route("fixture-сессия", "/investigations/queued-job"),
            None,
        )
        .await;
    assert_eq!(job["data"]["state"], "cancelled");
    let (_, events) = server
        .request("GET", "/api/v1/events/history?after=1&limit=100", None)
        .await;
    assert_eq!(events["data"][0]["event_type"], "session.stopped");
    assert_eq!(events["data"][1]["event_type"], "investigation.cancelled");
    server.stop().await;
}

#[tokio::test]
async fn malformed_autostart_metadata_is_rejected_without_consuming_intent() {
    use ori_archive::{Archive, ArchiveConfig};
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00Z");
    archive.create_session(session).await.unwrap();
    let project = json!({"id":"guard-project","project_id":"guard-project","created_at":"2026-11-11T12:00:03Z","session_ids":["fixture-сессия"],"active_session_id":"fixture-сессия","state":"armed","coverage":"wide","autostart":true,"resume_requested":true});
    archive
        .put_monitoring("project", project.clone())
        .await
        .unwrap();
    archive.close().await.unwrap();
    assert!(
        Core::open(CoreConfig::replay(directory.path()))
            .await
            .is_err(),
        "candidate claimed readiness with invalid guard recovery metadata"
    );
    let archive = Archive::open(ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    assert_eq!(
        archive
            .monitoring("project", "guard-project")
            .await
            .unwrap()
            .unwrap(),
        project
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn nested_archive_override_cannot_bypass_a_different_data_directory_owner() {
    let directory = tempfile::tempdir().unwrap();
    let nested = directory.path().join("nested");
    std::fs::create_dir(&nested).unwrap();
    let _owner = ori_runtime::lease::WriterLease::acquire(&nested).unwrap();
    let mut configuration = CoreConfig::replay(directory.path());
    configuration.database = nested.join("sessions.sqlite3");
    assert!(Core::open(configuration).await.is_err());
    assert!(!nested.join("sessions.sqlite3").exists());
}

#[tokio::test]
async fn supervised_candidate_cannot_silently_select_another_archive_filename() {
    let directory = tempfile::tempdir().unwrap();
    let mut configuration = CoreConfig::replay(directory.path());
    configuration.database = directory.path().join("other.sqlite");
    configuration.identity = Some(RuntimeIdentity {
        token: "A".repeat(43),
        instance_id: "supervised".into(),
    });
    assert!(Core::open(configuration).await.is_err());
    assert!(!directory.path().join("other.sqlite").exists());
}

#[tokio::test]
async fn measurement_payload_errors_precede_missing_session_and_not_found_preserves_exact_requested_id()
 {
    let directory = tempfile::tempdir().unwrap();
    let mut server = Server::start(CoreConfig::replay(directory.path())).await;
    for kind in ["sweeps", "observations"] {
        for (payload, index) in [
            (json!({"items":[false]}), 0),
            (json!({"items":[{},false]}), 1),
        ] {
            let (status, response) = server
                .request(
                    "POST",
                    &route("missing-é", &format!("/{kind}/batch")),
                    Some(&payload),
                )
                .await;
            assert_eq!(status, 400, "{response}");
            assert_eq!(response["error"]["code"], "invalid_request");
            assert_eq!(
                response["error"]["message"],
                format!("Элемент пакета {index} должен быть JSON-объектом")
            );
        }
        let (status, response) = server
            .request(
                "POST",
                &route("missing-é", &format!("/{kind}")),
                Some(&json!({})),
            )
            .await;
        assert_eq!(status, 400, "{response}");
        assert_eq!(response["error"]["code"], "missing_field");
        assert_eq!(response["error"]["message"], "Отсутствует поле: stream_id");
    }
    let (status, response) = server
        .request("GET", &route("session-e\u{301}", ""), None)
        .await;
    assert_eq!(status, 404);
    assert_eq!(response["error"]["code"], "session_not_found");
    assert_eq!(
        response["error"]["message"],
        "Сеанс не найден: session-e\u{301}"
    );
    let (status, response) = server
        .request("POST", &route("missing-é", "/stop"), Some(&json!({})))
        .await;
    assert_eq!(status, 404, "{response}");
    assert_eq!(response["error"]["code"], "session_not_found");
    assert_eq!(response["error"]["message"], "Сеанс не найден: missing-é");
    let (_, events) = server
        .request("GET", "/api/v1/events/history?limit=100", None)
        .await;
    assert_eq!(events["data"], json!([]));
    server.stop().await;
}

#[cfg(unix)]
#[tokio::test]
async fn a_hardlinked_sqlite_file_cannot_create_two_independent_writer_lease_domains() {
    use ori_archive::{Archive, ArchiveConfig};
    let first = tempfile::tempdir().unwrap();
    let second = tempfile::tempdir().unwrap();
    let path = first.path().join("sessions.sqlite3");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.close().await.unwrap();
    std::fs::hard_link(&path, second.path().join("sessions.sqlite3")).unwrap();
    assert!(Core::open(CoreConfig::replay(first.path())).await.is_err());
    assert!(Core::open(CoreConfig::replay(second.path())).await.is_err());
}

#[tokio::test]
async fn database_preflight_does_not_claim_that_a_listener_is_serving_yet() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::replay(directory.path()))
        .await
        .unwrap();
    let status = core.runtime_status();
    assert!(status.preflight_complete);
    assert!(status.writer_owned);
    assert!(!status.ready);
    core.close().await.unwrap();
}
