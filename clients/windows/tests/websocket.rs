use base64::Engine;
use ori_native_bridge::{BinaryStreamConfig, Bridge};
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_tungstenite::tungstenite::handshake::derive_accept_key;
use tokio_util::sync::CancellationToken;

async fn request(socket: &mut tokio::net::TcpStream) -> String {
    let mut raw = Vec::new();
    while !raw.ends_with(b"\r\n\r\n") {
        let byte = socket.read_u8().await.unwrap();
        raw.push(byte);
        assert!(raw.len() < 16_384);
    }
    String::from_utf8(raw).unwrap()
}
fn upgrade(request: &str) -> Vec<u8> {
    let key = request
        .lines()
        .find_map(|line| {
            line.split_once(':')
                .filter(|(name, _)| name.eq_ignore_ascii_case("sec-websocket-key"))
                .map(|(_, value)| value.trim())
        })
        .unwrap();
    format!("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: {}\r\nSec-WebSocket-Protocol: ori.measurements.v1.protobuf\r\n\r\n",derive_accept_key(key.as_bytes())).into_bytes()
}
fn frame(payload: &[u8]) -> Vec<u8> {
    let mut output = vec![0x82];
    if payload.len() < 126 {
        output.push(payload.len() as u8);
    } else {
        output.push(126);
        output.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    }
    output.extend_from_slice(payload);
    output
}
async fn ack(socket: &mut tokio::net::TcpStream) -> String {
    assert_eq!(socket.read_u8().await.unwrap(), 0x81);
    let size = socket.read_u8().await.unwrap();
    assert!(size & 0x80 != 0);
    let size = (size & 0x7f) as usize;
    assert!(size <= 20);
    let mut mask = [0; 4];
    socket.read_exact(&mut mask).await.unwrap();
    let mut bytes = vec![0; size];
    socket.read_exact(&mut bytes).await.unwrap();
    for (i, byte) in bytes.iter_mut().enumerate() {
        *byte ^= mask[i % 4];
    }
    String::from_utf8(bytes).unwrap()
}
async fn closed_without_ack(socket: &mut tokio::net::TcpStream) {
    match socket.read(&mut [0; 1]).await {
        Ok(0) => {}
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::ConnectionReset
                    | std::io::ErrorKind::ConnectionAborted
                    | std::io::ErrorKind::BrokenPipe
                    | std::io::ErrorKind::UnexpectedEof
            ) => {}
        other => panic!("Cancelled/invalid frame received bytes instead of closing: {other:?}"),
    }
}
fn event(sequence: u64) -> Vec<u8> {
    ori_stream::encode_frame(
        format!(r#"{{"contract":"openradioinspector-platform","contract_version":1,"data":{{"sequence":{sequence},"event_type":"future.extension","occurred_at":"2026-11-11T12:00:00.123456Z","payload":{{}}}}}}"#).as_bytes(),
    ).unwrap()
}
#[tokio::test]
async fn coalesced_upgrade_binary_bytes_reach_ui_unchanged_and_ack_waits_for_application() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let raw=br#"{"contract":"openradioinspector-platform","contract_version":1,"data":{"sequence":9007199254740993,"event_type":"future.extension","occurred_at":"2026-11-11T12:00:00.123456Z","payload":{"huge":184467440737095516160000,"negative_zero":-0.0,"$serde_json::private::Number":{"$serde_json::private::RawValue":"literal"}}}}"#;
    let wire = ori_stream::encode_frame(raw).unwrap();
    let expected = wire.clone();
    let permit = Arc::new(tokio::sync::Notify::new());
    let apply = permit.clone();
    let entered = Arc::new(tokio::sync::Notify::new());
    let applying = entered.clone();
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let captured = request(&mut socket).await;
        assert!(captured.starts_with("GET /api/v1/events/ws?after=9007199254740992 HTTP/1.1"));
        assert!(
            captured
                .to_ascii_lowercase()
                .contains("authorization: bearer synthetic-test-token")
        );
        assert!(!captured.to_ascii_lowercase().contains("\r\norigin:"));
        let mut response = upgrade(&captured);
        response.extend(frame(&wire));
        socket.write_all(&response).await.unwrap();
        entered.notified().await;
        let mut byte = [0];
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(40), socket.read(&mut byte))
                .await
                .is_err(),
            "ACK preceded UI application"
        );
        permit.notify_one();
        assert_eq!(ack(&mut socket).await, "9007199254740993");
        owner.cancel();
    });
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        Bridge::new(&node, "synthetic-test-token")
            .unwrap()
            .events_negotiated(
                9_007_199_254_740_992,
                Some(BinaryStreamConfig {
                    max_frame_bytes: ori_stream::MAX_FRAME_BYTES,
                }),
                cancel,
                move |message| {
                    let expected = expected.clone();
                    let apply = apply.clone();
                    let applying = applying.clone();
                    async move {
                        if message.kind == "binary" {
                            assert_eq!(
                                base64::engine::general_purpose::STANDARD
                                    .decode(message.data.unwrap())
                                    .unwrap(),
                                expected
                            );
                            assert!(message.delivery_id.is_some());
                            applying.notify_one();
                            apply.notified().await;
                        }
                        true
                    }
                },
            ),
    )
    .await
    .unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn an_explicit_unsupported_upgrade_uses_sse_at_the_same_cursor() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        assert!(request(&mut socket).await.contains("/events/ws?after=7"));
        socket
            .write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            .await
            .unwrap();
        drop(socket);
        let (mut socket, _) = listener.accept().await.unwrap();
        assert!(request(&mut socket).await.contains("/events?after=7 "));
        socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nid: 8\ndata: {\"data\":{\"sequence\":8}}\n\n").await.unwrap();
    });
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        Bridge::new(&node, "").unwrap().events_negotiated(
            7,
            Some(BinaryStreamConfig {
                max_frame_bytes: ori_stream::MAX_FRAME_BYTES,
            }),
            cancel,
            move |message| {
                let owner = owner.clone();
                async move {
                    if message.kind == "event" {
                        owner.cancel();
                    }
                    true
                }
            },
        ),
    )
    .await
    .unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn authorization_failure_does_not_downgrade_to_sse() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        assert!(request(&mut socket).await.contains("/events/ws?after=7"));
        socket
            .write_all(
                b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            )
            .await
            .unwrap();
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(100), listener.accept())
                .await
                .is_err()
        );
    });
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        Bridge::new(&node, "").unwrap().events_negotiated(
            7,
            Some(BinaryStreamConfig {
                max_frame_bytes: ori_stream::MAX_FRAME_BYTES,
            }),
            cancel,
            move |message| {
                let owner = owner.clone();
                async move {
                    if message.kind == "reconnecting" {
                        owner.cancel();
                    }
                    assert_ne!(message.kind, "fallback");
                    true
                }
            },
        ),
    )
    .await
    .unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn a_real_failed_tls_handshake_does_not_downgrade_to_sse_or_plain_http() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("https://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut header = [0; 5];
        socket.read_exact(&mut header).await.unwrap();
        assert_eq!(header[0], 0x16, "Native client did not initiate TLS");
        // Deliberately invalid TLS peer, not a trusted-CA/hostname acceptance test.
        socket
            .write_all(b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n")
            .await
            .unwrap();
        drop(socket);
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(100), listener.accept())
                .await
                .is_err()
        );
    });
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        Bridge::new(&node, "").unwrap().events_negotiated(
            7,
            Some(BinaryStreamConfig {
                max_frame_bytes: ori_stream::MAX_FRAME_BYTES,
            }),
            cancel,
            move |message| {
                let owner = owner.clone();
                async move {
                    assert_ne!(message.kind, "fallback");
                    if message.kind == "reconnecting" {
                        owner.cancel();
                    }
                    true
                }
            },
        ),
    )
    .await
    .unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn malformed_frame_is_not_acknowledged_and_sse_resumes_after_the_last_applied_frame() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let captured = request(&mut socket).await;
        assert!(captured.contains("/events/ws?after=7 "));
        socket.write_all(&upgrade(&captured)).await.unwrap();
        socket.write_all(&frame(&event(8))).await.unwrap();
        assert_eq!(ack(&mut socket).await, "8");
        socket
            .write_all(&frame(b"malformed protobuf"))
            .await
            .unwrap();
        closed_without_ack(&mut socket).await;
        let (mut fallback, _) = listener.accept().await.unwrap();
        assert!(request(&mut fallback).await.contains("/events?after=8 "));
        fallback.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nid: 9\ndata: {\"data\":{\"sequence\":9}}\n\n").await.unwrap();
    });
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    let deliveries = Arc::new(std::sync::Mutex::new(Vec::new()));
    let observed = deliveries.clone();
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        Bridge::new(&node, "").unwrap().events_negotiated(
            7,
            Some(BinaryStreamConfig {
                max_frame_bytes: ori_stream::MAX_FRAME_BYTES,
            }),
            cancel,
            move |message| {
                let owner = owner.clone();
                let observed = observed.clone();
                async move {
                    if matches!(message.kind.as_str(), "binary" | "event") {
                        observed
                            .lock()
                            .unwrap()
                            .push((message.kind.clone(), message.delivery_id.unwrap()));
                        if message.kind == "event" {
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
    assert_eq!(
        *deliveries.lock().unwrap(),
        [("binary".into(), "1".into()), ("event".into(), "2".into())]
    );
}

#[tokio::test]
async fn a_slow_consumer_replays_over_one_thousand_frames_with_one_application_at_a_time() {
    const COUNT: u64 = 1_205;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let captured = request(&mut socket).await;
        assert!(captured.contains("/events/ws?after=0 "));
        socket.write_all(&upgrade(&captured)).await.unwrap();
        for sequence in 1..=COUNT {
            socket.write_all(&frame(&event(sequence))).await.unwrap();
            assert_eq!(ack(&mut socket).await, sequence.to_string());
        }
        owner.cancel();
    });
    let applied = Arc::new(std::sync::Mutex::new(Vec::new()));
    let observed = applied.clone();
    tokio::time::timeout(
        std::time::Duration::from_secs(10),
        Bridge::new(&node, "").unwrap().events_negotiated(
            0,
            Some(BinaryStreamConfig {
                max_frame_bytes: ori_stream::MAX_FRAME_BYTES,
            }),
            cancel,
            move |message| {
                let observed = observed.clone();
                async move {
                    if message.kind == "binary" {
                        let bytes = base64::engine::general_purpose::STANDARD
                            .decode(message.data.unwrap())
                            .unwrap();
                        let sequence = ori_stream::validate_frame(&bytes).unwrap().sequence;
                        if sequence.is_multiple_of(31) {
                            tokio::time::sleep(std::time::Duration::from_millis(2)).await;
                        }
                        let mut observed = observed.lock().unwrap();
                        assert_eq!(sequence, observed.len() as u64 + 1);
                        assert_eq!(message.delivery_id.unwrap(), sequence.to_string());
                        observed.push(sequence);
                    }
                    true
                }
            },
        ),
    )
    .await
    .unwrap();
    server.await.unwrap();
    assert_eq!(*applied.lock().unwrap(), (1..=COUNT).collect::<Vec<_>>());
}

#[tokio::test]
async fn cancellation_during_application_closes_without_acknowledging_the_frame() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    let entered = Arc::new(tokio::sync::Notify::new());
    let observed = entered.clone();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let captured = request(&mut socket).await;
        let mut response = upgrade(&captured);
        response.extend(frame(&event(8)));
        socket.write_all(&response).await.unwrap();
        entered.notified().await;
        owner.cancel();
        closed_without_ack(&mut socket).await;
    });
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        Bridge::new(&node, "").unwrap().events_negotiated(
            7,
            Some(BinaryStreamConfig {
                max_frame_bytes: ori_stream::MAX_FRAME_BYTES,
            }),
            cancel,
            move |message| {
                let observed = observed.clone();
                async move {
                    if message.kind == "binary" {
                        observed.notify_one();
                        std::future::pending::<()>().await;
                    }
                    true
                }
            },
        ),
    )
    .await
    .unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn oversized_websocket_framing_falls_back_without_delivering_or_acknowledging_it() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let node = format!("http://{}", listener.local_addr().unwrap());
    let cancel = CancellationToken::new();
    let owner = cancel.clone();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let captured = request(&mut socket).await;
        let mut response = upgrade(&captured);
        response.extend(frame(&event(8)));
        socket.write_all(&response).await.unwrap();
        closed_without_ack(&mut socket).await;
        let (mut fallback, _) = listener.accept().await.unwrap();
        assert!(request(&mut fallback).await.contains("/events?after=7 "));
        fallback.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nid: 8\ndata: {\"data\":{\"sequence\":8}}\n\n").await.unwrap();
    });
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        Bridge::new(&node, "").unwrap().events_negotiated(
            7,
            Some(BinaryStreamConfig {
                max_frame_bytes: 16,
            }),
            cancel,
            move |message| {
                let owner = owner.clone();
                async move {
                    assert_ne!(message.kind, "binary");
                    assert_ne!(
                        message.kind, "reconnecting",
                        "Proven invalid framing must not retry forever"
                    );
                    if message.kind == "event" {
                        owner.cancel();
                    }
                    true
                }
            },
        ),
    )
    .await
    .unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn duplicate_negotiation_headers_and_unsolicited_extensions_are_not_accepted() {
    for extra in [
        "Sec-WebSocket-Protocol: ori.measurements.v1.protobuf\r\n",
        "Sec-WebSocket-Accept: extra-invalid-accept\r\n",
        "Sec-WebSocket-Extensions: permessage-deflate\r\n",
    ] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let node = format!("http://{}", listener.local_addr().unwrap());
        let cancel = CancellationToken::new();
        let owner = cancel.clone();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let captured = request(&mut socket).await;
            let mut response = upgrade(&captured);
            response.truncate(response.len() - 2);
            response.extend_from_slice(extra.as_bytes());
            response.extend_from_slice(b"\r\n");
            response.extend(frame(&event(8)));
            socket.write_all(&response).await.unwrap();
            closed_without_ack(&mut socket).await;
            let (mut fallback, _) = listener.accept().await.unwrap();
            assert!(request(&mut fallback).await.contains("/events?after=7 "));
            fallback.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nid: 8\ndata: {\"data\":{\"sequence\":8}}\n\n").await.unwrap();
        });
        tokio::time::timeout(
            std::time::Duration::from_secs(3),
            Bridge::new(&node, "").unwrap().events_negotiated(
                7,
                Some(BinaryStreamConfig {
                    max_frame_bytes: ori_stream::MAX_FRAME_BYTES,
                }),
                cancel,
                move |message| {
                    let owner = owner.clone();
                    async move {
                        assert_ne!(message.kind, "binary", "Invalid101 was accepted");
                        if message.kind == "event" {
                            owner.cancel();
                        }
                        true
                    }
                },
            ),
        )
        .await
        .unwrap();
        server.await.unwrap();
    }
}
