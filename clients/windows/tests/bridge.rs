use ori_native_bridge::{Bridge, SseDecoder};
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

#[test]
fn configured_node_and_relative_api_boundary_prevent_navigation_and_credential_leaks() {
    assert!(Bridge::new("https://radio.example:8770", "session-token").is_ok());
    for bad in [
        "file:///etc/passwd",
        "https://user:secret@radio.example",
        "https://radio.example/other",
        "https://radio.example?token=x",
        "javascript:alert(1)",
    ] {
        assert!(Bridge::new(bad, "").is_err(), "accepted {bad}");
    }
    let bridge = Bridge::new("https://radio.example:8770", "").unwrap();
    assert_eq!(
        bridge
            .endpoint("/api/v1/monitoring-projects/a%2Fb/history?limit=32")
            .unwrap()
            .as_str(),
        "https://radio.example:8770/api/v1/monitoring-projects/a%2Fb/history?limit=32"
    );
    for bad in [
        "https://elsewhere/api/v1/health",
        "//elsewhere/api/v1/health",
        "/api/v1/../secret",
        "/api/v1/%2e%2e/secret",
        "/api/v1/health#fragment",
        "/api/v1/health\\evil",
        "/api/legacy",
        "/api/v1/health\r\nX: yes",
    ] {
        assert!(bridge.endpoint(bad).is_err(), "accepted {bad}");
    }
}

#[test]
fn sse_reassembles_split_utf8_crlf_multiline_data_and_bounds_a_slow_or_invalid_source() {
    let mut decoder = SseDecoder::default();
    assert!(
        decoder
            .push(b": keep-alive\r\nid: 7\r\nevent: sweep.received\r\ndata: {\"note\":\"")
            .unwrap()
            .is_empty()
    );
    assert!(decoder.push(&[0xd0]).unwrap().is_empty());
    assert!(decoder.push(&[0x96, b'"', b'}', b'\r']).unwrap().is_empty());
    assert_eq!(
        decoder.push(b"\ndata: second line\r\n\r\n").unwrap(),
        vec!["{\"note\":\"Ж\"}\nsecond line"]
    );
    assert!(decoder.push(&vec![b'x'; 8_388_609]).is_err());
}

#[tokio::test]
async fn native_http_posts_without_browser_origin_retains_paths_and_never_follows_redirects() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let captured = Arc::new(Mutex::new(String::new()));
    let output = captured.clone();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut data = vec![0; 8192];
        let size = socket.read(&mut data).await.unwrap();
        *output.lock().unwrap() = String::from_utf8_lossy(&data[..size]).into_owned();
        socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 10\r\nConnection: close\r\n\r\n{\"data\":1}").await.unwrap();
    });
    let bridge = Bridge::new(&node, "private-token").unwrap();
    assert_eq!(
        bridge
            .request(
                "/api/v1/monitoring-projects/id/manual/tune",
                Some(serde_json::json!({"token":"lease", "frequency_hz":100000000})),
                false
            )
            .await
            .unwrap(),
        "{\"data\":1}"
    );
    server.await.unwrap();
    let raw = captured.lock().unwrap().to_lowercase();
    assert!(raw.starts_with("post /api/v1/monitoring-projects/id/manual/tune http/1.1"));
    assert!(raw.contains("authorization: bearer private-token"));
    assert!(!raw.contains("\r\norigin:"));

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut data = [0; 4096];
        let _ = socket.read(&mut data).await;
        socket.write_all(b"HTTP/1.1 302 Found\r\nLocation: https://elsewhere.example/secret\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await.unwrap();
    });
    assert!(
        Bridge::new(&node, "")
            .unwrap()
            .request("/api/v1/health", None, false)
            .await
            .is_err()
    );
    server.await.unwrap();
}

#[tokio::test]
async fn rejects_declared_oversized_json_without_waiting_for_the_body() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut data = [0; 4096];
        let _ = socket.read(&mut data).await;
        socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 8388609\r\nConnection: close\r\n\r\n").await.unwrap();
    });
    let result = Bridge::new(&node, "")
        .unwrap()
        .request("/api/v1/health", None, false)
        .await;
    assert!(result.unwrap_err().contains("client limit"));
    server.await.unwrap();
}

#[tokio::test]
async fn native_sse_delivers_complete_frames_and_stops_when_the_owner_cancels() {
    use tokio_util::sync::CancellationToken;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut data = [0; 4096];
        let size = socket.read(&mut data).await.unwrap();
        assert!(
            String::from_utf8_lossy(&data[..size])
                .starts_with("GET /api/v1/events?after=7 HTTP/1.1")
        );
        socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nid: 8\nevent: sweep.received\ndata: {\"data\":{\"sequence\":8}}\n\n").await.unwrap();
    });
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    let messages = Arc::new(Mutex::new(Vec::new()));
    let output = messages.clone();
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        Bridge::new(&node, "")
            .unwrap()
            .events(7, cancel, move |value| {
                if value.kind == "event" {
                    output.lock().unwrap().push(value.data.unwrap());
                    owner.cancel();
                }
                true
            }),
    )
    .await
    .unwrap();
    server.await.unwrap();
    assert_eq!(
        *messages.lock().unwrap(),
        vec!["{\"data\":{\"sequence\":8}}"]
    );
}
