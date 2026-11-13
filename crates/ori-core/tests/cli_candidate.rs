use sha2::Digest;
use std::{
    io::{Read, Seek, Write},
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
    launch_mode(data, ready, "replay")
}
fn launch_mode(data: &Path, ready: &Path, mode: &str) -> Process {
    let mut command = Command::new(env!("CARGO_BIN_EXE_ori-core"));
    command
        .args([
            "--mode",
            mode,
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

async fn wait_for_cli_ready(process: &mut Process, ready: &Path) -> String {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !ready.exists() {
        assert!(
            process.0.try_wait().unwrap().is_none(),
            "core exited before readiness"
        );
        assert!(Instant::now() < deadline, "core readiness deadline");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    std::fs::read_to_string(ready).unwrap()
}

async fn cli_json(
    url: &str,
    method: &str,
    path: &str,
    body: Option<&serde_json::Value>,
    status: u16,
) -> serde_json::Value {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap();
    let mut request = client.request(method.parse().unwrap(), format!("{url}{path}"));
    if let Some(body) = body {
        request = request
            .header("Content-Type", "application/json")
            .body(ori_contracts::to_json_vec(body).unwrap());
    }
    let response = request.send().await.unwrap();
    assert_eq!(response.status().as_u16(), status);
    ori_contracts::parse_json_value(&response.bytes().await.unwrap(), 16 * 1024 * 1024).unwrap()
}

async fn stop_cli(process: &mut Process) {
    drop(process.0.stdin.take());
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = process.0.try_wait().unwrap() {
            assert!(status.success());
            return;
        }
        assert!(Instant::now() < deadline, "core did not release its writer");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

#[tokio::test]
async fn cli_live_preserves_sources_and_single_writer_across_restart() {
    let directory = tempfile::tempdir().unwrap();
    let ready = directory.path().join("ready.url");
    let fixture = ori_contracts::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        65536,
    )
    .unwrap();
    let session = ori_contracts::encode_path_segment(fixture["session"]["id"].as_str().unwrap());
    let base = format!("/api/v1/sessions/{session}");
    let mut process = launch_mode(directory.path(), &ready, "live");
    let url = wait_for_cli_ready(&mut process, &ready).await;
    let caps = cli_json(&url, "GET", "/api/v1/capabilities", None, 200).await;
    assert_eq!(caps["data"]["candidate"]["mode"], "live");
    assert_eq!(caps["data"]["persistent_archive"], true);
    assert_eq!(caps["data"]["candidate"]["release_accepted"], false);
    assert_eq!(caps["data"]["candidate"]["hardware_validation"], "not_run");
    assert_eq!(caps["data"]["features"]["explicit_serial_adapters"], false);
    assert_eq!(caps["data"]["features"]["field_strength_rms_v2"], false);
    assert_eq!(caps["data"]["features"]["analog_audio_inputs"], false);
    assert!(ori_runtime::lease::WriterLease::acquire(directory.path()).is_err());
    cli_json(
        &url,
        "POST",
        "/api/v1/sessions",
        Some(&fixture["session"]),
        201,
    )
    .await;
    cli_json(
        &url,
        "POST",
        &format!("{base}/start"),
        Some(&serde_json::json!({})),
        200,
    )
    .await;
    let workers = cli_json(&url, "GET", "/api/v1/workers", None, 200).await;
    let workers = workers["data"].as_array().unwrap();
    assert_eq!(workers.len(), 2, "the API retains idle stream descriptors");
    assert!(
        workers.iter().all(|worker| worker["alive"] == false),
        "session start must not connect instruments"
    );
    for (resource, key) in [("sweeps", "sweep"), ("observations", "observation")] {
        cli_json(
            &url,
            "POST",
            &format!("{base}/{resource}"),
            Some(&fixture[key]),
            201,
        )
        .await;
    }
    stop_cli(&mut process).await;
    assert!(!ready.exists());
    drop(ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap());
    let mut process = launch_mode(directory.path(), &ready, "live");
    let url = wait_for_cli_ready(&mut process, &ready).await;
    let sweeps = cli_json(&url, "GET", &format!("{base}/sweeps?limit=100"), None, 200).await;
    assert_eq!(sweeps["data"].as_array().unwrap().len(), 1);
    let sweep = &sweeps["data"][0];
    assert_eq!(sweep["id"], fixture["sweep"]["id"]);
    assert_eq!(sweep["frequencies_hz"], fixture["sweep"]["frequencies_hz"]);
    assert_eq!(
        sweep["interval"]["started_at"],
        "2026-11-11T12:00:00.123456Z"
    );
    for (level, bits) in sweep["levels"].as_array().unwrap().iter().zip([
        0xc059080000000000_u64,
        0x8000000000000000,
        0xbff0000000000001,
        0xc040600000000000,
    ]) {
        assert_eq!(level.as_f64().unwrap().to_bits(), bits);
    }
    let observations = cli_json(
        &url,
        "GET",
        &format!("{base}/observations?limit=100"),
        None,
        200,
    )
    .await;
    assert_eq!(observations["data"].as_array().unwrap().len(), 1);
    assert_eq!(observations["data"][0]["id"], fixture["observation"]["id"]);
    assert_eq!(observations["data"][0]["level_unit"], "s_meter_count");
    assert_eq!(observations["data"][0]["level"], 137.0);
    stop_cli(&mut process).await;
}

#[test]
fn cli_live_requires_an_explicit_data_directory_before_creating_state() {
    let directory = tempfile::tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_ori-core"))
        .args(["--mode", "live", "--port", "0", "--stdin-lifeline"])
        .env("HOME", directory.path())
        .env("LOCALAPPDATA", directory.path())
        .env_remove("ORI_RUNTIME_DATA_DIR")
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("live mode requires an explicit --data-dir")
    );
    assert_eq!(std::fs::read_dir(directory.path()).unwrap().count(), 0);
}

#[cfg(feature = "serial")]
#[test]
fn native_serial_cli_refuses_shadow_and_replay_before_opening_an_archive() {
    for mode in ["shadow", "replay"] {
        let directory = tempfile::tempdir().unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_ori-core"))
            .args([
                "--mode",
                mode,
                "--allow-serial",
                "--port",
                "0",
                "--stdin-lifeline",
                "--data-dir",
            ])
            .arg(directory.path())
            .stdin(Stdio::null())
            .output()
            .unwrap();
        assert!(
            !output.status.success(),
            "physical serial was admitted in {mode}"
        );
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("native serial requires --mode live")
        );
        assert!(!directory.path().join("sessions.sqlite3").exists());
    }
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

struct LanInitOutcome {
    code: Option<i32>,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

fn lan_init_outcome_matches_platform(
    windows: bool,
    repeated: bool,
    outcome: &LanInitOutcome,
) -> bool {
    if !windows && !repeated {
        return outcome.code == Some(0) && outcome.stderr.is_empty();
    }
    let expected: &[u8] = if windows {
        b"ori-core: runtime ownership failed: invalid or unsafe state: private LAN state requires a verified owner-only Windows DACL; this build supports explicit loopback mode only\n"
    } else {
        b"ori-core: runtime ownership failed: invalid or unsafe state: identity already exists; explicit re-enrollment is required for rotation\n"
    };
    outcome.code == Some(1) && outcome.stdout.is_empty() && outcome.stderr == expected
}

fn bounded_lan_capture(file: &mut std::fs::File) -> std::io::Result<Vec<u8>> {
    file.rewind()?;
    let mut bytes = Vec::new();
    file.take(4097).read_to_end(&mut bytes)?;
    if bytes.len() > 4096 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "init-lan output exceeded the capture bound",
        ));
    }
    Ok(bytes)
}

fn run_lan_init(data: &Path) -> LanInitOutcome {
    let mut stdout = tempfile::tempfile().unwrap();
    let mut stderr = tempfile::tempfile().unwrap();
    let status = Command::new(env!("CARGO_BIN_EXE_ori-core"))
        .args(["init-lan", "--data-dir"])
        .arg(data)
        .args(["--name", "127.0.0.1"])
        .stdout(Stdio::from(stdout.try_clone().unwrap()))
        .stderr(Stdio::from(stderr.try_clone().unwrap()))
        .status()
        .unwrap();
    // Capture only after the original child wait; pipes cannot hold up its exit.
    LanInitOutcome {
        code: status.code(),
        stdout: bounded_lan_capture(&mut stdout).unwrap(),
        stderr: bounded_lan_capture(&mut stderr).unwrap(),
    }
}

fn assert_lan_init_outcome(repeated: bool, outcome: &LanInitOutcome) {
    let stderr_sha256: String = sha2::Sha256::digest(&outcome.stderr)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    assert!(
        lan_init_outcome_matches_platform(cfg!(windows), repeated, outcome),
        "Unexpected init-lan outcome: windows={}, repeated={repeated}, exit={:?}, stdout_bytes={}, stderr_bytes={}, stderr_sha256={stderr_sha256}",
        cfg!(windows),
        outcome.code,
        outcome.stdout.len(),
        outcome.stderr.len(),
    );
}

#[test]
fn lan_init_capture_rejects_truncation_and_read_errors() {
    let mut file = tempfile::tempfile().unwrap();
    file.write_all(&[b'x'; 4096]).unwrap();
    assert!(bounded_lan_capture(&mut file).unwrap() == [b'x'; 4096]);
    file.set_len(0).unwrap();
    file.rewind().unwrap();
    file.write_all(&[b'x'; 4097]).unwrap();
    assert!(
        matches!(bounded_lan_capture(&mut file), Err(error) if error.kind() == std::io::ErrorKind::InvalidData),
        "oversized init-lan output was accepted"
    );
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("write-only");
    std::fs::write(&path, b"synthetic capture").unwrap();
    let mut write_only = std::fs::OpenOptions::new().write(true).open(path).unwrap();
    assert!(
        bounded_lan_capture(&mut write_only).is_err(),
        "init-lan capture read error was ignored"
    );
}

#[test]
fn lan_init_oracle_rejects_unexpected_platform_status_and_output() {
    let windows_denial = b"ori-core: runtime ownership failed: invalid or unsafe state: private LAN state requires a verified owner-only Windows DACL; this build supports explicit loopback mode only\n";
    let existing_denial = b"ori-core: runtime ownership failed: invalid or unsafe state: identity already exists; explicit re-enrollment is required for rotation\n";
    let outcome = |code, stdout: &[u8], stderr: &[u8]| LanInitOutcome {
        code,
        stdout: stdout.to_vec(),
        stderr: stderr.to_vec(),
    };
    let mut extra_windows_stderr = windows_denial.to_vec();
    extra_windows_stderr.extend_from_slice(b"unexpected suffix\n");
    let mut extra_existing_stderr = existing_denial.to_vec();
    extra_existing_stderr.extend_from_slice(b"unexpected suffix\n");
    for (name, result, expected) in [
        (
            "created",
            outcome(Some(0), b"installation report\n", b""),
            [true, false, false, false],
        ),
        (
            "Windows DACL refusal",
            outcome(Some(1), b"", windows_denial),
            [false, false, true, true],
        ),
        (
            "existing Unix identity",
            outcome(Some(1), b"", existing_denial),
            [false, true, false, false],
        ),
        (
            "Windows wrong exit",
            outcome(Some(2), b"", windows_denial),
            [false; 4],
        ),
        (
            "Windows missing exit",
            outcome(None, b"", windows_denial),
            [false; 4],
        ),
        (
            "arbitrary failure",
            outcome(Some(1), b"", b"ori-core: candidate I/O failed: synthetic\n"),
            [false; 4],
        ),
        (
            "Windows extra stdout",
            outcome(Some(1), b"unexpected output\n", windows_denial),
            [false; 4],
        ),
        (
            "Windows extra stderr",
            outcome(Some(1), b"", &extra_windows_stderr),
            [false; 4],
        ),
        (
            "missing failure message",
            outcome(Some(1), b"", b""),
            [false; 4],
        ),
        (
            "success with stderr",
            outcome(Some(0), b"installation report\n", b"unexpected stderr\n"),
            [false; 4],
        ),
        (
            "repeat wrong exit",
            outcome(Some(2), b"", existing_denial),
            [false; 4],
        ),
        (
            "repeat missing exit",
            outcome(None, b"", existing_denial),
            [false; 4],
        ),
        (
            "repeat extra stdout",
            outcome(Some(1), b"unexpected output\n", existing_denial),
            [false; 4],
        ),
        (
            "repeat extra stderr",
            outcome(Some(1), b"", &extra_existing_stderr),
            [false; 4],
        ),
    ] {
        for ((windows, repeated), expected) in
            [(false, false), (false, true), (true, false), (true, true)]
                .into_iter()
                .zip(expected)
        {
            assert_eq!(
                lan_init_outcome_matches_platform(windows, repeated, &result),
                expected,
                "{name}: windows={windows}, repeated={repeated}, exit={:?}",
                result.code
            );
        }
    }
}

#[test]
fn explicit_lan_initialization_creates_private_identity_once_and_never_rotates_silently() {
    let directory = tempfile::tempdir().unwrap();
    let security = directory.path().join(".ori-runtime/security");
    let identity = security.join("identity.json");
    let ca = security.join("public-ca.pem");
    let first = run_lan_init(directory.path());
    assert_lan_init_outcome(false, &first);
    if cfg!(windows) {
        let assert_absent = || {
            for path in [&security, &identity, &ca] {
                assert!(
                    !path.try_exists().unwrap(),
                    "Windows init-lan created security material before verified DACL support"
                );
            }
        };
        assert_absent();
        let repeated = run_lan_init(directory.path());
        assert_lan_init_outcome(true, &repeated);
        assert_absent();

        std::fs::create_dir(&security).unwrap();
        let foreign_identity = b"foreign-identity-sentinel";
        let foreign_ca = b"foreign-public-ca-sentinel";
        std::fs::write(&identity, foreign_identity).unwrap();
        std::fs::write(&ca, foreign_ca).unwrap();
        let entries = || {
            std::fs::read_dir(&security)
                .unwrap()
                .map(|entry| entry.unwrap().file_name())
                .collect::<std::collections::BTreeSet<_>>()
        };
        let before_entries = entries();
        let foreign = run_lan_init(directory.path());
        assert_lan_init_outcome(true, &foreign);
        assert!(
            std::fs::read(&identity).unwrap() == foreign_identity,
            "Windows init-lan modified a foreign identity"
        );
        assert!(
            std::fs::read(&ca).unwrap() == foreign_ca,
            "Windows init-lan modified a foreign public CA"
        );
        assert_eq!(entries(), before_entries);
    } else {
        let before_identity = std::fs::read(&identity).unwrap();
        let before_ca = std::fs::read(&ca).unwrap();
        let loaded = ori_runtime::security::TlsIdentity::load(&security).unwrap();
        let fingerprint = loaded.fingerprint_sha256();
        assert_eq!(fingerprint.len(), 64);
        assert!(
            before_ca == loaded.trust_anchor_pem().as_bytes(),
            "init-lan public CA does not match the private identity"
        );
        let repeated = run_lan_init(directory.path());
        assert_lan_init_outcome(true, &repeated);
        assert!(
            std::fs::read(&identity).unwrap() == before_identity,
            "Repeated init-lan modified the private identity"
        );
        assert!(
            std::fs::read(&ca).unwrap() == before_ca,
            "Repeated init-lan modified the public CA"
        );
        assert!(
            ori_runtime::security::TlsIdentity::load(&security)
                .unwrap()
                .fingerprint_sha256()
                == fingerprint,
            "Repeated init-lan changed the reloaded identity"
        );
    }
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
