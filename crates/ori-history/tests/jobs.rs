#![cfg(unix)]
use ori_archive::{Archive, ArchiveConfig};
use ori_history::{
    ExportTable, HistoryConfig, HistoryError, HistoryRequest, HistoryService, JobPhase, Operation,
    Threshold,
};
use ori_runtime::worker::{ResearchPool, WorkerSpec};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::PathBuf, time::Duration};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap()
}
fn temporary() -> tempfile::TempDir {
    tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap()
}
fn spec() -> WorkerSpec {
    WorkerSpec {
        program: std::env::var_os("ORI_RESEARCH_PYTHON")
            .map(PathBuf::from)
            .expect("Set ORI_RESEARCH_PYTHON to the installed isolated research interpreter"),
        args: vec!["-I".into(), "-m".into(), "ori_research".into()],
        environment: BTreeMap::new(),
        deadline_ms: 10_000,
        require_hard_memory: false,
        linux_cgroup: None,
    }
}
async fn archive(root: &std::path::Path, padding: bool) -> Archive {
    let archive = Archive::open(ArchiveConfig::new(root.join("source.sqlite"))).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    if padding {
        session["metadata"]["synthetic-padding"] = json!("x".repeat(2_000_000));
    }
    archive.create_session(session).await.unwrap();
    archive
        .append_sweeps(vec![fixture()["sweep"].clone()])
        .await
        .unwrap();
    archive
        .append_observations(vec![fixture()["observation"].clone()])
        .await
        .unwrap();
    archive
}
fn request(operation: Operation) -> HistoryRequest {
    let mut request = HistoryRequest::new(operation);
    request.filters.session_id = Some("fixture-сессия".into());
    request
}
async fn finished(history: &HistoryService, id: &str) -> ori_history::JobView {
    tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            let state = history.status(id).await.unwrap();
            if matches!(
                state.phase,
                JobPhase::Completed | JobPhase::Failed | JobPhase::Cancelled
            ) {
                return state;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("owned history job did not finish within its test budget")
}

#[tokio::test]
async fn installed_isolated_worker_runs_export_summary_and_raw_unit_occupancy_on_sealed_source() {
    let root = temporary();
    let archive = archive(root.path(), false).await;
    let pool = ResearchPool::default();
    let history = HistoryService::new(
        archive.clone(),
        pool,
        spec(),
        HistoryConfig::new(root.path().join("cache"), root.path().join("snapshots")),
    )
    .await
    .unwrap();
    for operation in [Operation::Export, Operation::Summary, Operation::Occupancy] {
        let mut query = request(operation);
        if operation == Operation::Occupancy {
            query.threshold = Some(Threshold {
                value: 137.0,
                unit: "s_meter_count".into(),
            });
        }
        let id = history.submit(query).await.unwrap();
        let view = finished(&history, &id).await;
        assert_eq!(view.phase, JobPhase::Completed, "{:?}", view.failure);
        assert_eq!(view.source_boundary, Some(3));
        let output = history.result(&id).await.unwrap();
        let serialized = serde_json::to_string(output.as_ref()).unwrap();
        assert!(
            !serialized.contains(root.path().to_str().unwrap()),
            "private paths leaked into public result"
        );
        assert!(!serialized.contains("cache_path"));
        assert!(!view.enforcement.as_ref().unwrap().hard_memory);
        match operation {
            Operation::Export => {
                assert_eq!(output.data["manifest"]["counts"]["sweeps"], 1);
                assert_eq!(output.data["manifest"]["derived"], true);
            }
            Operation::Summary => assert!(!output.data["rows"].as_array().unwrap().is_empty()),
            Operation::Occupancy => {
                assert_eq!(output.data["rows"][0]["level_unit"], "s_meter_count");
                assert_eq!(output.data["rows"][0]["occupied_count"], 1);
            }
        }
    }
    let raw = archive.get_sweep("fixture-sweep-α").await.unwrap().unwrap();
    assert_eq!(
        raw["levels"][1].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        raw["levels"][2].as_f64().unwrap().to_bits(),
        (-1.0000000000000002_f64).to_bits()
    );
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn replay_uses_same_snapshot_and_cursor_after_new_original_commits() {
    let root = temporary();
    let archive = archive(root.path(), false).await;
    let history = HistoryService::new(
        archive.clone(),
        ResearchPool::default(),
        spec(),
        HistoryConfig::new(root.path().join("cache"), root.path().join("snapshots")),
    )
    .await
    .unwrap();
    let mut query = request(Operation::Summary);
    query.page_size = 1;
    let id = history.submit(query).await.unwrap();
    assert_eq!(finished(&history, &id).await.phase, JobPhase::Completed);
    let first = history.result(&id).await.unwrap();
    let cursor = first.data["next_cursor"].as_str().unwrap().to_owned();
    let mut later = fixture()["sweep"].clone();
    later["id"] = json!("after-snapshot");
    archive.append_sweeps(vec![later]).await.unwrap();
    let next = history.replay(&id, Some(cursor)).await.unwrap();
    assert_eq!(finished(&history, &next).await.phase, JobPhase::Completed);
    let second = history.result(&next).await.unwrap();
    assert_eq!(first.data["cache_key"], second.data["cache_key"]);
    assert_eq!(second.data["event_watermark"], 3);
    assert_eq!(second.data["rows"][0]["measurement_count"], 1);
    assert!(archive.get_sweep("after-snapshot").await.unwrap().is_some());
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn shared_pool_busy_pressure_and_cancelled_close_keep_owned_reaping() {
    let root = temporary();
    let archive = archive(root.path(), true).await;
    let pool = ResearchPool::default();
    let history = HistoryService::new(
        archive.clone(),
        pool.clone(),
        spec(),
        HistoryConfig::new(root.path().join("cache"), root.path().join("snapshots")),
    )
    .await
    .unwrap();
    let reserved = pool.try_reserve().unwrap();
    assert!(matches!(
        history.submit(request(Operation::Export)).await,
        Err(HistoryError::Busy)
    ));
    drop(reserved);
    let id = history.submit(request(Operation::Export)).await.unwrap();
    history.cancel(&id).await.unwrap();
    assert_eq!(finished(&history, &id).await.phase, JobPhase::Cancelled);
    assert!(pool.try_reserve().is_ok());
    let _id = history.submit(request(Operation::Export)).await.unwrap();
    history.qos().pressure(true).await.unwrap();
    assert!(
        pool.try_reserve().is_ok(),
        "pressure acknowledged before job JOIN released shared admission"
    );
    assert!(matches!(
        history.submit(request(Operation::Export)).await,
        Err(HistoryError::Pressure)
    ));
    history.qos().pressure(false).await.unwrap();
    let _id = history.submit(request(Operation::Export)).await.unwrap();
    let _ = tokio::time::timeout(Duration::from_millis(1), history.close()).await;
    // Dropping the first close future must not detach the owned worker/snapshot.
    history.close().await.unwrap();
    assert!(pool.try_reserve().is_ok());
    assert!(
        archive
            .get_session("fixture-сессия")
            .await
            .unwrap()
            .is_some()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn completed_metadata_is_bounded_and_unsafe_or_hard_memory_without_cgroup_is_refused() {
    let root = temporary();
    let archive = archive(root.path(), false).await;
    let mut hard = spec();
    hard.require_hard_memory = true;
    assert!(
        HistoryService::new(
            archive.clone(),
            ResearchPool::default(),
            hard,
            HistoryConfig::new(
                root.path().join("unused-cache"),
                root.path().join("unused-snapshots")
            )
        )
        .await
        .is_err()
    );
    let mut unsafe_spec = spec();
    unsafe_spec.args = vec!["-c".into(), "arbitrary()".into()];
    assert!(
        HistoryService::new(
            archive.clone(),
            ResearchPool::default(),
            unsafe_spec,
            HistoryConfig::new(
                root.path().join("unused-cache"),
                root.path().join("unused-snapshots")
            )
        )
        .await
        .is_err()
    );
    let mut config = HistoryConfig::new(root.path().join("cache"), root.path().join("snapshots"));
    config.completed_limit = 2;
    let history = HistoryService::new(archive.clone(), ResearchPool::default(), spec(), config)
        .await
        .unwrap();
    let mut ids = Vec::new();
    for _ in 0..3 {
        let id = history.submit(request(Operation::Export)).await.unwrap();
        assert_eq!(finished(&history, &id).await.phase, JobPhase::Completed);
        ids.push(id);
    }
    assert_eq!(history.jobs().await.unwrap().len(), 2);
    assert!(matches!(
        history.status(&ids[0]).await,
        Err(HistoryError::NotFound)
    ));
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn exported_tables_have_bounded_leased_reads_and_close_revokes_idle_readers() {
    let root = temporary();
    let archive = archive(root.path(), false).await;
    for id in ["é", "e\u{301}"] {
        let mut value = fixture()["sweep"].clone();
        value["id"] = json!(id);
        archive.append_sweeps(vec![value]).await.unwrap();
    }
    let config = HistoryConfig::new(root.path().join("cache"), root.path().join("snapshots"));
    let pool = ResearchPool::default();
    let history = HistoryService::new(archive.clone(), pool.clone(), spec(), config.clone())
        .await
        .unwrap();
    let id = history.submit(request(Operation::Export)).await.unwrap();
    assert_eq!(finished(&history, &id).await.phase, JobPhase::Completed);
    let mut reader = history
        .export_part(&id, ExportTable::SweepPoints)
        .await
        .unwrap();
    let reserved = pool.try_reserve().unwrap();
    assert!(
        matches!(reader.read_chunk().await, Err(HistoryError::Busy)),
        "file export bypassed global heavy admission"
    );
    drop(reserved);
    assert_eq!(reader.info().file, "sweep_points.parquet");
    let mut data = Vec::new();
    loop {
        let bytes = reader.read_chunk().await.unwrap();
        assert!(bytes.len() <= 65536);
        if bytes.is_empty() {
            break;
        }
        data.extend(bytes);
    }
    assert_eq!(data.len() as u64, reader.info().bytes);
    assert_eq!(&data[..4], b"PAR1");
    assert_eq!(&data[data.len() - 4..], b"PAR1");
    let exported = root.path().join("synthetic-points.parquet");
    std::fs::write(&exported, &data).unwrap();
    let verified=std::process::Command::new(spec().program)
        .args(["-I","-c",r#"
import pyarrow.parquet as pq, struct, sys
rows=pq.read_table(sys.argv[1],use_threads=False).to_pylist()
ids={row['sweep_id'].encode('utf-8') for row in rows}
assert ids=={s.encode('utf-8') for s in ['fixture-sweep-α','é','e\u0301']}
expected=['c059080000000000','8000000000000000','bff0000000000001','c040600000000000']
for identity in ids:
    points=sorted([r for r in rows if r['sweep_id'].encode('utf-8')==identity],key=lambda r:r['point_index'])
    assert [r['point_index'] for r in points]==[0,1,2,3]
    assert [struct.pack('>d',r['level']).hex() for r in points]==expected
    assert [r['frequency_hz'] for r in points]==[100000001,100000002,100000003,100000004]
    assert all(r['started_at_us']==1794398400123456 and r['level_unit']=='dBm' for r in points)
print('synthetic exported precision verified')
"#])
        .arg(exported).env_clear().env("OPENBLAS_NUM_THREADS","1").env("OMP_NUM_THREADS","1")
        .output().unwrap();
    assert!(
        verified.status.success(),
        "owned Parquet verification failed: {}",
        String::from_utf8_lossy(&verified.stderr)
    );
    assert!(
        matches!(
            history.submit(request(Operation::Summary)).await,
            Err(HistoryError::Busy)
        ),
        "leased cache must not be mutated or reclaimed"
    );
    let mut second = history
        .export_part(&id, ExportTable::Observations)
        .await
        .unwrap();
    assert!(matches!(
        history.export_part(&id, ExportTable::Streams).await,
        Err(HistoryError::Busy)
    ));
    history.close().await.unwrap();
    assert!(second.read_chunk().await.is_err());
    // Close is a proof even while the callers still retain closed reader objects.
    let reopened = HistoryService::new(archive.clone(), ResearchPool::default(), spec(), config)
        .await
        .unwrap();
    assert!(reopened.jobs().await.unwrap().is_empty());
    reopened.close().await.unwrap();
    drop(reader);
    drop(second);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn last_service_client_drop_still_reaps_background_work_and_releases_owned_roots() {
    let root = temporary();
    let archive = archive(root.path(), true).await;
    let pool = ResearchPool::default();
    let config = HistoryConfig::new(root.path().join("cache"), root.path().join("snapshots"));
    let history = HistoryService::new(archive.clone(), pool.clone(), spec(), config.clone())
        .await
        .unwrap();
    let _id = history.submit(request(Operation::Export)).await.unwrap();
    let qos = history.qos();
    drop(history);
    tokio::time::timeout(Duration::from_secs(15), qos.pressure(true))
        .await
        .unwrap()
        .unwrap();
    assert!(pool.try_reserve().is_ok());
    let reopened = tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            match HistoryService::new(archive.clone(), pool.clone(), spec(), config.clone()).await {
                Ok(service) => break service,
                Err(HistoryError::Busy) => tokio::task::yield_now().await,
                Err(error) => panic!("unexpected private-root reopen error: {error}"),
            }
        }
    })
    .await
    .unwrap();
    reopened.close().await.unwrap();
    assert!(
        archive
            .get_sweep("fixture-sweep-α")
            .await
            .unwrap()
            .is_some()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn source_cap_and_worker_resource_failure_are_visible_without_removing_originals() {
    let root = temporary();
    let archive = archive(root.path(), false).await;
    let mut config = HistoryConfig::new(
        root.path().join("small-cache"),
        root.path().join("small-snapshots"),
    );
    config.max_source_bytes = 1;
    let history = HistoryService::new(archive.clone(), ResearchPool::default(), spec(), config)
        .await
        .unwrap();
    let id = history.submit(request(Operation::Summary)).await.unwrap();
    let failed = finished(&history, &id).await;
    assert_eq!(failed.phase, JobPhase::Failed);
    assert_eq!(failed.failure.unwrap().code, "source_limit");
    history.close().await.unwrap();
    let pool = ResearchPool::default();
    let mut config = HistoryConfig::new(
        root.path().join("rss-cache"),
        root.path().join("rss-snapshots"),
    );
    config.worker_rss_bytes = 1;
    let history = HistoryService::new(archive.clone(), pool.clone(), spec(), config)
        .await
        .unwrap();
    let id = history.submit(request(Operation::Export)).await.unwrap();
    let failed = finished(&history, &id).await;
    assert_eq!(failed.phase, JobPhase::Failed);
    assert_eq!(failed.failure.unwrap().code, "resource_limit");
    assert!(pool.try_reserve().is_ok(), "failed child was not reaped");
    assert!(
        archive
            .get_sweep("fixture-sweep-α")
            .await
            .unwrap()
            .is_some()
    );
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn small_selected_source_fits_even_when_unrelated_canonical_bytes_exceed_source_budget() {
    let root = temporary();
    let archive = archive(root.path(), true).await;
    let budget = 64 * 1024;
    assert!(
        archive.capacity().await.unwrap()["database_bytes"]
            .as_u64()
            .unwrap()
            > budget
    );
    let original_session = archive
        .get_session("fixture-сессия")
        .await
        .unwrap()
        .unwrap();
    let original_sweep = archive.get_sweep("fixture-sweep-α").await.unwrap().unwrap();
    let original_observation = archive
        .get_observation("fixture-observation-α")
        .await
        .unwrap()
        .unwrap();
    let mut config = HistoryConfig::new(
        root.path().join("selected-cache"),
        root.path().join("selected-snapshots"),
    );
    config.max_source_bytes = budget;
    let history = HistoryService::new(archive.clone(), ResearchPool::default(), spec(), config)
        .await
        .unwrap();
    let id = history.submit(request(Operation::Export)).await.unwrap();
    let view = finished(&history, &id).await;
    assert_eq!(view.phase, JobPhase::Completed, "{:?}", view.failure);
    let output = history.result(&id).await.unwrap();
    assert_eq!(
        output.data["manifest"]["source"]["kind"],
        "scoped-derived-v1"
    );
    assert_eq!(output.data["manifest"]["source"]["event_watermark"], 3);
    assert!(output.data["manifest"]["source"]["bytes"].as_u64().unwrap() < budget);
    assert_eq!(output.data["manifest"]["counts"]["sweep_points"], 4);
    assert_eq!(output.data["manifest"]["counts"]["observations"], 1);
    assert_eq!(
        archive
            .get_session("fixture-сессия")
            .await
            .unwrap()
            .unwrap(),
        original_session
    );
    assert_eq!(
        archive.get_sweep("fixture-sweep-α").await.unwrap().unwrap(),
        original_sweep
    );
    assert_eq!(
        archive
            .get_observation("fixture-observation-α")
            .await
            .unwrap()
            .unwrap(),
        original_observation
    );
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn selected_overflow_publishes_no_source_and_normal_restart_keeps_originals() {
    let root = temporary();
    let archive = archive(root.path(), false).await;
    let mut oversized = fixture()["sweep"].clone();
    oversized["id"] = json!("selected-overflow");
    oversized["settings"]["synthetic_padding"] = json!("x".repeat(100_000));
    archive.append_sweeps(vec![oversized]).await.unwrap();
    let original = archive
        .get_sweep("selected-overflow")
        .await
        .unwrap()
        .unwrap();
    let snapshots = root.path().join("snapshots");
    let mut config = HistoryConfig::new(root.path().join("cache"), snapshots.clone());
    config.max_source_bytes = 64 * 1024;
    let history = HistoryService::new(
        archive.clone(),
        ResearchPool::default(),
        spec(),
        config.clone(),
    )
    .await
    .unwrap();
    let id = history.submit(request(Operation::Export)).await.unwrap();
    let failed = finished(&history, &id).await;
    assert_eq!(failed.phase, JobPhase::Failed);
    assert_eq!(failed.failure.unwrap().code, "source_limit");
    assert!(failed.source_sha256.is_none());
    assert!(!std::fs::read_dir(&snapshots).unwrap().any(|p| {
        p.unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".history-snapshot-")
    }));
    history.close().await.unwrap();
    let restarted = HistoryService::new(archive.clone(), ResearchPool::default(), spec(), config)
        .await
        .unwrap();
    assert!(restarted.jobs().await.unwrap().is_empty());
    assert_eq!(
        archive
            .get_sweep("selected-overflow")
            .await
            .unwrap()
            .unwrap(),
        original
    );
    restarted.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn shared_worker_source_is_readonly_checksummed_and_pressure_joins_pending_selection() {
    use fs2::FileExt;
    use sha2::Digest;
    let root = temporary();
    let archive = archive(root.path(), false).await;
    let pool = ResearchPool::default();
    let qos = ori_runtime::qos::BackgroundQos::with_shared_io(root.path().join("io")).unwrap();
    let snapshots = root.path().join("snapshots");
    let mut config = HistoryConfig::new(root.path().join("cache"), snapshots.clone());
    config.qos = qos.clone();
    let history = HistoryService::new(archive.clone(), pool.clone(), spec(), config)
        .await
        .unwrap();
    let id = history.submit(request(Operation::Export)).await.unwrap();
    let view = finished(&history, &id).await;
    assert_eq!(view.phase, JobPhase::Completed, "{:?}", view.failure);
    let output = history.result(&id).await.unwrap();
    assert_eq!(output.metrics["cross_process_logical_io"], true);
    let worker_bytes = [
        "source_bytes_read",
        "cache_bytes_read",
        "cache_bytes_written",
    ]
    .iter()
    .map(|name| output.metrics[name].as_u64().unwrap())
    .sum::<u64>();
    assert!(
        qos.status().logical_bytes > worker_bytes,
        "selected source reads/writes bypassed the worker's shared timeline"
    );
    let retained: Vec<_> = std::fs::read_dir(&snapshots)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.join("sealed").exists())
        .collect();
    assert_eq!(retained.len(), 1);
    let source = retained[0].join("sealed/records.ndjson");
    assert!(std::fs::metadata(&source).unwrap().permissions().readonly());
    let bytes = std::fs::read(&source).unwrap();
    let sha = sha2::Sha256::digest(&bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    assert_eq!(view.source_sha256.as_deref(), Some(sha.as_str()));
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(retained[0].join("sealed/manifest.json")).unwrap())
            .unwrap();
    assert_eq!(manifest["purpose"], "derived-history");
    assert!(manifest.get("backup_version").is_none());
    let capability = serde_json::to_value(qos.worker_io().unwrap()).unwrap();
    let lock = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(capability["path"].as_str().unwrap())
        .unwrap();
    lock.try_lock_exclusive().unwrap();
    let pending = history.submit(request(Operation::Summary)).await.unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let count = std::fs::read_dir(&snapshots)
                .unwrap()
                .filter(|e| {
                    e.as_ref()
                        .unwrap()
                        .file_name()
                        .to_string_lossy()
                        .starts_with(".history-snapshot-")
                })
                .count();
            if count > retained.len() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        history.status(&pending).await.unwrap().phase,
        JobPhase::Snapshot
    );
    qos.pressure(true).await.unwrap();
    FileExt::unlock(&lock).unwrap();
    drop(lock);
    assert!(matches!(
        finished(&history, &pending).await.phase,
        JobPhase::Cancelled | JobPhase::Failed
    ));
    assert!(pool.try_reserve().is_ok());
    assert_eq!(std::fs::read(&source).unwrap(), bytes);
    let count = std::fs::read_dir(&snapshots)
        .unwrap()
        .filter(|e| {
            e.as_ref()
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".history-snapshot-")
        })
        .count();
    assert_eq!(
        count,
        retained.len(),
        "pressure acknowledged before incomplete source cleanup"
    );
    qos.pressure(false).await.unwrap();
    history.close().await.unwrap();
    assert!(!source.exists());
    qos.close_shared_io().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn replaced_source_cleanup_is_sticky_preserves_foreign_files_and_quarantines_admission() {
    use std::os::unix::fs::DirBuilderExt;
    let root = temporary();
    let archive = archive(root.path(), false).await;
    let pool = ResearchPool::default();
    let snapshots = root.path().join("snapshots");
    let mut config = HistoryConfig::new(root.path().join("cache"), snapshots.clone());
    config.completed_limit = 1;
    let history = HistoryService::new(archive.clone(), pool.clone(), spec(), config)
        .await
        .unwrap();
    let id = history.submit(request(Operation::Summary)).await.unwrap();
    assert_eq!(finished(&history, &id).await.phase, JobPhase::Completed);
    let source = std::fs::read_dir(&snapshots)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| path.join("sealed/records.ndjson").is_file())
        .unwrap();
    let retained = root.path().join("moved-original-source");
    std::fs::rename(&source, &retained).unwrap();
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&source)
        .unwrap();
    let sentinel = source.join("unrelated-data");
    std::fs::write(&sentinel, b"must survive a replaced owned path").unwrap();
    // Evict the previous retained source while the next job still owns the
    // shared reservation. Cancellation must not acknowledge unknown cleanup.
    history.submit(request(Operation::Summary)).await.unwrap();
    let closed = history.close().await;
    assert!(sentinel.exists(), "cleanup removed an unknown replacement");
    assert!(matches!(closed, Err(HistoryError::Internal)));
    assert_eq!(
        std::fs::read(sentinel).unwrap(),
        b"must survive a replaced owned path"
    );
    assert!(retained.join("sealed/records.ndjson").is_file());
    assert!(
        pool.try_reserve().is_err(),
        "unknown cleanup advertised free admission"
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn absent_selection_keeps_the_worker_invalid_filter_contract_and_removes_staging() {
    let root = temporary();
    let archive = archive(root.path(), false).await;
    let snapshots = root.path().join("snapshots");
    let history = HistoryService::new(
        archive.clone(),
        ResearchPool::default(),
        spec(),
        HistoryConfig::new(root.path().join("cache"), snapshots.clone()),
    )
    .await
    .unwrap();
    for (session, stream) in [
        (Some("missing-session"), None),
        (None, Some("missing-stream")),
    ] {
        let mut request = request(Operation::Summary);
        request.filters.session_id = session.map(str::to_owned);
        request.filters.stream_id = stream.map(str::to_owned);
        let id = history.submit(request).await.unwrap();
        let view = finished(&history, &id).await;
        assert_eq!(view.phase, JobPhase::Failed);
        assert_eq!(view.failure.unwrap().code, "invalid_filter");
        assert!(view.source_sha256.is_none());
    }
    assert!(!std::fs::read_dir(snapshots).unwrap().any(|entry| {
        entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".history-snapshot-")
    }));
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn private_roots_refuse_existing_data_symlinks_and_another_live_owner() {
    let root = temporary();
    let archive = archive(root.path(), false).await;
    let existing = root.path().join("existing");
    std::fs::create_dir(&existing).unwrap();
    let original = existing.join("not-a-cache.sqlite");
    std::fs::write(&original, b"original-data").unwrap();
    assert!(
        HistoryService::new(
            archive.clone(),
            ResearchPool::default(),
            spec(),
            HistoryConfig::new(existing, root.path().join("s1"))
        )
        .await
        .is_err()
    );
    assert_eq!(std::fs::read(original).unwrap(), b"original-data");
    let linked = root.path().join("linked");
    std::os::unix::fs::symlink(root.path(), &linked).unwrap();
    assert!(
        HistoryService::new(
            archive.clone(),
            ResearchPool::default(),
            spec(),
            HistoryConfig::new(linked, root.path().join("s2"))
        )
        .await
        .is_err()
    );
    let config = HistoryConfig::new(root.path().join("cache"), root.path().join("snapshots"));
    let history = HistoryService::new(
        archive.clone(),
        ResearchPool::default(),
        spec(),
        config.clone(),
    )
    .await
    .unwrap();
    assert!(matches!(
        history.status(&"x".repeat(1025)).await,
        Err(HistoryError::Invalid(_))
    ));
    assert!(matches!(
        HistoryService::new(archive.clone(), ResearchPool::default(), spec(), config).await,
        Err(HistoryError::Busy)
    ));
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[test]
fn frontend_cannot_supply_sql_paths_commands_or_nonfinite_thresholds() {
    for extension in [
        json!({"sql":"SELECT *"}),
        json!({"snapshot_path":"/user/archive.sqlite"}),
        json!({"program":"/bin/sh"}),
    ] {
        let mut value = serde_json::to_value(request(Operation::Summary)).unwrap();
        value
            .as_object_mut()
            .unwrap()
            .extend(extension.as_object().unwrap().clone());
        assert!(serde_json::from_value::<HistoryRequest>(value).is_err());
    }
    let mut invalid = request(Operation::Occupancy);
    invalid.threshold = Some(Threshold {
        value: f64::NAN,
        unit: "dBm".into(),
    });
    assert!(invalid.validate().is_err());
}
