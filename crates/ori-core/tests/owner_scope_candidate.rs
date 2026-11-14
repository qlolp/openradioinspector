//! A privately verified owner must remain the peer of every HTTP/stream request.
use ori_core::{Core, CoreConfig, RuntimeIdentity};
use serde_json::{Value, json};
use std::{net::SocketAddr, path::Path, sync::Arc, time::Duration};
use tokio::{net::TcpListener, task::JoinHandle};

struct Server {
    core: Arc<Core>,
    address: SocketAddr,
    task: Option<JoinHandle<ori_core::Result<()>>>,
}
impl Server {
    async fn open(data: &Path, identity: Option<&str>, address: Option<SocketAddr>) -> Self {
        std::fs::create_dir_all(data).unwrap();
        let mut config = CoreConfig::live(data);
        config.identity = identity.map(|id| RuntimeIdentity {
            token: "T".repeat(32),
            instance_id: id.to_owned(),
        });
        let core = Core::open(config).await.unwrap();
        let listener = TcpListener::bind(address.unwrap_or_else(|| "127.0.0.1:0".parse().unwrap()))
            .await
            .unwrap();
        let address = listener.local_addr().unwrap();
        let running = core.clone();
        Self {
            core,
            address,
            task: Some(tokio::spawn(async move { running.serve(listener).await })),
        }
    }
    async fn request(&self, method: &str, path: &str, body: Option<&Value>) -> (u16, Value) {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap();
        let mut request = client.request(
            method.parse().unwrap(),
            format!("http://{}{path}", self.address),
        );
        if let Some(body) = body {
            request = request.body(ori_contracts::to_json_vec(body).unwrap());
        }
        let response = request.send().await.unwrap();
        let status = response.status().as_u16();
        (
            status,
            ori_contracts::parse_json_value(&response.bytes().await.unwrap(), 16 * 1024 * 1024)
                .unwrap(),
        )
    }
    async fn close(&mut self) {
        self.core.request_shutdown();
        if let Some(task) = self.task.take() {
            tokio::time::timeout(Duration::from_secs(5), task)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
        }
        self.core.close().await.unwrap();
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
    ori_contracts::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        65536,
    )
    .unwrap()
}

#[tokio::test]
async fn owner_namespace_rejects_replacement_at_the_same_port_before_any_archive_mutation() {
    let root = tempfile::tempdir().unwrap();
    let first = root.path().join("A");
    let second = root.path().join("B");
    let mut a = Server::open(&first, Some("owner-A"), None).await;
    let health = a
        .request("GET", "/api/v1/instances/owner-A/health", None)
        .await;
    let security = a
        .request("GET", "/api/v1/instances/owner-A/security/status", None)
        .await;
    a.close().await;
    assert_eq!(health.0, 200, "{}", health.1);
    assert_eq!(security.0, 200, "{}", security.1);
    assert_eq!(security.1["data"]["mode"], "legacy_loopback");
    assert_eq!(health.1["data"]["runtime_instance_id"], "owner-A");
    let mut b = Server::open(&second, Some("owner-B"), Some(a.address)).await;
    let refused = b
        .request(
            "POST",
            "/api/v1/instances/owner-A/sessions",
            Some(&fixture()["session"]),
        )
        .await;
    let contents = b.request("GET", "/api/v1/sessions", None).await;
    let wrong_stream = b
        .request("GET", "/api/v1/instances/owner-A/events?after=0", None)
        .await;
    let correct = b
        .request(
            "POST",
            "/api/v1/instances/owner-B/sessions",
            Some(&fixture()["session"]),
        )
        .await;
    b.close().await;
    assert_eq!(refused.0, 409, "{}", refused.1);
    assert_eq!(refused.1["error"]["code"], "runtime_owner_changed");
    assert_eq!(contents.1["data"], json!([]));
    assert_eq!(wrong_stream.0, 409);
    assert_eq!(correct.0, 201, "{}", correct.1);
}

#[tokio::test]
async fn scoped_measurements_keep_the_large_body_budget_and_an_unowned_peer_cannot_accept_them() {
    let root = tempfile::tempdir().unwrap();
    let mut server = Server::open(root.path(), Some("owner-B"), None).await;
    let fixture = fixture();
    assert_eq!(
        server
            .request("POST", "/api/v1/sessions", Some(&fixture["session"]))
            .await
            .0,
        201
    );
    let session = ori_contracts::encode_path_segment(fixture["session"]["id"].as_str().unwrap());
    let root_path = format!("/api/v1/instances/owner-B/sessions/{session}");
    server
        .request("POST", &format!("{root_path}/start"), Some(&json!({})))
        .await;
    let mut sample = fixture["sweep"].clone();
    sample["settings"]["opaque_padding"] = json!("x".repeat(70000));
    let stored = server
        .request("POST", &format!("{root_path}/sweeps"), Some(&sample))
        .await;
    let saved = server
        .request("GET", &format!("{root_path}/sweeps?limit=100"), None)
        .await;
    let private = server
        .request(
            "POST",
            "/api/v1/instances/owner-B/internal/runtime/v1/shutdown",
            Some(&json!({})),
        )
        .await;
    let still_ready = server.core.runtime_status().ready;
    server.close().await;
    assert!(
        still_ready,
        "owner namespace must never expose private shutdown"
    );
    assert_eq!(stored.0, 201, "{}", stored.1);
    assert_eq!(saved.1["data"][0]["settings"], sample["settings"]);
    assert_eq!(private.0, 501);
    let unowned_data = root.path().join("unowned");
    let mut unowned = Server::open(&unowned_data, None, None).await;
    let denied = unowned
        .request(
            "POST",
            "/api/v1/instances/owner-B/sessions",
            Some(&fixture["session"]),
        )
        .await;
    let contents = unowned.request("GET", "/api/v1/sessions", None).await;
    unowned.close().await;
    assert_ne!(denied.0, 201);
    assert_eq!(contents.1["data"], json!([]));
}

#[tokio::test]
async fn owner_scope_covers_sse_and_protobuf_websocket() {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    let root = tempfile::tempdir().unwrap();
    let mut server = Server::open(root.path(), Some("owner-stream"), None).await;
    let created = server
        .request(
            "POST",
            "/api/v1/instances/owner-stream/sessions",
            Some(&fixture()["session"]),
        )
        .await;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap();
    let response = client
        .get(format!(
            "http://{}/api/v1/instances/owner-stream/events?after=0",
            server.address
        ))
        .send()
        .await
        .unwrap();
    let sse_status = response.status();
    let sse_type = response
        .headers()
        .get("content-type")
        .unwrap()
        .to_str()
        .unwrap()
        .to_owned();
    drop(response);
    let mut request = format!(
        "ws://{}/api/v1/instances/owner-stream/events/ws?after=0",
        server.address
    )
    .into_client_request()
    .unwrap();
    request.headers_mut().insert(
        "Sec-WebSocket-Protocol",
        ori_stream::SUBPROTOCOL.parse().unwrap(),
    );
    let (socket, upgraded) = tokio_tungstenite::connect_async(request).await.unwrap();
    let protocol = upgraded
        .headers()
        .get("Sec-WebSocket-Protocol")
        .unwrap()
        .to_str()
        .unwrap()
        .to_owned();
    drop(socket);
    let mut wrong = format!(
        "ws://{}/api/v1/instances/old-owner/events/ws?after=0",
        server.address
    )
    .into_client_request()
    .unwrap();
    wrong.headers_mut().insert(
        "Sec-WebSocket-Protocol",
        ori_stream::SUBPROTOCOL.parse().unwrap(),
    );
    let refused = tokio_tungstenite::connect_async(wrong).await;
    server.close().await;
    assert_eq!(created.0, 201);
    assert_eq!(sse_status, 200);
    assert!(sse_type.starts_with("text/event-stream"));
    assert_eq!(protocol, ori_stream::SUBPROTOCOL);
    assert!(
        matches!(refused,Err(tokio_tungstenite::tungstenite::Error::Http(response)) if response.status()==409)
    );
}
