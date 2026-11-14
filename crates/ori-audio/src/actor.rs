use crate::*;
use chrono::{DateTime, SecondsFormat, Utc};
use ori_contracts::UtcTimestamp;
use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};
use tokio::sync::{broadcast, oneshot, watch};

/// Invoked only on the owned audio thread. `open` must recheck exact selection,
/// native format and sink authorization immediately before opening the device.
/// A backend injects original PCM only; segmentation and WAV belong to the actor.
pub trait AudioBackend: Send + Sync + 'static {
    fn inputs(&self) -> AudioResult<Vec<AudioInput>>;
    fn open(
        &self,
        input: &InputSelection,
        sample_rate_hz: u32,
        sink: PcmSink,
    ) -> AudioResult<Box<dyn AudioStream>>;
}
/// Intentionally no Send bound: the stream is built, controlled and dropped on
/// one OS thread even on platforms whose native stream cannot cross threads.
/// Drop must wait for actual device release and the end of all callbacks. A
/// backend must not detach native work or retain the input after Drop returns.
pub trait AudioStream: 'static {
    fn start(&mut self) -> AudioResult<()>;
    fn stop(&mut self) -> AudioResult<()>;
}

struct Completion<T>(watch::Sender<Option<Arc<AudioResult<T>>>>);
impl<T> Completion<T> {
    fn new() -> Self {
        Self(watch::channel(None).0)
    }
    fn finish(&self, result: AudioResult<T>) {
        self.0.send_if_modified(|slot| {
            if slot.is_some() {
                false
            } else {
                *slot = Some(Arc::new(result));
                true
            }
        });
    }
    async fn wait(&self) -> Arc<AudioResult<T>> {
        let mut rx = self.0.subscribe();
        loop {
            let value = rx.borrow().clone();
            if let Some(value) = value {
                return value;
            }
            // Sender is retained by self for the whole wait.
            let _ = rx.changed().await;
        }
    }
}
struct Attempt {
    id: u64,
    cancelled: AtomicBool,
    result: Completion<RecordedAudio>,
}
struct Shared {
    closing: AtomicBool,
    busy: AtomicBool,
    next_capture: AtomicU64,
    next_event: AtomicU64,
    active: Mutex<Option<Arc<Attempt>>>,
    cleanup_error: Mutex<Option<AudioError>>,
    closed: Completion<()>,
    live: broadcast::Sender<Arc<LiveEvent>>,
}
impl Shared {
    fn emit(&self, id: u64, kind: LiveEventKind) {
        let sequence = self.next_event.fetch_add(1, Ordering::AcqRel);
        let _ = self.live.send(Arc::new(LiveEvent {
            sequence,
            capture_id: id,
            kind,
        }));
    }
    fn failed_cleanup(&self, error: AudioError) {
        let mut failure = self.cleanup_error.lock().unwrap_or_else(|p| p.into_inner());
        if failure.is_none() {
            *failure = Some(error);
        }
        self.closing.store(true, Ordering::Release);
    }
    fn finish(&self, attempt: &Arc<Attempt>, result: AudioResult<RecordedAudio>) {
        let frames = result.as_ref().map_or(0, |v| v.frames);
        self.emit(
            attempt.id,
            LiveEventKind::Finished {
                frames,
                error: result.as_ref().err().cloned(),
            },
        );
        let mut active = self.active.lock().unwrap_or_else(|p| p.into_inner());
        if active
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, attempt))
        {
            *active = None;
            self.busy.store(false, Ordering::Release);
        }
        attempt.result.finish(result);
    }
}
enum Command {
    Inputs(oneshot::Sender<AudioResult<Vec<AudioInput>>>),
    Capture(CaptureRequest, Arc<dyn CaptureAuthorization>, Arc<Attempt>),
}
struct SystemClock;
impl AudioClock for SystemClock {
    fn now(&self) -> AudioResult<UtcTimestamp> {
        Utc::now()
            .to_rfc3339_opts(SecondsFormat::Micros, false)
            .parse()
            .map_err(|_| AudioError::Clock)
    }
}

pub struct AudioService {
    commands: mpsc::SyncSender<Command>,
    shared: Arc<Shared>,
    limits: CaptureLimits,
}
impl AudioService {
    pub fn new(backend: Arc<dyn AudioBackend>, limits: CaptureLimits) -> AudioResult<Arc<Self>> {
        Self::with_clock(backend, limits, Arc::new(SystemClock))
    }
    pub fn with_clock(
        backend: Arc<dyn AudioBackend>,
        limits: CaptureLimits,
        clock: Arc<dyn AudioClock>,
    ) -> AudioResult<Arc<Self>> {
        limits.validate()?;
        let (live, _) = broadcast::channel(limits.live_capacity);
        let shared = Arc::new(Shared {
            closing: AtomicBool::new(false),
            busy: AtomicBool::new(false),
            next_capture: AtomicU64::new(1),
            next_event: AtomicU64::new(1),
            active: Mutex::new(None),
            cleanup_error: Mutex::new(None),
            closed: Completion::new(),
            live,
        });
        let (commands, rx) = mpsc::sync_channel(4);
        let owner_shared = shared.clone();
        let owner_limits = limits.clone();
        let owner = thread::Builder::new()
            .name("ori-audio-owner".into())
            .spawn(move || owner_loop(rx, backend, clock, owner_limits, owner_shared))
            .map_err(|e| AudioError::ThreadInitialization(e.to_string()))?;
        let owned = Arc::new(Mutex::new(Some(owner)));
        let reaper_owned = owned.clone();
        let reaper_shared = shared.clone();
        if let Err(error) = thread::Builder::new()
            .name("ori-audio-join".into())
            .spawn(move || {
                let thread = reaper_owned
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .take()
                    .expect("owned audio thread");
                let result = match thread.join() {
                    Ok(result) => result,
                    Err(_) => Err(AudioError::ThreadPanicked),
                };
                reaper_shared.closing.store(true, Ordering::Release);
                let active = reaper_shared
                    .active
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .take();
                if let Some(active) = active {
                    active
                        .result
                        .finish(Err(result.clone().err().unwrap_or(AudioError::Closed)));
                }
                reaper_shared.closed.finish(result);
            })
        {
            // No service has escaped, hence no backend command can be running.
            // Retain and join the owner even if creation of its joiner failed.
            shared.closing.store(true, Ordering::Release);
            if let Some(owner) = owned.lock().unwrap_or_else(|p| p.into_inner()).take() {
                let _ = owner.join();
            }
            return Err(AudioError::ThreadInitialization(error.to_string()));
        }
        Ok(Arc::new(Self {
            commands,
            shared,
            limits,
        }))
    }
    pub fn native(options: NativeOptions) -> AudioResult<Arc<Self>> {
        Self::new(crate::native::backend(options.enabled)?, options.limits)
    }
    pub async fn inputs(&self) -> AudioResult<Vec<AudioInput>> {
        if self.shared.closing.load(Ordering::Acquire) {
            return Err(AudioError::Closed);
        }
        if self.shared.busy.load(Ordering::Acquire) {
            return Err(AudioError::Busy);
        }
        let (tx, rx) = oneshot::channel();
        self.commands
            .try_send(Command::Inputs(tx))
            .map_err(command_error)?;
        rx.await.map_err(|_| AudioError::Closed)?
    }
    pub fn start_capture(
        &self,
        request: CaptureRequest,
        gate: Arc<dyn CaptureAuthorization>,
    ) -> AudioResult<CaptureHandle> {
        self.limits.validate_request(&request)?;
        let mut active = self
            .shared
            .active
            .lock()
            .map_err(|_| AudioError::ThreadPanicked)?;
        if self.shared.closing.load(Ordering::Acquire) {
            return Err(AudioError::Closed);
        }
        if self.shared.busy.swap(true, Ordering::AcqRel) {
            return Err(AudioError::Busy);
        }
        let attempt = Arc::new(Attempt {
            id: self.shared.next_capture.fetch_add(1, Ordering::AcqRel),
            cancelled: AtomicBool::new(false),
            result: Completion::new(),
        });
        *active = Some(attempt.clone());
        if let Err(error) = self
            .commands
            .try_send(Command::Capture(request, gate, attempt.clone()))
        {
            *active = None;
            self.shared.busy.store(false, Ordering::Release);
            return Err(command_error(error));
        }
        Ok(CaptureHandle { attempt })
    }
    pub async fn record(
        &self,
        request: CaptureRequest,
        gate: Arc<dyn CaptureAuthorization>,
    ) -> AudioResult<RecordedAudio> {
        let handle = self.start_capture(request, gate)?;
        handle.result().await
    }
    pub fn subscribe(&self) -> LiveSubscription {
        LiveSubscription {
            rx: self.shared.live.subscribe(),
        }
    }
    /// Revocation is synchronous; the existing owned join task is never replaced
    /// when a caller drops its future. Failure is latched on this same handle.
    pub fn begin_close(&self) -> AudioResult<AudioClose> {
        self.shared.closing.store(true, Ordering::Release);
        let active = self
            .shared
            .active
            .lock()
            .map_err(|_| AudioError::ThreadPanicked)?;
        if let Some(active) = active.as_ref() {
            active.cancelled.store(true, Ordering::Release);
        }
        Ok(AudioClose {
            shared: self.shared.clone(),
        })
    }
}
impl Drop for AudioService {
    fn drop(&mut self) {
        self.shared.closing.store(true, Ordering::Release);
        if let Ok(active) = self.shared.active.lock()
            && let Some(active) = active.as_ref()
        {
            active.cancelled.store(true, Ordering::Release);
        }
    }
}
fn command_error<T>(error: mpsc::TrySendError<T>) -> AudioError {
    match error {
        mpsc::TrySendError::Full(_) => AudioError::Busy,
        mpsc::TrySendError::Disconnected(_) => AudioError::Closed,
    }
}
pub struct CaptureHandle {
    attempt: Arc<Attempt>,
}
impl CaptureHandle {
    pub fn capture_id(&self) -> u64 {
        self.attempt.id
    }
    pub fn cancel(&self) {
        self.attempt.cancelled.store(true, Ordering::Release);
    }
    pub async fn result(&self) -> AudioResult<RecordedAudio> {
        self.attempt.result.wait().await.as_ref().clone()
    }
}
impl Drop for CaptureHandle {
    fn drop(&mut self) {
        self.cancel();
    }
}
#[derive(Clone)]
pub struct AudioClose {
    shared: Arc<Shared>,
}
impl AudioClose {
    /// Resolves only after the device-owning thread has actually been joined.
    pub async fn completed(&self) -> AudioResult<()> {
        self.shared.closed.wait().await.as_ref().clone()
    }
}
pub struct LiveSubscription {
    rx: broadcast::Receiver<Arc<LiveEvent>>,
}
impl LiveSubscription {
    pub async fn recv(&mut self) -> AudioResult<LiveItem> {
        match self.rx.recv().await {
            Ok(event) => Ok(LiveItem::Event(event)),
            Err(broadcast::error::RecvError::Lagged(missed_events)) => {
                Ok(LiveItem::Gap { missed_events })
            }
            Err(broadcast::error::RecvError::Closed) => Err(AudioError::Closed),
        }
    }
}

fn owner_loop(
    rx: mpsc::Receiver<Command>,
    backend: Arc<dyn AudioBackend>,
    clock: Arc<dyn AudioClock>,
    limits: CaptureLimits,
    shared: Arc<Shared>,
) -> AudioResult<()> {
    while !shared.closing.load(Ordering::Acquire) {
        let command = match rx.recv_timeout(Duration::from_millis(5)) {
            Ok(command) => command,
            Err(mpsc::RecvTimeoutError::Timeout) => continue,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        };
        match command {
            Command::Inputs(tx) => {
                let result = catch_unwind(AssertUnwindSafe(|| checked_inputs(&*backend)))
                    .unwrap_or(Err(AudioError::ThreadPanicked));
                if matches!(result, Err(AudioError::ThreadPanicked)) {
                    shared.failed_cleanup(AudioError::ThreadPanicked);
                }
                let _ = tx.send(result);
            }
            Command::Capture(request, gate, attempt) => {
                shared.emit(attempt.id, LiveEventKind::Started);
                let result = catch_unwind(AssertUnwindSafe(|| {
                    run_capture(
                        &*backend, &*clock, &limits, &shared, &attempt, &request, gate,
                    )
                }))
                .unwrap_or(Err(AudioError::ThreadPanicked));
                if matches!(result, Err(AudioError::ThreadPanicked)) {
                    shared.failed_cleanup(AudioError::ThreadPanicked);
                }
                shared.finish(&attempt, result);
            }
        }
    }
    // Pending commands cannot retain an unresolved capture after close.
    while let Ok(command) = rx.try_recv() {
        match command {
            Command::Inputs(tx) => {
                let _ = tx.send(Err(AudioError::Closed));
            }
            Command::Capture(_, _, attempt) => shared.finish(&attempt, Err(AudioError::Closed)),
        }
    }
    shared
        .cleanup_error
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .clone()
        .map_or(Ok(()), Err)
}
fn checked_inputs(backend: &dyn AudioBackend) -> AudioResult<Vec<AudioInput>> {
    let inputs = backend.inputs()?;
    if inputs.len() > 1024
        || inputs
            .iter()
            .any(|i| i.name.len() > 4096 || i.formats.len() > 256)
    {
        return Err(AudioError::Backend(
            "input enumeration exceeds bounded descriptor limits".into(),
        ));
    }
    Ok(inputs)
}

struct RawBlock {
    sequence: u64,
    started_at: UtcTimestamp,
    bytes: Vec<u8>,
}
struct SinkState {
    tx: mpsc::SyncSender<RawBlock>,
    fault: mpsc::SyncSender<AudioError>,
    active: AtomicBool,
    producer: AtomicBool,
    armed: AtomicBool,
    remaining: AtomicU64,
    sequence: AtomicU64,
    dropped_blocks: AtomicU64,
    dropped_frames: AtomicU64,
    gate: Arc<dyn CaptureAuthorization>,
    shared: Arc<Shared>,
    attempt: Arc<Attempt>,
    max_block_frames: usize,
    rate: u32,
    not_before: AtomicI64,
}
/// Nonblocking bounded ingress shared by CPAL and raw-PCM synthetic backends.
/// Exactly one producer callback may run at a time. Callbacks never touch the
/// archive/network, wait for queue capacity, or perform channel/sample conversion.
#[derive(Clone)]
pub struct PcmSink {
    inner: Arc<SinkState>,
}
impl PcmSink {
    pub fn max_block_frames(&self) -> usize {
        self.inner.max_block_frames
    }
    pub fn is_accepting(&self) -> bool {
        self.inner.armed.load(Ordering::Acquire)
            && self.inner.active.load(Ordering::Acquire)
            && !self.inner.shared.closing.load(Ordering::Acquire)
            && !self.inner.attempt.cancelled.load(Ordering::Acquire)
    }
    pub fn revalidate(&self) -> AudioResult<()> {
        if self.inner.shared.closing.load(Ordering::Acquire) {
            return Err(AudioError::Closed);
        }
        if self.inner.attempt.cancelled.load(Ordering::Acquire) {
            return Err(AudioError::Cancelled);
        }
        catch_unwind(AssertUnwindSafe(|| self.inner.gate.revalidate()))
            .unwrap_or(Err(AudioError::CallbackPanicked))
    }
    pub fn fail(&self, error: AudioError) {
        self.inner.active.store(false, Ordering::Release);
        let error = match error {
            AudioError::Backend(text) => AudioError::Backend(text.chars().take(256).collect()),
            other => other,
        };
        let _ = self.inner.fault.try_send(error);
    }
    pub fn push_pcm16(&self, bytes: &[u8], captured_at: UtcTimestamp) -> PushOutcome {
        match catch_unwind(AssertUnwindSafe(|| self.push(bytes, captured_at))) {
            Ok(outcome) => outcome,
            Err(_) => {
                self.fail(AudioError::CallbackPanicked);
                PushOutcome::Rejected
            }
        }
    }
    fn push(&self, bytes: &[u8], captured_at: UtcTimestamp) -> PushOutcome {
        if !self.inner.active.load(Ordering::Acquire)
            || self.inner.shared.closing.load(Ordering::Acquire)
            || self.inner.attempt.cancelled.load(Ordering::Acquire)
        {
            return PushOutcome::Rejected;
        }
        if !self.inner.armed.load(Ordering::Acquire) {
            return PushOutcome::Discarded;
        }
        if let Err(error) = self.revalidate() {
            self.fail(error);
            return PushOutcome::Rejected;
        }
        if self.inner.producer.swap(true, Ordering::AcqRel) {
            self.fail(AudioError::Backend("concurrent input callbacks".into()));
            return PushOutcome::Rejected;
        }
        struct Reset<'a>(&'a AtomicBool);
        impl Drop for Reset<'_> {
            fn drop(&mut self) {
                self.0.store(false, Ordering::Release);
            }
        }
        let _reset = Reset(&self.inner.producer);
        if !bytes.len().is_multiple_of(2) || bytes.is_empty() {
            self.fail(AudioError::MalformedPcm);
            return PushOutcome::Rejected;
        }
        let frames = bytes.len() / 2;
        if frames > self.inner.max_block_frames {
            self.fail(AudioError::BlockTooLarge);
            return PushOutcome::Rejected;
        }
        let delta = i128::from(self.inner.not_before.load(Ordering::Acquire))
            - i128::from(captured_at.unix_micros());
        let trim = if delta > 0 {
            (delta * i128::from(self.inner.rate) + 999_999) / 1_000_000
        } else {
            0
        };
        if trim >= frames as i128 {
            return PushOutcome::Discarded;
        }
        let trim = trim as usize;
        let remaining = self.inner.remaining.load(Ordering::Acquire);
        if remaining == 0 {
            return PushOutcome::Rejected;
        }
        let accepted = (frames - trim).min(remaining as usize);
        let started_at = match add_frames(&captured_at, trim as u64, self.inner.rate) {
            Ok(v) => v,
            Err(e) => {
                self.fail(e);
                return PushOutcome::Rejected;
            }
        };
        let block = RawBlock {
            sequence: self.inner.sequence.fetch_add(1, Ordering::AcqRel),
            started_at,
            bytes: bytes[trim * 2..(trim + accepted) * 2].to_vec(),
        };
        match self.inner.tx.try_send(block) {
            Ok(()) => {
                self.inner
                    .remaining
                    .fetch_sub(accepted as u64, Ordering::AcqRel);
                if remaining == accepted as u64 {
                    self.inner.active.store(false, Ordering::Release);
                }
                PushOutcome::Accepted
            }
            Err(mpsc::TrySendError::Full(_)) => {
                self.inner.dropped_blocks.fetch_add(1, Ordering::AcqRel);
                self.inner
                    .dropped_frames
                    .fetch_add(accepted as u64, Ordering::AcqRel);
                self.fail(AudioError::Overflow);
                PushOutcome::Dropped
            }
            Err(mpsc::TrySendError::Disconnected(_)) => {
                self.inner.active.store(false, Ordering::Release);
                PushOutcome::Rejected
            }
        }
    }
}
fn capture_gate(
    shared: &Shared,
    attempt: &Attempt,
    gate: &dyn CaptureAuthorization,
) -> AudioResult<()> {
    if shared.closing.load(Ordering::Acquire) {
        return Err(AudioError::Closed);
    }
    if attempt.cancelled.load(Ordering::Acquire) {
        return Err(AudioError::Cancelled);
    }
    gate.revalidate()
}
fn run_capture(
    backend: &dyn AudioBackend,
    clock: &dyn AudioClock,
    limits: &CaptureLimits,
    shared: &Arc<Shared>,
    attempt: &Arc<Attempt>,
    request: &CaptureRequest,
    gate: Arc<dyn CaptureAuthorization>,
) -> AudioResult<RecordedAudio> {
    capture_gate(shared, attempt, &*gate)?;
    let inputs = checked_inputs(backend)?;
    let input = inputs
        .iter()
        .find(|i| {
            i.index == request.input.index && i.name.as_bytes() == request.input.name.as_bytes()
        })
        .ok_or(AudioError::InputUnavailable)?;
    if !input.formats.iter().any(|f| {
        f.channels == 1
            && f.encoding == SampleEncoding::I16
            && f.min_sample_rate_hz <= request.sample_rate_hz
            && request.sample_rate_hz <= f.max_sample_rate_hz
    }) {
        return Err(AudioError::UnsupportedFormat);
    }
    capture_gate(shared, attempt, &*gate)?;
    let opened_at = clock.now()?;
    let not_before = opened_at
        .unix_micros()
        .max(request.not_before.unix_micros());
    let (tx, rx) = mpsc::sync_channel(limits.ring_blocks);
    let (fault, faults) = mpsc::sync_channel(1);
    let sink = PcmSink {
        inner: Arc::new(SinkState {
            tx,
            fault,
            active: AtomicBool::new(true),
            producer: AtomicBool::new(false),
            armed: AtomicBool::new(false),
            remaining: AtomicU64::new(request.frames),
            sequence: AtomicU64::new(1),
            dropped_blocks: AtomicU64::new(0),
            dropped_frames: AtomicU64::new(0),
            gate: gate.clone(),
            shared: shared.clone(),
            attempt: attempt.clone(),
            max_block_frames: limits.max_block_frames,
            rate: request.sample_rate_hz,
            not_before: AtomicI64::new(not_before),
        }),
    };
    struct Deactivate(PcmSink);
    impl Drop for Deactivate {
        fn drop(&mut self) {
            self.0.inner.active.store(false, Ordering::Release);
        }
    }
    let _deactivate = Deactivate(sink.clone());
    let mut stream = backend.open(&request.input, request.sample_rate_hz, sink.clone())?;
    let result = (|| {
        // Backend initialization can take time or deliver an old host buffer.
        // Keep ingress unarmed until the actual open has returned, then refresh
        // its UTC lower bound before play/acceptance and recheck the same gate.
        let actual_open = clock.now()?;
        sink.inner
            .not_before
            .fetch_max(actual_open.unix_micros(), Ordering::AcqRel);
        capture_gate(shared, attempt, &*gate)?;
        sink.inner.armed.store(true, Ordering::Release);
        stream.start()?;
        let target = request.frames as usize * 2;
        let mut pcm = Vec::with_capacity(target);
        let mut first = None;
        let rate = u64::from(request.sample_rate_hz);
        let duration = Duration::new(
            request.frames / rate,
            ((request.frames % rate) * 1_000_000_000 / rate) as u32,
        );
        let deadline = Instant::now() + duration + limits.stall_timeout;
        loop {
            capture_gate(shared, attempt, &*gate)?;
            if let Ok(error) = faults.try_recv() {
                let dropped_frames = sink.inner.dropped_frames.load(Ordering::Acquire);
                if dropped_frames > 0 {
                    shared.emit(
                        attempt.id,
                        LiveEventKind::SourceGap {
                            dropped_frames,
                            dropped_blocks: sink.inner.dropped_blocks.load(Ordering::Acquire),
                        },
                    );
                }
                return Err(error);
            }
            if pcm.len() == target {
                break;
            }
            if Instant::now() >= deadline {
                return Err(AudioError::Timeout);
            }
            match rx.recv_timeout(Duration::from_millis(5)) {
                Ok(block) => {
                    capture_gate(shared, attempt, &*gate)?;
                    let first_frame = pcm.len() as u64 / 2;
                    if first.is_none() {
                        first = Some(block.started_at.clone());
                    }
                    pcm.extend_from_slice(&block.bytes);
                    shared.emit(
                        attempt.id,
                        LiveEventKind::Pcm {
                            source_sequence: block.sequence,
                            first_frame,
                            started_at: block.started_at,
                            bytes: Arc::from(block.bytes),
                        },
                    );
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err(AudioError::Backend("audio callback disconnected".into()));
                }
            }
        }
        let started_at = first.ok_or(AudioError::MalformedPcm)?;
        let ended_at = add_frames(&started_at, request.frames, request.sample_rate_hz)?;
        Ok(RecordedAudio {
            wav_bytes: pcm16_wav(&pcm, request.sample_rate_hz),
            started_at,
            ended_at,
            sample_rate_hz: request.sample_rate_hz,
            channels: 1,
            frames: request.frames,
        })
    })();
    sink.inner.active.store(false, Ordering::Release);
    let stopped = stream.stop();
    drop(stream);
    if let Err(error) = stopped {
        shared.failed_cleanup(error.clone());
        return Err(error);
    }
    // Native Drop has completed: no callback can now race finalization.
    // A source error while stop/drop was draining invalidates the segment too.
    if let Ok(error) = faults.try_recv() {
        return Err(error);
    }
    let result = result?;
    capture_gate(shared, attempt, &*gate)?;
    Ok(result)
}
pub(crate) fn add_frames(
    start: &UtcTimestamp,
    frames: u64,
    rate: u32,
) -> AudioResult<UtcTimestamp> {
    let numerator = u128::from(frames) * 1_000_000;
    let denominator = u128::from(rate);
    let q = numerator / denominator;
    let r = numerator % denominator;
    let rounded = q + u128::from(r * 2 > denominator || (r * 2 == denominator && q % 2 == 1));
    let micros = start
        .unix_micros()
        .checked_add(i64::try_from(rounded).map_err(|_| AudioError::Clock)?)
        .ok_or(AudioError::Clock)?;
    DateTime::from_timestamp_micros(micros)
        .ok_or(AudioError::Clock)?
        .to_rfc3339_opts(SecondsFormat::Micros, false)
        .parse()
        .map_err(|_| AudioError::Clock)
}
fn pcm16_wav(pcm: &[u8], rate: u32) -> Vec<u8> {
    let size = pcm.len() as u32;
    let mut out = Vec::with_capacity(44 + pcm.len());
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + size).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&rate.to_le_bytes());
    out.extend_from_slice(&(rate * 2).to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&size.to_le_bytes());
    out.extend_from_slice(pcm);
    out
}
