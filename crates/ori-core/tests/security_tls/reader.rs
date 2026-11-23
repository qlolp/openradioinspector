use super::*;
use ori_runtime::security::CredentialScope;

async fn pair_with_scope(server: &LanServer, code: &str, scope: &str) -> Response {
    server.client.post(format!("{}/api/v1/security/pair", server.url))
        .json(&json!({"pairing_code":code,"client":"native","label":"reader fixture","required_scope":scope}))
        .send().await.unwrap()
}

async fn security_status(server: &LanServer, token: Option<&str>) -> Value {
    let mut request = server
        .client
        .get(format!("{}/api/v1/security/status", server.url));
    if let Some(token) = token {
        request = request.bearer_auth(token);
    }
    request.send().await.unwrap().json().await.unwrap()
}

#[tokio::test]
async fn local_reader_pairing_requires_an_explicit_scope_acknowledgement() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let data = tempfile::tempdir().unwrap();
    let state = ori_runtime::lease::state_directory(data.path()).unwrap();
    let listener = tokio::net::UnixListener::bind(state.join("core-admin.sock")).unwrap();
    let old_node = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut request = Vec::new();
        socket.read_to_end(&mut request).await.unwrap();
        let request: Value = serde_json::from_slice(&request).unwrap();
        assert_eq!(request["scope"], "reader");
        socket
            .write_all(br#"{"version":1,"pairing_code":"synthetic-unscoped-window"}"#)
            .await
            .unwrap();
    });
    let response =
        ori_core::admin::request_scoped(data.path(), 60, 3, CredentialScope::Reader).await;
    assert!(response.is_err());
    old_node.await.unwrap();
}

#[tokio::test]
async fn the_local_window_controls_scope_and_status_requires_a_fresh_credential() {
    let mut server = LanServer::start().await;
    let public = security_status(&server, None).await;
    assert_eq!(
        public["data"],
        json!({"mode":"lan_tls","pairing_required":true,"reader_pairing":true})
    );

    let local =
        ori_core::admin::request_scoped(server._directory.path(), 60, 3, CredentialScope::Reader)
            .await
            .unwrap();
    assert_eq!(local["access_scope"], "reader");
    let code = local["pairing_code"].as_str().unwrap();
    assert_eq!(
        pair_with_scope(&server, code, "operator").await.status(),
        401
    );
    let reader = pair_with_scope(&server, code, "reader").await;
    assert_eq!(reader.status(), 200);
    assert!(reader.headers().get("Set-Cookie").is_none());
    let paired: Value = reader.json().await.unwrap();
    assert_eq!(paired["data"]["paired"], true);
    assert_eq!(paired["data"]["credential"], "bearer");
    assert_eq!(paired["data"]["access_scope"], "reader");
    let token = paired["data"]["token"].as_str().unwrap();
    let state = security_status(&server, Some(token)).await;
    assert_eq!(
        state["data"],
        json!({"mode":"lan_tls","pairing_required":false,"reader_pairing":true,"access_scope":"reader"})
    );
    assert_eq!(server.pair(code, "native").await.status(), 429);
    let local = ori_core::admin::request(server._directory.path(), 60, 3)
        .await
        .unwrap();
    let code = local["pairing_code"].as_str().unwrap();
    assert_eq!(pair_with_scope(&server, code, "reader").await.status(), 401);
    let old_client: Value = server.pair(code, "native").await.json().await.unwrap();
    assert!(old_client["data"].get("access_scope").is_none());
    let operator = old_client["data"]["token"].as_str().unwrap();
    let state = security_status(&server, Some(operator)).await;
    assert_eq!(state["data"]["access_scope"], "operator");
    let revoked = server
        .client
        .delete(format!(
            "{}/api/v1/security/credentials/current",
            server.url
        ))
        .bearer_auth(token)
        .send()
        .await
        .unwrap();
    assert_eq!(revoked.status(), 200);
    let expired = security_status(&server, Some(token)).await;
    assert_eq!(expired, public);
    server.stop().await;
}

#[tokio::test]
async fn reader_can_browse_measurements_while_mutations_and_unrelated_reads_are_forbidden() {
    let mut server = LanServer::start().await;
    let code = server.core.begin_pairing_window(60, 3).unwrap();
    let pairing: Value = server.pair(&code, "native").await.json().await.unwrap();
    let operator = pairing["data"]["token"].as_str().unwrap();
    let fixture: Value = serde_json::from_slice(include_bytes!(
        "../../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap();
    let base = format!("{}/api/v1", server.url);
    let session = format!(
        "{base}/sessions/{}",
        ori_contracts::encode_path_segment(fixture["session"]["id"].as_str().unwrap())
    );
    for (url, body, status) in [
        (format!("{base}/sessions"), fixture["session"].clone(), 201),
        (format!("{session}/start"), json!({}), 200),
        (format!("{session}/sweeps"), fixture["sweep"].clone(), 201),
        (
            format!("{session}/observations"),
            fixture["observation"].clone(),
            201,
        ),
    ] {
        let response = server
            .client
            .post(url)
            .bearer_auth(operator)
            .json(&body)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), status);
    }

    let code = server
        .core
        .begin_scoped_pairing_window(60, 3, CredentialScope::Reader)
        .unwrap();
    let reader: Value = pair_with_scope(&server, &code, "reader")
        .await
        .json()
        .await
        .unwrap();
    let token = reader["data"]["token"].as_str().unwrap();
    let source = ori_contracts::encode_path_segment(fixture["sweep"]["id"].as_str().unwrap());
    let stream =
        ori_contracts::encode_path_segment(fixture["sweep"]["stream_id"].as_str().unwrap());
    for url in [
        format!("{base}/capabilities"),
        format!("{base}/health"),
        format!("{base}/sessions"),
        session.clone(),
        format!("{session}/streams"),
        format!("{session}/streams/{stream}"),
        format!("{session}/streams/{stream}/rf-explorer/passes"),
        format!("{session}/sweeps/{source}"),
        format!("{session}/sweeps?limit=1"),
        format!("{session}/sweep-summaries?limit=1"),
        format!("{base}/events/history"),
    ] {
        let response = server
            .client
            .get(&url)
            .bearer_auth(token)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 200, "{url}");
    }

    let receiver = fixture["observation"]["stream_id"].as_str().unwrap();
    let observations: Value = server
        .client
        .get(format!(
            "{session}/observations?stream_id={}&limit=1",
            ori_contracts::encode_path_segment(receiver)
        ))
        .bearer_auth(token)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let item = &observations["data"][0];
    assert_eq!(item["stream_id"], receiver);
    assert_eq!(item["frequency_hz"], 100000003);
    assert_eq!(item["level"], 137.0);
    assert_eq!(item["level_unit"], "s_meter_count");

    let endpoint = format!("{base}/health");
    let before: Value = server
        .client
        .get(&endpoint)
        .bearer_auth(token)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    for (method, url) in [
        ("POST", format!("{base}/sessions")),
        ("POST", format!("{session}/stop")),
        ("POST", format!("{session}/observations")),
        ("POST", format!("{session}/sweeps")),
        ("POST", format!("{base}/security/browser-session")),
        ("POST", format!("{base}/history/jobs")),
        ("POST", format!("{base}/instances/other/sessions")),
        ("POST", format!("{base}/security/pair")),
        ("GET", format!("{base}/archive/status")),
        ("GET", format!("{session}/workers")),
        (
            "GET",
            format!("{session}/streams/{stream}/instrument/settings"),
        ),
        ("GET", format!("{}/api/status", server.url)),
    ] {
        let response = server
            .client
            .request(method.parse().unwrap(), &url)
            .bearer_auth(token)
            .json(&json!({"name":"blocked"}))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 403, "{method} {url}");
    }
    let after: Value = server
        .client
        .get(&endpoint)
        .bearer_auth(token)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        after["data"]["last_event_id"],
        before["data"]["last_event_id"]
    );
    let cursor = after["data"]["last_event_id"].as_u64().unwrap();
    let mut live = server
        .client
        .get(format!("{base}/events?after={cursor}"))
        .bearer_auth(token)
        .send()
        .await
        .unwrap();
    assert_eq!(live.status(), 200);
    let revoked = server
        .client
        .post(format!("{base}/security/credentials/current/revoke"))
        .bearer_auth(token)
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    assert_eq!(revoked.status(), 200);
    assert!(
        tokio::time::timeout(Duration::from_secs(3), live.chunk())
            .await
            .unwrap()
            .unwrap()
            .is_none()
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
    assert_eq!(
        server
            .client
            .get(&endpoint)
            .bearer_auth(operator)
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    server.stop().await;
}

#[tokio::test]
async fn a_browser_that_redeems_a_reader_code_gets_the_same_read_only_scope() {
    let mut server = LanServer::start().await;
    let code = server
        .core
        .begin_scoped_pairing_window(60, 3, CredentialScope::Reader)
        .unwrap();
    let response = server.pair(&code, "browser").await;
    let cookie = cookie(&response);
    let paired: Value = response.json().await.unwrap();
    assert_eq!(
        paired["data"],
        json!({"paired":true,"credential":"browser_cookie","access_scope":"reader"})
    );
    let sessions = format!("{}/api/v1/sessions", server.url);
    assert_eq!(
        server
            .client
            .get(&sessions)
            .header("Cookie", &cookie)
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    let create = server
        .client
        .post(&sessions)
        .header("Cookie", &cookie)
        .header("Origin", &server.origin)
        .json(&json!({"name":"blocked"}))
        .send()
        .await
        .unwrap();
    assert_eq!(create.status(), 403);
    let denial: Value = create.json().await.unwrap();
    assert_eq!(denial["error"]["code"], "insufficient_scope");
    server.stop().await;
}
