use ori_native_bridge::{ApplicationAck, Bridge, SseDecoder};
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
                Some(serde_json::json!({"token":"lease", "frequency_hz":100000000}).to_string()),
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

#[tokio::test]
async fn raw_json_transport_preserves_integer_float_and_metadata_bytes_without_reencoding() {
    let raw = r#"{"settings":{"$serde_json::private::Number":{"$serde_json::private::RawValue":"literal"},"integer":184467440737095516160000,"negative_zero":-0.0,"largest":1.7976931348623157e308,"smallest":5e-324,"note":"Ж𐀀"}}"#;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let expected = raw.to_owned();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut bytes = vec![0; 8192];
        let count = socket.read(&mut bytes).await.unwrap();
        let request = std::str::from_utf8(&bytes[..count]).unwrap();
        assert!(request.ends_with(&expected));
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            expected.len(),
            expected
        );
        socket.write_all(response.as_bytes()).await.unwrap();
    });
    assert_eq!(
        Bridge::new(&node, "")
            .unwrap()
            .request("/api/v1/sessions", Some(raw.into()), false)
            .await
            .unwrap(),
        raw
    );
    server.await.unwrap();
    let bridge = Bridge::new("http://127.0.0.1:1", "").unwrap();
    for invalid in ["[]", r#"{"same":1,"same":2}"#, r#"{"overflow":1e9999}"#] {
        assert!(
            bridge
                .request("/api/v1/sessions", Some(invalid.into()), false)
                .await
                .unwrap_err()
                .contains("JSON")
        );
    }
}

#[test]
fn remote_plain_http_is_rejected() {
    assert!(Bridge::new("http://radio.example:8770", "").is_err());
    assert!(Bridge::new("http://192.0.2.10:8770", "").is_err());
}

#[tokio::test]
async fn credential_issuing_routes_are_not_available_to_generic_ipc_and_pairing_requires_tls() {
    let bridge = Bridge::new("http://127.0.0.1:1", "").unwrap();
    for path in [
        "/api/v1/security/pair",
        "/api/v1/security/%70air",
        "/api/v1/se%63urity/pair",
        "/api/v1/security/pair/",
    ] {
        assert!(
            bridge
                .request(
                    path,
                    Some(r#"{"client":"native","pairing_code":"synthetic","label":"test"}"#.into()),
                    false
                )
                .await
                .unwrap_err()
                .contains("pairing command")
        );
    }
    assert!(
        bridge
            .pair_native("synthetic", "Windows test")
            .await
            .err()
            .unwrap()
            .contains("HTTPS")
    );
}

#[test]
fn tls_legacy_browser_entry_uses_cookie_pairing_gate_without_a_url_token() {
    let secure = Bridge::new("https://radio.example:8770", "private-native-token").unwrap();
    assert_eq!(
        secure.legacy_browser_url(),
        "https://radio.example:8770/modern/?open=legacy"
    );
    let local = Bridge::new("http://127.0.0.1:8770", "").unwrap();
    assert_eq!(local.legacy_browser_url(), "http://127.0.0.1:8770/");
}

#[tokio::test]
async fn native_replay_waits_for_application_ack_and_cancellation_interrupts_a_blocked_ack() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio_util::sync::CancellationToken;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let initial = 9_007_199_254_740_992_u64;
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut request = [0; 4096];
        let n = socket.read(&mut request).await.unwrap();
        assert!(String::from_utf8_lossy(&request[..n]).contains(&format!("after={initial}")));
        socket
            .write_all(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n",
            )
            .await
            .unwrap();
        for i in 1..=1205 {
            let seq = initial + i;
            let frame = format!(
                "id: {seq}\nevent: future.unlisted\ndata: {{\"data\":{{\"sequence\":{seq}}}}}\n\n"
            );
            socket.write_all(frame.as_bytes()).await.unwrap();
        }
    });
    let cancel = CancellationToken::new();
    let count = Arc::new(AtomicUsize::new(0));
    let output = count.clone();
    let ack = Arc::new(tokio::sync::Notify::new());
    let permit = ack.clone();
    let owner = cancel.clone();
    let bridge = Bridge::new(&node, "").unwrap();
    let task = tokio::spawn(async move {
        bridge
            .events_acked(initial, owner, move |message| {
                let permit = permit.clone();
                let output = output.clone();
                async move {
                    if message.kind == "event" {
                        let index = output.fetch_add(1, Ordering::SeqCst);
                        if index == 0 || index == 1204 {
                            permit.notified().await;
                        }
                    }
                    true
                }
            })
            .await;
    });
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while count.load(Ordering::SeqCst) == 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(30)).await;
    assert_eq!(
        count.load(Ordering::SeqCst),
        1,
        "delivery ran ahead of application ACK"
    );
    ack.notify_one();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while count.load(Ordering::SeqCst) < 1205 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    cancel.cancel();
    tokio::time::timeout(std::time::Duration::from_secs(1), task)
        .await
        .unwrap()
        .unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn native_ack_window_rejects_stale_duplicate_and_unbounded_acknowledgements() {
    let (ack, mut receiver) = ApplicationAck::channel();
    assert!(ack.acknowledge("1").is_err());
    assert_eq!(ack.arm("9007199254740993").unwrap(), 9_007_199_254_740_993);
    assert!(ack.arm("2").is_err());
    assert!(ack.acknowledge("9007199254740992").is_err());
    ack.acknowledge("9007199254740993").unwrap();
    assert!(ack.acknowledge("9007199254740993").is_err());
    assert_eq!(receiver.recv().await, Some(9_007_199_254_740_993));
    for invalid in ["0", "01", "+1", "18446744073709551616", &"9".repeat(100)] {
        assert!(ack.arm(invalid).is_err());
    }
    ack.arm("2").unwrap();
    ack.acknowledge("2").unwrap();
    assert_eq!(receiver.recv().await, Some(2));
}

#[tokio::test]
async fn native_reconnect_cursor_is_exact_after_lossless_metadata_is_applied() {
    use tokio_util::sync::CancellationToken;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let first = r#"{"data":{"sequence":9007199254740993,"event_type":"future.unlisted","payload":{"huge":184467440737095516160000,"$serde_json::private::Number":{"$serde_json::private::RawValue":"Ж𐀀"},"negative_zero":-0.0}}}"#;
    let server = tokio::spawn(async move {
        for (cursor, body) in [
            ("9007199254740992", first),
            (
                "9007199254740993",
                r#"{"data":{"sequence":9007199254740994}}"#,
            ),
        ] {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut bytes = [0; 4096];
            let n = socket.read(&mut bytes).await.unwrap();
            assert!(String::from_utf8_lossy(&bytes[..n]).contains(&format!("after={cursor}")));
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: {body}\n\n"
            );
            socket.write_all(response.as_bytes()).await.unwrap();
        }
    });
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    let received = Arc::new(Mutex::new(Vec::new()));
    let output = received.clone();
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        Bridge::new(&node, "").unwrap().events_acked(
            9_007_199_254_740_992,
            cancel,
            move |message| {
                let output = output.clone();
                let owner = owner.clone();
                async move {
                    if message.kind == "event" {
                        let mut rows = output.lock().unwrap();
                        rows.push(message.data.unwrap());
                        if rows.len() == 2 {
                            owner.cancel();
                        }
                    }
                    true
                }
            },
        ),
    )
    .await
    .unwrap();
    server.await.unwrap();
    assert_eq!(received.lock().unwrap()[0], first);
}
