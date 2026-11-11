#![cfg(unix)]
use ori_runtime::supervisor::BackendSpec;
use std::{
    collections::BTreeMap,
    io::{Read, Write},
    net::TcpListener,
    os::unix::net::UnixStream,
    path::Path,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            // The handle still owns an unreaped process; graceful teardown prevents orphan fixtures on assertion failure.
            unsafe {
                libc::kill(self.0.id() as i32, libc::SIGTERM);
            }
            let deadline = Instant::now() + Duration::from_secs(1);
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

fn command(data: &Path, request: serde_json::Value) -> serde_json::Value {
    let mut socket = UnixStream::connect(data.join(".ori-runtime/supervisor.sock")).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    socket
        .write_all(&serde_json::to_vec(&request).unwrap())
        .unwrap();
    socket.shutdown(std::net::Shutdown::Write).unwrap();
    let mut reply = Vec::new();
    socket.read_to_end(&mut reply).unwrap();
    serde_json::from_slice(&reply).unwrap()
}
fn backend(data: &Path, mode: &str) -> BackendSpec {
    let address = TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap();
    BackendSpec {
        name: mode.into(),
        program: "/usr/bin/python3".into(),
        args: vec![
            format!("{}/tests/fake_backend.py", env!("CARGO_MANIFEST_DIR")),
            data.display().to_string(),
            address.port().to_string(),
            mode.into(),
        ],
        environment: BTreeMap::new(),
        control_addr: address,
        ports: vec![address],
        startup_timeout_ms: 2000,
        shutdown_timeout_ms: 2000,
    }
}
fn launch(data: &Path) -> ChildGuard {
    let configuration = data.join("supervisor.json");
    if !configuration.exists() {
        std::fs::write(
            &configuration,
            serde_json::to_vec(
                &serde_json::json!({"data_directory":data,"backend":backend(data,"legacy")}),
            )
            .unwrap(),
        )
        .unwrap();
    }
    let process = ChildGuard(
        Command::new(env!("CARGO_BIN_EXE_ori-supervisor"))
            .arg("run")
            .arg(configuration)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    while !data.join(".ori-runtime/supervisor.sock").exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        data.join(".ori-runtime/supervisor.sock").exists(),
        "supervisor did not expose its private admin socket"
    );
    process
}
fn wait(process: &mut ChildGuard) {
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if let Some(status) = process.0.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
}
#[test]
fn local_service_cli_status_and_shutdown_use_owned_runtime_and_preserve_intent() {
    let temp = tempfile::tempdir().unwrap();
    let mut process = launch(temp.path());
    assert_eq!(
        command(temp.path(), serde_json::json!({"command":"status"}))["status"]["ready"],
        true
    );
    assert_eq!(
        command(temp.path(), serde_json::json!({"command":"shutdown"}))["ok"],
        true
    );
    wait(&mut process);
    assert!(!temp.path().join(".ori-runtime/supervisor.sock").exists());
    let intent: serde_json::Value =
        serde_json::from_slice(&std::fs::read(temp.path().join("shutdown-intent.json")).unwrap())
            .unwrap();
    assert_eq!(intent["preserve_resume_requested"], true);
    assert_eq!(intent["reason"], "shutdown");
}
#[test]
fn sigterm_uses_graceful_control_instead_of_forwarding_termination_to_backend() {
    let temp = tempfile::tempdir().unwrap();
    let mut process = launch(temp.path());
    assert_eq!(
        unsafe { libc::kill(process.0.id() as i32, libc::SIGTERM) },
        0
    );
    wait(&mut process);
    let intent: serde_json::Value =
        serde_json::from_slice(&std::fs::read(temp.path().join("shutdown-intent.json")).unwrap())
            .unwrap();
    assert_eq!(intent["preserve_resume_requested"], true);
    assert_eq!(intent["reason"], "shutdown");
}
#[test]
fn successful_explicit_selection_survives_service_restart() {
    let temp = tempfile::tempdir().unwrap();
    let mut process = launch(temp.path());
    assert_eq!(
        command(
            temp.path(),
            serde_json::json!({"command":"cutover","candidate":backend(temp.path(),"candidate")})
        )["outcome"],
        "activated"
    );
    command(temp.path(), serde_json::json!({"command":"shutdown"}));
    wait(&mut process);
    let mut restarted = launch(temp.path());
    command(temp.path(), serde_json::json!({"command":"shutdown"}));
    wait(&mut restarted);
    assert_eq!(
        std::fs::read_to_string(temp.path().join("committed-data")).unwrap(),
        "legacy\ncandidate\ncandidate\n"
    );
}
#[test]
fn selection_persistence_failure_rolls_back_and_retains_candidate_commits() {
    let temp = tempfile::tempdir().unwrap();
    let mut process = launch(temp.path());
    std::fs::create_dir(temp.path().join(".ori-runtime/selection.json")).unwrap();
    let reply = command(
        temp.path(),
        serde_json::json!({"command":"cutover","candidate":backend(temp.path(),"candidate")}),
    );
    assert_eq!(reply["outcome"], "rolled_back");
    assert_eq!(
        std::fs::read_to_string(temp.path().join("committed-data")).unwrap(),
        "legacy\ncandidate\nlegacy\n"
    );
    command(temp.path(), serde_json::json!({"command":"shutdown"}));
    wait(&mut process);
}

#[test]
fn termination_during_incomplete_startup_still_closes_the_owned_backend_gracefully() {
    let temp = tempfile::tempdir().unwrap();
    let mut backend = backend(temp.path(), "not-ready");
    backend.startup_timeout_ms = 10000;
    let address = backend.control_addr;
    let configuration = temp.path().join("supervisor.json");
    std::fs::write(
        &configuration,
        serde_json::to_vec(&serde_json::json!({"data_directory":temp.path(),"backend":backend}))
            .unwrap(),
    )
    .unwrap();
    let mut process = ChildGuard(
        Command::new(env!("CARGO_BIN_EXE_ori-supervisor"))
            .arg("run")
            .arg(configuration)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    while std::net::TcpStream::connect(address).is_err() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(
        unsafe { libc::kill(process.0.id() as i32, libc::SIGTERM) },
        0
    );
    wait(&mut process);
    let intent: serde_json::Value =
        serde_json::from_slice(&std::fs::read(temp.path().join("shutdown-intent.json")).unwrap())
            .unwrap();
    assert_eq!(intent["preserve_resume_requested"], true);
}
