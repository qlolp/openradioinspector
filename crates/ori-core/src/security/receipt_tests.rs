//! Original request authority, real private credential files and coordinated wakes.
use super::*;
use ori_runtime::security::TlsIdentity;
use std::time::Duration;

#[cfg(not(windows))]
struct Fixture {
    directory: tempfile::TempDir,
    state: Arc<SecurityState>,
    first: String,
    second: String,
}
#[cfg(not(windows))]
impl Fixture {
    fn new() -> Self {
        let directory = tempfile::tempdir().unwrap();
        TlsIdentity::create(directory.path(), vec!["localhost".into()]).unwrap();
        let state = SecurityState::open(Some(&LanSecurityConfig {
            directory: directory.path().to_owned(),
            hosts: vec!["localhost:8770".into()],
            origins: vec!["https://localhost:8770".into()],
        }))
        .unwrap();
        let (first, second) = {
            let mut private = state.private.as_ref().unwrap().lock().unwrap();
            (
                private.credentials.issue("synthetic first", now()).unwrap(),
                private
                    .credentials
                    .issue("synthetic second", now())
                    .unwrap(),
            )
        };
        Self {
            directory,
            state,
            first,
            second,
        }
    }
    async fn context(&self, token: &str, cookie: bool) -> ApiResult<AuthContext> {
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, "localhost:8770".parse().unwrap());
        if cookie {
            headers.insert(
                header::COOKIE,
                format!("ori_session={token}").parse().unwrap(),
            );
        } else {
            headers.insert(
                header::AUTHORIZATION,
                format!("Bearer {token}").parse().unwrap(),
            );
        }
        self.state
            .check(&Method::GET, "/api/v1/health", &headers, ListenerRole::Tls)
            .await
    }
}

#[tokio::test]
#[cfg(not(windows))]
async fn cached_receipt_does_not_read_files_and_failed_fresh_validation_revokes_it() {
    let fixture = Fixture::new();
    let context = fixture.context(&fixture.first, false).await.unwrap();
    let receipt = context.receipt();
    std::fs::rename(
        fixture.directory.path().join("credentials.json"),
        fixture.directory.path().join("offline.json"),
    )
    .unwrap();
    receipt.revalidate().unwrap(); // Pure cache gate, independent of disk availability.
    let original = receipt.clone();
    let cancelled = tokio::spawn(async move { original.revoked().await });
    assert!(fixture.state.validate(&context).await.is_err());
    assert!(receipt.revalidate().is_err());
    tokio::time::timeout(Duration::from_secs(1), cancelled)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
#[cfg(not(windows))]
async fn credential_revocation_invalidates_original_bearer_and_cookie_only() {
    let fixture = Fixture::new();
    let first = fixture.context(&fixture.first, false).await.unwrap();
    let cookie = fixture.context(&fixture.first, true).await.unwrap();
    let other = fixture.context(&fixture.second, false).await.unwrap();
    let original = first.receipt();
    let pending = original.clone();
    let cancelled = tokio::spawn(async move { pending.revoked().await });
    fixture.state.revoke(&first, &json!({})).await.unwrap();
    assert!(original.revalidate().is_err());
    assert!(cookie.receipt().revalidate().is_err());
    other.receipt().revalidate().unwrap();
    tokio::time::timeout(Duration::from_secs(1), cancelled)
        .await
        .unwrap()
        .unwrap();
    assert!(fixture.context(&fixture.first, false).await.is_err());
    // New activity by a different credential cannot renew the original receipt.
    fixture
        .context(&fixture.second, false)
        .await
        .unwrap()
        .receipt()
        .revalidate()
        .unwrap();
    assert!(original.revalidate().is_err());
}

#[cfg(not(windows))]
async fn raw_export_core(fixture: &Fixture) -> Arc<crate::Core> {
    let data = fixture.directory.path().join("data");
    std::fs::create_dir(&data).unwrap();
    let mut config = crate::CoreConfig::replay(data);
    config.lan_security = Some(LanSecurityConfig {
        directory: fixture.directory.path().to_owned(),
        hosts: vec!["localhost:8770".into()],
        origins: vec!["https://localhost:8770".into()],
    });
    let core = crate::Core::open(config).await.unwrap();
    let value = ori_contracts::parse_json_value(
        include_bytes!("../../../../contracts/fixtures/measurement-v1.json"),
        65536,
    )
    .unwrap();
    let mut session = value["session"].clone();
    session["created_at"] = json!("2026-11-11T00:00:00.000000Z");
    core.archive()
        .unwrap()
        .create_session(session)
        .await
        .unwrap();
    core.archive()
        .unwrap()
        .append_sweeps(vec![value["sweep"].clone()])
        .await
        .unwrap();
    core
}
#[cfg(not(windows))]
async fn raw_context(core: &crate::Core, token: &str) -> AuthContext {
    let mut headers = HeaderMap::new();
    headers.insert(header::HOST, "localhost:8770".parse().unwrap());
    headers.insert(
        header::AUTHORIZATION,
        format!("Bearer {token}").parse().unwrap(),
    );
    core.security
        .check(&Method::GET, "/api/v1/health", &headers, ListenerRole::Tls)
        .await
        .unwrap()
}
#[cfg(not(windows))]
async fn raw_response(core: &crate::Core, receipt: AuthReceipt) -> ApiResult<Response> {
    let path = [
        "api",
        "v1",
        "sessions",
        "fixture-сессия",
        "archive",
        "export.ndjson",
    ]
    .map(str::to_owned);
    crate::raw_export::get(core, &path, &std::collections::BTreeMap::new(), receipt)
        .await?
        .ok_or_else(|| ApiError::missing("not_found"))
}

#[cfg(not(windows))]
#[tokio::test]
async fn raw_export_revocation_between_prefix_and_measurement_cannot_emit_prepared_bytes() {
    use futures_util::StreamExt;
    let fixture = Fixture::new();
    let core = raw_export_core(&fixture).await;
    let original = raw_context(&core, &fixture.first).await;
    let response = raw_response(&core, original.receipt()).await.unwrap();
    let mut body = response.into_body().into_data_stream();
    let header = body.next().await.unwrap().unwrap();
    let prefix = body.next().await.unwrap().unwrap();
    core.security.revoke(&original, &json!({})).await.unwrap();
    let next = body.next().await;
    let leaked = matches!(next,Some(Ok(bytes)) if !bytes.is_empty());
    drop(body);
    core.close().await.unwrap();
    assert!(String::from_utf8_lossy(&header).contains("openradioinspector-archive-ndjson"));
    assert_eq!(prefix.as_ref(), b"{\"kind\":\"sweep\",\"measurement\":");
    assert!(
        !leaked,
        "a revoked request emitted its already prepared measurement"
    );
}

#[cfg(not(windows))]
#[tokio::test]
async fn raw_export_revocation_checks_resumed_json_separators_and_ndjson_suffix() {
    use futures_util::StreamExt;
    for (format, consumed) in [
        ("export", 1),
        ("export", 3),
        ("export", 5),
        ("export", 6),
        ("export.ndjson", 3),
    ] {
        let fixture = Fixture::new();
        let core = raw_export_core(&fixture).await;
        let values = ori_contracts::parse_json_value(
            include_bytes!("../../../../contracts/fixtures/measurement-v1.json"),
            65536,
        )
        .unwrap();
        let mut sweep = values["sweep"].clone();
        sweep["id"] = json!("second");
        core.archive()
            .unwrap()
            .append_sweeps(vec![sweep])
            .await
            .unwrap();
        core.archive()
            .unwrap()
            .append_observations(vec![values["observation"].clone()])
            .await
            .unwrap();
        let original = raw_context(&core, &fixture.first).await;
        let path =
            ["api", "v1", "sessions", "fixture-сессия", "archive", format].map(str::to_owned);
        let response = crate::raw_export::get(
            &core,
            &path,
            &std::collections::BTreeMap::new(),
            original.receipt(),
        )
        .await
        .unwrap()
        .unwrap();
        let mut body = response.into_body().into_data_stream();
        for _ in 0..consumed {
            body.next().await.unwrap().unwrap();
        }
        core.security.revoke(&original, &json!({})).await.unwrap();
        let leaked = matches!(body.next().await,Some(Ok(bytes)) if !bytes.is_empty());
        drop(body);
        core.close().await.unwrap();
        assert!(
            !leaked,
            "{format} emitted bytes after chunk {consumed} was revoked"
        );
    }
}

#[cfg(not(windows))]
#[tokio::test]
async fn raw_export_revocation_reaps_two_unpolled_bodies_before_fresh_credential_admission() {
    let fixture = Fixture::new();
    let core = raw_export_core(&fixture).await;
    let original = raw_context(&core, &fixture.first).await;
    let one = raw_response(&core, original.receipt()).await.unwrap();
    let two = raw_response(&core, original.receipt()).await.unwrap();
    let occupied = core
        .background
        .as_ref()
        .unwrap()
        .qos
        .status()
        .registered_work;
    core.security.revoke(&original, &json!({})).await.unwrap();
    let cleared = tokio::time::timeout(Duration::from_secs(1), async {
        while core
            .background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work
            != 0
        {
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .is_ok();
    let fresh = raw_context(&core, &fixture.second).await;
    let third = raw_response(&core, fresh.receipt()).await;
    let admitted = third.is_ok();
    // Keep both old bodies unpolled and alive until AFTER fresh admission.
    drop((one, two, third));
    core.close().await.unwrap();
    assert_eq!(occupied, 2);
    assert!(
        cleared && admitted,
        "revoked unpolled bodies retained both live reader slots"
    );
}

#[cfg(windows)]
#[test]
fn authenticated_lan_receipts_require_verified_owner_only_windows_acl() {
    let directory = tempfile::tempdir().unwrap();
    assert!(TlsIdentity::create(directory.path(), vec!["localhost".into()]).is_err());
    assert!(Credentials::open(directory.path()).is_err());
}

#[cfg(not(windows))]
#[tokio::test]
async fn actual_tls_revocation_during_upload_cannot_mutate_after_bounded_body_read() {
    use crate::{Core, CoreConfig};
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
    };
    let directory = tempfile::tempdir().unwrap();
    let security = directory.path().join("security");
    let identity = TlsIdentity::create(&security, vec!["127.0.0.1".into()]).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let authority = listener.local_addr().unwrap().to_string();
    let url = format!("https://{authority}");
    let mut config = CoreConfig::replay(directory.path());
    config.lan_security = Some(LanSecurityConfig {
        directory: security,
        hosts: vec![authority.clone()],
        origins: vec![url.clone()],
    });
    let core = Core::open(config).await.unwrap();
    let (token, verification) = {
        let mut private = core.security.private.as_ref().unwrap().lock().unwrap();
        (
            private
                .credentials
                .issue("synthetic upload", now())
                .unwrap(),
            private.credentials.issue("synthetic audit", now()).unwrap(),
        )
    };
    let owned = core.clone();
    let serving = tokio::spawn(async move { owned.serve_tls(listener).await });
    let mut roots = rustls::RootCertStore::empty();
    for certificate in rustls_pemfile::certs(&mut identity.trust_anchor_pem().as_bytes()) {
        roots.add(certificate.unwrap()).unwrap();
    }
    let client = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(roots)
    .with_no_client_auth();
    let tcp = TcpStream::connect(&authority).await.unwrap();
    let mut upload = tokio_rustls::TlsConnector::from(Arc::new(client))
        .connect(
            rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(),
            tcp,
        )
        .await
        .unwrap();
    let payload=ori_contracts::to_json_vec(&json!({"id":"revoked-upload","name":"Synthetic upload","streams":[{"id":"receiver","device":"Synthetic only","role":"receiver","level_unit":"s_meter_count","settings":{"adapter":"ardv1-sim"}}]})).unwrap();
    upload.write_all(format!("POST /api/v1/sessions HTTP/1.1\r\nHost: {authority}\r\nAuthorization: Bearer {token}\r\nContent-Length: {}\r\nContent-Type: application/json\r\n\r\n",payload.len()).as_bytes()).await.unwrap();
    upload.write_all(&payload[..1]).await.unwrap();
    // Coordinate on the originating authorization capture, not a timing sleep.
    let digest: [u8; 32] = Sha256::digest(token.as_bytes()).into();
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let captured = core
                .security
                .private
                .as_ref()
                .unwrap()
                .lock()
                .unwrap()
                .receipts
                .get(&digest)
                .and_then(Weak::upgrade)
                .is_some();
            if captured {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let client = reqwest::Client::builder()
        .tls_certs_only([
            reqwest::Certificate::from_pem(identity.trust_anchor_pem().as_bytes()).unwrap(),
        ])
        .build()
        .unwrap();
    let revoked = client
        .post(format!("{url}/api/v1/security/credentials/current/revoke"))
        .bearer_auth(&token)
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    assert_eq!(revoked.status(), 200);
    assert_eq!(
        revoked.json::<Value>().await.unwrap()["data"]["revoked"],
        true
    );
    upload.write_all(&payload[1..]).await.unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(3), upload.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    let health = client
        .get(format!("{url}/api/v1/health"))
        .bearer_auth(verification)
        .send()
        .await
        .unwrap()
        .json::<Value>()
        .await
        .unwrap();
    core.request_shutdown();
    tokio::time::timeout(Duration::from_secs(5), serving)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(
        response.starts_with(b"HTTP/1.1 401"),
        "{}",
        String::from_utf8_lossy(&response)
    );
    assert_eq!(health["data"]["sessions"], 0);
}

#[tokio::test]
async fn loopback_original_receipt_is_cancelled_from_owned_nonruntime_shutdown_thread() {
    let state = SecurityState::open(None).unwrap();
    let context = state
        .check(
            &Method::GET,
            "/api/v1/health",
            &HeaderMap::new(),
            ListenerRole::Plain,
        )
        .await
        .unwrap();
    let original = context.receipt();
    original.revalidate().unwrap();
    let pending = original.clone();
    let cancelled = tokio::spawn(async move { pending.revoked().await });
    let closed = state.clone();
    std::thread::spawn(move || closed.begin_shutdown())
        .join()
        .unwrap();
    assert!(original.revalidate().is_err());
    tokio::time::timeout(Duration::from_secs(1), cancelled)
        .await
        .unwrap()
        .unwrap();
}
