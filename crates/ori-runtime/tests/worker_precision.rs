#![cfg(unix)]
use ori_runtime::worker::{Cancellation, ResearchPool, WorkerSpec};
use std::collections::BTreeMap;

fn spec() -> WorkerSpec {
    WorkerSpec {
        program: std::env::var_os("ORI_TEST_PYTHON")
            .map(Into::into)
            .unwrap_or_else(|| "/usr/bin/python3".into()),
        args: vec![format!(
            "{}/tests/worker_echo.py",
            env!("CARGO_MANIFEST_DIR")
        )],
        environment: BTreeMap::new(),
        deadline_ms: 5000,
        require_hard_memory: false,
        linux_cgroup: None,
    }
}

#[tokio::test]
async fn child_environment_excludes_parent_credentials_and_user_python_configuration() {
    // A fresh copy of this test owns its synthetic parent environment. Do not
    // mutate the concurrent Rust test runner's process-global environment.
    if std::env::var_os("ORI_WORKER_ENV_TEST_CHILD").is_none() {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("--exact")
            .arg("child_environment_excludes_parent_credentials_and_user_python_configuration")
            .arg("--nocapture")
            .env("ORI_WORKER_ENV_TEST_CHILD", "1")
            .env("ORI_RUNTIME_CONTROL_TOKEN", "synthetic-test-token")
            .env("HOME", "/synthetic/inherited/home")
            .env("PYTHONPATH", "/synthetic/untrusted/modules")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "isolated environment probe failed: {}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }
    let mut config = spec();
    config.environment = BTreeMap::from([
        ("PATH".into(), "/usr/bin:/bin".into()),
        ("LC_ALL".into(), "C".into()),
        ("TZ".into(), "UTC".into()),
    ]);
    let request =
        br#"{"version":1,"request_id":"env","operation":"analysis","payload":"environment"}"#
            .to_vec();
    let value = ResearchPool::default()
        .run(config, request, Cancellation::default())
        .await
        .unwrap()
        .response;
    assert_eq!(
        value["result"]["inherited_private_keys"],
        serde_json::json!([])
    );
    assert_eq!(value["result"]["path"], "/usr/bin:/bin");
    assert_eq!(value["result"]["timezone"], "UTC");
    assert_eq!(value["result"]["locale"], "C");
    assert_eq!(value["result"]["no_user_site"], "1");
    assert_eq!(value["result"]["utf8"], "1");
    for (_, value) in value["result"]["threads"].as_object().unwrap() {
        assert_eq!(value, "1");
    }
}

#[tokio::test]
async fn worker_environment_refuses_explicit_private_or_code_injection_variables() {
    for key in [
        "ORI_RUNTIME_CONTROL_TOKEN",
        "PYTHONPATH",
        "HOME",
        "LD_PRELOAD",
    ] {
        let mut config = spec();
        config
            .environment
            .insert(key.into(), "synthetic-test-value".into());
        let request =
            br#"{"version":1,"request_id":"invalid-env","operation":"analysis","payload":"echo"}"#
                .to_vec();
        assert!(matches!(
            ResearchPool::default()
                .run(config, request, Cancellation::default())
                .await,
            Err(ori_runtime::Error::Unsafe(_))
        ));
    }
}

#[tokio::test]
async fn structured_scientific_errors_survive_expected_nonzero_exit() {
    let request=br#"{"version":1,"request_id":"validation","operation":"analysis","payload":"expected-error"}"#.to_vec();
    let result = ResearchPool::default()
        .run(spec(), request, Cancellation::default())
        .await
        .unwrap();
    assert_eq!(result.response["ok"], false);
    assert_eq!(result.response["error"]["api_code"], "invalid_request");
    assert_eq!(result.response["error"]["http_status"], 400);
}

#[tokio::test]
async fn opaque_metadata_float_bits_and_large_integer_tokens_survive_both_ipc_boundaries() {
    let raw = br#"{"version":1,"request_id":"exact","operation":"analysis","payload":{"value":-0.0,"big":18446744073709551616,"literal":{"$serde_json::private::Number":"123"}}}"#.to_vec();
    let value = ResearchPool::default()
        .run(spec(), raw, Cancellation::default())
        .await
        .unwrap()
        .response;
    assert_eq!(
        value["result"]["value"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(value["result"]["big"].to_string(), "18446744073709551616");
    assert_eq!(
        value["result"]["literal"]["$serde_json::private::Number"],
        "123"
    );
}

#[tokio::test]
async fn only_private_scientific_operations_can_use_the_larger_bounded_envelope() {
    let payload = "x".repeat(3 * 1024 * 1024);
    let pool = ResearchPool::default();
    let request = |operation| {
        serde_json::to_vec(&serde_json::json!({"version":1,"request_id":"large","operation":operation,"payload":payload})).unwrap()
    };
    for operation in [
        "summary",
        "occupancy",
        "export",
        "control-analysis",
        "arbitrary",
    ] {
        assert!(matches!(
            pool.run(spec(), request(operation), Cancellation::default())
                .await,
            Err(ori_runtime::Error::Unsafe(_))
        ));
    }
    for operation in ["analysis", "control_analysis"] {
        let result = pool
            .run(spec(), request(operation), Cancellation::default())
            .await
            .unwrap();
        assert_eq!(
            result.response["result"].as_str().unwrap().len(),
            payload.len()
        );
        let oversized = serde_json::to_vec(&serde_json::json!({
            "version":1,"request_id":"too-large","operation":operation,
            "payload":"x".repeat(16 * 1024 * 1024)
        }))
        .unwrap();
        assert!(
            pool.run(spec(), oversized, Cancellation::default())
                .await
                .is_err()
        );
    }
}
