use std::{
    io::Write,
    path::Path,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
struct Process(Child);
#[cfg(not(feature = "serial"))]
#[test]
fn native_serial_flag_fails_closed_when_connector_feature_was_not_built() {
    let output = Command::new(env!("CARGO_BIN_EXE_ori-core"))
        .args(["--allow-serial", "--stdin-lifeline", "--port", "0"])
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("serial support was not compiled"));
}
impl Drop for Process {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            if let Some(mut stdin) = self.0.stdin.take() {
                let _ = stdin.flush();
                drop(stdin);
            }
            let deadline = Instant::now() + Duration::from_secs(2);
            while matches!(self.0.try_wait(), Ok(None)) && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
            if matches!(self.0.try_wait(), Ok(None)) {
                let _ = self.0.kill();
            }
        }
        let _ = self.0.wait();
    }
}
fn launch(data: &Path, ready: &Path) -> Process {
    let mut command = Command::new(env!("CARGO_BIN_EXE_ori-core"));
    command
        .args([
            "--mode",
            "replay",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--stdin-lifeline",
            "--data-dir",
        ])
        .arg(data)
        .arg("--ready-file")
        .arg(ready)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for (key, _) in std::env::vars() {
        if key.starts_with("ORI_RUNTIME_") {
            command.env_remove(key);
        }
    }
    Process(command.spawn().unwrap())
}
#[test]
fn cli_ready_file_and_stdin_lifeline_close_the_owned_core_and_writer() {
    let directory = tempfile::tempdir().unwrap();
    let ready = directory.path().join("ready.url");
    let mut process = launch(directory.path(), &ready);
    let deadline = Instant::now() + Duration::from_secs(5);
    while !ready.exists() && Instant::now() < deadline {
        if let Some(status) = process.0.try_wait().unwrap() {
            panic!("candidate exited before readiness: {status}");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        ready.exists(),
        "candidate did not publish an owned readiness file"
    );
    assert!(
        directory.path().join("sessions.sqlite3").exists(),
        "CLI silently selected a different database from legacy LocalCore"
    );
    let url = std::fs::read_to_string(&ready).unwrap();
    assert!(url.starts_with("http://127.0.0.1:"));
    drop(process.0.stdin.take());
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = process.0.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        assert!(
            Instant::now() < deadline,
            "EOF did not stop this owned child"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(!ready.exists());
    let _lease = ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap();
}
#[test]
fn cli_cannot_overwrite_or_delete_an_existing_readiness_file() {
    let directory = tempfile::tempdir().unwrap();
    let ready = directory.path().join("ready.url");
    std::fs::write(&ready, b"existing-owner\n").unwrap();
    let mut process = launch(directory.path(), &ready);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = process.0.try_wait().unwrap() {
            assert!(!status.success());
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(std::fs::read(ready).unwrap(), b"existing-owner\n");
}

#[test]
fn explicit_lan_initialization_creates_private_identity_once_and_never_rotates_silently() {
    let directory = tempfile::tempdir().unwrap();
    let run = || {
        Command::new(env!("CARGO_BIN_EXE_ori-core"))
            .args(["init-lan", "--data-dir"])
            .arg(directory.path())
            .args(["--name", "127.0.0.1"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .unwrap()
    };
    assert!(run().success());
    let identity = directory.path().join(".ori-runtime/security/identity.json");
    let before = std::fs::read(&identity).unwrap();
    assert!(!run().success());
    assert_eq!(std::fs::read(identity).unwrap(), before);
}

#[cfg(unix)]
#[tokio::test]
async fn cli_tls_listener_local_pairing_and_verified_readiness_work_without_a_port_reservation_race()
 {
    let directory = tempfile::tempdir().unwrap();
    let ready = directory.path().join("ready.url");
    let security = directory.path().join("security");
    std::fs::create_dir(&security).unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&security, std::fs::Permissions::from_mode(0o700)).unwrap();
    let identity =
        ori_runtime::security::TlsIdentity::create(&security, vec!["127.0.0.1".into()]).unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_ori-core"));
    command
        .args([
            "--mode",
            "replay",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--lan",
            "--trusted-host",
            "127.0.0.1:0",
            "--stdin-lifeline",
            "--data-dir",
        ])
        .arg(directory.path())
        .arg("--tls-state")
        .arg(&security)
        .arg("--ready-file")
        .arg(&ready)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for (key, _) in std::env::vars() {
        if key.starts_with("ORI_RUNTIME_") {
            command.env_remove(key);
        }
    }
    let mut process = Process(command.spawn().unwrap());
    let deadline = Instant::now() + Duration::from_secs(5);
    while !ready.exists() && Instant::now() < deadline {
        if let Some(status) = process.0.try_wait().unwrap() {
            panic!("TLS candidate exited before readiness: {status}");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let url = std::fs::read_to_string(&ready).unwrap();
    let url = url.trim();
    assert!(url.starts_with("https://127.0.0.1:"));
    let authority = url.trim_start_matches("https://");
    let port = authority.rsplit(':').next().unwrap();
    let status = Command::new(env!("CARGO_BIN_EXE_ori-core"))
        .args([
            "--check-ready",
            "--lan",
            "--host",
            "127.0.0.1",
            "--port",
            port,
            "--trusted-host",
            authority,
            "--data-dir",
        ])
        .arg(directory.path())
        .arg("--tls-state")
        .arg(&security)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .unwrap();
    assert!(
        status.success(),
        "readiness bypassed or failed local CA verification"
    );
    let local = ori_core::admin::request(directory.path(), 60, 3)
        .await
        .unwrap();
    let code = local["pairing_code"].as_str().unwrap();
    let client = reqwest::Client::builder()
        .tls_certs_only([
            reqwest::Certificate::from_pem(identity.trust_anchor_pem().as_bytes()).unwrap(),
        ])
        .build()
        .unwrap();
    let response=client.post(format!("{url}/api/v1/security/pair")).json(&serde_json::json!({"pairing_code":code,"label":"synthetic CLI native","client":"native"})).send().await.unwrap();
    assert_eq!(response.status(), 200);
    drop(process.0.stdin.take());
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = process.0.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[tokio::test]
async fn explicit_analysis_program_flags_are_owned_argv_and_report_only_configured_capabilities() {
    let directory = tempfile::tempdir().unwrap();
    let ready = directory.path().join("ready.url");
    let program = std::env::var_os("ORI_TEST_PYTHON").unwrap_or_else(|| "/usr/bin/python3".into());
    let mut command = Command::new(env!("CARGO_BIN_EXE_ori-core"));
    command
        .args([
            "--mode",
            "replay",
            "--port",
            "0",
            "--stdin-lifeline",
            "--analysis-python",
        ])
        .arg(program)
        .args(["--research-best-effort", "--data-dir"])
        .arg(directory.path())
        .arg("--ready-file")
        .arg(&ready)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for (key, _) in std::env::vars() {
        if key.starts_with("ORI_RUNTIME_") {
            command.env_remove(key);
        }
    }
    let mut process = Process(command.spawn().unwrap());
    let deadline = Instant::now() + Duration::from_secs(5);
    while !ready.exists() {
        if let Some(status) = process.0.try_wait().unwrap() {
            panic!("analysis-configured owned child exited: {status}");
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let url = std::fs::read_to_string(&ready).unwrap();
    let value = reqwest::get(format!("{}/api/v1/capabilities", url.trim()))
        .await
        .unwrap()
        .json::<serde_json::Value>()
        .await
        .unwrap();
    assert_eq!(value["data"]["features"]["sweep_comparison"], true);
    assert_eq!(value["data"]["candidate"]["release_accepted"], false);
    drop(process.0.stdin.take());
    let deadline = Instant::now() + Duration::from_secs(5);
    while process.0.try_wait().unwrap().is_none() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
}
