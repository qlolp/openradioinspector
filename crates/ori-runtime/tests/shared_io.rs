#![cfg(unix)]
#[path = "support/shared_io_child.rs"]
mod shared_io_child;

use ori_runtime::{qos::BackgroundQos, worker::Cancellation};
use serde_json::Value;
use shared_io_child::OwnedChild;
use std::{
    io::{BufRead, BufReader, Write},
    process::Command,
    sync::Arc,
    time::{Duration, Instant},
};

fn python() -> std::ffi::OsString {
    std::env::var_os("ORI_RESEARCH_PYTHON")
        .or_else(|| std::env::var_os("ORI_CORE_PYTHON"))
        .unwrap_or_else(|| "python3".into())
}

fn assert_reaped(pid: u32) {
    let mut status = 0;
    // SAFETY: WNOHANG only queries this fixture's known child PID; no signal is sent.
    let result = unsafe { libc::waitpid(pid as libc::pid_t, &mut status, libc::WNOHANG) };
    assert_eq!(result, -1, "owned child was not already reaped");
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::ECHILD)
    );
}

#[test]
fn failed_python_startup_keeps_bounded_stderr_and_reaps_before_assertions() {
    let child = OwnedChild::spawn(Command::new(python()).args([
        "-I",
        "-B",
        "-c",
        "import sys;sys.stderr.write('x'*65536);sys.exit(7)",
    ]))
    .unwrap();
    let pid = child.id();
    let result = child.join().unwrap();
    assert_reaped(pid);
    assert_eq!(result.status.code(), Some(7));
    assert_eq!(result.stderr, vec![b'x'; shared_io_child::STDERR_BYTES]);
    assert!(result.stderr_overflow);
}

#[test]
fn dropping_a_worker_before_readiness_joins_and_reaps_the_original_child() {
    let child = OwnedChild::spawn(Command::new(python()).args([
        "-I",
        "-B",
        "-c",
        "import sys;sys.stdin.read()",
    ]))
    .unwrap();
    let pid = child.id();
    drop(child);
    assert_reaped(pid);
}

#[test]
fn declared_research_python_works_with_an_unprepared_python_on_path() {
    let declared = std::env::var_os("ORI_RESEARCH_PYTHON").unwrap_or_else(python);
    let metadata = Command::new(&declared)
        .args(["-I", "-B", "-c", "import sys;print(sys.executable)"])
        .output()
        .unwrap();
    assert!(metadata.status.success(), "{metadata:?}");
    let declared = std::path::PathBuf::from(
        String::from_utf8(metadata.stdout)
            .unwrap()
            .trim_end_matches(['\r', '\n']),
    );
    assert!(declared.is_absolute()); // Preserve its venv symlink; do not canonicalize.
    let root = tempfile::tempdir().unwrap();
    let prefix = root.path().join("unprepared-python");
    let prepared = Command::new(&declared)
        .args(["-I", "-B", "-m", "venv", "--without-pip"])
        .arg(&prefix)
        .output()
        .unwrap();
    assert!(prepared.status.success(), "{prepared:?}");
    let fresh = prefix.join("bin/python3");
    let inventory = Command::new(&fresh)
        .args([
            "-I", "-B", "-c",
            "import importlib.util;assert all(importlib.util.find_spec(name) is None for name in ('ori_research','numpy','radioinspector'))",
        ])
        .output()
        .unwrap();
    assert!(inventory.status.success(), "{inventory:?}");
    let inherited = std::env::var_os("PATH").unwrap_or_default();
    let path = std::env::join_paths(
        std::iter::once(prefix.join("bin")).chain(std::env::split_paths(&inherited)),
    )
    .unwrap();
    let result = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "two_native_actors_and_python_worker_budget_share_one_timeline",
            "--nocapture",
        ])
        .env("ORI_RESEARCH_PYTHON", &declared)
        .env_remove("ORI_CORE_PYTHON")
        .env("PATH", path)
        .env("RUST_BACKTRACE", "0")
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "declared research interpreter was ignored; joined test child stdout={} stderr={}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
}

fn path(qos: &BackgroundQos) -> std::path::PathBuf {
    serde_json::to_value(qos.worker_io().unwrap()).unwrap()["path"]
        .as_str()
        .unwrap()
        .into()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn two_native_actors_and_python_worker_budget_share_one_timeline() {
    let root = tempfile::tempdir().unwrap();
    let qos = BackgroundQos::with_shared_io(root.path().join("owned-io")).unwrap();
    let capability = serde_json::to_string(&qos.worker_io().unwrap()).unwrap();
    let repository = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let script = r#"
import json,sys
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[1]+'/research/src')
from ori_research.resources import Budget
from ori_research.protocol import DEFAULT_LIMITS
budget=Budget(DEFAULT_LIMITS,shared_io=json.loads(sys.stdin.readline()))
print('ready',flush=True)
assert sys.stdin.readline().strip()=='go'
budget.account_io(2*1024*1024,'source_bytes_read')
print(budget.metrics['source_bytes_read'],flush=True)
"#;
    let mut child = OwnedChild::spawn(
        Command::new(python())
            .args(["-I", "-B", "-c", script])
            .arg(repository),
    )
    .unwrap();
    let child_pid = child.id();
    let mut input = child.stdin();
    writeln!(input, "{capability}").unwrap();
    let mut output = BufReader::new(child.stdout());
    let mut ready = String::new();
    let readiness = output.read_line(&mut ready);
    if readiness.is_err() || ready.trim() != "ready" {
        drop(input);
        drop(output);
        let result = child.join().unwrap();
        assert_reaped(child_pid);
        panic!(
            "Python shared-I/O startup failed after child JOIN: readiness={readiness:?}, \
             stdout={ready:?}, status={}, stderr_overflow={}, stderr={}",
            result.status,
            result.stderr_overflow,
            String::from_utf8_lossy(&result.stderr)
        );
    }
    let gate = Arc::new(tokio::sync::Barrier::new(3));
    let began = Instant::now();
    let asynchronous = {
        let qos = qos.clone();
        let gate = gate.clone();
        tokio::spawn(async move {
            gate.wait().await;
            qos.account(2 * 1024 * 1024, &Cancellation::default()).await
        })
    };
    let blocking = {
        let qos = qos.clone();
        let gate = gate.clone();
        tokio::spawn(async move {
            gate.wait().await;
            tokio::task::spawn_blocking(move || {
                qos.account_blocking(2 * 1024 * 1024, &Cancellation::default())
            })
            .await
            .unwrap()
        })
    };
    gate.wait().await;
    writeln!(input, "go").unwrap();
    drop(input);
    let child = tokio::task::spawn_blocking(move || {
        let mut line = String::new();
        let read = output.read_line(&mut line);
        let result = child.join();
        (read, line, result)
    });
    let asynchronous = asynchronous.await;
    let blocking = blocking.await;
    let (read, line, result) = child.await.unwrap();
    let result = result.unwrap();
    assert_reaped(child_pid);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(
        !result.stderr_overflow,
        "Python stderr exceeded its byte budget"
    );
    asynchronous.unwrap().unwrap();
    blocking.unwrap().unwrap();
    read.unwrap();
    assert_eq!(line.trim(), (2 * 1024 * 1024).to_string());
    assert!(
        began.elapsed() >= Duration::from_millis(280),
        "independent timelines exceeded the aggregate rate"
    );
    assert_eq!(qos.status().logical_bytes, 6 * 1024 * 1024);
    assert!(qos.status().cross_process_logical_io);
    assert!(!qos.status().physical_io_enforced);
    qos.close_shared_io().await.unwrap();
}

#[tokio::test]
async fn pressure_joins_pending_io_and_keeps_cancelled_debt_charged() {
    let root = tempfile::tempdir().unwrap();
    let qos = BackgroundQos::with_shared_io(root.path().join("io")).unwrap();
    let cancel = Cancellation::default();
    let guard = qos.register(cancel.clone()).unwrap();
    let work = {
        let qos = qos.clone();
        tokio::spawn(async move {
            let result = qos.account(8 * 1024 * 1024, &cancel).await;
            drop(guard);
            result
        })
    };
    tokio::time::timeout(Duration::from_secs(2), async {
        while qos.status().logical_bytes == 0 {
            // status() observes the same private ledger lock used by the
            // blocking reservation. Do not starve that writer with a hot reader.
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    let began = Instant::now();
    qos.pressure(true).await.unwrap();
    assert!(work.await.unwrap().is_err());
    assert!(began.elapsed() < Duration::from_millis(150));
    assert_eq!(qos.status().registered_work, 0);
    qos.pressure(false).await.unwrap();
    let began = Instant::now();
    qos.account(1024, &Cancellation::default()).await.unwrap();
    assert!(
        began.elapsed() > Duration::from_millis(200),
        "cancellation refunded future I/O debt"
    );
    assert_eq!(qos.status().logical_bytes, 8 * 1024 * 1024 + 1024);
    qos.close_shared_io().await.unwrap();
}

#[tokio::test]
async fn work_guard_retains_instance_and_replaced_cleanup_is_sticky_and_visible() {
    let root = tempfile::tempdir().unwrap();
    let qos = BackgroundQos::with_shared_io(root.path().join("io")).unwrap();
    let file = path(&qos);
    let guard = qos.register(Cancellation::default()).unwrap();
    drop(qos);
    assert!(file.exists());
    drop(guard);
    assert!(!file.exists());
    let qos = BackgroundQos::with_shared_io(root.path().join("io")).unwrap();
    let file = path(&qos);
    std::fs::rename(&file, file.with_extension("original")).unwrap();
    std::fs::write(&file, b"unknown replacement").unwrap();
    assert!(qos.close_shared_io().await.is_err());
    assert!(qos.status().cleanup_failed);
    assert!(qos.close_shared_io().await.is_err());
    assert_eq!(std::fs::read(&file).unwrap(), b"unknown replacement");
}

#[tokio::test]
async fn corrupt_future_debt_and_unsafe_capabilities_fail_closed() {
    use std::os::unix::fs::PermissionsExt;
    let root = tempfile::tempdir().unwrap();
    let qos = BackgroundQos::with_shared_io(root.path().join("io")).unwrap();
    let file = path(&qos);
    let original = std::fs::read(&file).unwrap();
    assert_eq!(original.len(), 128);
    let mut corrupt = original.clone();
    corrupt[32..40].copy_from_slice(&u64::MAX.to_le_bytes());
    std::fs::write(&file, corrupt).unwrap();
    assert!(qos.account(1, &Cancellation::default()).await.is_err());
    std::fs::write(&file, &original).unwrap();
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o644)).unwrap();
    assert!(qos.account(1, &Cancellation::default()).await.is_err());
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600)).unwrap();
    // Accounting failures remain latched; they cannot silently resume after a
    // caller rewrites an unsafe ledger back to a plausible shape.
    assert!(qos.account(1, &Cancellation::default()).await.is_err());
    assert!(qos.status().cleanup_failed);
    assert!(
        qos.register(Cancellation::default()).is_err(),
        "unsafe shared I/O still admitted work"
    );
    let serialized = serde_json::to_value(qos.worker_io().unwrap()).unwrap();
    assert_eq!(serialized["clock"], Value::String("posix-monotonic".into()));
    assert!(qos.close_shared_io().await.is_err());
}

#[tokio::test]
async fn replacing_the_owned_directory_cannot_rebind_or_remove_an_original_file() {
    use std::os::unix::fs::DirBuilderExt;
    let root = tempfile::tempdir().unwrap();
    let qos = BackgroundQos::with_shared_io(root.path().join("io")).unwrap();
    let file = path(&qos);
    let directory = file.parent().unwrap();
    let original = directory.with_extension("original");
    std::fs::rename(directory, &original).unwrap();
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(directory)
        .unwrap();
    std::fs::rename(original.join("budget"), &file).unwrap();
    assert!(qos.account(1, &Cancellation::default()).await.is_err());
    assert!(qos.close_shared_io().await.is_err());
    assert!(
        file.exists(),
        "cleanup removed a path in a replaced directory"
    );
}
