#![cfg(unix)]
// This deliberate pre-exec pause must have its own process: a peer test's
// temporary listener reservation could otherwise also be inherited and held.
#[path = "support/backend_python.rs"]
mod backend_python;
#[path = "support/fork_before_exec.rs"]
mod fork_before_exec;

use ori_runtime::{
    Error,
    lease::WriterLease,
    supervisor::{BackendSpec, Supervisor},
};
use std::{
    collections::BTreeMap,
    fs,
    io::{self, Read},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    path::Path,
    process::Command,
};

const STARTUP_DIAGNOSTIC_BYTES: usize = 4096;

#[derive(Debug)]
struct StartupDiagnostics {
    trace: String,
    truncated: bool,
    read_error: Option<io::ErrorKind>,
}

fn startup_diagnostics(data: &Path) -> StartupDiagnostics {
    let mut bytes = Vec::new();
    let result = fs::File::open(data.join("backend-startup.ndjson")).and_then(|file| {
        file.take((STARTUP_DIAGNOSTIC_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
    });
    let truncated = bytes.len() > STARTUP_DIAGNOSTIC_BYTES;
    bytes.truncate(STARTUP_DIAGNOSTIC_BYTES);
    StartupDiagnostics {
        trace: String::from_utf8_lossy(&bytes).into_owned(),
        truncated,
        read_error: result.err().map(|error| error.kind()),
    }
}

#[tokio::test]
async fn dropped_writer_probe_cannot_block_start_through_an_unrelated_fork() {
    let temp = tempfile::tempdir().unwrap();
    let addr = TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    let probe = WriterLease::acquire(temp.path()).unwrap();
    let mut unrelated = fork_before_exec::ForkBeforeExec::spawn().unwrap();
    let excluded_while_owned = WriterLease::acquire(temp.path());
    drop(probe);

    // The unrelated child is still before exec: CLOEXEC alone cannot release
    // its inherited copy of a probe descriptor when the parent drops the probe.
    let started = supervisor
        .start(BackendSpec {
            name: "candidate".into(),
            program: backend_python::program(),
            args: vec![
                format!("{}/tests/fake_backend.py", env!("CARGO_MANIFEST_DIR")),
                temp.path().display().to_string(),
                addr.port().to_string(),
                "candidate".into(),
            ],
            environment: BTreeMap::from([("ORI_TEST_BACKEND_DIAGNOSTICS".into(), "1".into())]),
            control_addr: addr,
            ports: vec![addr],
            startup_timeout_ms: 2000,
            shutdown_timeout_ms: 2000,
        })
        .await;
    let shutdown = supervisor.shutdown().await;
    let unrelated = unrelated.finish();
    let released_after_cleanup = WriterLease::acquire(temp.path());
    // Read evidence only after both original owned children confirmed cleanup.
    // An unavailable trace is evidence missing, never a startup cause.
    let diagnostics = (shutdown.is_ok() && matches!(&unrelated, Ok(status) if status.success()))
        .then(|| startup_diagnostics(temp.path()));

    // Reap both owned children before any assertion, including the expected RED.
    assert!(matches!(excluded_while_owned, Err(Error::Busy)));
    assert!(shutdown.is_ok(), "backend cleanup failed: {shutdown:?}");
    assert!(
        matches!(unrelated, Ok(status) if status.success()),
        "unrelated child cleanup failed: {unrelated:?}"
    );
    assert!(released_after_cleanup.is_ok());
    assert!(
        started.is_ok(),
        "full startup failed while an unrelated child waited for exec: {started:?}; bounded startup evidence after cleanup: {diagnostics:?}"
    );
    let diagnostics = diagnostics.unwrap();
    assert!(
        diagnostics.read_error.is_none() && !diagnostics.truncated,
        "startup diagnostics unavailable or truncated after successful startup: {diagnostics:?}"
    );
    let events: Vec<serde_json::Value> = diagnostics
        .trace
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert!(!events.is_empty() && events.len() <= 12);
    for phase in [
        "python_entered",
        "writer_acquired",
        "status_replied",
        "closed",
    ] {
        assert!(
            events.iter().any(|event| event["phase"] == phase),
            "missing real backend milestone {phase}: {diagnostics:?}"
        );
    }
}

#[test]
fn configured_python_runs_the_owned_fork_fixture() {
    let temp = tempfile::tempdir().unwrap();
    let wrapper = temp.path().join("configured-python");
    let marker = temp.path().join("configured-python-used");
    fs::write(
        &wrapper,
        b"#!/bin/sh\nprintf 'selected\\n' > \"$ORI_FORK_TEST_PYTHON_MARKER\"\nexec \"$ORI_FORK_TEST_REAL_PYTHON\" \"$@\"\n",
    )
    .unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o700)).unwrap();
    let python = std::env::var_os("ORI_TEST_PYTHON").unwrap_or_else(|| "/usr/bin/python3".into());
    // A fresh test process owns the configured environment. Its full original
    // startup/barrier oracle and every owned-child JOIN run before assertions.
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "dropped_writer_probe_cannot_block_start_through_an_unrelated_fork",
            "--nocapture",
        ])
        .env("ORI_TEST_PYTHON", &wrapper)
        .env("ORI_FORK_TEST_REAL_PYTHON", python)
        .env("ORI_FORK_TEST_PYTHON_MARKER", &marker)
        .output()
        .unwrap();
    let marker = fs::read_to_string(marker);
    assert!(
        output.status.success(),
        "configured fixture failed after child cleanup (stdout/stderr prefixes capped at 4096 bytes each): {}{}",
        String::from_utf8_lossy(&output.stdout[..output.stdout.len().min(4096)]),
        String::from_utf8_lossy(&output.stderr[..output.stderr.len().min(4096)])
    );
    assert_eq!(
        marker.ok().as_deref(),
        Some("selected\n"),
        "full startup ignored the explicitly configured interpreter"
    );
}
