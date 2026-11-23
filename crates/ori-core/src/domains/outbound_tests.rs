use super::outbound::*;
use serde_json::json;

#[test]
fn telegram_request_only_contains_allowlisted_text_and_fixed_send_message_destination() {
    let client = reqwest::Client::new();
    let payload = json!({"project_id":"room","event_id":"owned-event","kind":"new_unknown","occurred_at":"2026-11-23T10:23:00Z",
        "frequency_hz":433920000,"delta":7.0,"delta_unit":"s_meter_count","source_refs":[{"private":"forbidden-spectrum"}],
        "recording":"forbidden-audio","message":"forbidden-input"});
    let request = telegram_request(&client, "1:fixture", "-123", &payload)
        .unwrap()
        .build()
        .unwrap();
    assert_eq!(
        request.url().as_str(),
        "https://api.telegram.org/bot1:fixture/sendMessage"
    );
    let body: serde_json::Value =
        serde_json::from_slice(request.body().unwrap().as_bytes().unwrap()).unwrap();
    assert_eq!(body["chat_id"], "-123");
    let text = body["text"].as_str().unwrap();
    for field in [
        "owned-event",
        "433920000",
        "s_meter_count",
        "2026-11-23T10:23:00Z",
    ] {
        assert!(text.contains(field));
    }
    assert!(!text.contains("forbidden"));
    assert_eq!(body.as_object().unwrap().len(), 2);
}
#[tokio::test]
async fn telegram_body_retry_after_is_bounded_and_webhook_does_not_read_response_body() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let server = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = server.local_addr().unwrap();
    let respond = tokio::spawn(async move {
        let (mut socket, _) = server.accept().await.unwrap();
        let mut input = [0; 512];
        socket.read(&mut input).await.unwrap();
        let body = r#"{"ok":false,"error_code":429,"description":"synthetic-private","parameters":{"retry_after":120}}"#;
        socket.write_all(format!("HTTP/1.1 429 Too Many Requests\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).as_bytes()).await.unwrap();
    });
    let response = reqwest::get(format!("http://{address}/fixture"))
        .await
        .unwrap();
    let receipt = response_receipt(response, true).await;
    assert_eq!(receipt.status, Some(429));
    assert_eq!(receipt.retry_after_seconds, Some(120));
    assert!(!receipt.delivered);
    respond.await.unwrap();
}

#[tokio::test]
async fn operator_setup_reaches_real_outbox_and_429_journal_without_persisting_destination() {
    use crate::{Core, CoreConfig};
    use std::time::Duration;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let dir = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::replay(dir.path())).await.unwrap();
    let archive = core.archive.as_ref().unwrap();
    archive
        .put_monitoring(
            "project",
            json!({"id":"outbound-fixture","created_at":"2026-11-23T00:00:00Z","session_ids":[]}),
        )
        .await
        .unwrap();
    let guard = &core.domains.get().unwrap().guard;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!(
        "http://{}/private-endpoint-fixture",
        listener.local_addr().unwrap()
    );
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut bytes = [0; 4096];
        let n = socket.read(&mut bytes).await.unwrap();
        let request = String::from_utf8_lossy(&bytes[..n]).to_lowercase();
        assert!(request.contains("x-openradioinspector-signature: sha256="));
        socket.write_all(b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 240\r\nConnection: close\r\nContent-Length: 0\r\n\r\n").await.unwrap();
    });
    let patch = json!({"notifications":{"webhook":{"enabled":true,"url":url,"secret":"synthetic-private-outbox-signature"}}});
    let settings = guard
        .update_settings("outbound-fixture", patch)
        .await
        .unwrap();
    assert_eq!(
        settings["notifications"]["webhook"],
        json!({"enabled":true,"configured":true})
    );
    let queued = guard
        .test_notification("outbound-fixture", json!({"channel":"webhook"}))
        .await
        .unwrap();
    assert_eq!(queued["state"], "pending");
    let journal = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let duty = guard.duty("outbound-fixture").await.unwrap();
            let item = &duty["deliveries"][0];
            if item["error"] == "HTTP 429" {
                break item.clone();
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(journal["attempts"], 1);
    assert_eq!(journal["id"], queued["notification_id"]);
    let created =
        chrono::DateTime::parse_from_rfc3339(journal["created_at"].as_str().unwrap()).unwrap();
    let next = chrono::DateTime::parse_from_rfc3339(journal["next_at"].as_str().unwrap()).unwrap();
    assert!((next - created).num_seconds() >= 240);
    server.await.unwrap();
    guard
        .update_settings(
            "outbound-fixture",
            json!({"notifications":{"webhook":{"enabled":false}}}),
        )
        .await
        .unwrap();
    assert_eq!(
        guard.duty("outbound-fixture").await.unwrap()["deliveries"][0]["state"],
        "disabled"
    );
    core.close().await.unwrap();
    let sqlite = std::fs::read(dir.path().join("sessions.sqlite3")).unwrap();
    let observed = String::from_utf8_lossy(&sqlite);
    for hidden in [
        "synthetic-private-outbox-signature",
        "private-endpoint-fixture",
    ] {
        assert!(!settings.to_string().contains(hidden));
        assert!(!journal.to_string().contains(hidden));
        assert!(!observed.contains(hidden));
    }
}
