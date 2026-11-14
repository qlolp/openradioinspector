//! Real legacy algorithms and owned temporary files; no listener or hardware.
use ori_contracts::{parse_json_value, to_json_vec};
use ori_legacy::*;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    time::Duration,
};

const DENY_PREP: usize = 1;
const DENY_COMMIT: usize = 2;
const CANCEL_COMMIT: usize = 3;
const BLOCK_COMMIT: usize = 4;
const BLOCK_PREP: usize = 5;

fn python() -> std::ffi::OsString {
    std::env::var_os("ORI_TEST_PYTHON").unwrap_or_else(|| "python".into())
}
fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}
fn source(directory: &Path) -> PathBuf {
    let output = Command::new(python())
        .arg(root().join("crates/ori-legacy/tests/generate_fixtures.py"))
        .arg(directory)
        .arg("--sidecar")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    PathBuf::from(String::from_utf8(output.stdout).unwrap().trim())
}
fn hashes(path: &Path) -> Vec<Vec<u8>> {
    [path.to_owned(), path.with_file_name("signals.json")]
        .iter()
        .map(|path| Sha256::digest(fs::read(path).unwrap()).to_vec())
        .collect()
}
fn no_stages(directory: &Path) -> bool {
    let scratch = directory.join("scratch");
    !scratch.exists() || fs::read_dir(scratch).unwrap().next().is_none()
}
fn options(directory: &Path) -> LegacyOptions {
    LegacyOptions {
        library: Some(directory.to_owned()),
        scratch_directory: directory.join("scratch"),
        ..LegacyOptions::default()
    }
}

#[derive(Default)]
struct Controls {
    mode: AtomicUsize,
    after_child: AtomicBool,
    charges: Mutex<Vec<(u64, bool)>>,
    calls: AtomicUsize,
    registrations: AtomicUsize,
    released_with_stage: AtomicBool,
    directory: Mutex<Option<PathBuf>>,
    entered: Mutex<Option<mpsc::Sender<()>>>,
    release: Mutex<Option<mpsc::Receiver<()>>>,
}
struct Budget(Arc<Controls>);
impl Drop for Budget {
    fn drop(&mut self) {
        if self
            .0
            .directory
            .lock()
            .unwrap()
            .as_ref()
            .is_some_and(|path| !no_stages(path))
        {
            self.0.released_with_stage.store(true, Ordering::SeqCst);
        }
        self.0.registrations.fetch_sub(1, Ordering::SeqCst);
    }
}
impl LegacyIoBudget for Budget {
    fn account(&self, bytes: u64, cancel: &LegacyCancellation) -> Result<()> {
        let after_child = self.0.after_child.load(Ordering::SeqCst);
        self.0.charges.lock().unwrap().push((bytes, after_child));
        assert_eq!(self.0.registrations.load(Ordering::SeqCst), 1);
        let mode = self.0.mode.load(Ordering::SeqCst);
        if mode == DENY_PREP || (mode == DENY_COMMIT && after_child) {
            return Err(LegacyError::ResourceLimit(
                "Synthetic shared I/O pressure".into(),
            ));
        }
        if mode == CANCEL_COMMIT && after_child {
            cancel.cancel();
        }
        if ((mode == BLOCK_COMMIT && after_child) || (mode == BLOCK_PREP && !after_child))
            && let Some(entered) = self.0.entered.lock().unwrap().take()
        {
            entered.send(()).unwrap();
            self.0
                .release
                .lock()
                .unwrap()
                .take()
                .unwrap()
                .recv()
                .unwrap();
        }
        Ok(())
    }
}
struct Python(Arc<Controls>);
struct Permit {
    controls: Arc<Controls>,
    budget: Arc<dyn LegacyIoBudget>,
}
impl LegacyScience for Python {
    fn try_reserve(&self) -> Result<Box<dyn LegacySciencePermit>> {
        self.0.after_child.store(false, Ordering::SeqCst);
        self.0.registrations.fetch_add(1, Ordering::SeqCst);
        Ok(Box::new(Permit {
            controls: self.0.clone(),
            budget: Arc::new(Budget(self.0.clone())),
        }))
    }
}
impl LegacySciencePermit for Permit {
    fn io_budget(&self) -> Option<Arc<dyn LegacyIoBudget>> {
        Some(self.budget.clone())
    }
    fn execute(self: Box<Self>, request: Value, _: LegacyCancellation) -> ScienceFuture {
        Box::pin(async move {
            self.controls.calls.fetch_add(1, Ordering::SeqCst);
            let threshold = request["action"] == "threshold";
            let result = tokio::task::spawn_blocking(move || {
                let bootstrap = format!("import runpy,sys;sys.path.insert(0,{:?});runpy.run_module('radioinspector.legacy_rpc',run_name='__main__')", root());
                let mut child = Command::new(python()).args(["-I", "-c", &bootstrap])
                    .envs([("OMP_NUM_THREADS", "1"), ("OPENBLAS_NUM_THREADS", "1"), ("MKL_NUM_THREADS", "1"), ("NUMEXPR_NUM_THREADS", "1"), ("VECLIB_MAXIMUM_THREADS", "1"), ("ARROW_NUM_THREADS", "1")])
                    .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()?;
                child.stdin.take().unwrap().write_all(&to_json_vec(&request)?)?;
                let output = child.wait_with_output()?;
                assert!(!output.stdout.is_empty(), "{}", String::from_utf8_lossy(&output.stderr));
                parse_json_value(&output.stdout, 16 * 1024 * 1024).map_err(Into::into)
            }).await.unwrap();
            self.controls.after_child.store(threshold, Ordering::SeqCst);
            drop(self); // The library's cloned budget must now own post-child work.
            result
        })
    }
}
async fn workspace(directory: &Path, controls: &Arc<Controls>) -> LegacyWorkspace {
    *controls.directory.lock().unwrap() = Some(directory.to_owned());
    LegacyWorkspace::new(options(directory), Arc::new(Python(controls.clone())))
        .await
        .unwrap()
}

#[tokio::test]
async fn denied_preparation_never_runs_science_or_publishes_a_version() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let before = hashes(&path);
    let controls = Arc::new(Controls::default());
    controls.mode.store(DENY_PREP, Ordering::SeqCst);
    let workspace = workspace(directory.path(), &controls).await;
    let empty = workspace.snapshot(true).await.unwrap();
    assert!(matches!(
        workspace.load_database(&path).await,
        Err(LegacyError::ResourceLimit(_))
    ));
    assert_eq!(workspace.version(), 0);
    assert_eq!(workspace.snapshot(true).await.unwrap(), empty);
    assert_eq!(controls.calls.load(Ordering::SeqCst), 0);
    assert_eq!(controls.registrations.load(Ordering::SeqCst), 0);
    assert!(!controls.released_with_stage.load(Ordering::SeqCst));
    assert_eq!(hashes(&path), before);
    assert!(no_stages(directory.path()));
    workspace.close().await.unwrap();
}

#[tokio::test]
async fn database_and_sidecar_read_write_chunks_preserve_the_actual_python_oracle() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let sidecar = path.with_file_name("signals.json");
    let mut file = fs::OpenOptions::new().append(true).open(&sidecar).unwrap();
    let padding = 2 * 65536 + 123 - file.metadata().unwrap().len() as usize;
    file.write_all(&vec![b' '; padding]).unwrap();
    drop(file);
    let before = hashes(&path);
    let db = rusqlite::Connection::open(&path).unwrap();
    let bytes = db
        .query_row(
            "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()",
            [],
            |row| row.get::<_, i64>(0),
        )
        .unwrap() as u64;
    drop(db);
    assert!(bytes <= 512 * 1024);
    let controls = Arc::new(Controls::default());
    let workspace = workspace(directory.path(), &controls).await;
    assert_eq!(
        workspace.load_database(&path).await.unwrap(),
        json!({"ok":true,"version":1})
    );
    let charges = controls.charges.lock().unwrap().clone();
    assert_eq!(charges.len(), 9);
    assert_eq!(
        &charges[..8],
        &[
            (bytes, false),
            (bytes, false),
            (65536, false),
            (65536, false),
            (65536, false),
            (65536, false),
            (123, false),
            (123, false)
        ]
    );
    assert!((1..1024).contains(&charges[8].0));
    let mut actual = workspace.snapshot(true).await.unwrap();
    actual["database"] = json!("__SOURCE_PAN__");
    let oracle = parse_json_value(
        include_bytes!("../../../contracts/fixtures/legacy-workspace-v1.json"),
        16 * 1024 * 1024,
    )
    .unwrap();
    assert_eq!(actual, oracle["sidecar"]["loaded"]);
    assert_eq!(hashes(&path), before);
    assert_eq!(controls.registrations.load(Ordering::SeqCst), 0);
    assert!(!controls.released_with_stage.load(Ordering::SeqCst));
    assert!(no_stages(directory.path()));
    workspace.close().await.unwrap();
}

#[cfg(unix)]
#[tokio::test]
async fn readonly_database_keeps_original_python_state_and_refuses_canonical_threshold() {
    use std::os::unix::fs::PermissionsExt;
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    fs::set_permissions(&path, fs::Permissions::from_mode(0o444)).unwrap();
    let before = hashes(&path);
    let controls = Arc::new(Controls::default());
    let workspace = workspace(directory.path(), &controls).await;
    workspace.load_database(&path).await.unwrap();
    let loaded = workspace.snapshot(true).await.unwrap();
    let mut actual = loaded.clone();
    actual["database"] = json!("__SOURCE_PAN__");
    let oracle = parse_json_value(
        include_bytes!("../../../contracts/fixtures/legacy-workspace-v1.json"),
        16 * 1024 * 1024,
    )
    .unwrap();
    assert_eq!(actual, oracle["sidecar"]["loaded"]);
    assert_eq!(
        loaded["database"],
        path.canonicalize().unwrap().to_str().unwrap()
    );
    let error = workspace
        .threshold(json!({"offset_db":3.5}))
        .await
        .unwrap_err();
    assert!(matches!(error, LegacyError::Sqlite(message) if message.contains("readonly")));
    assert_eq!(workspace.version(), 1);
    assert_eq!(workspace.snapshot(true).await.unwrap(), loaded);
    assert_eq!(hashes(&path), before);
    assert_eq!(controls.calls.load(Ordering::SeqCst), 2);
    assert_eq!(controls.registrations.load(Ordering::SeqCst), 0);
    assert!(!controls.released_with_stage.load(Ordering::SeqCst));
    assert!(no_stages(directory.path()));
    workspace.close().await.unwrap();
}

async fn refused_commit(mode: usize) {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let controls = Arc::new(Controls::default());
    let workspace = workspace(directory.path(), &controls).await;
    workspace.load_database(&path).await.unwrap();
    let before = workspace.snapshot(true).await.unwrap();
    let source_hash = hashes(&path);
    controls.mode.store(mode, Ordering::SeqCst);
    let error = workspace
        .threshold(json!({"offset_db":3.5}))
        .await
        .unwrap_err();
    if mode == CANCEL_COMMIT {
        assert!(matches!(error, LegacyError::Cancelled));
    } else {
        assert!(matches!(error, LegacyError::ResourceLimit(_)));
    }
    assert_eq!(controls.calls.load(Ordering::SeqCst), 2);
    assert!(
        controls
            .charges
            .lock()
            .unwrap()
            .iter()
            .any(|(_, after_child)| *after_child)
    );
    assert_eq!(controls.registrations.load(Ordering::SeqCst), 0);
    assert!(!controls.released_with_stage.load(Ordering::SeqCst));
    assert_eq!(workspace.version(), 1);
    assert_eq!(workspace.snapshot(true).await.unwrap(), before);
    assert_eq!(hashes(&path), source_hash);
    assert!(no_stages(directory.path()));
    workspace.close().await.unwrap();
}
#[tokio::test]
async fn denied_post_child_threshold_write_never_changes_sql_or_cached_state() {
    refused_commit(DENY_COMMIT).await;
}
#[tokio::test]
async fn cancellation_inside_post_child_callback_never_commits_or_publishes_state() {
    refused_commit(CANCEL_COMMIT).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn aborted_waiter_retains_post_child_budget_and_file_lock_until_actual_cleanup() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let controls = Arc::new(Controls::default());
    let workspace = workspace(directory.path(), &controls).await;
    workspace.load_database(&path).await.unwrap();
    let before = hashes(&path);
    let (entered, ready) = mpsc::channel();
    let (release, gate) = mpsc::channel();
    *controls.entered.lock().unwrap() = Some(entered);
    *controls.release.lock().unwrap() = Some(gate);
    controls.mode.store(BLOCK_COMMIT, Ordering::SeqCst);
    let copy = workspace.clone();
    let waiter = tokio::spawn(async move { copy.threshold(json!({"offset_db":3.5})).await });
    tokio::task::spawn_blocking(move || ready.recv_timeout(Duration::from_secs(3)).unwrap())
        .await
        .unwrap();
    waiter.abort();
    let _ = waiter.await;
    assert_eq!(controls.registrations.load(Ordering::SeqCst), 1);
    let probe = fs::File::open(&path).unwrap();
    assert!(probe.try_lock().is_err());
    // Record close/cancellation intent while the post-child callback is held,
    // retaining the same future until the original resources actually close.
    let mut close = std::pin::pin!(workspace.close());
    std::future::poll_fn(|context| {
        assert!(std::future::Future::poll(close.as_mut(), context).is_pending());
        std::task::Poll::Ready(())
    })
    .await;
    assert_eq!(controls.registrations.load(Ordering::SeqCst), 1);
    assert!(probe.try_lock().is_err());
    release.send(()).unwrap();
    close.await.unwrap();
    assert_eq!(controls.registrations.load(Ordering::SeqCst), 0);
    assert!(!controls.released_with_stage.load(Ordering::SeqCst));
    assert_eq!(workspace.version(), 1);
    assert_eq!(hashes(&path), before);
    assert!(no_stages(directory.path()));
    probe.try_lock().unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn callback_returning_after_snapshot_deadline_never_runs_science() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let controls = Arc::new(Controls::default());
    let (entered, ready) = mpsc::channel();
    let (release, gate) = mpsc::channel();
    *controls.entered.lock().unwrap() = Some(entered);
    *controls.release.lock().unwrap() = Some(gate);
    controls.mode.store(BLOCK_PREP, Ordering::SeqCst);
    let mut config = options(directory.path());
    *controls.directory.lock().unwrap() = Some(directory.path().to_owned());
    config.sql_timeout = Duration::from_millis(500);
    let workspace = LegacyWorkspace::new(config, Arc::new(Python(controls.clone())))
        .await
        .unwrap();
    let copy = workspace.clone();
    let source = path.clone();
    let waiter = tokio::spawn(async move { copy.load_database(&source).await });
    tokio::task::spawn_blocking(move || ready.recv_timeout(Duration::from_secs(3)).unwrap())
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(550)).await;
    assert!(!waiter.is_finished());
    release.send(()).unwrap();
    assert!(matches!(
        waiter.await.unwrap(),
        Err(LegacyError::ResourceLimit(_))
    ));
    assert_eq!(controls.calls.load(Ordering::SeqCst), 0);
    assert_eq!(controls.registrations.load(Ordering::SeqCst), 0);
    assert!(!controls.released_with_stage.load(Ordering::SeqCst));
    assert_eq!(workspace.version(), 0);
    assert!(no_stages(directory.path()));
    workspace.close().await.unwrap();
}
