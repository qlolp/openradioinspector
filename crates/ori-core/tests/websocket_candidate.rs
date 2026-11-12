//! Real owned TCP upgrade/replay; application ACK is after consumer application.
use futures_util::{SinkExt, StreamExt};
use ori_archive::{Archive, ArchiveConfig, EventDraft, Mutation};
use ori_core::{Core, CoreConfig};
use serde_json::json;
use std::{sync::Arc, time::Duration};
use tokio::{
    net::{TcpListener, TcpStream},
    task::JoinHandle,
};
use tokio_tungstenite::{
    MaybeTlsStream, WebSocketStream,
    tungstenite::{Message, client::IntoClientRequest, protocol::frame::coding::CloseCode},
};
type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;
struct Server {
    core: Arc<Core>,
    url: String,
    task: Option<JoinHandle<ori_core::Result<()>>>,
    _directory: tempfile::TempDir,
}
impl Server {
    async fn start(events: usize) -> Self {
        let directory = tempfile::tempdir().unwrap();
        let archive = Archive::open(ArchiveConfig::new(
            directory.path().join("sessions.sqlite3"),
        ))
        .unwrap();
        for first in (0..events).step_by(100) {
            let mutations = (first..events.min(first+100)).map(|index| Mutation::Event(EventDraft::now("fixture", None, json!({"index":index,"opaque":{"$serde_json::private::Number":"metadata","音":"original"}})))).collect();
            archive.apply(mutations).await.unwrap();
        }
        archive.close().await.unwrap();
        Self::open(directory).await
    }
    async fn open(directory: tempfile::TempDir) -> Self {
        let core = Core::open(CoreConfig::replay(directory.path()))
            .await
            .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("ws://{}", listener.local_addr().unwrap());
        let own = core.clone();
        let task = tokio::spawn(async move { own.serve(listener).await });
        Self {
            core,
            url,
            task: Some(task),
            _directory: directory,
        }
    }
    async fn connect(&self, after: u64) -> Socket {
        let mut request = format!(
            "{}{path}?after={after}",
            self.url,
            path = ori_stream::WS_PATH
        )
        .into_client_request()
        .unwrap();
        request.headers_mut().insert(
            "Sec-WebSocket-Protocol",
            ori_stream::SUBPROTOCOL.parse().unwrap(),
        );
        let (socket, response) = tokio_tungstenite::connect_async_with_config(request, None, true)
            .await
            .unwrap();
        assert_eq!(response.status(), 101);
        assert_eq!(
            response.headers()["Sec-WebSocket-Protocol"],
            ori_stream::SUBPROTOCOL
        );
        socket
    }
    async fn stop(mut self) {
        self.core.request_shutdown();
        tokio::time::timeout(Duration::from_secs(4), self.task.take().unwrap())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        let _released = ori_runtime::lease::WriterLease::acquire(self._directory.path()).unwrap();
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
async fn event<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(
    socket: &mut WebSocketStream<S>,
) -> ori_stream::DecodedFrame {
    match tokio::time::timeout(Duration::from_secs(4), socket.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap()
    {
        Message::Binary(bytes) => ori_stream::decode_frame(&bytes).unwrap(),
        other => panic!("Expected committed binary event, got {other:?}"),
    }
}
async fn closed<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(
    socket: &mut WebSocketStream<S>,
    code: CloseCode,
) {
    match tokio::time::timeout(Duration::from_secs(4), socket.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap()
    {
        Message::Close(Some(frame)) => assert_eq!(frame.code, code),
        other => panic!("Expected bounded protocol close, got {other:?}"),
    }
}

#[tokio::test]
async fn websocket_sends_one_event_until_applied_ack_and_reconnect_replays_unacknowledged_source() {
    let server = Server::start(3).await;
    let mut socket = server.connect(0).await;
    let first = event(&mut socket).await;
    assert_eq!(first.sequence, 1);
    assert_eq!(
        first.envelope["data"]["payload"]["opaque"]["$serde_json::private::Number"],
        "metadata"
    );
    assert!(
        tokio::time::timeout(Duration::from_millis(40), socket.next())
            .await
            .is_err(),
        "server queued ahead of the applied ACK"
    );
    drop(socket);
    let mut socket = server.connect(0).await;
    let replay = event(&mut socket).await;
    assert_eq!(replay.envelope, first.envelope);
    socket.send(Message::Text("1".into())).await.unwrap();
    assert_eq!(event(&mut socket).await.sequence, 2);
    socket.send(Message::Text("2".into())).await.unwrap();
    assert_eq!(event(&mut socket).await.sequence, 3);
    let http = server.url.replacen("ws://", "http://", 1);
    let late = reqwest::Client::new()
        .post(format!("{http}/api/v1/sessions"))
        .json(&json!({"id":"late","name":"Late committed event","streams":[]}))
        .send()
        .await
        .unwrap();
    assert_eq!(late.status(), 201);
    assert!(
        tokio::time::timeout(Duration::from_millis(40), socket.next())
            .await
            .is_err(),
        "a commit notice bypassed the outstanding application ACK"
    );
    socket.send(Message::Text("3".into())).await.unwrap();
    assert_eq!(event(&mut socket).await.sequence, 4);
    server.stop().await;
    closed(&mut socket, CloseCode::Away).await;
}

#[tokio::test]
async fn invalid_future_stale_and_oversized_ack_never_advance_committed_cursor() {
    let server = Server::start(2).await;
    for (raw, code) in [
        ("", CloseCode::Policy),
        ("0", CloseCode::Policy),
        ("01", CloseCode::Policy),
        (" 1", CloseCode::Policy),
        ("1 ", CloseCode::Policy),
        ("+1", CloseCode::Policy),
        ("2", CloseCode::Policy),
        ("18446744073709551616", CloseCode::Policy),
        ("123456789012345678901", CloseCode::Size),
    ] {
        let mut socket = server.connect(0).await;
        assert_eq!(event(&mut socket).await.sequence, 1);
        socket.send(Message::Text(raw.into())).await.unwrap();
        closed(&mut socket, code).await;
    }
    let mut socket = server.connect(0).await;
    assert_eq!(event(&mut socket).await.sequence, 1);
    socket.send(Message::Text("1".into())).await.unwrap();
    assert_eq!(event(&mut socket).await.sequence, 2);
    socket.send(Message::Text("1".into())).await.unwrap();
    closed(&mut socket, CloseCode::Policy).await;
    let mut replay = server.connect(1).await;
    assert_eq!(event(&mut replay).await.sequence, 2);
    server.stop().await;
}

#[tokio::test]
async fn slow_applied_pipeline_replays_more_than_one_thousand_events_without_cursor_skips() {
    let server = Server::start(1205).await;
    let mut socket = server.connect(0).await;
    for expected in 1..=1205_u64 {
        let frame = event(&mut socket).await;
        assert_eq!(frame.sequence, expected);
        assert_eq!(frame.envelope["data"]["payload"]["index"], expected - 1);
        if expected % 100 == 0 {
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
        // Exactly where an applied worker/display acknowledgement occurs.
        socket
            .send(Message::Text(expected.to_string().into()))
            .await
            .unwrap();
    }
    assert!(
        tokio::time::timeout(Duration::from_millis(40), socket.next())
            .await
            .is_err()
    );
    server.stop().await;
    closed(&mut socket, CloseCode::Away).await;
}

#[tokio::test]
async fn binary_and_idle_ack_are_protocol_errors_and_large_cursors_remain_exact() {
    let server = Server::start(1).await;
    let mut socket = server.connect(0).await;
    assert_eq!(event(&mut socket).await.sequence, 1);
    socket
        .send(Message::Binary(b"1".as_slice().into()))
        .await
        .unwrap();
    closed(&mut socket, CloseCode::Policy).await;
    let mut socket = server.connect(u64::MAX).await;
    socket
        .send(Message::Text(u64::MAX.to_string().into()))
        .await
        .unwrap();
    closed(&mut socket, CloseCode::Policy).await;
    server.stop().await;
}

#[tokio::test]
async fn typed_sweep_frames_preserve_float_bits_utc_private_metadata_and_sequences_above_double_precision()
 {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("sessions.sqlite3");
    let fixture = ori_contracts::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        65536,
    )
    .unwrap();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let mut session = fixture["session"].clone();
    session["created_at"] = json!("2026-11-11T00:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    archive.close().await.unwrap();
    let boundary = 9_007_199_254_741_003_u64;
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute("UPDATE events SET sequence=?1", [boundary as i64])
        .unwrap();
    drop(db);
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let mut sweep = fixture["sweep"].clone();
    let levels = (0..256)
        .map(|index| [-0.0, 0.0, -1.0000000000000002, -1e-300, -100.125][index % 5])
        .collect::<Vec<f64>>();
    sweep["frequencies_hz"] = json!(
        (0..256)
            .map(|index| 100_000_001_u64 + index)
            .collect::<Vec<_>>()
    );
    sweep["levels"] = json!(levels);
    sweep["settings"]["opaque"]=ori_contracts::parse_json_value(r#"{"$serde_json::private::Number":{"x":1},"huge":18446744073709551616000,"音":"original"}"#.as_bytes(),4096).unwrap();
    archive.append_sweeps(vec![sweep]).await.unwrap();
    let expected = archive.get_sweep("fixture-sweep-α").await.unwrap().unwrap();
    archive.close().await.unwrap();
    let server = Server::open(directory).await;
    let http = server.url.replacen("ws://", "http://", 1);
    let capabilities = reqwest::get(format!("{http}/api/v1/capabilities"))
        .await
        .unwrap()
        .json::<serde_json::Value>()
        .await
        .unwrap();
    assert_eq!(
        capabilities["data"]["features"]["measurement_binary_stream"],
        ori_stream::capability()
    );
    let mut socket = server.connect(boundary).await;
    let frame = event(&mut socket).await;
    assert_eq!(frame.sequence, boundary + 1);
    assert!(
        frame.spectrum.is_some(),
        "regular arrays were unnecessarily kept as point JSON"
    );
    let restored = frame.into_envelope();
    let actual = &restored["data"]["payload"]["sweep"];
    assert_eq!(actual, &expected);
    for (index, level) in levels.iter().enumerate() {
        assert_eq!(
            actual["levels"][index].as_f64().unwrap().to_bits(),
            level.to_bits()
        );
    }
    socket
        .send(Message::Text((boundary + 1).to_string().into()))
        .await
        .unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(40), socket.next())
            .await
            .is_err()
    );
    server.stop().await;
}

#[tokio::test]
async fn missing_protocol_and_malformed_cursors_cannot_upgrade_or_claim_a_stream() {
    let server = Server::start(1).await;
    for suffix in [
        "?after=01",
        "?after=-1",
        "?after=18446744073709551616",
        "?after=0&after=1",
        "?after=0&token=secret",
        "?session_id=other",
    ] {
        let mut request = format!("{}{path}{suffix}", server.url, path = ori_stream::WS_PATH)
            .into_client_request()
            .unwrap();
        request.headers_mut().insert(
            "Sec-WebSocket-Protocol",
            ori_stream::SUBPROTOCOL.parse().unwrap(),
        );
        assert!(
            matches!(tokio_tungstenite::connect_async(request).await,Err(tokio_tungstenite::tungstenite::Error::Http(response)) if response.status()==400)
        );
    }
    let request = format!("{}{path}?after=0", server.url, path = ori_stream::WS_PATH)
        .into_client_request()
        .unwrap();
    assert!(
        matches!(tokio_tungstenite::connect_async(request).await,Err(tokio_tungstenite::tungstenite::Error::Http(response)) if response.status()==400)
    );
    server.stop().await;
}

#[tokio::test]
async fn applied_ack_deadline_is_absolute_thirty_seconds_and_ping_does_not_extend_it() {
    let server = Server::start(1).await;
    let mut socket = server.connect(0).await;
    assert_eq!(event(&mut socket).await.sequence, 1);
    let start = tokio::time::Instant::now();
    for delay in [11, 9] {
        tokio::time::sleep(Duration::from_secs(delay)).await;
        socket.send(Message::Ping(Vec::new().into())).await.unwrap();
        assert!(matches!(
            tokio::time::timeout(Duration::from_secs(3), socket.next())
                .await
                .unwrap()
                .unwrap()
                .unwrap(),
            Message::Pong(_)
        ));
    }
    match tokio::time::timeout(Duration::from_secs(13), socket.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap()
    {
        Message::Close(Some(frame)) => assert_eq!(frame.code, CloseCode::Policy),
        other => panic!("Applied ACK timeout: {other:?}"),
    }
    assert!(
        start.elapsed() >= Duration::from_secs(29),
        "HTTP's ten-second upload budget leaked into the upgraded stream"
    );
    assert!(
        start.elapsed() < Duration::from_secs(34),
        "ping extended the absolute ACK deadline"
    );
    server.stop().await;
}

#[tokio::test]
async fn websocket_and_sse_share_bounded_subscriptions_and_shutdown_drains_both_before_writer_release()
 {
    let server = Server::start(1).await;
    let http = server.url.replacen("ws://", "http://", 1);
    let sse = reqwest::get(format!("{http}/api/v1/events?after=0"))
        .await
        .unwrap();
    assert_eq!(sse.status(), 200);
    let mut sockets = Vec::new();
    for _ in 0..7 {
        let mut socket = server.connect(0).await;
        assert_eq!(event(&mut socket).await.sequence, 1);
        sockets.push(socket);
    }
    let mut request = format!("{}{}?after=0", server.url, ori_stream::WS_PATH)
        .into_client_request()
        .unwrap();
    request.headers_mut().insert(
        "Sec-WebSocket-Protocol",
        ori_stream::SUBPROTOCOL.parse().unwrap(),
    );
    assert!(
        matches!(tokio_tungstenite::connect_async(request).await,Err(tokio_tungstenite::tungstenite::Error::Http(reply)) if reply.status()==503)
    );
    assert_eq!(
        reqwest::get(format!("{http}/api/v1/health"))
            .await
            .unwrap()
            .status(),
        200
    );
    server.stop().await;
    for socket in &mut sockets {
        closed(socket, CloseCode::Away).await;
    }
    drop(sse);
}

#[cfg(unix)]
#[tokio::test]
async fn websocket_uses_verified_tls_trusted_host_origin_and_shared_revocable_bearer_cookie() {
    use ori_core::LanSecurityConfig;
    use ori_runtime::security::TlsIdentity;
    use std::os::unix::fs::PermissionsExt;
    let directory = tempfile::tempdir().unwrap();
    let security = directory.path().join("security");
    std::fs::create_dir(&security).unwrap();
    std::fs::set_permissions(&security, std::fs::Permissions::from_mode(0o700)).unwrap();
    let identity = TlsIdentity::create(&security, vec!["127.0.0.1".into()]).unwrap();
    let http = reqwest::Client::builder()
        .tls_certs_only([
            reqwest::Certificate::from_pem(identity.trust_anchor_pem().as_bytes()).unwrap(),
        ])
        .build()
        .unwrap();
    let mut roots = rustls::RootCertStore::empty();
    for certificate in rustls_pemfile::certs(&mut identity.trust_anchor_pem().as_bytes()) {
        roots.add(certificate.unwrap()).unwrap();
    }
    let tls = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(roots)
    .with_no_client_auth();
    let connector = tokio_rustls::TlsConnector::from(Arc::new(tls));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let authority = listener.local_addr().unwrap().to_string();
    let origin = format!("https://{authority}");
    let mut config = CoreConfig::replay(directory.path());
    config.lan_security = Some(LanSecurityConfig {
        directory: security,
        hosts: vec![authority.clone()],
        origins: vec![origin.clone()],
    });
    let core = Core::open(config).await.unwrap();
    let own = core.clone();
    let task = tokio::spawn(async move { own.serve_tls(listener).await });
    let connect = |headers: Vec<(&'static str, String)>| {
        let authority = authority.clone();
        let connector = connector.clone();
        async move {
            let tcp = TcpStream::connect(&authority).await.unwrap();
            tcp.set_nodelay(true).unwrap();
            let tls = connector
                .connect(
                    rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(),
                    tcp,
                )
                .await
                .unwrap();
            let mut request = format!("wss://{authority}{}?after=0", ori_stream::WS_PATH)
                .into_client_request()
                .unwrap();
            request.headers_mut().insert(
                "Sec-WebSocket-Protocol",
                ori_stream::SUBPROTOCOL.parse().unwrap(),
            );
            for (name, value) in headers {
                request.headers_mut().insert(name, value.parse().unwrap());
            }
            tokio_tungstenite::client_async(request, tls).await
        }
    };
    assert!(
        matches!(connect(Vec::new()).await,Err(tokio_tungstenite::tungstenite::Error::Http(reply)) if reply.status()==401)
    );
    let code = core.begin_pairing_window(60, 3).unwrap();
    let paired = http
        .post(format!("{origin}/api/v1/security/pair"))
        .header("Origin", &origin)
        .json(&json!({"pairing_code":code,"label":"synthetic WS","client":"native"}))
        .send()
        .await
        .unwrap();
    assert_eq!(paired.status(), 200);
    let paired = ori_contracts::parse_json_value(&paired.bytes().await.unwrap(), 4096).unwrap();
    let token = paired["data"]["token"].as_str().unwrap();
    let bearer = format!("Bearer {token}");
    let exchange = http
        .post(format!("{origin}/api/v1/security/browser-session"))
        .bearer_auth(token)
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    assert_eq!(exchange.status(), 200);
    let cookie = exchange.headers()["Set-Cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    assert!(
        matches!(connect(vec![("Authorization",bearer.clone()),("Origin","https://untrusted.invalid".into())]).await,Err(tokio_tungstenite::tungstenite::Error::Http(reply)) if reply.status()==403)
    );
    assert!(
        matches!(connect(vec![("Authorization",bearer.clone()),("Host","untrusted.invalid".into())]).await,Err(tokio_tungstenite::tungstenite::Error::Http(reply)) if reply.status()==403)
    );
    assert!(
        matches!(connect(vec![("Authorization",bearer.clone()),("Cookie",cookie.clone())]).await,Err(tokio_tungstenite::tungstenite::Error::Http(reply)) if reply.status()==401)
    );
    let created = http
        .post(format!("{origin}/api/v1/sessions"))
        .bearer_auth(token)
        .json(&json!({"id":"ws-source","name":"Synthetic WS","streams":[]}))
        .send()
        .await
        .unwrap();
    assert_eq!(created.status(), 201);
    let (mut native, reply) = connect(vec![("Authorization", bearer.clone())])
        .await
        .unwrap();
    assert_eq!(reply.status(), 101);
    let (mut browser, _) = connect(vec![("Cookie", cookie), ("Origin", origin.clone())])
        .await
        .unwrap();
    let first = event(&mut native).await;
    assert_eq!(first.sequence, event(&mut browser).await.sequence);
    let revoked = http
        .post(format!(
            "{origin}/api/v1/security/credentials/current/revoke"
        ))
        .bearer_auth(token)
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    assert_eq!(revoked.status(), 200);
    closed(&mut native, CloseCode::Policy).await;
    closed(&mut browser, CloseCode::Policy).await;
    assert!(
        matches!(connect(vec![("Authorization",bearer)]).await,Err(tokio_tungstenite::tungstenite::Error::Http(reply)) if reply.status()==401)
    );
    core.request_shutdown();
    tokio::time::timeout(Duration::from_secs(4), task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let _released = ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap();
}
