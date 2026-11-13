#![cfg(unix)]
#[path = "support/backend_python.rs"]
mod backend_python;

use ori_runtime::{lease::WriterLease, service::ServiceConfig, supervisor::BackendSpec};
use std::{
    cell::Cell,
    collections::BTreeMap,
    io::{self, Read, Write},
    net::TcpListener,
    os::unix::{net::UnixStream, process::ExitStatusExt},
    path::Path,
    process::{Child, Command, ExitStatus, Stdio},
    rc::Rc,
    time::{Duration, Instant},
};
struct ChildGuard(Child, Rc<Cell<Option<ExitStatus>>>);
impl ChildGuard {
    fn new(child: Child) -> Self {
        Self(child, Rc::new(Cell::new(None)))
    }
    fn cleanup(&mut self) -> io::Result<ExitStatus> {
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
        let status = self.0.wait()?;
        self.1.set(Some(status));
        Ok(status)
    }
}
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

// Recheck the backend that the service actually selected, including restart
// selection precedence. Missing evidence is never proof of backend cleanup.
fn backend_released(data: &Path) -> bool {
    let selection = data.join(".ori-runtime/selection.json");
    let selected = if selection.exists() {
        std::fs::File::open(selection)
            .and_then(bounded_read)
            .ok()
            .filter(|(_, truncated)| !truncated)
            .and_then(|(bytes, _)| serde_json::from_slice::<BackendSpec>(&bytes).ok())
    } else {
        std::fs::File::open(data.join("supervisor.json"))
            .and_then(bounded_read)
            .ok()
            .filter(|(_, truncated)| !truncated)
            .and_then(|(bytes, _)| serde_json::from_slice::<ServiceConfig>(&bytes).ok())
            .map(|config| config.backend)
    };
    let Some(selected) = selected else {
        return false;
    };
    let Ok(_writer) = WriterLease::acquire(data) else {
        return false;
    };
    selected
        .ports
        .iter()
        .map(TcpListener::bind)
        .collect::<io::Result<Vec<_>>>()
        .is_ok()
}

fn bounded_read(reader: impl Read) -> io::Result<(Vec<u8>, bool)> {
    let mut bytes = Vec::new();
    reader.take(4097).read_to_end(&mut bytes)?;
    let truncated = bytes.len() > 4096;
    bytes.truncate(4096);
    Ok((bytes, truncated))
}

fn startup_evidence(data: &Path, child: &mut Child) -> serde_json::Value {
    // The original supervisor Child has been waited and writer/ports released.
    // Its controlled CLI prints one exit error; backend stderr is null. Read
    // only a bounded prefix and format fixed classes, never raw stderr or paths.
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
        .and_then(bounded_read);
    let mut error_class = "evidence_unavailable";
    let stderr_read_error = stderr
        .as_ref()
        .err()
        .map(|error| format!("{:?}", error.kind()));
    let mut stderr_truncated = false;
    let mut backend_read_error = None;
    let mut backend_truncated = false;
    let mut backend_invalid = false;
    let mut backend_phases = Vec::new();
    if let Ok((bytes, truncated)) = stderr {
        stderr_truncated = truncated;
        let text = std::str::from_utf8(&bytes).unwrap_or_default();
        error_class = if text.contains("candidate readiness deadline exceeded") {
            "candidate_readiness_deadline"
        } else if text.contains("candidate exited before readiness") {
            "candidate_exited_before_readiness"
        } else if text.contains("refusing to replace foreign admin socket") {
            "foreign_admin_refused"
        } else if text.contains("ori-supervisor: I/O failure:") {
            "io_failure"
        } else if bytes.is_empty() {
            "none"
        } else {
            "unclassified"
        };
    }
    const PHASES: [&str; 12] = [
        "python_entered",
        "imports_complete",
        "writer_acquired",
        "committed",
        "socket_create_started",
        "socket_created",
        "socket_bound",
        "listen_started",
        "listener_bound",
        "status_replied",
        "shutdown_accepted",
        "closed",
    ];
    match std::fs::File::open(data.join("backend-startup.ndjson")).and_then(bounded_read) {
        Ok((bytes, truncated)) => {
            backend_truncated = truncated;
            for line in bytes
                .split(|byte| *byte == b'\n')
                .filter(|line| !line.is_empty())
            {
                let event = serde_json::from_slice::<serde_json::Value>(line).ok();
                let milestone = event.as_ref().and_then(|event| {
                    let phase = PHASES
                        .iter()
                        .find(|phase| Some(**phase) == event["phase"].as_str())?;
                    Some((*phase, event["elapsed_us"].as_u64()?))
                });
                if let Some(milestone) = milestone.filter(|_| backend_phases.len() < 12) {
                    backend_phases.push(milestone);
                } else {
                    backend_invalid = true;
                }
            }
        }
        Err(error) => backend_read_error = Some(format!("{:?}", error.kind())),
    }
    serde_json::json!({
        "error_class":error_class,
        "stderr_read_error":stderr_read_error,
        "stderr_truncated":stderr_truncated,
        "backend_read_error":backend_read_error,
        "backend_truncated":backend_truncated,
        "backend_invalid":backend_invalid,
        "backend_phases":backend_phases,
    })
}

fn command(data: &Path, request: serde_json::Value) -> serde_json::Value {
    let mut socket =
        UnixStream::connect(ori_runtime::service::admin_socket_path(data).unwrap()).unwrap();
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
        program: backend_python::program(),
        args: vec![
            format!("{}/tests/fake_backend.py", env!("CARGO_MANIFEST_DIR")),
            data.display().to_string(),
            address.port().to_string(),
            mode.into(),
        ],
        environment: BTreeMap::from([("ORI_TEST_BACKEND_DIAGNOSTICS".into(), "1".into())]),
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
    let mut process = ChildGuard::new(
        Command::new(env!("CARGO_BIN_EXE_ori-supervisor"))
            .arg("run")
            .arg(configuration)
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    while !ori_runtime::service::admin_socket_path(data)
        .unwrap()
        .exists()
        && Instant::now() < deadline
    {
        std::thread::sleep(Duration::from_millis(10));
    }
    let exposed = ori_runtime::service::admin_socket_path(data)
        .unwrap()
        .exists();
    if !exposed {
        let cleanup = process.cleanup();
        let released = cleanup.is_ok() && backend_released(data);
        let evidence = if released {
            startup_evidence(data, &mut process.0)
        } else {
            serde_json::Value::Null
        };
        assert!(
            exposed,
            "supervisor did not expose its private admin socket; supervisor_exit={:?}; supervisor_signal={:?}; supervisor_wait_error={:?}; backend_released={released}; bounded evidence after cleanup={evidence}",
            cleanup.as_ref().ok().and_then(ExitStatus::code),
            cleanup.as_ref().ok().and_then(ExitStatusExt::signal),
            cleanup.as_ref().err().map(io::Error::kind),
        );
    }
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
    assert!(
        !ori_runtime::service::admin_socket_path(temp.path())
            .unwrap()
            .exists()
    );
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
    let mut process = ChildGuard::new(
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

#[test]
fn configured_python_runs_the_full_service_cli_fixture() {
    use std::os::unix::fs::PermissionsExt;
    let temp = tempfile::tempdir().unwrap();
    let wrapper = temp.path().join("configured-python");
    let marker = temp.path().join("configured-python-used");
    std::fs::write(
        &wrapper,
        b"#!/bin/sh\nprintf 'selected\\n' > \"$ORI_SERVICE_TEST_PYTHON_MARKER\"\nexec \"$ORI_SERVICE_TEST_REAL_PYTHON\" \"$@\"\n",
    )
    .unwrap();
    std::fs::set_permissions(&wrapper, std::fs::Permissions::from_mode(0o700)).unwrap();
    // A fresh process owns this environment and runs the complete original
    // startup/status/shutdown body, including the retained Child's wait.
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "local_service_cli_status_and_shutdown_use_owned_runtime_and_preserve_intent",
        ])
        .env("ORI_TEST_PYTHON", &wrapper)
        .env("ORI_SERVICE_TEST_REAL_PYTHON", backend_python::program())
        .env("ORI_SERVICE_TEST_PYTHON_MARKER", &marker)
        .output()
        .unwrap();
    let selected = std::fs::read_to_string(marker);
    assert!(
        output.status.success(),
        "configured service fixture failed after child cleanup: {:?}",
        output.status.code()
    );
    assert_eq!(
        selected.ok().as_deref(),
        Some("selected\n"),
        "full service startup ignored the explicitly configured interpreter"
    );
}

#[test]
fn failed_readiness_preserves_the_primary_assertion_after_owned_cleanup() {
    let temp = tempfile::tempdir().unwrap();
    let spec = backend(temp.path(), "not-ready");
    let address = spec.control_addr;
    std::fs::write(
        temp.path().join("supervisor.json"),
        serde_json::to_vec(&serde_json::json!({"data_directory":temp.path(),"backend":spec}))
            .unwrap(),
    )
    .unwrap();
    let failed = std::panic::catch_unwind(|| launch(temp.path()));
    let panic = match failed {
        Ok(process) => {
            drop(process);
            panic!("a non-ready backend unexpectedly exposed an admin socket");
        }
        Err(panic) => panic,
    };
    let message = panic
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| panic.downcast_ref::<&str>().copied())
        .unwrap();
    // The full real HTTP startup reached its existing 2s readiness deadline.
    // Both resources must be released after the original Child was waited.
    let writer_released = WriterLease::acquire(temp.path()).is_ok();
    let port_released = TcpListener::bind(address).is_ok();
    assert!(writer_released && port_released);
    assert!(message.starts_with("supervisor did not expose its private admin socket"));
    assert!(message.contains("supervisor_exit=Some(1)"));
    assert!(message.contains("backend_released=true"));
    assert!(!message.contains(temp.path().to_str().unwrap()));
    let evidence: serde_json::Value = serde_json::from_str(
        message
            .split_once("; bounded evidence after cleanup=")
            .unwrap()
            .1,
    )
    .unwrap();
    assert_eq!(evidence["error_class"], "candidate_readiness_deadline");
    for flag in ["stderr_truncated", "backend_truncated", "backend_invalid"] {
        assert_eq!(evidence[flag], false);
    }
    for error in ["stderr_read_error", "backend_read_error"] {
        assert_eq!(evidence[error], serde_json::Value::Null);
    }
    let phases = evidence["backend_phases"].as_array().unwrap();
    assert!(!phases.is_empty() && phases.len() <= 12);
    assert!(phases.iter().any(|phase| phase[0] == "status_replied"));

    // Unknown selected metadata must not fall back to the baseline's released
    // port and incorrectly authorize reading failure evidence.
    let selection = temp.path().join(".ori-runtime/selection.json");
    let mut oversized = spec;
    oversized
        .environment
        .insert("padding".into(), "x".repeat(8192));
    std::fs::write(&selection, serde_json::to_vec(&oversized).unwrap()).unwrap();
    assert!(!backend_released(temp.path()));
    std::fs::write(&selection, b"{").unwrap();
    assert!(!backend_released(temp.path()));
    std::fs::remove_file(selection).unwrap();
    assert!(backend_released(temp.path()));
}

#[test]
fn assertion_unwind_waits_the_original_child_before_backend_resources_are_reused() {
    let temp = tempfile::tempdir().unwrap();
    let spec = backend(temp.path(), "legacy");
    let address = spec.control_addr;
    std::fs::write(
        temp.path().join("supervisor.json"),
        serde_json::to_vec(&serde_json::json!({"data_directory":temp.path(),"backend":spec}))
            .unwrap(),
    )
    .unwrap();
    let process = launch(temp.path());
    let original_child_wait = process.1.clone();
    let failed = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || {
        let _owned = process;
        panic!("controlled fixture assertion unwind");
    }));
    let joined = original_child_wait.get();
    let writer_released = WriterLease::acquire(temp.path()).is_ok();
    let port_released = TcpListener::bind(address).is_ok();
    assert!(failed.is_err());
    assert!(joined.is_some_and(|status| status.success()));
    assert!(writer_released && port_released);
    let intent: serde_json::Value =
        serde_json::from_slice(&std::fs::read(temp.path().join("shutdown-intent.json")).unwrap())
            .unwrap();
    assert_eq!(intent["preserve_resume_requested"], true);
    assert_eq!(intent["reason"], "shutdown");
}
