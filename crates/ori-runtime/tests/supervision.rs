#![cfg(unix)]
#[path = "support/backend_ports.rs"]
mod backend_ports;
#[path = "support/backend_python.rs"]
mod backend_python;

use ori_runtime::{Error, lease::WriterLease, supervisor::*};
use std::{
    collections::BTreeMap,
    io::{self, Read},
    net::{SocketAddr, TcpListener},
    path::Path,
};
fn spec(data: &Path, mode: &str, address: SocketAddr) -> BackendSpec {
    BackendSpec {
        name: mode.into(),
        program: backend_python::program(),
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
fn address() -> SocketAddr {
    backend_ports::address()
}

#[test]
fn fixture_ports_do_not_reissue_released_addresses_or_use_client_ports() {
    let released = TcpListener::bind("127.0.0.1:0").unwrap();
    let a = released.local_addr().unwrap();
    let held = TcpListener::bind("127.0.0.1:0").unwrap();
    let b = held.local_addr().unwrap();
    let spare = TcpListener::bind("127.0.0.1:0").unwrap();
    let c = spare.local_addr().unwrap();
    drop((released, spare));
    let mut issued = std::collections::BTreeSet::new();
    let select = |issued: &mut std::collections::BTreeSet<u16>, candidates: Vec<u16>| {
        backend_ports::select_from(issued, 1..=1023, candidates).unwrap()
    };
    assert_eq!(select(&mut issued, vec![a.port()]), a);
    // Force the same still-free port to be offered again, and a genuinely held
    // listener. Neither may be assigned to another fixture during cutover.
    assert_eq!(select(&mut issued, vec![a.port(), b.port(), c.port()]), c);
    assert_eq!(held.local_addr().unwrap(), b);
    let mut fresh = std::collections::BTreeSet::new();
    assert!(backend_ports::select_from(&mut fresh, a.port()..=a.port(), [a.port()]).is_err());
    assert!(fresh.is_empty());
    assert!(TcpListener::bind(a).is_ok());
}

#[test]
fn independent_fixture_allocators_cannot_claim_the_same_temporarily_unbound_port() {
    let first = TcpListener::bind("127.0.0.1:0").unwrap();
    let second = TcpListener::bind("127.0.0.1:0").unwrap();
    let a = first.local_addr().unwrap();
    let b = second.local_addr().unwrap();
    drop((first, second));
    // Separate test executables each have their own issued set. During a real
    // cutover neither backend binds its address; the fixture claim must survive.
    let mut one = std::collections::BTreeSet::new();
    let mut two = std::collections::BTreeSet::new();
    assert_eq!(
        backend_ports::select_from(&mut one, 1..=1023, [a.port()]).unwrap(),
        a
    );
    assert_eq!(
        backend_ports::select_from(&mut two, 1..=1023, [a.port(), b.port()]).unwrap(),
        b
    );
}
fn options() -> CutoverOptions {
    CutoverOptions {
        automatic: false,
        acknowledge_coverage_gap: true,
    }
}

fn diagnostic_spec(data: &Path, mode: &str, address: SocketAddr) -> BackendSpec {
    let mut backend = spec(data, mode, address);
    backend
        .environment
        .insert("ORI_TEST_BACKEND_DIAGNOSTICS".into(), "1".into());
    backend
}

fn resources_released(data: &Path, ports: &[SocketAddr]) -> bool {
    let Ok(_writer) = WriterLease::acquire(data) else {
        return false;
    };
    ports
        .iter()
        .map(TcpListener::bind)
        .collect::<io::Result<Vec<_>>>()
        .is_ok()
}

fn trace_snapshot(data: &Path) -> serde_json::Value {
    let mut bytes = Vec::new();
    let read = std::fs::File::open(data.join("backend-startup.ndjson"))
        .and_then(|file| file.take(4097).read_to_end(&mut bytes));
    let truncated = bytes.len() > 4096;
    bytes.truncate(4096);
    let mut phases = Vec::new();
    let mut invalid = false;
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
    for line in bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
    {
        let event = serde_json::from_slice::<serde_json::Value>(line).ok();
        let milestone = event.as_ref().and_then(|event| {
            let phase = PHASES.iter().find(|phase| Some(**phase) == event["phase"].as_str())?;
            let pid = u32::try_from(event["pid"].as_u64()?).ok()?;
            Some(serde_json::json!({"phase":phase,"pid":pid,"elapsed_us":event["elapsed_us"].as_u64()?}))
        });
        if let Some(milestone) = milestone.filter(|_| phases.len() < 12) {
            phases.push(milestone);
        } else {
            invalid = true;
        }
    }
    serde_json::json!({
        "read_error":read.err().map(|error| format!("{:?}",error.kind())),
        "truncated":truncated,"invalid":invalid,"phases":phases,
    })
}

fn result_class<T>(result: &ori_runtime::Result<T>) -> &'static str {
    match result {
        Ok(_) => "ok",
        Err(Error::Protocol(message)) if message == "candidate readiness deadline exceeded" => {
            "readiness_deadline"
        }
        Err(Error::Protocol(message)) if message == "candidate exited before readiness" => {
            "candidate_exited"
        }
        Err(Error::Protocol(_)) => "protocol",
        Err(Error::Io(_)) => "io",
        Err(Error::Unsafe(_)) => "unsafe",
        Err(Error::Busy) => "busy",
        Err(_) => "other",
    }
}

struct Attempt {
    result: ori_runtime::Result<RuntimeStatus>,
    backend_running: ori_runtime::Result<bool>,
    released: bool,
    trace_reset_error: Option<io::ErrorKind>,
    trace: Option<serde_json::Value>,
    cleanup: Option<ori_runtime::Result<()>>,
    released_after_cleanup: Option<bool>,
}
impl Attempt {
    fn evidence(&self) -> serde_json::Value {
        serde_json::json!({"start":result_class(&self.result),
            "backend_running":self.backend_running.as_ref().ok(),
            "backend_status":result_class(&self.backend_running),
            "released_before_cleanup":self.released,
            "cleanup":self.cleanup.as_ref().map(result_class).unwrap_or("not_called"),
            "released_after_cleanup":self.released_after_cleanup,
            "trace_reset_error":self.trace_reset_error.map(|error| format!("{error:?}")),
            "trace":self.trace})
    }
}

fn reset_trace(data: &Path, previous: Option<&Attempt>) -> Result<(), io::ErrorKind> {
    let path = data.join("backend-startup.ndjson");
    let result = match previous {
        // A snapshot must already be retained before its private file is removed.
        Some(previous)
            if previous.result.is_err()
                && matches!(&previous.backend_running, Ok(false))
                && previous.released
                && previous.trace.is_some()
                && previous.cleanup.is_none() =>
        {
            std::fs::remove_file(path)
        }
        Some(_) => return Err(io::ErrorKind::WouldBlock),
        None => match std::fs::symlink_metadata(path) {
            Ok(_) => return Err(io::ErrorKind::AlreadyExists),
            Err(error) => Err(error),
        },
    };
    match result {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.kind()),
    }
}

async fn attempt(
    supervisor: &mut Supervisor,
    data: &Path,
    backend: BackendSpec,
    previous: Option<&Attempt>,
) -> Attempt {
    let ports = backend.ports.clone();
    let trace_reset_error = reset_trace(data, previous).err();
    // Evidence rotation never gates the real start or changes its deadlines.
    let result = supervisor.start(backend).await;
    let backend_running = supervisor.backend_running();
    let released = resources_released(data, &ports);
    // No remedial shutdown/stop occurs here: the subsequent real start must
    // detect any retained active backend from a rejected attempt.
    let trace = (trace_reset_error.is_none()
        && result.is_err()
        && matches!(&backend_running, Ok(false))
        && released)
        .then(|| trace_snapshot(data));
    Attempt {
        result,
        backend_running,
        released,
        trace_reset_error,
        trace,
        cleanup: None,
        released_after_cleanup: None,
    }
}

async fn final_cleanup(
    supervisor: &mut Supervisor,
    data: &Path,
    ports: &[SocketAddr],
    last: &mut Attempt,
) {
    // The healthy start result has already been observed. This teardown can
    // release a leaked negative backend, but cannot turn its Busy result green.
    let cleanup = supervisor.shutdown().await;
    let inactive = matches!(supervisor.backend_running(), Ok(false));
    let released = resources_released(data, ports);
    if cleanup.is_ok() && inactive && released && last.trace_reset_error.is_none() {
        last.trace = Some(trace_snapshot(data));
    }
    last.cleanup = Some(cleanup);
    last.released_after_cleanup = Some(released);
}

#[tokio::test]
async fn failed_candidate_is_fully_stopped_before_rollback_preserves_latest_data() {
    let temp = tempfile::tempdir().unwrap();
    let addr = address();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    supervisor
        .start(spec(temp.path(), "legacy", addr))
        .await
        .unwrap();
    let result = supervisor
        .cutover(spec(temp.path(), "crash", addr), options())
        .await
        .unwrap();
    assert!(matches!(result, CutoverOutcome::RolledBack { .. }));
    assert!(supervisor.status().await.unwrap().ready);
    supervisor.shutdown().await.unwrap();
    assert_eq!(
        std::fs::read_to_string(temp.path().join("committed-data")).unwrap(),
        "legacy\ncrash\nlegacy\n"
    );
    let journal =
        std::fs::read_to_string(temp.path().join(".ori-runtime/coverage.ndjson")).unwrap();
    assert!(journal.contains("coverage_interruption_started"));
    assert!(journal.contains("coverage_interruption_ended"));
}

#[tokio::test]
async fn armed_automatic_cutover_is_refused_explicit_ack_preserves_resume_intent() {
    let temp = tempfile::tempdir().unwrap();
    let addr = address();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    supervisor
        .start(spec(temp.path(), "armed", addr))
        .await
        .unwrap();
    assert!(
        supervisor
            .cutover(
                spec(temp.path(), "candidate", addr),
                CutoverOptions {
                    automatic: true,
                    acknowledge_coverage_gap: true
                }
            )
            .await
            .is_err()
    );
    assert!(supervisor.status().await.unwrap().guard_armed);
    assert!(
        supervisor
            .cutover(
                spec(temp.path(), "candidate", addr),
                CutoverOptions {
                    automatic: false,
                    acknowledge_coverage_gap: false
                }
            )
            .await
            .is_err()
    );
    assert!(matches!(
        supervisor
            .cutover(spec(temp.path(), "candidate", addr), options())
            .await
            .unwrap(),
        CutoverOutcome::Activated
    ));
    let intent: serde_json::Value =
        serde_json::from_slice(&std::fs::read(temp.path().join("shutdown-intent.json")).unwrap())
            .unwrap();
    assert_eq!(intent["preserve_resume_requested"], true);
    assert_eq!(intent["acknowledge_coverage_gap"], true);
    supervisor.shutdown().await.unwrap();
}

#[tokio::test]
async fn occupied_legacy_port_and_foreign_writer_require_orderly_handover() {
    let temp = tempfile::tempdir().unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    assert!(
        supervisor
            .start(spec(
                temp.path(),
                "candidate",
                listener.local_addr().unwrap()
            ))
            .await
            .is_err()
    );
    assert!(listener.local_addr().is_ok());
    drop(listener);
    let _writer = ori_runtime::lease::WriterLease::acquire(temp.path()).unwrap();
    assert!(
        supervisor
            .start(spec(temp.path(), "candidate", address()))
            .await
            .is_err()
    );
    assert!(!temp.path().join("committed-data").exists());
}

#[tokio::test]
async fn readiness_requires_actual_writer_lease_and_failed_start_releases_owned_process() {
    let temp = tempfile::tempdir().unwrap();
    let addr = address();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    let mut liar = diagnostic_spec(temp.path(), "no-writer", addr);
    liar.startup_timeout_ms = 200;
    let negative = attempt(&mut supervisor, temp.path(), liar, None).await;
    let mut healthy = attempt(
        &mut supervisor,
        temp.path(),
        diagnostic_spec(temp.path(), "legacy", addr),
        Some(&negative),
    )
    .await;
    final_cleanup(&mut supervisor, temp.path(), &[addr], &mut healthy).await;
    drop(supervisor);
    let released = resources_released(temp.path(), &[addr]);
    let evidence = serde_json::json!({"negative":negative.evidence(),"healthy":healthy.evidence()});
    assert!(
        negative.result.is_err(),
        "readiness trusted a flag without an actual writer lease; {evidence}"
    );
    assert!(
        healthy.result.is_ok(),
        "healthy startup failed directly after rejected start; {evidence}"
    );
    assert!(
        healthy.cleanup.as_ref().is_some_and(Result::is_ok),
        "healthy shutdown failed; {evidence}"
    );
    assert!(
        negative.cleanup.is_none()
            && matches!(&negative.backend_running, Ok(false))
            && negative.released,
        "rejected start did not release its backend without repair; {evidence}"
    );
    assert!(
        healthy.released_after_cleanup == Some(true) && released,
        "backend resources remain owned; {evidence}"
    );
}

#[tokio::test]
async fn incomplete_startup_preflight_fails_and_does_not_leave_a_writer() {
    let temp = tempfile::tempdir().unwrap();
    let addr = address();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    let mut candidate = diagnostic_spec(temp.path(), "not-ready", addr);
    candidate.startup_timeout_ms = 200;
    let negative = attempt(&mut supervisor, temp.path(), candidate, None).await;
    let mut healthy = attempt(
        &mut supervisor,
        temp.path(),
        diagnostic_spec(temp.path(), "legacy", addr),
        Some(&negative),
    )
    .await;
    final_cleanup(&mut supervisor, temp.path(), &[addr], &mut healthy).await;
    drop(supervisor);
    let released = resources_released(temp.path(), &[addr]);
    let evidence = serde_json::json!({"negative":negative.evidence(),"healthy":healthy.evidence()});
    assert!(
        negative.result.is_err(),
        "incomplete preflight unexpectedly started; {evidence}"
    );
    assert!(
        healthy.result.is_ok(),
        "healthy startup failed directly after rejected start; {evidence}"
    );
    assert!(
        healthy.cleanup.as_ref().is_some_and(Result::is_ok),
        "healthy shutdown failed; {evidence}"
    );
    assert!(
        negative.cleanup.is_none()
            && matches!(&negative.backend_running, Ok(false))
            && negative.released,
        "rejected start did not release its backend without repair; {evidence}"
    );
    assert!(
        healthy.released_after_cleanup == Some(true) && released,
        "backend resources remain owned; {evidence}"
    );
}

#[tokio::test]
async fn pre_python_exit_cannot_reuse_the_previous_failed_attempt_trace() {
    let temp = tempfile::tempdir().unwrap();
    let addr = address();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    // This real negative reaches its durable commit before its deliberate
    // exit, giving the next attempt a known complete stale trace to reject.
    let negative = attempt(
        &mut supervisor,
        temp.path(),
        diagnostic_spec(temp.path(), "crash", addr),
        None,
    )
    .await;
    let mut early = diagnostic_spec(temp.path(), "legacy", addr);
    early.args = vec!["-c".into(), "raise SystemExit(17)".into()];
    let mut early = attempt(&mut supervisor, temp.path(), early, Some(&negative)).await;
    final_cleanup(&mut supervisor, temp.path(), &[addr], &mut early).await;
    drop(supervisor);
    let released = resources_released(temp.path(), &[addr]);
    assert!(negative.result.is_err(), "negative={}", negative.evidence());
    assert!(
        negative.cleanup.is_none()
            && matches!(&negative.backend_running, Ok(false))
            && negative.released
    );
    let previous = negative.trace.as_ref().unwrap()["phases"]
        .as_array()
        .unwrap();
    assert!(previous.iter().any(|phase| phase["phase"] == "committed"));
    assert!(early.result.is_err(), "early={}", early.evidence());
    assert!(
        early.cleanup.as_ref().is_some_and(Result::is_ok)
            && early.released_after_cleanup == Some(true)
            && released
    );
    assert_eq!(result_class(&early.result), "candidate_exited");
    let current = early.trace.as_ref().unwrap();
    assert!(
        current["phases"].as_array().unwrap().is_empty(),
        "an exit before executing the fixture reused earlier phases: {}",
        early.evidence()
    );
    assert_eq!(current["read_error"], "NotFound");

    // These owned-file counterexamples run only after both real children are
    // joined. Unknown or oversized bytes cannot become unbounded/raw evidence.
    let path = temp.path().join("backend-startup.ndjson");
    std::fs::write(&path, vec![b' '; 8192]).unwrap();
    let oversized = trace_snapshot(temp.path());
    assert_eq!(oversized["truncated"], true);
    assert_eq!(oversized["invalid"], true);
    assert!(oversized["phases"].as_array().unwrap().is_empty());
    std::fs::write(path, b"{").unwrap();
    let malformed = trace_snapshot(temp.path());
    assert_eq!(malformed["truncated"], false);
    assert_eq!(malformed["invalid"], true);
    assert!(malformed["phases"].as_array().unwrap().is_empty());
}
