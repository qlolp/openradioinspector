use crate::*;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    sync::{mpsc, oneshot, watch},
};

/// Queue acknowledgement is separate from I/O completion. Dropping this reply
/// receiver does not abort a command already sent to hardware; call cancel.
pub struct Pending<T> {
    receiver: oneshot::Receiver<Result<T>>,
}
impl<T> Pending<T> {
    pub async fn result(self) -> Result<T> {
        self.receiver
            .await
            .map_err(|_| InstrumentError::ActorStopped)?
    }
}
type ExecutionGuard = Box<dyn FnOnce() -> Result<()> + Send + 'static>;
struct Envelope<C, R> {
    command: C,
    response: oneshot::Sender<Result<R>>,
    guard: Option<ExecutionGuard>,
}
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub enum TinySaRequest {
    Prepare,
    Poll,
    AcquireRaw(RawScanConfig),
    CaptureScreen,
    Control(TinySaControl),
    ReadSd { filename: String },
    ListSd,
    ReadTrace { trace: u8 },
    Marker(MarkerCommand),
    ReadRbw,
    State,
    Close,
}
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub enum TinySaReply {
    Ready,
    Spectrum(SpectrumSample),
    RawSpectrum(RawSpectrumSample),
    Screen(Vec<u8>),
    Bytes(Vec<u8>),
    Text(DeviceTextSample),
    Directory(String),
    RbwHz(f64),
    State(TinySaState),
    Closed,
}
#[derive(Clone)]
pub struct TinySaActor {
    sender: mpsc::Sender<Envelope<TinySaRequest, TinySaReply>>,
    cancel: CancellationHandle,
    snapshot: watch::Receiver<TinySaState>,
    mutating: Arc<AtomicBool>,
}
impl TinySaActor {
    pub fn spawn<T: AsyncRead + AsyncWrite + Unpin + Send + 'static>(
        mut adapter: TinySa<T>,
        capacity: usize,
    ) -> Result<Self> {
        validate_capacity(capacity)?;
        let (sender, mut receiver) =
            mpsc::channel::<Envelope<TinySaRequest, TinySaReply>>(capacity);
        let (snapshot_tx, snapshot) = watch::channel(adapter.state());
        let cancel = adapter.cancellation_handle();
        let mutating = Arc::new(AtomicBool::new(false));
        let active = mutating.clone();
        tokio::spawn(async move {
            while let Some(mut request) = receiver.recv().await {
                if request.guard.take().is_some_and(|guard| guard().is_err()) {
                    // Core policy details and tokens never enter transport errors.
                    let _ = request.response.send(Err(InstrumentError::PolicyDenied));
                    continue;
                }
                let closing = matches!(request.command, TinySaRequest::Close);
                active.store(
                    matches!(
                        request.command,
                        TinySaRequest::Prepare | TinySaRequest::Control(_) | TinySaRequest::Close
                    ),
                    Ordering::SeqCst,
                );
                let result = match request.command {
                    TinySaRequest::Prepare => adapter.prepare().await.map(|_| TinySaReply::Ready),
                    TinySaRequest::Poll => adapter.poll_spectrum().await.map(TinySaReply::Spectrum),
                    TinySaRequest::AcquireRaw(config) => adapter
                        .acquire_raw(config)
                        .await
                        .map(TinySaReply::RawSpectrum),
                    TinySaRequest::CaptureScreen => {
                        adapter.capture_screen().await.map(TinySaReply::Screen)
                    }
                    TinySaRequest::Control(control) => {
                        adapter.execute(control).await.map(TinySaReply::State)
                    }
                    TinySaRequest::ReadSd { filename } => {
                        adapter.read_sd(&filename).await.map(TinySaReply::Bytes)
                    }
                    TinySaRequest::ListSd => adapter.list_sd().await.map(TinySaReply::Directory),
                    TinySaRequest::ReadTrace { trace } => {
                        adapter.read_trace(trace).await.map(TinySaReply::Text)
                    }
                    TinySaRequest::Marker(command) => {
                        adapter.marker(command).await.map(TinySaReply::Text)
                    }
                    TinySaRequest::ReadRbw => adapter.read_rbw().await.map(TinySaReply::RbwHz),
                    TinySaRequest::State => Ok(TinySaReply::State(adapter.state())),
                    TinySaRequest::Close => adapter.close().await.map(|_| TinySaReply::Closed),
                };
                snapshot_tx.send_replace(adapter.state());
                active.store(false, Ordering::SeqCst);
                let _ = request.response.send(result);
                if closing {
                    break;
                }
            }
            let _ = adapter.close().await;
            snapshot_tx.send_replace(adapter.state());
        });
        Ok(Self {
            sender,
            cancel,
            snapshot,
            mutating,
        })
    }
    pub fn enqueue(&self, command: TinySaRequest) -> Result<Pending<TinySaReply>> {
        validate_tinysa_request(&command)?;
        enqueue(&self.sender, command, None)
    }
    /// Recheck caller-owned policy after queue wait, immediately before I/O.
    /// The synchronous guard must be quick and nonblocking. Rejection is a
    /// bounded PolicyDenied error, with no caller token/error text disclosed.
    pub fn enqueue_guarded<G>(
        &self,
        command: TinySaRequest,
        guard: G,
    ) -> Result<Pending<TinySaReply>>
    where
        G: FnOnce() -> Result<()> + Send + 'static,
    {
        validate_tinysa_request(&command)?;
        enqueue(&self.sender, command, Some(Box::new(guard)))
    }
    /// Immediate cached state. A mutating exchange publishes unknown mode until
    /// its complete acknowledgement; status never waits behind a long scan.
    pub fn state(&self) -> TinySaState {
        let mut state = self.snapshot.borrow().clone();
        if self.cancel.is_tainted() || self.mutating.load(Ordering::SeqCst) {
            state.device_mode = DeviceMode::Unknown;
            state.settings_source = "unknown".into();
        }
        state.restart_required |= self.cancel.is_tainted();
        state
    }
    pub fn cancel(&self) {
        self.cancel.cancel();
    }
}
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub enum ArDv1Request {
    Prepare,
    Poll,
    Tune {
        frequency_hz: u64,
        mode: ReceiverMode,
    },
    State,
    Close,
}
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub enum ArDv1Reply {
    Ready,
    Observation(ReceiverSample),
    State(ReceiverState),
    Closed,
}
#[derive(Clone)]
pub struct ArDv1Actor {
    sender: mpsc::Sender<Envelope<ArDv1Request, ArDv1Reply>>,
    cancel: CancellationHandle,
    snapshot: watch::Receiver<ReceiverState>,
    mutating: Arc<AtomicBool>,
}
impl ArDv1Actor {
    pub fn spawn<T: AsyncRead + AsyncWrite + Unpin + Send + 'static>(
        mut adapter: ArDv1<T>,
        capacity: usize,
    ) -> Result<Self> {
        validate_capacity(capacity)?;
        let (sender, mut receiver) = mpsc::channel::<Envelope<ArDv1Request, ArDv1Reply>>(capacity);
        let (snapshot_tx, snapshot) = watch::channel(adapter.state());
        let cancel = adapter.cancellation_handle();
        let mutating = Arc::new(AtomicBool::new(false));
        let active = mutating.clone();
        tokio::spawn(async move {
            while let Some(mut request) = receiver.recv().await {
                if request.guard.take().is_some_and(|guard| guard().is_err()) {
                    // Core policy details and tokens never enter transport errors.
                    let _ = request.response.send(Err(InstrumentError::PolicyDenied));
                    continue;
                }
                let closing = matches!(request.command, ArDv1Request::Close);
                active.store(
                    matches!(
                        request.command,
                        ArDv1Request::Prepare | ArDv1Request::Tune { .. } | ArDv1Request::Close
                    ),
                    Ordering::SeqCst,
                );
                let result = match request.command {
                    ArDv1Request::Prepare => adapter.prepare().await.map(|_| ArDv1Reply::Ready),
                    ArDv1Request::Poll => {
                        adapter.poll_receiver().await.map(ArDv1Reply::Observation)
                    }
                    ArDv1Request::Tune { frequency_hz, mode } => adapter
                        .tune(frequency_hz, mode)
                        .await
                        .map(|_| ArDv1Reply::State(adapter.state())),
                    ArDv1Request::State => Ok(ArDv1Reply::State(adapter.state())),
                    ArDv1Request::Close => adapter.close().await.map(|_| ArDv1Reply::Closed),
                };
                snapshot_tx.send_replace(adapter.state());
                active.store(false, Ordering::SeqCst);
                let _ = request.response.send(result);
                if closing {
                    break;
                }
            }
            let _ = adapter.close().await;
            snapshot_tx.send_replace(adapter.state());
        });
        Ok(Self {
            sender,
            cancel,
            snapshot,
            mutating,
        })
    }
    pub fn enqueue(&self, command: ArDv1Request) -> Result<Pending<ArDv1Reply>> {
        enqueue(&self.sender, command, None)
    }
    /// Recheck caller-owned lease/guard/revision/generation before device I/O.
    pub fn enqueue_guarded<G>(&self, command: ArDv1Request, guard: G) -> Result<Pending<ArDv1Reply>>
    where
        G: FnOnce() -> Result<()> + Send + 'static,
    {
        enqueue(&self.sender, command, Some(Box::new(guard)))
    }
    pub fn state(&self) -> ReceiverState {
        let mut state = self.snapshot.borrow().clone();
        if self.cancel.is_tainted() || self.mutating.load(Ordering::SeqCst) {
            state.frequency_hz = None;
            state.mode = None;
            state.frequency_source = "unknown".into();
        }
        state.restart_required |= self.cancel.is_tainted();
        state
    }
    pub fn cancel(&self) {
        self.cancel.cancel();
    }
}
fn validate_capacity(capacity: usize) -> Result<()> {
    if !(1..=1024).contains(&capacity) {
        return Err(InstrumentError::InvalidRequest(
            "device queue capacity must be 1..1024".into(),
        ));
    }
    Ok(())
}
fn enqueue<C, R>(
    sender: &mpsc::Sender<Envelope<C, R>>,
    command: C,
    guard: Option<ExecutionGuard>,
) -> Result<Pending<R>> {
    let (response, receiver) = oneshot::channel();
    sender
        .try_send(Envelope {
            command,
            response,
            guard,
        })
        .map_err(|error| match error {
            mpsc::error::TrySendError::Full(_) => InstrumentError::QueueFull,
            mpsc::error::TrySendError::Closed(_) => InstrumentError::ActorStopped,
        })?;
    Ok(Pending { receiver })
}

fn validate_tinysa_request(command: &TinySaRequest) -> Result<()> {
    match command {
        TinySaRequest::Control(control) => crate::tinysa::validate_control_request(control)?,
        TinySaRequest::ReadSd { filename } => crate::tinysa::validate_filename(filename)?,
        TinySaRequest::ReadTrace { trace } if !(1..=4).contains(trace) => {
            return Err(InstrumentError::InvalidRequest("trace must be 1..4".into()));
        }
        TinySaRequest::Marker(command) => {
            crate::tinysa::marker_command(command)?;
        }
        _ => {}
    }
    Ok(())
}
