//! Real Python algorithms on owned temporary .pan files; no listener/devices.
use ori_contracts::{parse_json_value, to_json_vec};
use ori_legacy::*;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};
static PROCESS_SLOT: Mutex<()> = Mutex::new(());

fn python() -> std::ffi::OsString {
    std::env::var_os("ORI_TEST_PYTHON").unwrap_or_else(|| "python".into())
}

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}
fn oracle() -> Value {
    parse_json_value(
        include_bytes!("../../../contracts/fixtures/legacy-workspace-v1.json"),
        16 * 1024 * 1024,
    )
    .unwrap()
}
fn source(directory: &Path) -> PathBuf {
    let _slot = PROCESS_SLOT.lock().unwrap();
    let output = Command::new(python())
        .arg(root().join("crates/ori-legacy/tests/generate_fixtures.py"))
        .arg(directory)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    PathBuf::from(String::from_utf8(output.stdout).unwrap().trim())
}
fn normalized(mut value: Value) -> Value {
    if value.get("database").is_some_and(|v| !v.is_null()) {
        value["database"] = json!("__SOURCE_PAN__");
    }
    value
}
fn raw_rows(path: &Path) -> Value {
    let db =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap();
    let mut statement=db.prepare("SELECT id,recorded_at,hex(levels),start_hz,stop_hz,step_hz,unit FROM panorama ORDER BY id").unwrap();
    statement.query_map([],|row|Ok(json!({"id":row.get::<_,i64>(0)?,"recorded_at":row.get::<_,String>(1)?,"levels_hex":row.get::<_,String>(2)?,"start_hz":row.get::<_,f64>(3)?,"stop_hz":row.get::<_,f64>(4)?,"step_hz":row.get::<_,f64>(5)?,"unit":row.get::<_,String>(6)?}))).unwrap().collect::<std::result::Result<Vec<_>,_>>().unwrap().into()
}
fn source_audit(path: &Path) -> Vec<u8> {
    let db =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap();
    let mut hash = Sha256::new();
    for sql in [
        "SELECT * FROM panorama_meta ORDER BY key",
        "SELECT * FROM panorama ORDER BY id",
        "SELECT * FROM signal_event ORDER BY id",
        "SELECT * FROM sqlite_sequence ORDER BY name",
        "SELECT * FROM threshold_line WHERE line_index<>0 ORDER BY line_index",
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name",
    ] {
        hash.update(sql.as_bytes());
        let mut statement = db.prepare(sql).unwrap();
        let count = statement.column_count();
        let mut rows = statement.query([]).unwrap();
        while let Some(row) = rows.next().unwrap() {
            for column in 0..count {
                use rusqlite::types::ValueRef;
                match row.get_ref(column).unwrap() {
                    ValueRef::Null => hash.update([0]),
                    ValueRef::Integer(v) => {
                        hash.update([1]);
                        hash.update(v.to_le_bytes());
                    }
                    ValueRef::Real(v) => {
                        hash.update([2]);
                        hash.update(v.to_bits().to_le_bytes());
                    }
                    ValueRef::Text(v) | ValueRef::Blob(v) => {
                        hash.update([
                            if matches!(row.get_ref(column).unwrap(), ValueRef::Text(_)) {
                                3
                            } else {
                                4
                            },
                        ]);
                        hash.update((v.len() as u64).to_le_bytes());
                        hash.update(v);
                    }
                }
            }
        }
    }
    hash.finalize().to_vec()
}

struct Python {
    busy: Arc<AtomicBool>,
    calls: Arc<AtomicUsize>,
    gate: Option<Arc<tokio::sync::Semaphore>>,
    honor_cancel: bool,
    panic_in_execute: bool,
}
impl Default for Python {
    fn default() -> Self {
        Self {
            busy: Arc::new(AtomicBool::new(false)),
            calls: Arc::new(AtomicUsize::new(0)),
            gate: None,
            honor_cancel: true,
            panic_in_execute: false,
        }
    }
}
struct Permit {
    busy: Arc<AtomicBool>,
    calls: Arc<AtomicUsize>,
    gate: Option<Arc<tokio::sync::Semaphore>>,
    honor_cancel: bool,
    panic_in_execute: bool,
}
impl Drop for Permit {
    fn drop(&mut self) {
        self.busy.store(false, Ordering::SeqCst);
    }
}
impl LegacyScience for Python {
    fn try_reserve(&self) -> Result<Box<dyn LegacySciencePermit>> {
        if self.busy.swap(true, Ordering::SeqCst) {
            return Err(LegacyError::Busy);
        }
        Ok(Box::new(Permit {
            busy: self.busy.clone(),
            calls: self.calls.clone(),
            gate: self.gate.clone(),
            honor_cancel: self.honor_cancel,
            panic_in_execute: self.panic_in_execute,
        }))
    }
}
impl LegacySciencePermit for Permit {
    fn execute(self: Box<Self>, request: Value, cancel: LegacyCancellation) -> ScienceFuture {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            assert!(!self.panic_in_execute, "owned synthetic executor panic");
            if let Some(gate) = &self.gate {
                if self.honor_cancel {
                    tokio::select! {value=gate.acquire()=>{value.unwrap().forget();},_=cancel.cancelled()=>return Err(LegacyError::Cancelled)}
                } else {
                    gate.acquire().await.unwrap().forget();
                }
            }
            if cancel.is_cancelled() {
                return Err(LegacyError::Cancelled);
            }
            // Fixed trusted source bootstrap only in tests. Production Core uses
            // the installed module with interpreter -I -m radioinspector.legacy_rpc.
            let bootstrap = format!(
                "import runpy,sys;sys.path.insert(0,{:?});runpy.run_module('radioinspector.legacy_rpc',run_name='__main__')",
                root()
            );
            tokio::task::spawn_blocking(move || {
                let _slot = PROCESS_SLOT.lock().unwrap();
                let mut child = Command::new(python())
                    .args(["-I", "-c", &bootstrap])
                    .envs([
                        ("OMP_NUM_THREADS", "1"),
                        ("OPENBLAS_NUM_THREADS", "1"),
                        ("MKL_NUM_THREADS", "1"),
                        ("NUMEXPR_NUM_THREADS", "1"),
                        ("VECLIB_MAXIMUM_THREADS", "1"),
                        ("ARROW_NUM_THREADS", "1"),
                    ])
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .spawn()
                    .unwrap();
                child
                    .stdin
                    .take()
                    .unwrap()
                    .write_all(&to_json_vec(&request).unwrap())
                    .unwrap();
                let output = child.wait_with_output().unwrap();
                assert!(
                    !output.stdout.is_empty(),
                    "{}",
                    String::from_utf8_lossy(&output.stderr)
                );
                Ok(parse_json_value(&output.stdout, 16 * 1024 * 1024).unwrap())
            })
            .await
            .unwrap()
        })
    }
}
fn options(directory: &Path) -> LegacyOptions {
    LegacyOptions {
        library: Some(directory.to_owned()),
        scratch_directory: directory.join("scratch"),
        ..LegacyOptions::default()
    }
}

#[tokio::test]
async fn empty_workspace_is_exact_and_cold_reads_create_no_panorama_database() {
    let directory = tempfile::tempdir().unwrap();
    let science = Arc::new(Python::default());
    let workspace = LegacyWorkspace::new(options(directory.path()), science.clone())
        .await
        .unwrap();
    assert_eq!(workspace.version(), 0);
    assert_eq!(
        workspace.snapshot(true).await.unwrap(),
        oracle()["no_database"]
    );
    assert_eq!(workspace.signals().await.unwrap(), json!({"signals":[]}));
    assert_eq!(workspace.occupancy(None).await.unwrap(), json!({}));
    assert_eq!(
        workspace.measurements(1., 2., None).await.unwrap(),
        json!({})
    );
    assert_eq!(science.calls.load(Ordering::SeqCst), 0);
    let missing = directory.path().join("new/missing.pan");
    assert_eq!(
        workspace
            .load_database(&missing)
            .await
            .unwrap_err()
            .api_message(),
        "Укажите существующую базу панорам .pan"
    );
    assert!(!missing.exists());
    assert_eq!(workspace.version(), 0);
    assert_eq!(
        workspace
            .threshold(json!({"offset_db":1}))
            .await
            .unwrap_err()
            .api_message(),
        "нет активной линии порога"
    );
    workspace.close().await.unwrap();
}

#[tokio::test]
async fn actual_panorama_science_and_threshold_versions_match_python_without_raw_rewrites() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let before = raw_rows(&path);
    let audit = source_audit(&path);
    assert_eq!(before, oracle()["raw_rows"]);
    let workspace = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    let mut changed = workspace.subscribe();
    assert_eq!(
        workspace.load_database(&path).await.unwrap(),
        json!({"ok":true,"version":1})
    );
    changed.changed().await.unwrap();
    assert_eq!(*changed.borrow(), 1);
    assert_eq!(
        normalized(workspace.snapshot(true).await.unwrap()),
        oracle()["loaded"]
    );
    let no_waterfall = workspace.snapshot(false).await.unwrap();
    assert!(no_waterfall.get("waterfall").is_none());
    for case in oracle()["queries"].as_array().unwrap() {
        let p = &case["parameters"];
        let query = match case["action"].as_str().unwrap() {
            "coverage" => LegacyQuery::Coverage { rows: 3, cols: 4 },
            "occupancy" => LegacyQuery::Occupancy {
                threshold_db: p["threshold"].as_f64(),
            },
            "efficiency" => LegacyQuery::Efficiency { threshold_db: None },
            "measurements" => LegacyQuery::Measurements {
                start_hz: p["start"].as_f64().unwrap(),
                stop_hz: p["stop"].as_f64().unwrap(),
                norm_hz: p["norm"].as_f64(),
            },
            "keywords" => LegacyQuery::Keywords,
            _ => panic!("unknown fixture query"),
        };
        let bytes = workspace.query_json(query).await.unwrap();
        assert_eq!(
            parse_json_value(&bytes, 16 * 1024 * 1024).unwrap(),
            case["body"],
            "{}",
            case["action"]
        );
    }
    for case in oracle()["thresholds"].as_array().unwrap() {
        let result = workspace.threshold(case["payload"].clone()).await;
        if case["status"] == 200 {
            assert_eq!(result.unwrap(), case["body"]);
        } else {
            let error = result.unwrap_err();
            assert_eq!(error.http_status(), 400);
            assert_eq!(error.api_message(), case["body"]["error"].as_str().unwrap());
        }
        assert_eq!(
            normalized(workspace.snapshot(true).await.unwrap()),
            case["state"]
        );
        assert_eq!(raw_rows(&path), before);
        assert_eq!(source_audit(&path), audit);
    }
    let final_version = workspace.version();
    workspace.close().await.unwrap();
    let reopened = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    reopened.load_database(&path).await.unwrap();
    assert_eq!(reopened.version(), 1);
    assert_eq!(
        reopened.snapshot(true).await.unwrap()["threshold"],
        oracle()["thresholds"][2]["body"]["threshold"]
    );
    assert_eq!(raw_rows(&path), before);
    assert_eq!(source_audit(&path), audit);
    assert_eq!(final_version, 4);
    reopened.close().await.unwrap();
}

#[tokio::test]
async fn library_containment_per_file_exclusion_and_invalid_load_preserve_state() {
    let directory = tempfile::tempdir().unwrap();
    let foreign = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let other = source(foreign.path());
    let workspace = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    workspace.load_database(&path).await.unwrap();
    let saved = workspace.snapshot(true).await.unwrap();
    assert_eq!(
        workspace
            .load_database(&other)
            .await
            .unwrap_err()
            .api_message(),
        "База находится вне настроенного каталога"
    );
    let bad = directory.path().join("malformed.pan");
    std::fs::write(&bad, b"not a database").unwrap();
    let original = std::fs::read(&bad).unwrap();
    assert!(workspace.load_database(&bad).await.is_err());
    assert_eq!(std::fs::read(&bad).unwrap(), original);
    assert_eq!(workspace.snapshot(true).await.unwrap(), saved);
    let second = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    assert!(second.load_database(&path).await.is_err());
    workspace.close().await.unwrap();
    second.load_database(&path).await.unwrap();
    second.close().await.unwrap();
}

#[tokio::test]
async fn busy_science_and_source_budget_fail_before_snapshot_or_state_change() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let science = Arc::new(Python::default());
    science.busy.store(true, Ordering::SeqCst);
    let workspace = LegacyWorkspace::new(options(directory.path()), science.clone())
        .await
        .unwrap();
    assert!(matches!(
        workspace.load_database(&path).await,
        Err(LegacyError::Busy)
    ));
    assert_eq!(workspace.version(), 0);
    assert_eq!(science.calls.load(Ordering::SeqCst), 0);
    science.busy.store(false, Ordering::SeqCst);
    workspace.close().await.unwrap();
    let mut limited = options(directory.path());
    limited.limits.max_total_points = 2;
    let workspace = LegacyWorkspace::new(limited, science.clone())
        .await
        .unwrap();
    assert_eq!(
        workspace
            .load_database(&path)
            .await
            .unwrap_err()
            .http_status(),
        413
    );
    assert_eq!(workspace.version(), 0);
    assert_eq!(science.calls.load(Ordering::SeqCst), 0);
    workspace.close().await.unwrap();
}

#[tokio::test]
async fn close_cancels_science_and_waits_for_acknowledged_owner_join_before_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let gate = Arc::new(tokio::sync::Semaphore::new(0));
    let science = Arc::new(Python {
        gate: Some(gate),
        ..Python::default()
    });
    let workspace = LegacyWorkspace::new(options(directory.path()), science.clone())
        .await
        .unwrap();
    let copy = workspace.clone();
    let loading = tokio::spawn(async move { copy.load_database(&path).await });
    while science.calls.load(Ordering::SeqCst) == 0 {
        tokio::task::yield_now().await;
    }
    workspace.close().await.unwrap();
    assert!(matches!(
        loading.await.unwrap(),
        Err(LegacyError::Cancelled) | Err(LegacyError::Closed)
    ));
    assert_eq!(workspace.version(), 0);
    let path = directory.path().join("source-é-é-😀.pan");
    let reopened = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    reopened.load_database(&path).await.unwrap();
    reopened.close().await.unwrap();
}

#[tokio::test]
async fn unconfirmed_science_close_retains_source_ownership_until_completion() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let gate = Arc::new(tokio::sync::Semaphore::new(0));
    let science = Arc::new(Python {
        gate: Some(gate.clone()),
        honor_cancel: false,
        ..Python::default()
    });
    let mut config = options(directory.path());
    config.close_timeout = std::time::Duration::from_millis(20);
    let workspace = LegacyWorkspace::new(config, science.clone()).await.unwrap();
    let copy = workspace.clone();
    let p = path.clone();
    let loading = tokio::spawn(async move { copy.load_database(&p).await });
    while science.calls.load(Ordering::SeqCst) == 0 {
        tokio::task::yield_now().await;
    }
    assert!(matches!(
        workspace.close().await,
        Err(LegacyError::CleanupPending)
    ));
    let other = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    assert!(matches!(
        other.load_database(&path).await,
        Err(LegacyError::Busy)
    ));
    gate.add_permits(1);
    assert!(loading.await.unwrap().is_err());
    workspace.close().await.unwrap();
    other.load_database(&path).await.unwrap();
    other.close().await.unwrap();
}

#[tokio::test]
async fn concurrent_threshold_load_and_read_observe_complete_serial_versions() {
    let directory = tempfile::tempdir().unwrap();
    let first = source(&directory.path().join("one"));
    let second = source(&directory.path().join("two"));
    let before = raw_rows(&first);
    let gate = Arc::new(tokio::sync::Semaphore::new(1));
    let science = Arc::new(Python {
        gate: Some(gate.clone()),
        ..Python::default()
    });
    let workspace = LegacyWorkspace::new(options(directory.path()), science.clone())
        .await
        .unwrap();
    workspace.load_database(&first).await.unwrap();
    let copy = workspace.clone();
    let threshold = tokio::spawn(async move { copy.threshold(json!({"offset_db":3.5})).await });
    while science.calls.load(Ordering::SeqCst) < 2 {
        tokio::task::yield_now().await;
    }
    assert_eq!(workspace.version(), 1);
    let copy = workspace.clone();
    let expected = second.clone();
    let loading = tokio::spawn(async move { copy.load_database(&second).await });
    tokio::task::yield_now().await;
    let copy = workspace.clone();
    let reading = tokio::spawn(async move { copy.snapshot(true).await });
    gate.add_permits(2);
    threshold.await.unwrap().unwrap();
    loading.await.unwrap().unwrap();
    let snapshot = reading.await.unwrap().unwrap();
    assert_eq!(snapshot["version"], 3);
    assert_eq!(snapshot["database"], expected.to_str().unwrap());
    assert_eq!(snapshot["threshold"], oracle()["loaded"]["threshold"]);
    assert_eq!(raw_rows(&first), before);
    workspace.close().await.unwrap();
}

#[tokio::test]
async fn source_change_during_science_refuses_canonical_threshold_commit() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let gate = Arc::new(tokio::sync::Semaphore::new(1));
    let science = Arc::new(Python {
        gate: Some(gate.clone()),
        ..Python::default()
    });
    let workspace = LegacyWorkspace::new(options(directory.path()), science.clone())
        .await
        .unwrap();
    workspace.load_database(&path).await.unwrap();
    let copy = workspace.clone();
    let updating = tokio::spawn(async move { copy.threshold(json!({"offset_db":3.5})).await });
    while science.calls.load(Ordering::SeqCst) < 2 {
        tokio::task::yield_now().await;
    }
    let external = rusqlite::Connection::open(&path).unwrap();
    let bytes: Vec<u8> = (0..11).flat_map(|_| (-60_f64).to_le_bytes()).collect();
    external
        .execute("UPDATE panorama SET levels=? WHERE id=1", [bytes])
        .unwrap();
    gate.add_permits(1);
    assert!(matches!(updating.await.unwrap(), Err(LegacyError::Stale)));
    assert_eq!(workspace.version(), 1);
    assert_eq!(
        external
            .query_row(
                "SELECT COUNT(*) FROM threshold_line WHERE line_index=0",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        0
    );
    assert!(matches!(
        workspace.snapshot(true).await,
        Err(LegacyError::Stale)
    ));
    gate.add_permits(1);
    workspace.load_database(&path).await.unwrap();
    assert_eq!(workspace.version(), 2);
    workspace.close().await.unwrap();
}

#[tokio::test]
async fn custom_threshold_triggers_cannot_modify_historical_raw_rows() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER unexpected_raw_write AFTER INSERT ON threshold_line BEGIN DELETE FROM panorama; END;").unwrap();
    let before = raw_rows(&path);
    let workspace = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    workspace.load_database(&path).await.unwrap();
    assert!(workspace.threshold(json!({"offset_db":3})).await.is_err());
    assert_eq!(workspace.version(), 1);
    assert_eq!(raw_rows(&path), before);
    assert_eq!(
        db.query_row(
            "SELECT COUNT(*) FROM threshold_line WHERE line_index=0",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
    workspace.close().await.unwrap();
}

#[tokio::test]
async fn actor_join_failure_retains_per_file_owner_after_sql_thread_stops() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let mut config = options(directory.path());
    config.close_timeout = std::time::Duration::from_millis(20);
    let workspace = LegacyWorkspace::new(
        config,
        Arc::new(Python {
            panic_in_execute: true,
            ..Python::default()
        }),
    )
    .await
    .unwrap();
    assert!(workspace.load_database(&path).await.is_err());
    assert!(matches!(
        workspace.close().await,
        Err(LegacyError::CleanupPending)
    ));
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    let other = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    assert!(matches!(
        other.load_database(&path).await,
        Err(LegacyError::Busy)
    ));
    other.close().await.unwrap();
}

#[tokio::test]
async fn sidecar_signal_rows_remain_until_actual_threshold_reconciliation_without_file_rewrite() {
    let directory = tempfile::tempdir().unwrap();
    let _slot = PROCESS_SLOT.lock().unwrap();
    let output = Command::new(python())
        .arg(root().join("crates/ori-legacy/tests/generate_fixtures.py"))
        .arg(directory.path())
        .arg("--sidecar")
        .output()
        .unwrap();
    drop(_slot);
    assert!(output.status.success());
    let path = PathBuf::from(String::from_utf8(output.stdout).unwrap().trim());
    let sidecar = directory.path().join("signals.json");
    let original = std::fs::read(&sidecar).unwrap();
    let audit = source_audit(&path);
    let workspace = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    workspace.load_database(&path).await.unwrap();
    assert_eq!(
        normalized(workspace.snapshot(true).await.unwrap()),
        oracle()["sidecar"]["loaded"]
    );
    assert_eq!(
        workspace.signals().await.unwrap()["signals"],
        oracle()["sidecar"]["loaded"]["signals"]
    );
    workspace.threshold(json!({"offset_db":3.5})).await.unwrap();
    assert_eq!(
        normalized(workspace.snapshot(true).await.unwrap()),
        oracle()["sidecar"]["changed"]["state"]
    );
    assert_eq!(std::fs::read(&sidecar).unwrap(), original);
    assert_eq!(source_audit(&path), audit);
    workspace.close().await.unwrap();
}

#[tokio::test]
async fn threshold_foreign_key_cascades_cannot_rewrite_other_historical_rows() {
    let directory = tempfile::tempdir().unwrap();
    let path = source(directory.path());
    let db = rusqlite::Connection::open(&path).unwrap();
    let levels: Vec<u8> = db
        .query_row("SELECT levels FROM panorama WHERE id=1", [], |r| r.get(0))
        .unwrap();
    let frequencies: Vec<u8> = (0..11)
        .flat_map(|i| (100000000.125 + f64::from(i) * 0.25).to_le_bytes())
        .collect();
    db.execute(
        "INSERT INTO threshold_line VALUES(0,?,?)",
        rusqlite::params![frequencies, levels],
    )
    .unwrap();
    db.execute_batch("CREATE UNIQUE INDEX threshold_levels_unique ON threshold_line(levels);CREATE TABLE external_raw_history(id INTEGER PRIMARY KEY,levels BLOB REFERENCES threshold_line(levels) ON UPDATE CASCADE);").unwrap();
    db.execute("INSERT INTO external_raw_history VALUES(1,?)", [&levels])
        .unwrap();
    let workspace = LegacyWorkspace::new(options(directory.path()), Arc::new(Python::default()))
        .await
        .unwrap();
    workspace.load_database(&path).await.unwrap();
    assert!(workspace.threshold(json!({"offset_db":3.5})).await.is_err());
    assert_eq!(
        db.query_row(
            "SELECT levels FROM external_raw_history WHERE id=1",
            [],
            |r| r.get::<_, Vec<u8>>(0)
        )
        .unwrap(),
        levels
    );
    assert_eq!(workspace.version(), 1);
    workspace.close().await.unwrap();
}
