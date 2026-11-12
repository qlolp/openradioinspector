//! Real WAL/copy/QoS ownership, using a latch in the actual native I/O callback.
use super::*;
use crate::{CoreConfig, CoreMode};
use ori_archive::{
    Archive, ArchiveConfig, ArchiveError, Artifact, BackupCancellation, SnapshotExport,
    SnapshotExportLimits, SnapshotExportQuery, restore_backup, verify_backup,
};
use ori_runtime::{lease::WriterLease, qos::QosError};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    sync::mpsc::{Receiver, Sender, channel},
    time::Duration,
};
use tokio::sync::oneshot;

const SESSION: &str = " backup-e\u{301} ";
const SWEEP: &str = "source-e\u{301}/α";
const OBSERVATION: &str = "observation-e\u{301}";
const AUDIO: &str = "audio-e\u{301}";

pub(super) struct HeldIo {
    pub entered: oneshot::Sender<()>,
    pub release: Receiver<()>,
}
struct IoLatch {
    entered: oneshot::Receiver<()>,
    release: Option<Sender<()>>,
}
impl IoLatch {
    fn new() -> (Self, Hooks) {
        let (entered, receiver) = oneshot::channel();
        let (release, held) = channel();
        (
            Self {
                entered: receiver,
                release: Some(release),
            },
            Hooks {
                first_io: Some(HeldIo {
                    entered,
                    release: held,
                }),
            },
        )
    }
    async fn wait(&mut self) {
        tokio::time::timeout(Duration::from_secs(3), &mut self.entered)
            .await
            .expect("backup never entered its actual SQL I/O budget")
            .expect("backup I/O owner ended before the latch");
    }
    fn release(&mut self) {
        if let Some(release) = self.release.take() {
            let _ = release.send(());
        }
    }
}
impl Drop for IoLatch {
    fn drop(&mut self) {
        self.release();
    }
}
fn options() -> BackupOptions {
    BackupOptions {
        max_snapshot_bytes: 8 * 1024 * 1024,
        copy_timeout: Duration::from_secs(5),
        total_timeout: Duration::from_secs(10),
        cancel: BackupCancellation::default(),
    }
}
async fn until(check: impl Fn() -> bool) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !check() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("owned backup state did not advance");
}
fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap()
}
fn wav() -> Vec<u8> {
    let mut bytes = Vec::from(&b"RIFF"[..]);
    bytes.extend_from_slice(&42_u32.to_le_bytes());
    bytes.extend_from_slice(b"WAVEfmt ");
    bytes.extend_from_slice(&16_u32.to_le_bytes());
    bytes.extend_from_slice(&1_u16.to_le_bytes());
    bytes.extend_from_slice(&1_u16.to_le_bytes());
    bytes.extend_from_slice(&24_000_u32.to_le_bytes());
    bytes.extend_from_slice(&48_000_u32.to_le_bytes());
    bytes.extend_from_slice(&2_u16.to_le_bytes());
    bytes.extend_from_slice(&16_u16.to_le_bytes());
    bytes.extend_from_slice(b"data");
    bytes.extend_from_slice(&6_u32.to_le_bytes());
    for sample in [i16::MIN, -1, i16::MAX] {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    bytes
}
async fn seeded() -> (tempfile::TempDir, Arc<Core>) {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::replay(directory.path()))
        .await
        .unwrap();
    let archive = core.archive.as_ref().unwrap();
    let source = fixture();
    let mut session = source["session"].clone();
    session["id"] = json!(SESSION);
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    session["metadata"] = json!({"exact":"e\u{301}","unknown":{"source":true}});
    archive.create_session(session).await.unwrap();
    let mut sweep = source["sweep"].clone();
    sweep["id"] = json!(SWEEP);
    sweep["session_id"] = json!(SESSION);
    sweep["frequencies_hz"] = json!([
        9_007_199_254_740_993_u64,
        9_007_199_254_740_994_u64,
        9_007_199_254_740_995_u64,
        9_007_199_254_740_996_u64
    ]);
    archive.append_sweeps(vec![sweep]).await.unwrap();
    let mut observation = source["observation"].clone();
    observation["id"] = json!(OBSERVATION);
    observation["session_id"] = json!(SESSION);
    observation["level"] = json!(-0.0);
    observation["audio_uri"] = json!(format!("artifact:{AUDIO}"));
    archive
        .append_observations(vec![observation])
        .await
        .unwrap();
    let payload = wav();
    archive.put_artifact(Artifact {
        id: AUDIO.into(), session_id: SESSION.into(), stream_id: "receiver+one".into(),
        schema_version: 1, kind: "audio".into(), mime_type: "audio/wav".into(),
        filename: "owned.wav".into(), sha256: Sha256::digest(&payload).iter().map(|byte| format!("{byte:02x}")).collect(),
        created_at: "2026-11-11T12:00:00.123456Z".into(), payload,
        metadata: json!({"level_unit":"s_meter_count","interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.123581Z"},"unknown":"e\u{301}"}),
    }).await.unwrap();
    (directory, core)
}

#[tokio::test]
async fn shadow_and_acquisition_pressure_refuse_before_creating_a_destination() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::new(directory.path())).await.unwrap();
    assert_eq!(core.mode(), CoreMode::Shadow);
    let target = directory.path().join("absent");
    assert!(core.backup(&target, options()).await.is_err());
    assert!(!target.exists());
    core.close().await.unwrap();

    let (directory, core) = seeded().await;
    let target = directory.path().join("absent");
    core.background
        .as_ref()
        .unwrap()
        .qos
        .pressure(true)
        .await
        .unwrap();
    assert!(matches!(
        core.backup(&target, options()).await,
        Err(CoreError::Background(QosError::Pressure))
    ));
    assert!(!target.exists());
    assert_eq!(
        core.background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work,
        0
    );
    assert!(
        core.archive
            .as_ref()
            .unwrap()
            .get_sweep(SWEEP)
            .await
            .unwrap()
            .is_some()
    );
    core.close().await.unwrap();
}

#[tokio::test]
async fn verified_backup_coexists_with_writer_live_export_and_heavy_research_admission() {
    let (directory, core) = seeded().await;
    let archive = core.archive.as_ref().unwrap().clone();
    let source_session = archive.get_session(SESSION).await.unwrap().unwrap();
    let source_sweep = archive.get_sweep(SWEEP).await.unwrap().unwrap();
    let source_observation = archive.get_observation(OBSERVATION).await.unwrap().unwrap();
    let source_audio = archive
        .get_artifact(SESSION, "receiver+one", AUDIO)
        .await
        .unwrap()
        .unwrap();
    let export = SnapshotExport::open_live(
        archive.clone(),
        SnapshotExportQuery {
            session_id: SESSION.into(),
            stream_id: None,
        },
        SnapshotExportLimits::default(),
    )
    .unwrap();
    assert_eq!(export.ready().await.unwrap().sweeps, 1);
    let heavy = core.research_pool.try_reserve().unwrap();
    let target = directory.path().join("backup");
    let (mut latch, hooks) = IoLatch::new();
    let own = core.clone();
    let output = target.clone();
    let backup = tokio::spawn(async move { run_inner(&own, &output, options(), hooks).await });
    latch.wait().await;
    assert_eq!(
        core.background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work,
        1
    );
    drop(heavy);
    let heavy = core
        .research_pool
        .try_reserve()
        .expect("backup must not own research admission");
    let mut late = source_sweep.clone();
    late["id"] = json!("late-raw-source");
    archive.append_sweeps(vec![late]).await.unwrap();
    latch.release();
    let manifest = backup.await.unwrap().unwrap();
    assert_eq!(manifest["counts"]["sweeps"], 1);
    assert_eq!(manifest["counts"]["artifacts"], 1);
    assert!(
        archive
            .get_sweep("late-raw-source")
            .await
            .unwrap()
            .is_some()
    );
    let mut exported = Vec::new();
    while let Some(record) = export.next_record().await.unwrap() {
        exported.push(record.value);
    }
    export.close().await.unwrap();
    assert_eq!(exported.len(), 2);
    assert!(
        !exported
            .iter()
            .any(|record| record["id"] == "late-raw-source")
    );
    assert!(core.background.as_ref().unwrap().qos.status().logical_bytes > 0);
    assert_eq!(
        core.background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work,
        0
    );
    drop(heavy);
    let restored_path = directory.path().join("restored.sqlite");
    let bundle = target.clone();
    let restore = restored_path.clone();
    tokio::task::spawn_blocking(move || {
        assert_eq!(verify_backup(&bundle).unwrap(), manifest);
        restore_backup(&bundle, &restore).unwrap();
    })
    .await
    .unwrap();
    let restored = Archive::open(ArchiveConfig::new(restored_path)).unwrap();
    assert_eq!(
        restored.get_session(SESSION).await.unwrap().unwrap(),
        source_session
    );
    let sweep = restored.get_sweep(SWEEP).await.unwrap().unwrap();
    assert_eq!(sweep, source_sweep);
    for (actual, original) in sweep["levels"]
        .as_array()
        .unwrap()
        .iter()
        .zip(source_sweep["levels"].as_array().unwrap())
    {
        assert_eq!(
            actual.as_f64().unwrap().to_bits(),
            original.as_f64().unwrap().to_bits()
        );
    }
    let observation = restored
        .get_observation(OBSERVATION)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(observation, source_observation);
    assert_eq!(
        observation["level"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    let audio = restored
        .get_artifact(SESSION, "receiver+one", AUDIO)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(audio.payload, source_audio.payload);
    assert_eq!(audio.sha256, source_audio.sha256);
    assert_eq!(audio.metadata, source_audio.metadata);
    assert!(
        restored
            .get_sweep("late-raw-source")
            .await
            .unwrap()
            .is_none()
    );
    restored.close().await.unwrap();
    core.close().await.unwrap();
}

#[tokio::test]
async fn pressure_cancels_original_backup_and_waits_for_actual_sql_cleanup() {
    let (directory, core) = seeded().await;
    let target = directory.path().join("cancelled");
    let policy = options();
    let original = policy.cancel.clone();
    let (mut latch, hooks) = IoLatch::new();
    let own = core.clone();
    let output = target.clone();
    let backup = tokio::spawn(async move { run_inner(&own, &output, policy, hooks).await });
    latch.wait().await;
    let qos = core.background.as_ref().unwrap().qos.clone();
    let pressure = tokio::spawn(async move { qos.pressure(true).await });
    until(|| original.is_cancelled()).await;
    assert!(
        !pressure.is_finished(),
        "pressure ACK must wait for actual SQL JOIN"
    );
    assert!(!backup.is_finished());
    assert_eq!(
        core.background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work,
        1
    );
    latch.release();
    assert!(matches!(
        backup.await.unwrap(),
        Err(CoreError::Archive(ArchiveError::Closed))
    ));
    pressure.await.unwrap().unwrap();
    assert!(!target.exists());
    assert_eq!(
        core.background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work,
        0
    );
    core.close().await.unwrap();
}

#[tokio::test]
async fn dropped_embedding_waiter_keeps_writer_and_qos_until_original_copy_joins() {
    let (directory, core) = seeded().await;
    let target = directory.path().join("cancelled");
    let policy = options();
    let original = policy.cancel.clone();
    let (mut latch, hooks) = IoLatch::new();
    let own = core.clone();
    let output = target.clone();
    let waiter = tokio::spawn(async move { run_inner(&own, &output, policy, hooks).await });
    latch.wait().await;
    waiter.abort();
    assert!(waiter.await.unwrap_err().is_cancelled());
    until(|| original.is_cancelled()).await;
    let own = core.clone();
    let closure = tokio::spawn(async move { own.close().await });
    until(|| *core.shutdown.borrow()).await;
    assert!(!closure.is_finished());
    assert!(WriterLease::acquire(directory.path()).is_err());
    assert_eq!(
        core.background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work,
        1
    );
    latch.release();
    tokio::time::timeout(Duration::from_secs(3), closure)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(!target.exists());
    drop(WriterLease::acquire(directory.path()).unwrap());
}

#[tokio::test]
async fn caller_quotas_deadlines_and_existing_destination_are_preserved() {
    let (directory, core) = seeded().await;
    let absent = directory.path().join("absent");
    let mut policy = options();
    policy.max_snapshot_bytes = 1;
    assert!(matches!(
        core.backup(&absent, policy).await,
        Err(CoreError::Archive(ArchiveError::QueryBudget))
    ));
    assert!(!absent.exists());
    let mut policy = options();
    policy.copy_timeout = Duration::ZERO;
    assert!(matches!(
        core.backup(&absent, policy).await,
        Err(CoreError::Archive(ArchiveError::Invalid(_)))
    ));
    assert!(!absent.exists());
    let existing = directory.path().join("existing");
    std::fs::create_dir(&existing).unwrap();
    std::fs::write(existing.join("operator-owned"), b"unchanged").unwrap();
    assert!(core.backup(&existing, options()).await.is_err());
    assert_eq!(
        std::fs::read(existing.join("operator-owned")).unwrap(),
        b"unchanged"
    );
    assert_eq!(
        core.background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work,
        0
    );
    core.close().await.unwrap();
}
