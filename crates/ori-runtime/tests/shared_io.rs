#![cfg(unix)]
use ori_runtime::{qos::BackgroundQos, worker::Cancellation};
use serde_json::Value;
use std::{
    io::{BufRead, BufReader, Write},
    process::{Command, Stdio},
    sync::Arc,
    time::{Duration, Instant},
};

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
    let mut child =
        Command::new(std::env::var_os("ORI_CORE_PYTHON").unwrap_or_else(|| "python3".into()))
            .args(["-I", "-c", script])
            .arg(repository)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
    let mut input = child.stdin.take().unwrap();
    writeln!(input, "{capability}").unwrap();
    let mut output = BufReader::new(child.stdout.take().unwrap());
    let mut ready = String::new();
    output.read_line(&mut ready).unwrap();
    assert_eq!(ready.trim(), "ready");
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
        output.read_line(&mut line).unwrap();
        assert_eq!(line.trim(), (2 * 1024 * 1024).to_string());
        child.wait_with_output().unwrap()
    });
    asynchronous.await.unwrap().unwrap();
    blocking.await.unwrap().unwrap();
    let result = child.await.unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
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
            tokio::task::yield_now().await;
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
