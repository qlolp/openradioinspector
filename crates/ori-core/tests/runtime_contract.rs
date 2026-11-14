//! Real HTTP serves the shared generated candidate variant, not a second schema.
use ori_contracts::{RUNTIME_OPENAPI_V1_JSON, parse_json_value};
use ori_core::{Core, CoreConfig};
use std::time::Duration;

#[tokio::test]
async fn served_openapi_is_exact_shared_runtime_variant_with_reviewed_support_tags() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::new(directory.path())).await.unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!(
        "http://{}/api/v1/openapi.json",
        listener.local_addr().unwrap()
    );
    let owned = core.clone();
    let serving = tokio::spawn(async move { owned.serve(listener).await });
    let response = reqwest::get(url).await.unwrap();
    let status = response.status();
    let actual = parse_json_value(&response.bytes().await.unwrap(), 16 * 1024 * 1024).unwrap();
    core.request_shutdown();
    tokio::time::timeout(Duration::from_secs(3), serving)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(status, 200);
    let expected = parse_json_value(RUNTIME_OPENAPI_V1_JSON.as_bytes(), 16 * 1024 * 1024).unwrap();
    assert!(
        actual == expected,
        "HTTP OpenAPI must equal the canonical generated runtime variant"
    );
    let operations = actual["paths"]
        .as_object()
        .unwrap()
        .values()
        .flat_map(|path| path.as_object().unwrap().values())
        .filter_map(|operation| operation["operationId"].as_str())
        .collect::<Vec<_>>();
    assert!(!operations.is_empty());
    assert_eq!(
        operations.len(),
        operations
            .iter()
            .collect::<std::collections::BTreeSet<_>>()
            .len(),
        "canonical operation IDs must remain unique"
    );
}
