use ori_audio::*;
use ori_contracts::UtcTimestamp;
use sha2::{Digest, Sha256};
use std::sync::{
    Arc, Condvar, Mutex,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::time::Duration;

fn utc(s: &str) -> UtcTimestamp {
    s.parse().unwrap()
}
fn now() -> UtcTimestamp {
    utc("2026-11-11T12:00:00.000000+00:00")
}
struct Clock;
impl AudioClock for Clock {
    fn now(&self) -> AudioResult<UtcTimestamp> {
        Ok(now())
    }
}
struct Gate(AtomicBool);
impl Gate {
    fn allowed() -> Arc<Self> {
        Arc::new(Self(AtomicBool::new(true)))
    }
}
impl CaptureAuthorization for Gate {
    fn revalidate(&self) -> AudioResult<()> {
        if self.0.load(Ordering::Acquire) {
            Ok(())
        } else {
            Err(AudioError::Revoked)
        }
    }
}
struct RawBackend {
    sink: Mutex<Option<PcmSink>>,
    inputs: Mutex<Vec<AudioInput>>,
    opened: AtomicUsize,
    enumerated: AtomicUsize,
    released: AtomicUsize,
    stop_entered: AtomicBool,
    drop_entered: AtomicBool,
    stop_gate: Arc<(Mutex<bool>, Condvar)>,
    start_gate: Arc<(Mutex<bool>, Condvar)>,
    drop_gate: Arc<(Mutex<bool>, Condvar)>,
    open_gate: Arc<(Mutex<bool>, Condvar)>,
    fail_open: AtomicBool,
    fail_start: AtomicBool,
    fail_stop: AtomicBool,
    panic_open: AtomicBool,
}
impl RawBackend {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            sink: Mutex::new(None),
            inputs: Mutex::new(vec![AudioInput {
                index: 7,
                name: "LISTEN / analog e\u{301}".into(),
                formats: vec![AudioFormatRange {
                    channels: 1,
                    encoding: SampleEncoding::I16,
                    min_sample_rate_hz: 24000,
                    max_sample_rate_hz: 48000,
                }],
            }]),
            opened: AtomicUsize::new(0),
            enumerated: AtomicUsize::new(0),
            released: AtomicUsize::new(0),
            stop_entered: AtomicBool::new(false),
            drop_entered: AtomicBool::new(false),
            stop_gate: Arc::new((Mutex::new(true), Condvar::new())),
            start_gate: Arc::new((Mutex::new(true), Condvar::new())),
            drop_gate: Arc::new((Mutex::new(true), Condvar::new())),
            open_gate: Arc::new((Mutex::new(true), Condvar::new())),
            fail_open: AtomicBool::new(false),
            fail_start: AtomicBool::new(false),
            fail_stop: AtomicBool::new(false),
            panic_open: AtomicBool::new(false),
        })
    }
    async fn ready(&self) -> PcmSink {
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if let Some(s) = self.sink.lock().unwrap().clone()
                    && s.is_accepting()
                {
                    break s;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("capture opened")
    }
    fn release_stop(&self) {
        *self.stop_gate.0.lock().unwrap() = true;
        self.stop_gate.1.notify_all();
    }
}
struct RawStream {
    backend: Arc<RawBackend>,
}
impl AudioStream for RawStream {
    fn start(&mut self) -> AudioResult<()> {
        let (lock, signal) = &*self.backend.start_gate;
        let mut ready = lock.lock().unwrap();
        while !*ready {
            ready = signal.wait(ready).unwrap();
        }
        if self.backend.fail_start.load(Ordering::Acquire) {
            Err(AudioError::Backend("start failed".into()))
        } else {
            Ok(())
        }
    }
    fn stop(&mut self) -> AudioResult<()> {
        self.backend.stop_entered.store(true, Ordering::Release);
        let (lock, signal) = &*self.backend.stop_gate;
        let mut ready = lock.lock().unwrap();
        while !*ready {
            ready = signal.wait(ready).unwrap();
        }
        if self.backend.fail_stop.load(Ordering::Acquire) {
            Err(AudioError::Backend("stop failed".into()))
        } else {
            Ok(())
        }
    }
}
impl Drop for RawStream {
    fn drop(&mut self) {
        self.backend.drop_entered.store(true, Ordering::Release);
        let (lock, signal) = &*self.backend.drop_gate;
        let mut ready = lock.lock().unwrap();
        while !*ready {
            ready = signal.wait(ready).unwrap();
        }
        self.backend.released.fetch_add(1, Ordering::AcqRel);
    }
}
struct Backend(Arc<RawBackend>);
impl AudioBackend for Backend {
    fn inputs(&self) -> AudioResult<Vec<AudioInput>> {
        self.0.enumerated.fetch_add(1, Ordering::AcqRel);
        Ok(self.0.inputs.lock().unwrap().clone())
    }
    fn open(
        &self,
        input: &InputSelection,
        rate: u32,
        sink: PcmSink,
    ) -> AudioResult<Box<dyn AudioStream>> {
        assert_eq!(input.index, 7);
        assert_eq!(input.name, "LISTEN / analog e\u{301}");
        assert!([8000, 12000, 24000, 48000].contains(&rate));
        if self.0.panic_open.load(Ordering::Acquire) {
            panic!("driver init panic");
        }
        if self.0.fail_open.load(Ordering::Acquire) {
            return Err(AudioError::Backend("open failed".into()));
        }
        self.0.opened.fetch_add(1, Ordering::AcqRel);
        *self.0.sink.lock().unwrap() = Some(sink);
        let (lock, signal) = &*self.0.open_gate;
        let mut ready = lock.lock().unwrap();
        while !*ready {
            ready = signal.wait(ready).unwrap();
        }
        Ok(Box::new(RawStream {
            backend: self.0.clone(),
        }))
    }
}
fn service(backend: &Arc<RawBackend>) -> Arc<AudioService> {
    AudioService::with_clock(
        Arc::new(Backend(backend.clone())),
        CaptureLimits::default(),
        Arc::new(Clock),
    )
    .unwrap()
}
fn request(frames: u64) -> CaptureRequest {
    CaptureRequest {
        input: InputSelection {
            index: 7,
            name: "LISTEN / analog e\u{301}".into(),
        },
        sample_rate_hz: 24000,
        frames,
        not_before: now(),
    }
}
fn unhex(s: &str) -> Vec<u8> {
    s.as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|p| u8::from_str_radix(std::str::from_utf8(p).unwrap(), 16).unwrap())
        .collect()
}

#[tokio::test]
async fn original_pcm_prefix_matches_python_wave_bytes_hash_and_utc() {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/pcm16.json")).unwrap();
    let backend = RawBackend::new();
    let service = service(&backend);
    let handle = service.start_capture(request(8), Gate::allowed()).unwrap();
    let sink = backend.ready().await;
    let mut pcm = unhex(fixture["pcm_hex"].as_str().unwrap());
    pcm.extend_from_slice(&[44, 55, 66, 77]);
    assert_eq!(sink.push_pcm16(&pcm, now()), PushOutcome::Accepted);
    let out = handle.result().await.unwrap();
    assert_eq!(out.wav_bytes, unhex(fixture["wav_hex"].as_str().unwrap()));
    assert_eq!(
        Sha256::digest(&out.wav_bytes)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>(),
        fixture["wav_sha256"].as_str().unwrap()
    );
    assert_eq!(
        out.started_at.as_str(),
        fixture["started_at"].as_str().unwrap()
    );
    assert_eq!(out.ended_at.as_str(), fixture["ended_at"].as_str().unwrap());
    assert_eq!(
        (out.frames, out.sample_rate_hz, out.channels),
        (8, 24000, 1)
    );
    assert_eq!(backend.released.load(Ordering::Acquire), 1);
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn constructors_do_not_enumerate_or_open_and_selection_is_exact() {
    let backend = RawBackend::new();
    let service = service(&backend);
    assert_eq!(backend.opened.load(Ordering::Acquire), 0);
    assert_eq!(backend.enumerated.load(Ordering::Acquire), 0);
    let devices = service.inputs().await.unwrap();
    assert_eq!(devices[0].index, 7);
    let mut wrong = request(8);
    wrong.input.name = "LISTEN / analog é".into();
    let handle = service.start_capture(wrong, Gate::allowed()).unwrap();
    assert!(matches!(
        handle.result().await,
        Err(AudioError::InputUnavailable)
    ));
    assert_eq!(backend.opened.load(Ordering::Acquire), 0);
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn revoke_before_open_never_claims_device() {
    let backend = RawBackend::new();
    let service = service(&backend);
    let gate = Gate::allowed();
    gate.0.store(false, Ordering::Release);
    let result = service.start_capture(request(8), gate);
    match result {
        Err(AudioError::Revoked) => {}
        Ok(h) => assert!(matches!(h.result().await, Err(AudioError::Revoked))),
        _ => panic!("wrong denial"),
    }
    assert_eq!(backend.opened.load(Ordering::Acquire), 0);
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn retune_discards_accumulated_pcm_and_rejects_old_sink_after_new_capture() {
    let backend = RawBackend::new();
    let service = service(&backend);
    let gate = Gate::allowed();
    let first = service.start_capture(request(8), gate.clone()).unwrap();
    let old = backend.ready().await;
    old.push_pcm16(&[1, 2, 3, 4], now());
    gate.0.store(false, Ordering::Release);
    assert_eq!(old.push_pcm16(&[5, 6], now()), PushOutcome::Rejected);
    assert!(matches!(first.result().await, Err(AudioError::Revoked)));
    *backend.sink.lock().unwrap() = None;
    let next = service.start_capture(request(2), Gate::allowed()).unwrap();
    let fresh = backend.ready().await;
    assert_eq!(old.push_pcm16(&[99, 99], now()), PushOutcome::Rejected);
    fresh.push_pcm16(&[7, 8, 9, 10], now());
    assert_eq!(
        &next.result().await.unwrap().wav_bytes[44..],
        &[7, 8, 9, 10]
    );
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn pre_tune_frames_are_flushed_instead_of_relabelled() {
    let backend = RawBackend::new();
    let service = service(&backend);
    let handle = service.start_capture(request(2), Gate::allowed()).unwrap();
    let sink = backend.ready().await;
    assert_eq!(
        sink.push_pcm16(&[1, 2, 3, 4], utc("2026-11-11T11:59:59.999000+00:00")),
        PushOutcome::Discarded
    );
    sink.push_pcm16(&[5, 6, 7, 8], now());
    assert_eq!(
        &handle.result().await.unwrap().wav_bytes[44..],
        &[5, 6, 7, 8]
    );
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn unsupported_rate_channels_and_encoding_never_get_converted() {
    for formats in [
        vec![],
        vec![AudioFormatRange {
            channels: 2,
            encoding: SampleEncoding::I16,
            min_sample_rate_hz: 24000,
            max_sample_rate_hz: 48000,
        }],
        vec![AudioFormatRange {
            channels: 1,
            encoding: SampleEncoding::F32,
            min_sample_rate_hz: 24000,
            max_sample_rate_hz: 48000,
        }],
        vec![AudioFormatRange {
            channels: 1,
            encoding: SampleEncoding::I16,
            min_sample_rate_hz: 48000,
            max_sample_rate_hz: 48000,
        }],
    ] {
        let backend = RawBackend::new();
        backend.inputs.lock().unwrap()[0].formats = formats;
        let service = service(&backend);
        let h = service.start_capture(request(8), Gate::allowed()).unwrap();
        assert!(matches!(
            h.result().await,
            Err(AudioError::UnsupportedFormat)
        ));
        assert_eq!(backend.opened.load(Ordering::Acquire), 0);
        service.begin_close().unwrap().completed().await.unwrap();
    }
}

#[tokio::test]
async fn callback_error_and_malformed_pcm_never_produce_evidence() {
    for malformed in [false, true] {
        let backend = RawBackend::new();
        let service = service(&backend);
        let h = service.start_capture(request(8), Gate::allowed()).unwrap();
        let sink = backend.ready().await;
        if malformed {
            sink.push_pcm16(&[1], now());
        } else {
            sink.fail(AudioError::Backend("device disconnected".into()));
        }
        assert!(h.result().await.is_err());
        assert_eq!(backend.released.load(Ordering::Acquire), 1);
        service.begin_close().unwrap().completed().await.unwrap();
    }
}

#[tokio::test]
async fn cancelled_close_waiter_keeps_device_owned_until_actual_stop_and_join() {
    let backend = RawBackend::new();
    *backend.stop_gate.0.lock().unwrap() = false;
    let service = service(&backend);
    let h = service.start_capture(request(8), Gate::allowed()).unwrap();
    let sink = backend.ready().await;
    let closing = service.begin_close().unwrap();
    let waiting = tokio::spawn({
        let c = closing.clone();
        async move { c.completed().await }
    });
    tokio::time::timeout(Duration::from_secs(2), async {
        while !backend.stop_entered.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    waiting.abort();
    let _ = waiting.await;
    assert_eq!(backend.released.load(Ordering::Acquire), 0);
    assert_eq!(sink.push_pcm16(&[1, 2], now()), PushOutcome::Rejected);
    assert!(matches!(
        service.start_capture(request(8), Gate::allowed()),
        Err(AudioError::Closed)
    ));
    assert!(
        tokio::time::timeout(Duration::from_millis(20), closing.completed())
            .await
            .is_err()
    );
    backend.release_stop();
    closing.completed().await.unwrap();
    assert_eq!(backend.released.load(Ordering::Acquire), 1);
    assert!(matches!(
        h.result().await,
        Err(AudioError::Closed) | Err(AudioError::Cancelled)
    ));
}

#[tokio::test]
async fn blocking_audio_release_does_not_block_receiver_executor() {
    let backend = RawBackend::new();
    *backend.stop_gate.0.lock().unwrap() = false;
    let service = service(&backend);
    let _h = service.start_capture(request(8), Gate::allowed()).unwrap();
    backend.ready().await;
    let closing = service.begin_close().unwrap();
    let progress = Arc::new(AtomicUsize::new(0));
    let p = progress.clone();
    let polling = tokio::spawn(async move {
        for _ in 0..5 {
            tokio::time::sleep(Duration::from_millis(2)).await;
            p.fetch_add(1, Ordering::AcqRel);
        }
    });
    tokio::time::timeout(Duration::from_secs(1), polling)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(progress.load(Ordering::Acquire), 5);
    assert_eq!(backend.released.load(Ordering::Acquire), 0);
    backend.release_stop();
    closing.completed().await.unwrap();
}

#[tokio::test]
async fn init_start_stop_errors_and_thread_panic_are_visible() {
    for which in 0..4 {
        let backend = RawBackend::new();
        [
            &backend.fail_open,
            &backend.fail_start,
            &backend.fail_stop,
            &backend.panic_open,
        ][which]
            .store(true, Ordering::Release);
        let service = service(&backend);
        let h = service.start_capture(request(2), Gate::allowed()).unwrap();
        if which == 2 {
            backend.ready().await.push_pcm16(&[1, 2, 3, 4], now());
        }
        assert!(h.result().await.is_err());
        let closed = service.begin_close().unwrap().completed().await;
        if which == 2 || which == 3 {
            assert!(closed.is_err());
        } else {
            closed.unwrap();
        }
    }
}

#[tokio::test]
async fn duration_and_byte_limits_reject_before_device_open() {
    let backend = RawBackend::new();
    let service = service(&backend);
    for mut req in [request(0), request(24000 * 31), request(u64::MAX)] {
        assert!(matches!(
            service.start_capture(req.clone(), Gate::allowed()),
            Err(AudioError::InvalidRequest(_))
        ));
        req.sample_rate_hz = 44100;
        assert!(matches!(
            service.start_capture(req, Gate::allowed()),
            Err(AudioError::InvalidRequest(_))
        ));
    }
    assert_eq!(backend.opened.load(Ordering::Acquire), 0);
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn live_subscriber_lag_is_visible_without_corrupting_recording() {
    let backend = RawBackend::new();
    let limits = CaptureLimits {
        live_capacity: 2,
        ..CaptureLimits::default()
    };
    let service =
        AudioService::with_clock(Arc::new(Backend(backend.clone())), limits, Arc::new(Clock))
            .unwrap();
    let mut live = service.subscribe();
    let h = service.start_capture(request(8), Gate::allowed()).unwrap();
    let sink = backend.ready().await;
    for i in 0..8u8 {
        sink.push_pcm16(&[i, 0], now());
        tokio::time::sleep(Duration::from_millis(3)).await;
    }
    let out = h.result().await.unwrap();
    assert_eq!(
        &out.wav_bytes[44..],
        &[0, 0, 1, 0, 2, 0, 3, 0, 4, 0, 5, 0, 6, 0, 7, 0]
    );
    assert!(matches!(live.recv().await.unwrap(), LiveItem::Gap { .. }));
    let a = live.recv().await.unwrap();
    let b = live.recv().await.unwrap();
    match (a, b) {
        (LiveItem::Event(a), LiveItem::Event(b)) => assert!(b.sequence > a.sequence),
        _ => panic!("missing sequenced tail"),
    }
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn source_ring_overflow_reports_gap_and_rejects_incomplete_wav() {
    let backend = RawBackend::new();
    *backend.start_gate.0.lock().unwrap() = false;
    let limits = CaptureLimits {
        ring_blocks: 2,
        live_capacity: 16,
        ..CaptureLimits::default()
    };
    let service =
        AudioService::with_clock(Arc::new(Backend(backend.clone())), limits, Arc::new(Clock))
            .unwrap();
    let mut live = service.subscribe();
    let h = service
        .start_capture(request(100), Gate::allowed())
        .unwrap();
    let sink = backend.ready().await;
    assert_eq!(sink.push_pcm16(&[1, 2], now()), PushOutcome::Accepted);
    assert_eq!(sink.push_pcm16(&[3, 4], now()), PushOutcome::Accepted);
    assert_eq!(sink.push_pcm16(&[5, 6], now()), PushOutcome::Dropped);
    *backend.start_gate.0.lock().unwrap() = true;
    backend.start_gate.1.notify_all();
    assert!(matches!(h.result().await, Err(AudioError::Overflow)));
    let mut saw_gap = false;
    while let Ok(Ok(item)) = tokio::time::timeout(Duration::from_millis(10), live.recv()).await {
        if let LiveItem::Event(event) = item
            && matches!(
                event.kind,
                LiveEventKind::SourceGap {
                    dropped_frames: 1,
                    ..
                }
            )
        {
            saw_gap = true;
            break;
        }
    }
    assert!(
        saw_gap,
        "capture loss must be visible independently of final error"
    );
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn final_gate_after_device_release_rejects_retuned_complete_segment() {
    let backend = RawBackend::new();
    *backend.stop_gate.0.lock().unwrap() = false;
    let service = service(&backend);
    let gate = Gate::allowed();
    let h = service.start_capture(request(2), gate.clone()).unwrap();
    backend.ready().await.push_pcm16(&[1, 2, 3, 4], now());
    tokio::time::timeout(Duration::from_secs(2), async {
        while !backend.stop_entered.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    gate.0.store(false, Ordering::Release);
    backend.release_stop();
    assert!(matches!(h.result().await, Err(AudioError::Revoked)));
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn absent_selected_input_and_capture_timeout_are_explicit() {
    let backend = RawBackend::new();
    backend.inputs.lock().unwrap().clear();
    let service = service(&backend);
    let h = service.start_capture(request(8), Gate::allowed()).unwrap();
    assert!(matches!(
        h.result().await,
        Err(AudioError::InputUnavailable)
    ));
    service.begin_close().unwrap().completed().await.unwrap();
    let backend = RawBackend::new();
    let limits = CaptureLimits {
        stall_timeout: Duration::from_millis(25),
        ..CaptureLimits::default()
    };
    let service =
        AudioService::with_clock(Arc::new(Backend(backend.clone())), limits, Arc::new(Clock))
            .unwrap();
    let h = service.start_capture(request(8), Gate::allowed()).unwrap();
    backend.ready().await;
    assert!(matches!(h.result().await, Err(AudioError::Timeout)));
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn partial_prehistory_is_trimmed_at_frame_boundary_and_48k_duration_rounds_even() {
    let backend = RawBackend::new();
    let service = service(&backend);
    let h = service.start_capture(request(2), Gate::allowed()).unwrap();
    let sink = backend.ready().await;
    sink.push_pcm16(
        &[1, 0, 2, 0, 3, 0, 4, 0, 5, 0],
        utc("2026-11-11T11:59:59.999875+00:00"),
    );
    let out = h.result().await.unwrap();
    assert_eq!(&out.wav_bytes[44..], &[4, 0, 5, 0]);
    assert_eq!(out.started_at, now());
    for (frames, end) in [
        (3, "2026-11-11T12:00:00.000062Z"),
        (9, "2026-11-11T12:00:00.000188Z"),
    ] {
        *backend.sink.lock().unwrap() = None;
        let mut req = request(frames);
        req.sample_rate_hz = 48000;
        let h = service.start_capture(req, Gate::allowed()).unwrap();
        backend
            .ready()
            .await
            .push_pcm16(&vec![0; frames as usize * 2], now());
        assert_eq!(h.result().await.unwrap().ended_at.as_str(), end);
    }
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn a_cancelled_record_future_stops_capture_and_preserves_owner_until_release() {
    let backend = RawBackend::new();
    *backend.stop_gate.0.lock().unwrap() = false;
    let service = service(&backend);
    let recording = tokio::spawn({
        let service = service.clone();
        async move { service.record(request(8), Gate::allowed()).await }
    });
    let sink = backend.ready().await;
    recording.abort();
    let _ = recording.await;
    tokio::time::timeout(Duration::from_secs(2), async {
        while !backend.stop_entered.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(sink.push_pcm16(&[1, 2], now()), PushOutcome::Rejected);
    assert!(matches!(
        service.start_capture(request(8), Gate::allowed()),
        Err(AudioError::Busy)
    ));
    backend.release_stop();
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn default_native_options_cannot_enumerate_or_open_devices() {
    let service = AudioService::native(NativeOptions::default()).unwrap();
    assert!(matches!(service.inputs().await, Err(AudioError::Disabled)));
    let h = service.start_capture(request(8), Gate::allowed()).unwrap();
    assert!(matches!(h.result().await, Err(AudioError::Disabled)));
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn source_error_during_device_drain_invalidates_complete_wav() {
    let backend = RawBackend::new();
    *backend.stop_gate.0.lock().unwrap() = false;
    let service = service(&backend);
    let h = service.start_capture(request(2), Gate::allowed()).unwrap();
    let sink = backend.ready().await;
    sink.push_pcm16(&[1, 2, 3, 4], now());
    tokio::time::timeout(Duration::from_secs(2), async {
        while !backend.stop_entered.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    sink.fail(AudioError::Backend("input xrun during release".into()));
    backend.release_stop();
    assert!(
        h.result().await.is_err(),
        "a source error before finalization must not become evidence"
    );
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn byte_limit_and_callback_block_limit_are_enforced_independently() {
    let backend = RawBackend::new();
    let limits = CaptureLimits {
        max_pcm_bytes: 8,
        max_block_frames: 2,
        ..CaptureLimits::default()
    };
    let service =
        AudioService::with_clock(Arc::new(Backend(backend.clone())), limits, Arc::new(Clock))
            .unwrap();
    assert!(matches!(
        service.start_capture(request(5), Gate::allowed()),
        Err(AudioError::InvalidRequest(_))
    ));
    let h = service.start_capture(request(4), Gate::allowed()).unwrap();
    let sink = backend.ready().await;
    assert_eq!(sink.push_pcm16(&[0; 6], now()), PushOutcome::Rejected);
    assert!(matches!(h.result().await, Err(AudioError::BlockTooLarge)));
    service.begin_close().unwrap().completed().await.unwrap();
}

#[tokio::test]
async fn cancelled_close_waiter_cannot_detach_blocked_native_drop() {
    let backend = RawBackend::new();
    *backend.drop_gate.0.lock().unwrap() = false;
    let service = service(&backend);
    let h = service.start_capture(request(2), Gate::allowed()).unwrap();
    backend.ready().await.push_pcm16(&[1, 2, 3, 4], now());
    tokio::time::timeout(Duration::from_secs(2), async {
        while !backend.drop_entered.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let closing = service.begin_close().unwrap();
    let waiting = tokio::spawn({
        let closing = closing.clone();
        async move { closing.completed().await }
    });
    tokio::task::yield_now().await;
    waiting.abort();
    let _ = waiting.await;
    assert_eq!(backend.released.load(Ordering::Acquire), 0);
    assert!(
        tokio::time::timeout(Duration::from_millis(20), closing.completed())
            .await
            .is_err()
    );
    *backend.drop_gate.0.lock().unwrap() = true;
    backend.drop_gate.1.notify_all();
    closing.completed().await.unwrap();
    assert_eq!(backend.released.load(Ordering::Acquire), 1);
    assert!(matches!(h.result().await, Err(AudioError::Closed)));
}

#[cfg(not(feature = "native"))]
#[test]
fn explicitly_enabled_native_backend_is_unavailable_when_not_compiled() {
    assert!(matches!(
        AudioService::native(NativeOptions {
            enabled: true,
            ..NativeOptions::default()
        }),
        Err(AudioError::NativeUnavailable)
    ));
}

#[tokio::test]
async fn initialization_callbacks_cannot_seed_a_post_open_capture() {
    let backend = RawBackend::new();
    *backend.open_gate.0.lock().unwrap() = false;
    let service = service(&backend);
    let h = service.start_capture(request(2), Gate::allowed()).unwrap();
    let sink = tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Some(sink) = backend.sink.lock().unwrap().clone() {
                break sink;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let premature = sink.push_pcm16(&[1, 2, 3, 4], now());
    *backend.open_gate.0.lock().unwrap() = true;
    backend.open_gate.1.notify_all();
    let sink = backend.ready().await;
    sink.push_pcm16(&[5, 6, 7, 8], now());
    let out = h.result().await.unwrap();
    service.begin_close().unwrap().completed().await.unwrap();
    assert_eq!(premature, PushOutcome::Discarded);
    assert_eq!(&out.wav_bytes[44..], &[5, 6, 7, 8]);
}

#[tokio::test]
async fn actual_open_utc_is_resampled_after_backend_initialization() {
    struct AdvancingClock(AtomicUsize);
    impl AudioClock for AdvancingClock {
        fn now(&self) -> AudioResult<UtcTimestamp> {
            Ok(if self.0.fetch_add(1, Ordering::AcqRel) == 0 {
                now()
            } else {
                utc("2026-11-11T12:00:00.001000Z")
            })
        }
    }
    let backend = RawBackend::new();
    let service = AudioService::with_clock(
        Arc::new(Backend(backend.clone())),
        CaptureLimits::default(),
        Arc::new(AdvancingClock(AtomicUsize::new(0))),
    )
    .unwrap();
    let h = service.start_capture(request(2), Gate::allowed()).unwrap();
    let sink = backend.ready().await;
    let previous = sink.push_pcm16(&[1, 2, 3, 4], utc("2026-11-11T12:00:00.000500Z"));
    sink.push_pcm16(&[5, 6, 7, 8], utc("2026-11-11T12:00:00.001000Z"));
    let out = h.result().await.unwrap();
    service.begin_close().unwrap().completed().await.unwrap();
    assert_eq!(previous, PushOutcome::Discarded);
    assert_eq!(out.started_at.as_str(), "2026-11-11T12:00:00.001000Z");
    assert_eq!(&out.wav_bytes[44..], &[5, 6, 7, 8]);
}

#[tokio::test]
async fn legacy_8k_and_12k_rates_remain_original_when_explicitly_supported() {
    for rate in [8000, 12000] {
        let backend = RawBackend::new();
        backend.inputs.lock().unwrap()[0].formats[0].min_sample_rate_hz = 8000;
        let service = service(&backend);
        let mut req = request(2);
        req.sample_rate_hz = rate;
        let h = service.start_capture(req, Gate::allowed()).unwrap();
        backend.ready().await.push_pcm16(&[0, 128, 255, 127], now());
        let out = h.result().await.unwrap();
        service.begin_close().unwrap().completed().await.unwrap();
        assert_eq!(out.sample_rate_hz, rate);
        assert_eq!(&out.wav_bytes[24..28], &rate.to_le_bytes());
        assert_eq!(&out.wav_bytes[44..], &[0, 128, 255, 127]);
    }
}
