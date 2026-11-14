//! Actual Core-owned receiver actor, capture owner thread and temporary archive.
use super::*;
use crate::{
    Core, CoreConfig,
    devices::{ExecutionGate, Target},
};
use ori_audio::{
    AudioBackend, AudioClock, AudioFormatRange, AudioInput, AudioResult, AudioStream,
    InputSelection, PcmSink, PushOutcome, SampleEncoding,
};
use ori_contracts::UtcTimestamp;
use serde_json::json;
use std::sync::{
    Arc, Condvar, Mutex,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::time::Duration;

fn at() -> UtcTimestamp {
    "2030-01-01T00:00:00.000000Z".parse().unwrap()
}
struct Clock;
impl AudioClock for Clock {
    fn now(&self) -> AudioResult<UtcTimestamp> {
        Ok(at())
    }
}
#[derive(Default)]
struct Raw {
    sink: Mutex<Option<PcmSink>>,
    enumerated: AtomicUsize,
    opened: AtomicUsize,
    released: AtomicUsize,
    drop_entered: AtomicBool,
    drop_hold: Mutex<bool>,
    drop_release: Condvar,
    unsupported: AtomicBool,
}
struct Backend(Arc<Raw>);
impl AudioBackend for Backend {
    fn inputs(&self) -> AudioResult<Vec<AudioInput>> {
        self.0.enumerated.fetch_add(1, Ordering::AcqRel);
        Ok(vec![AudioInput {
            index: 7,
            name: "Owned analog e\u{301}".into(),
            formats: vec![AudioFormatRange {
                channels: 1,
                encoding: if self.0.unsupported.load(Ordering::Acquire) {
                    SampleEncoding::F32
                } else {
                    SampleEncoding::I16
                },
                min_sample_rate_hz: 24000,
                max_sample_rate_hz: 24000,
            }],
        }])
    }
    fn open(
        &self,
        input: &InputSelection,
        rate: u32,
        sink: PcmSink,
    ) -> AudioResult<Box<dyn AudioStream>> {
        assert_eq!(
            (input.index, input.name.as_str(), rate),
            (7, "Owned analog e\u{301}", 24000)
        );
        self.0.opened.fetch_add(1, Ordering::AcqRel);
        *self.0.sink.lock().unwrap() = Some(sink);
        Ok(Box::new(Stream(self.0.clone())))
    }
}
struct Stream(Arc<Raw>);
impl AudioStream for Stream {
    fn start(&mut self) -> AudioResult<()> {
        Ok(())
    }
    fn stop(&mut self) -> AudioResult<()> {
        Ok(())
    }
}
impl Drop for Stream {
    fn drop(&mut self) {
        self.0.drop_entered.store(true, Ordering::Release);
        let mut held = self.0.drop_hold.lock().unwrap();
        while *held {
            held = self.0.drop_release.wait(held).unwrap();
        }
        self.0.released.fetch_add(1, Ordering::AcqRel);
    }
}
async fn until(check: impl Fn() -> bool) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !check() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}
async fn sink(raw: &Raw) -> PcmSink {
    until(|| {
        raw.sink
            .lock()
            .unwrap()
            .as_ref()
            .is_some_and(PcmSink::is_accepting)
    })
    .await;
    raw.sink.lock().unwrap().as_ref().unwrap().clone()
}
async fn core(path: &std::path::Path, raw: Arc<Raw>) -> Arc<Core> {
    let mut config = CoreConfig::replay(path);
    config.audio = AudioOptions {
        backend: Some(Arc::new(Backend(raw))),
        clock: Some(Arc::new(Clock)),
        ..Default::default()
    };
    let core = Core::open(config).await.unwrap();
    core.archive().unwrap().create_session(json!({"id":"audio","name":"Audio fixture","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"contract_version":1,"metadata":{},"streams":[{"id":"r","session_id":"audio","device":"Owned receiver fixture","role":"receiver","state":"disconnected","level_unit":"s_meter_count","settings":{"adapter":"ardv1-sim","period_s":0.1}}]})).await.unwrap();
    let manager = core.devices.as_ref().unwrap();
    manager
        .start_stream("audio", "r", manager.public_gate(Target::new("audio", "r")))
        .await
        .unwrap();
    core
}
async fn proof(core: &Core, frequency: u64) -> TuneProof {
    let manager = core.devices.as_ref().unwrap();
    let target = Target::new("audio", "r");
    let out = manager
        .tune(
            target.clone(),
            frequency,
            Some("FM".into()),
            manager.public_gate(target),
        )
        .await
        .unwrap();
    let reply = ori_guard::TuneReply {
        acknowledged: Some(true),
        tuned: out
            .tuned
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect(),
        worker: ori_guard::WorkerStatus {
            session_id: out.worker.session_id,
            stream_id: out.worker.stream_id,
            state: out.worker.state,
            alive: out.worker.alive,
            current: out
                .worker
                .current
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
            extra: Default::default(),
            worker_instance: out.worker.worker_instance,
        },
        acknowledged_at: out.acknowledged_at.parse().unwrap(),
        applied_tune_revision: out.applied_tune_revision,
    };
    TuneProof::new(
        &ori_guard::ControlTarget {
            session_id: "audio".into(),
            stream_id: "r".into(),
        },
        &reply,
    )
    .unwrap()
}
fn input() -> InputSelection {
    InputSelection {
        index: 7,
        name: "Owned analog e\u{301}".into(),
    }
}
fn gate() -> ExecutionGate {
    ExecutionGate::new(|| Ok(()), None)
}

#[tokio::test]
async fn guard_inputs_only_report_actual_exact_24khz_mono_pcm16_formats() {
    let dir = tempfile::tempdir().unwrap();
    let raw = Arc::new(Raw::default());
    let core = core(dir.path(), raw.clone()).await;
    assert_eq!(raw.enumerated.load(Ordering::Acquire), 0);
    let inputs = core.audio.as_ref().unwrap().inputs().await.unwrap();
    assert_eq!(inputs.len(), 1);
    assert_eq!(inputs[0].name, "Owned analog e\u{301}");
    assert_eq!(inputs[0].extra["formats"][0]["encoding"], "pcm16");
    assert_eq!(
        core.domain().unwrap().guard.audio_inputs().await.unwrap()["available"],
        true
    );
    raw.unsupported.store(true, Ordering::Release);
    assert!(
        core.audio
            .as_ref()
            .unwrap()
            .inputs()
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        core.domain().unwrap().guard.audio_inputs().await.unwrap()["available"],
        false
    );
    assert_eq!(raw.opened.load(Ordering::Acquire), 0);
    core.close().await.unwrap();
}

#[tokio::test]
async fn original_owner_epoch_is_not_renewed_during_capture() {
    let dir = tempfile::tempdir().unwrap();
    let raw = Arc::new(Raw::default());
    let core = core(dir.path(), raw.clone()).await;
    let original = proof(&core, 145_500_000).await;
    let epoch = Arc::new(AtomicUsize::new(0));
    let captured = epoch.clone();
    let owner = ExecutionGate::new(
        move || {
            if captured.load(Ordering::Acquire) == 0 {
                Ok(())
            } else {
                Err(crate::devices::DeviceError::Denied)
            }
        },
        None,
    );
    let owned = core.clone();
    let capture = tokio::spawn(async move {
        owned
            .audio
            .as_ref()
            .unwrap()
            .record(&owned, input(), 8.0 / 24000.0, 24000, original, owner)
            .await
    });
    let sink = sink(&raw).await;
    epoch.store(2, Ordering::Release); // acquire and return still advances epoch.
    assert_eq!(
        sink.push_pcm16(&[1, 2, 3, 4, 5, 6, 7, 8], at()),
        PushOutcome::Rejected
    );
    assert!(matches!(
        capture.await.unwrap(),
        Err(ori_audio::AudioError::Revoked)
    ));
    assert_eq!(raw.released.load(Ordering::Acquire), 1);
    core.close().await.unwrap();
}

#[tokio::test]
async fn original_tune_capture_retains_exact_pcm_and_does_not_open_at_construction() {
    let dir = tempfile::tempdir().unwrap();
    let raw = Arc::new(Raw::default());
    let core = core(dir.path(), raw.clone()).await;
    assert_eq!(raw.enumerated.load(Ordering::Acquire), 0);
    assert_eq!(raw.opened.load(Ordering::Acquire), 0);
    let original = proof(&core, 145_500_000).await;
    let owned = core.clone();
    let capture = tokio::spawn(async move {
        owned
            .audio
            .as_ref()
            .unwrap()
            .record(&owned, input(), 8.0 / 24000.0, 24000, original, gate())
            .await
    });
    let sink = sink(&raw).await;
    let pcm = [
        0, 128, 255, 127, 0, 0, 255, 255, 1, 0, 254, 255, 17, 34, 51, 68,
    ];
    assert_eq!(sink.push_pcm16(&pcm, at()), PushOutcome::Accepted);
    let result = capture.await.unwrap().unwrap();
    assert_eq!(&result.wav_bytes[44..], pcm);
    assert_eq!(result.started_at, at());
    assert_eq!(
        result.ended_at.unix_micros() - result.started_at.unix_micros(),
        333
    );
    assert_eq!(raw.released.load(Ordering::Acquire), 1);
    core.close().await.unwrap();
}

#[tokio::test]
async fn newer_same_worker_tune_rejects_the_original_receipt_before_input_open() {
    let dir = tempfile::tempdir().unwrap();
    let raw = Arc::new(Raw::default());
    let core = core(dir.path(), raw.clone()).await;
    let original = proof(&core, 145_500_000).await;
    let _newer = proof(&core, 145_600_000).await;
    let error = core
        .audio
        .as_ref()
        .unwrap()
        .record(&core, input(), 8.0 / 24000.0, 24000, original, gate())
        .await
        .unwrap_err();
    assert!(matches!(error, ori_audio::AudioError::Revoked));
    assert_eq!(raw.opened.load(Ordering::Acquire), 0);
    core.close().await.unwrap();
}

#[tokio::test]
async fn retune_during_pcm_capture_discards_result_and_leaves_archive_unchanged() {
    let dir = tempfile::tempdir().unwrap();
    let raw = Arc::new(Raw::default());
    let core = core(dir.path(), raw.clone()).await;
    let original = proof(&core, 145_500_000).await;
    let owned = core.clone();
    let capture = tokio::spawn(async move {
        owned
            .audio
            .as_ref()
            .unwrap()
            .record(&owned, input(), 8.0 / 24000.0, 24000, original, gate())
            .await
    });
    let sink = sink(&raw).await;
    assert_eq!(
        sink.push_pcm16(&[1, 2, 3, 4, 5, 6, 7, 8], at()),
        PushOutcome::Accepted
    );
    let _newer = proof(&core, 145_600_000).await;
    assert_eq!(
        sink.push_pcm16(&[1, 2, 3, 4, 5, 6, 7, 8], at()),
        PushOutcome::Rejected
    );
    assert!(matches!(
        capture.await.unwrap(),
        Err(ori_audio::AudioError::Revoked)
    ));
    assert_eq!(raw.released.load(Ordering::Acquire), 1);
    assert!(
        core.archive()
            .unwrap()
            .list_artifacts("audio", "r", 100, 0)
            .await
            .unwrap()
            .is_empty()
    );
    core.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_keeps_writer_lease_until_actual_audio_stream_drop() {
    let dir = tempfile::tempdir().unwrap();
    let raw = Arc::new(Raw::default());
    let core = core(dir.path(), raw.clone()).await;
    *raw.drop_hold.lock().unwrap() = true;
    let original = proof(&core, 145_500_000).await;
    let owned = core.clone();
    let capture = tokio::spawn(async move {
        owned
            .audio
            .as_ref()
            .unwrap()
            .record(&owned, input(), 8.0 / 24000.0, 24000, original, gate())
            .await
    });
    let _sink = sink(&raw).await;
    let owned = core.clone();
    let close = tokio::spawn(async move { owned.close().await });
    until(|| raw.drop_entered.load(Ordering::Acquire)).await;
    assert!(!close.is_finished());
    assert!(ori_runtime::lease::WriterLease::acquire(dir.path()).is_err());
    *raw.drop_hold.lock().unwrap() = false;
    raw.drop_release.notify_all();
    assert!(capture.await.unwrap().is_err());
    close.await.unwrap().unwrap();
    assert_eq!(raw.released.load(Ordering::Acquire), 1);
    assert!(ori_runtime::lease::WriterLease::acquire(dir.path()).is_ok());
}
