#![cfg(unix)]
use ori_core::{Core, CoreConfig, acceptance::Acceptance};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{fs, sync::Arc};

fn profile(root: &std::path::Path, data: &std::path::Path) -> std::path::PathBuf {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(root, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(data, fs::Permissions::from_mode(0o700)).unwrap();
    let path = root.join("profile.json");
    let hash = Sha256::digest(fs::read(std::env::current_exe().unwrap()).unwrap())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    fs::write(
        &path,
        serde_json::to_vec(&json!({"version":1,"run_id":"real-commit-test",
        "output_dir":root,"data_dir":data,"expected_core_sha256":hash}))
        .unwrap(),
    )
    .unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    path
}
#[cfg(unix)]
#[tokio::test]
async fn explicit_acceptance_records_original_source_only_after_successful_commit() {
    let root = tempfile::tempdir().unwrap();
    let data = root.path().join("data");
    fs::create_dir(&data).unwrap();
    let recorder = Arc::new(Acceptance::open(&profile(root.path(), &data), &data).unwrap());
    let mut config = CoreConfig::replay(&data);
    config.acceptance = Some(recorder.clone());
    let core = Core::open(config).await.unwrap();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let owned = core.clone();
    let serving = tokio::spawn(async move { owned.serve(listener).await });
    let client = reqwest::Client::new();
    let base = format!("http://{address}/api/v1/sessions");
    assert!(
        client
            .post(&base)
            .json(&fixture["session"])
            .send()
            .await
            .unwrap()
            .status()
            .is_success()
    );
    let url = format!(
        "{}/{}/sweeps",
        base,
        fixture["session"]["id"].as_str().unwrap()
    );
    assert!(
        client
            .post(&url)
            .json(&fixture["sweep"])
            .send()
            .await
            .unwrap()
            .status()
            .is_success()
    );
    assert!(
        !client
            .post(&url)
            .json(&fixture["sweep"])
            .send()
            .await
            .unwrap()
            .status()
            .is_success()
    );
    core.request_shutdown();
    serving.await.unwrap().unwrap();
    core.close().await.unwrap();
    let rows: Vec<Value> = fs::read_to_string(root.path().join("core.jsonl"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let commits: Vec<_> = rows
        .iter()
        .filter(|row| row["stage"] == "committed" && row["kind"] == "sweep")
        .collect();
    assert_eq!(commits.len(), 1);
    assert_eq!(commits[0]["source_id"], fixture["sweep"]["id"]);
    assert!(commits[0]["sequence"].as_u64().unwrap() > 0);
    assert!(commits[0]["at_ns"].as_u64().unwrap() > 0);
}
#[cfg(unix)]
#[test]
fn mismatched_executable_or_linked_profile_cannot_enable_acceptance() {
    let root = tempfile::tempdir().unwrap();
    let data = root.path().join("data");
    fs::create_dir(&data).unwrap();
    let path = profile(root.path(), &data);
    let mut value: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    value["expected_core_sha256"] = json!("0".repeat(64));
    fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
    assert!(Acceptance::open(&path, &data).is_err());
    assert!(!root.path().join("core.jsonl").exists());
    std::os::unix::fs::symlink(&path, root.path().join("linked.json")).unwrap();
    assert!(Acceptance::open(&root.path().join("linked.json"), &data).is_err());
}

#[tokio::test]
async fn actual_core_cli_accepts_private_backup_without_exposing_an_http_operation() {
    use std::{
        io::Write,
        os::unix::fs::{OpenOptionsExt, PermissionsExt},
        process::{Command, Stdio},
        time::{Duration, Instant},
    };
    let root = tempfile::tempdir().unwrap();
    let data = root.path().join("data");
    fs::create_dir(&data).unwrap();
    fs::set_permissions(&data, fs::Permissions::from_mode(0o700)).unwrap();
    let path = profile(root.path(), &data);
    let mut value: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    value["expected_core_sha256"] = json!(
        Sha256::digest(fs::read(env!("CARGO_BIN_EXE_ori-core")).unwrap())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
    );
    fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
    let ready = root.path().join("ready.url");
    let log = fs::File::create(root.path().join("core.log")).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_ori-core"))
        .args([
            "--mode",
            "replay",
            "--port",
            "0",
            "--stdin-lifeline",
            "--data-dir",
        ])
        .arg(&data)
        .arg("--ready-file")
        .arg(&ready)
        .arg("--acceptance-profile")
        .arg(&path)
        .stdin(Stdio::piped())
        .stdout(log.try_clone().unwrap())
        .stderr(log)
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    while !ready.exists() && child.try_wait().unwrap().is_none() && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    if !ready.exists() {
        let _ = child.kill();
        let _ = child.wait();
        panic!(
            "packaged CLI did not start: {}",
            fs::read_to_string(root.path().join("core.log")).unwrap()
        );
    }
    let url = fs::read_to_string(&ready).unwrap();
    let client = reqwest::Client::new();
    assert_eq!(
        client
            .post(format!("{}/api/v1/acceptance/backup", url.trim()))
            .send()
            .await
            .unwrap()
            .status(),
        501
    );
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap();
    let base = format!("{}/api/v1/sessions", url.trim());
    assert!(
        client
            .post(&base)
            .json(&fixture["session"])
            .send()
            .await
            .unwrap()
            .status()
            .is_success()
    );
    let source_url = format!(
        "{}/{}/sweeps",
        base,
        fixture["session"]["id"].as_str().unwrap()
    );
    assert!(
        client
            .post(&source_url)
            .json(&fixture["sweep"])
            .send()
            .await
            .unwrap()
            .status()
            .is_success()
    );
    let mut request = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(root.path().join("backup.pending"))
        .unwrap();
    request.write_all(b"real-commit-test\n").unwrap();
    request.sync_all().unwrap();
    fs::hard_link(
        root.path().join("backup.pending"),
        root.path().join("backup.request"),
    )
    .unwrap();
    fs::remove_file(root.path().join("backup.pending")).unwrap();
    let finish = root.path().join("backup-finished.json");
    while !finish.exists() && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    drop(child.stdin.take());
    let result = child.wait().unwrap();
    assert!(
        result.success(),
        "{}",
        fs::read_to_string(root.path().join("core.log")).unwrap()
    );
    let receipt: Value = serde_json::from_slice(&fs::read(finish).unwrap()).unwrap();
    assert_eq!(receipt["phase"], "completed");
    assert!(root.path().join("backup").is_dir());
    let destination = root.path().join("restored.sqlite");
    let receipt = root.path().join("restore.json");
    let result = Command::new(env!("CARGO_BIN_EXE_ori-core"))
        .arg("acceptance-restore")
        .arg(root.path().join("backup"))
        .arg(&destination)
        .arg(&receipt)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let db = rusqlite::Connection::open(&destination).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM sweeps", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
    assert!(receipt.is_file());
}
