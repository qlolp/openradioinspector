use ori_core::{Core, CoreConfig};
use serde_json::Value;
use std::{path::Path, time::Duration};
use tokio::net::TcpListener;

#[tokio::test]
async fn packaged_modern_client_is_embedded_offline_with_exact_chunks_and_legacy_fallback() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::new(directory.path())).await.unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let running = core.clone();
    let server = tokio::spawn(async move { running.serve(listener).await });
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../radioinspector/web/modern");
    let mut results = Vec::new();
    let mut expected = Vec::new();
    if source.join("modern-build.json").is_file() {
        let manifest: Value =
            serde_json::from_slice(&std::fs::read(source.join("modern-build.json")).unwrap())
                .unwrap();
        assert_eq!(manifest["offline"], true);
        for file in manifest["files"].as_array().unwrap() {
            let name = file.as_str().unwrap();
            let response = client
                .get(format!("{base}/modern/{name}"))
                .send()
                .await
                .unwrap();
            results.push((
                response.status().as_u16(),
                response
                    .headers()
                    .get("x-content-type-options")
                    .unwrap()
                    .to_str()
                    .unwrap()
                    .to_owned(),
                response.bytes().await.unwrap(),
            ));
            expected.push(std::fs::read(source.join(name)).unwrap());
        }
        for path in ["/modern", "/modern/"] {
            let response = client.get(format!("{base}{path}")).send().await.unwrap();
            results.push((
                response.status().as_u16(),
                response
                    .headers()
                    .get("x-content-type-options")
                    .unwrap()
                    .to_str()
                    .unwrap()
                    .to_owned(),
                response.bytes().await.unwrap(),
            ));
            expected.push(std::fs::read(source.join("index.html")).unwrap());
        }
    } else {
        let response = client.get(format!("{base}/modern/")).send().await.unwrap();
        assert_eq!(response.status().as_u16(), 503);
    }
    let legacy = client.get(format!("{base}/")).send().await.unwrap();
    let legacy_status = legacy.status();
    let legacy_body = legacy.bytes().await.unwrap();
    std::fs::write(directory.path().join("private.txt"), b"never public").unwrap();
    let mut rejected = Vec::new();
    for path in [
        "/modern/private.txt",
        "/modern/assets/../../private.txt",
        "/modern/assets/%2e%2e/private.txt",
        "/modern/assets/.ori-runtime/identity.json",
        "/modern/modern-build.json",
    ] {
        rejected.push(
            client
                .get(format!("{base}{path}"))
                .send()
                .await
                .unwrap()
                .status()
                .as_u16(),
        );
    }
    core.request_shutdown();
    tokio::time::timeout(Duration::from_secs(5), server)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    for ((status, nosniff, bytes), source) in results.iter().zip(expected) {
        assert_eq!(*status, 200);
        assert_eq!(nosniff, "nosniff");
        assert_eq!(bytes.as_ref(), source);
    }
    assert_eq!(legacy_status.as_u16(), 200);
    assert!(std::str::from_utf8(&legacy_body).unwrap().contains("<html"));
    assert!(rejected.iter().all(|status| *status == 404));
    assert!(!directory.path().join("sessions.sqlite3").exists());
}
