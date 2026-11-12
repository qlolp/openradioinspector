#![cfg(unix)]
use ori_core::{Core, CoreConfig, LanSecurityConfig};
use ori_runtime::security::TlsIdentity;
use reqwest::{Client, Response};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use tokio::{net::TcpListener, task::JoinHandle};
struct LanServer {
    core: Arc<Core>,
    client: Client,
    url: String,
    origin: String,
    task: Option<JoinHandle<ori_core::Result<()>>>,
    _directory: tempfile::TempDir,
}
impl LanServer {
    async fn start() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let security = directory.path().join("security");
        std::fs::create_dir(&security).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&security, std::fs::Permissions::from_mode(0o700)).unwrap();
        let identity = TlsIdentity::create(&security, vec!["127.0.0.1".into()]).unwrap();
        let client = Client::builder()
            .tls_certs_only([reqwest::Certificate::from_pem(
                identity.trust_anchor_pem().as_bytes(),
            )
            .unwrap()])
            .build()
            .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let authority = listener.local_addr().unwrap().to_string();
        let url = format!("https://{authority}");
        let mut configuration = CoreConfig::replay(directory.path());
        configuration.lan_security = Some(LanSecurityConfig {
            directory: security,
            hosts: vec![authority],
            origins: vec![url.clone()],
        });
        let core = Core::open(configuration).await.unwrap();
        let serving = core.clone();
        let task = tokio::spawn(async move { serving.serve_tls(listener).await });
        Self {
            core,
            client,
            url: url.clone(),
            origin: url,
            task: Some(task),
            _directory: directory,
        }
    }
    async fn pair(&self, code: &str, client: &str) -> Response {
        self.client
            .post(format!("{}/api/v1/security/pair", self.url))
            .header("Origin", &self.origin)
            .json(&json!({"pairing_code":code,"label":"synthetic client","client":client}))
            .send()
            .await
            .unwrap()
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
impl Drop for LanServer {
    fn drop(&mut self) {
        self.core.request_shutdown();
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}
fn cookie(response: &Response) -> String {
    response
        .headers()
        .get("Set-Cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned()
}

async fn raw_tls(server: &LanServer, request: &[u8], eof: bool) -> Vec<u8> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let identity = TlsIdentity::load(&server._directory.path().join("security")).unwrap();
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls_pemfile::certs(&mut identity.trust_anchor_pem().as_bytes()) {
        roots.add(cert.unwrap()).unwrap();
    }
    let config = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(roots)
    .with_no_client_auth();
    let socket = tokio::net::TcpStream::connect(server.url.trim_start_matches("https://"))
        .await
        .unwrap();
    let mut socket = tokio_rustls::TlsConnector::from(Arc::new(config))
        .connect(
            rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(),
            socket,
        )
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
async fn verified_tls_head_gate_rejects_exact_two_to_10000_duplicate_lengths_and_transfer_encoding()
{
    let mut server = LanServer::start().await;
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
    let request = format!(
        "POST /api/v1/sessions HTTP/1.1\r\nHost: {}\r\nContent-Length: {exact}\r\n\r\n",
        server.url.trim_start_matches("https://")
    );
    let response = raw_tls(&server, request.as_bytes(), false).await;
    assert!(
        response.starts_with(b"HTTP/1.1 413"),
        "{}",
        String::from_utf8_lossy(&response)
    );
    for framing in [
        "Content-Length: 2\r\nContent-Length: 2\r\n",
        "Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
        "Transfer-Encoding: chunked\r\n",
    ] {
        let request = format!(
            "POST /api/v1/sessions HTTP/1.1\r\nHost: {}\r\n{framing}\r\n{{}}",
            server.url.trim_start_matches("https://")
        );
        let response = raw_tls(&server, request.as_bytes(), false).await;
        assert!(
            response.starts_with(b"HTTP/1.1 400"),
            "{}",
            String::from_utf8_lossy(&response)
        );
    }
    let code = server.core.begin_pairing_window(60, 3).unwrap();
    let paired = server
        .pair(&code, "native")
        .await
        .json::<Value>()
        .await
        .unwrap();
    let token = paired["data"]["token"].as_str().unwrap();
    let request = format!(
        "POST /api/v1/sessions HTTP/1.1\r\nHost: {}\r\nAuthorization: Bearer {token}\r\nContent-Length: 24\r\n\r\n{{}}",
        server.url.trim_start_matches("https://")
    );
    let response = raw_tls(&server, request.as_bytes(), true).await;
    assert!(
        response.starts_with(b"HTTP/1.1 400"),
        "{}",
        String::from_utf8_lossy(&response)
    );
    server.stop().await;
}

#[tokio::test]
async fn lan_requires_verified_tls_and_auth_before_exposing_archive_or_accepting_mutations() {
    let mut server = LanServer::start().await;
    assert!(
        Client::new()
            .get(format!("{}/api/v1/security/status", server.url))
            .send()
            .await
            .is_err(),
        "untrusted local CA was accepted"
    );
    let response = server
        .client
        .get(format!("{}/api/v1/security/status", server.url))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let status: Value = response.json().await.unwrap();
    assert_eq!(status["data"]["mode"], "lan_tls");
    assert_eq!(status["data"]["pairing_required"], true);
    assert_eq!(
        server
            .client
            .get(format!("{}/api/v1/health", server.url))
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    let response = server
        .client
        .post(format!("{}/api/v1/security/pair", server.url))
        .header("Origin", &server.origin)
        .json(&json!({"pairing_code":"A".repeat(43),"label":"client"}))
        .send()
        .await
        .unwrap();
    assert!(!response.status().is_success());
    server.stop().await;
}

#[tokio::test]
async fn unpaired_browser_can_load_shared_program_bytes_while_private_api_requires_pairing() {
    let mut server = LanServer::start().await;
    let response = server
        .client
        .get(format!("{}/platform", server.url))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    assert!(response.text().await.unwrap().contains("platform.js"));
    assert_eq!(
        server
            .client
            .get(format!("{}/api/v1/health", server.url))
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    assert_eq!(
        server
            .client
            .get(format!("{}/static/platform.js", server.url))
            .header("Host", "untrusted.invalid")
            .send()
            .await
            .unwrap()
            .status(),
        403
    );
    server.stop().await;
}

#[tokio::test]
async fn percent_encoded_api_prefix_cannot_bypass_authentication_on_reads_or_writes() {
    let mut server = LanServer::start().await;
    for prefix in [
        "/a%70i/v1/sessions",
        "/%61pi/v1/sessions",
        "/api/%76%31/sessions",
    ] {
        let response = server
            .client
            .post(format!("{}{prefix}", server.url))
            .json(&json!({"id":"unauthenticated-encoded","name":"must never commit","streams":[]}))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 401, "POST {prefix}");
        let response = server
            .client
            .get(format!("{}{prefix}", server.url))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 401, "GET {prefix}");
    }
    let code = server.core.begin_pairing_window(60, 3).unwrap();
    let paired = server
        .pair(&code, "native")
        .await
        .json::<Value>()
        .await
        .unwrap();
    let token = paired["data"]["token"].as_str().unwrap();
    let response = server
        .client
        .get(format!("{}/api/v1/sessions", server.url))
        .bearer_auth(token)
        .send()
        .await
        .unwrap()
        .json::<Value>()
        .await
        .unwrap();
    assert_eq!(response["data"], json!([]));
    server.stop().await;
}

#[tokio::test]
async fn browser_pair_issues_only_http_only_cookie_and_enforces_origin_and_revocation_for_sse() {
    let mut server = LanServer::start().await;
    let code = server.core.begin_pairing_window(300, 5).unwrap();
    let response = server.pair(&code, "browser").await;
    assert_eq!(response.status(), 200);
    let set_cookie = response.headers()["Set-Cookie"]
        .to_str()
        .unwrap()
        .to_owned();
    for flag in ["Secure", "HttpOnly", "SameSite=Strict", "Path=/api"] {
        assert!(set_cookie.contains(flag));
    }
    let cookie = cookie(&response);
    let paired: Value = response.json().await.unwrap();
    assert_eq!(paired["data"]["credential"], "browser_cookie");
    assert!(paired["data"].get("token").is_none());
    let endpoint = format!("{}/api/v1/sessions", server.url);
    assert_eq!(
        server
            .client
            .post(&endpoint)
            .header("Cookie", &cookie)
            .json(&json!({"name":"blocked"}))
            .send()
            .await
            .unwrap()
            .status(),
        403
    );
    let response = server
        .client
        .post(&endpoint)
        .header("Cookie", &cookie)
        .header("Origin", &server.origin)
        .json(&json!({"name":"paired"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 201);
    let mut stream = server
        .client
        .get(format!("{}/api/v1/events", server.url))
        .header("Cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(stream.status(), 200);
    assert!(stream.chunk().await.unwrap().is_some());
    let response = server
        .client
        .post(format!(
            "{}/api/v1/security/credentials/current/revoke",
            server.url
        ))
        .header("Cookie", &cookie)
        .header("Origin", &server.origin)
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    assert!(
        response.headers()["Set-Cookie"]
            .to_str()
            .unwrap()
            .contains("Max-Age=0")
    );
    assert_eq!(
        server
            .client
            .get(&endpoint)
            .header("Cookie", &cookie)
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    assert!(
        tokio::time::timeout(Duration::from_secs(2), stream.chunk())
            .await
            .unwrap()
            .unwrap()
            .is_none(),
        "revoked SSE remained authorized"
    );
    server.stop().await;
}

#[tokio::test]
async fn native_bearer_and_browser_session_exchange_share_one_revocable_hashed_credential() {
    let mut server = LanServer::start().await;
    let code = server.core.begin_pairing_window(300, 5).unwrap();
    let response = server.pair(&code, "native").await;
    assert_eq!(response.status(), 200);
    let value: Value = response.json().await.unwrap();
    let token = value["data"]["token"].as_str().unwrap();
    assert_eq!(value["data"]["credential"], "bearer");
    let endpoint = format!("{}/api/v1/health", server.url);
    assert_eq!(
        server
            .client
            .get(&endpoint)
            .bearer_auth(token)
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    let exchange = server
        .client
        .post(format!("{}/api/v1/security/browser-session", server.url))
        .bearer_auth(token)
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    assert_eq!(exchange.status(), 200);
    let cookie = cookie(&exchange);
    let value: Value = exchange.json().await.unwrap();
    assert_eq!(value["data"]["credential"], "browser_cookie");
    assert!(value["data"].get("token").is_none());
    assert_eq!(
        server
            .client
            .get(&endpoint)
            .header("Cookie", &cookie)
            .bearer_auth(token)
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    let bad_host = server
        .client
        .get(&endpoint)
        .bearer_auth(token)
        .header("Host", "attacker.invalid")
        .send()
        .await
        .unwrap();
    assert_eq!(bad_host.status(), 403);
    let bad_origin = server
        .client
        .get(&endpoint)
        .bearer_auth(token)
        .header("Origin", "https://attacker.invalid")
        .send()
        .await
        .unwrap();
    assert_eq!(bad_origin.status(), 403);
    let duplicate = server
        .client
        .get(&endpoint)
        .header("Cookie", format!("{cookie}; {cookie}"))
        .send()
        .await
        .unwrap();
    assert_eq!(duplicate.status(), 401);
    let revoke = server
        .client
        .delete(format!(
            "{}/api/v1/security/credentials/current",
            server.url
        ))
        .bearer_auth(token)
        .send()
        .await
        .unwrap();
    assert_eq!(revoke.status(), 200);
    assert_eq!(
        server
            .client
            .get(&endpoint)
            .header("Cookie", &cookie)
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    assert_eq!(
        server
            .client
            .get(&endpoint)
            .bearer_auth(token)
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    let persisted =
        std::fs::read_to_string(server._directory.path().join("security/credentials.json"))
            .unwrap();
    assert!(!persisted.contains(token));
    server.stop().await;
}

#[tokio::test]
async fn exhausted_expired_and_single_use_pairing_codes_do_not_create_more_credentials() {
    let mut server = LanServer::start().await;
    let code = server.core.begin_pairing_window(300, 2).unwrap();
    assert_eq!(server.pair(&"B".repeat(43), "browser").await.status(), 401);
    assert_eq!(server.pair(&"B".repeat(43), "browser").await.status(), 401);
    assert_eq!(server.pair(&code, "browser").await.status(), 429);
    let code = server.core.begin_pairing_window(1, 3).unwrap();
    tokio::time::sleep(Duration::from_millis(1100)).await;
    assert_eq!(server.pair(&code, "browser").await.status(), 429);
    let code = server.core.begin_pairing_window(300, 3).unwrap();
    assert_eq!(server.pair(&code, "native").await.status(), 200);
    assert_eq!(server.pair(&code, "native").await.status(), 429);
    server.stop().await;
}

#[tokio::test]
async fn stale_browser_cookie_does_not_block_explicit_repairing_with_a_new_local_code() {
    let mut server = LanServer::start().await;
    let code = server.core.begin_pairing_window(300, 5).unwrap();
    let response = server.pair(&code, "browser").await;
    let old = cookie(&response);
    server
        .client
        .post(format!(
            "{}/api/v1/security/credentials/current/revoke",
            server.url
        ))
        .header("Cookie", &old)
        .header("Origin", &server.origin)
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    let code = server.core.begin_pairing_window(300, 5).unwrap();
    let response = server
        .client
        .post(format!("{}/api/v1/security/pair", server.url))
        .header("Cookie", old)
        .header("Origin", &server.origin)
        .json(&json!({"pairing_code":code,"label":"repair","client":"browser"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    server.stop().await;
}
#[tokio::test]
async fn malformed_pairing_client_type_does_not_consume_the_local_window() {
    let mut server = LanServer::start().await;
    let code = server.core.begin_pairing_window(300, 5).unwrap();
    let response = server
        .client
        .post(format!("{}/api/v1/security/pair", server.url))
        .header("Origin", &server.origin)
        .json(&json!({"pairing_code":code,"label":"client","client":null}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 400);
    assert_eq!(server.pair(&code, "native").await.status(), 200);
    server.stop().await;
}
#[tokio::test]
async fn an_asserted_forwarded_protocol_cannot_turn_a_plain_router_into_tls() {
    let mut server = LanServer::start().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let router = server.core.router();
    let task = tokio::spawn(async move {
        axum::serve(
            listener,
            router.into_make_service_with_connect_info::<std::net::SocketAddr>(),
        )
        .await
        .unwrap();
    });
    let response = server
        .client
        .get(format!("http://{address}/api/v1/security/status"))
        .header("X-Forwarded-Proto", "https")
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 403);
    task.abort();
    server.stop().await;
}
#[tokio::test]
async fn local_owner_socket_opens_a_bounded_pair_window_without_a_lan_begin_route() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let mut server = LanServer::start().await;
    server
        .client
        .get(format!("{}/api/v1/security/status", server.url))
        .send()
        .await
        .unwrap();
    let path = server
        ._directory
        .path()
        .join(".ori-runtime/core-admin.sock");
    assert!(
        path.exists(),
        "TLS service did not provide its private operator pairing interface"
    );
    let mut socket = tokio::net::UnixStream::connect(path).await.unwrap();
    socket
        .write_all(br#"{"version":1,"command":"pairing.start","ttl_seconds":60,"attempts":3}"#)
        .await
        .unwrap();
    socket.shutdown().await.unwrap();
    let mut bytes = Vec::new();
    socket.read_to_end(&mut bytes).await.unwrap();
    let reply: Value = serde_json::from_slice(&bytes).unwrap();
    let code = reply["pairing_code"].as_str().unwrap();
    assert_eq!(server.pair(code, "native").await.status(), 200);
    server.stop().await;
}
