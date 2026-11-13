#![cfg(unix)]
// This deliberate pre-exec pause must have its own process: a peer test's
// temporary listener reservation could otherwise also be inherited and held.
#[path = "support/fork_before_exec.rs"]
mod fork_before_exec;

use ori_runtime::{
    Error,
    lease::WriterLease,
    supervisor::{BackendSpec, Supervisor},
};
use std::{collections::BTreeMap, net::TcpListener};

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
            program: "/usr/bin/python3".into(),
            args: vec![
                format!("{}/tests/fake_backend.py", env!("CARGO_MANIFEST_DIR")),
                temp.path().display().to_string(),
                addr.port().to_string(),
                "candidate".into(),
            ],
            environment: BTreeMap::new(),
            control_addr: addr,
            ports: vec![addr],
            startup_timeout_ms: 2000,
            shutdown_timeout_ms: 2000,
        })
        .await;
    let shutdown = supervisor.shutdown().await;
    let unrelated = unrelated.finish();
    let released_after_cleanup = WriterLease::acquire(temp.path());

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
        "dropped writer probe blocked start while an unrelated child waited for exec: {started:?}"
    );
}
