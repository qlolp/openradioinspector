//! Original uploaded-PCM recorder and network hub. This path opens no capture
//! device and performs no measurement/archive writes.
mod budget;
mod disconnect;
pub(crate) use disconnect::DisconnectIo;
mod hub;
mod pcm;
mod recorder;
#[cfg(test)]
mod tests;

use crate::{
    Core, CoreError,
    error::{ApiError, ApiResult},
    security::AuthReceipt,
};
use axum::{
    body::{Body, Bytes},
    http::{Method, StatusCode, header},
    response::Response,
};
use serde_json::Value;
use std::{
    collections::{BTreeMap, btree_map::Entry},
    convert::Infallible,
    sync::{
        Arc, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::sync::{Mutex, Notify, Semaphore, watch};

const RATE: u32 = 48_000;
const HISTORY_FRAMES: usize = 240_000;
const MAX_RECORDING_FRAMES: usize = 28_800_000;
const MAX_RECORDERS: usize = 8;
const MAX_SUBSCRIBERS: usize = 4;
const MAX_QUEUED_BLOCKS: usize = 64;
const RECORD_BYTES: usize = 64 * 1024 * 1024;
const LIVE_BYTES: usize = 16 * 1024 * 1024;
const RESPONSE_LEASES: usize = 32;
const IDLE: Duration = Duration::from_secs(120);

fn capacity() -> ApiError {
    ApiError::unavailable("audio_runtime_capacity")
}
fn phonogram(message: impl Into<String>) -> ApiError {
    ApiError::new(StatusCode::CONFLICT, "phonogram_error", message)
}
fn stream_error(message: impl Into<String>) -> ApiError {
    ApiError::new(StatusCode::CONFLICT, "audio_stream_unavailable", message)
}
fn stream_unavailable(message: impl Into<String>) -> ApiError {
    ApiError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "audio_stream_unavailable",
        message,
    )
}

#[derive(Default)]
struct Closed {
    value: AtomicBool,
    changed: Notify,
}
impl Closed {
    fn check(&self) -> ApiResult<()> {
        if self.value.load(Ordering::Acquire) {
            Err(ApiError::unavailable("runtime_quiescing"))
        } else {
            Ok(())
        }
    }
    fn close(&self) {
        self.value.store(true, Ordering::Release);
        self.changed.notify_waiters();
    }
    async fn cancelled(&self) {
        loop {
            let changed = self.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            if self.value.load(Ordering::Acquire) {
                return;
            }
            changed.await;
        }
    }
}

struct State {
    recorders: BTreeMap<hub::Key, recorder::Recorder>,
    hub: hub::Hub,
    budget: Arc<budget::Budget>,
}
impl State {
    fn recorder(&mut self, key: &hub::Key) -> ApiResult<&mut recorder::Recorder> {
        let count = self.recorders.len();
        match self.recorders.entry(key.clone()) {
            Entry::Occupied(entry) => Ok(entry.into_mut()),
            Entry::Vacant(entry) => {
                if count == MAX_RECORDERS {
                    return Err(capacity());
                }
                let recorder = recorder::Recorder::new(self.budget.clone())?;
                Ok(entry.insert(recorder))
            }
        }
    }
    fn status(&self, key: &hub::Key) -> Value {
        self.recorders
            .get(key)
            .map_or_else(recorder::Recorder::empty_status, recorder::Recorder::status)
    }
}

pub(crate) struct AudioHttp {
    state: Arc<Mutex<State>>,
    closed: Arc<Closed>,
    leases: Arc<Semaphore>,
    runtime: tokio::runtime::Handle,
    shutdown: OnceLock<CloneCloseHandle>,
    #[cfg(test)]
    mutation_latch: std::sync::Mutex<Option<tests::MutationLatch>>,
}
impl AudioHttp {
    fn new() -> Arc<Self> {
        let budget = budget::Budget::new(RECORD_BYTES);
        Arc::new(Self {
            state: Arc::new(Mutex::new(State {
                recorders: BTreeMap::new(),
                hub: hub::Hub::new(budget::Budget::new(LIVE_BYTES)),
                budget,
            })),
            closed: Arc::new(Closed::default()),
            leases: Arc::new(Semaphore::new(RESPONSE_LEASES)),
            runtime: tokio::runtime::Handle::current(),
            shutdown: OnceLock::new(),
            #[cfg(test)]
            mutation_latch: std::sync::Mutex::new(None),
        })
    }
    /// A retained attempt, not a new cleanup task per waiter. Network bodies
    /// only retain bounded inert memory, never an owner that close must JOIN.
    pub fn begin_shutdown(&self) -> CloneCloseHandle {
        self.shutdown
            .get_or_init(|| {
                self.closed.close();
                let state = self.state.clone();
                let (sent, completed) = watch::channel(false);
                self.runtime.spawn(async move {
                    // Includes any mutation admitted before the closed flag. No
                    // producer or encoder can outlive this state coordinator.
                    let mut state = state.lock().await;
                    state.hub.close();
                    state.recorders.clear();
                    drop(state);
                    sent.send_replace(true);
                });
                CloneCloseHandle { completed }
            })
            .clone()
    }
    #[cfg(test)]
    async fn pause_admitted_mutation(&self) {
        let latch = self
            .mutation_latch
            .lock()
            .expect("test mutation latch")
            .take();
        if let Some(latch) = latch {
            latch.pause().await;
        }
    }
}
#[derive(Clone)]
pub(crate) struct CloneCloseHandle {
    completed: watch::Receiver<bool>,
}
impl CloneCloseHandle {
    pub async fn completed(&self) -> crate::Result<()> {
        let mut completed = self.completed.clone();
        loop {
            if *completed.borrow() {
                return Ok(());
            }
            completed.changed().await.map_err(|_| CoreError::Task)?;
        }
    }
}

pub(crate) async fn initialize(core: &Arc<Core>) -> crate::Result<()> {
    core.audio_http
        .set(AudioHttp::new())
        .map_err(|_| CoreError::Invalid("uploaded audio HTTP initialized twice"))
}
fn owner(core: &Core) -> ApiResult<&Arc<AudioHttp>> {
    core.audio_http
        .get()
        .ok_or_else(|| ApiError::unavailable("audio_runtime_unavailable"))
}
fn route(parts: &[String]) -> Option<(&str, &str, &str)> {
    if parts.len() == 6 && parts[0] == "api" && parts[1] == "v1" && parts[2] == "sessions" {
        Some((&parts[3], &parts[4], &parts[5]))
    } else {
        None
    }
}
pub(crate) fn handles(method: &Method, parts: &[String]) -> bool {
    matches!(
        (method.as_str(), route(parts)),
        ("GET", Some((_, "phonogram", "status" | "wav")))
            | ("GET", Some((_, "audio", "status" | "stream")))
            | ("POST", Some((_, "phonogram", "frames" | "start" | "stop")))
    )
}
async fn key(core: &Core, session_id: &str, stream_id: Option<&Value>) -> ApiResult<hub::Key> {
    let stream_id = stream_id
        .and_then(Value::as_str)
        .filter(|id| !id.trim().is_empty())
        .ok_or_else(|| ApiError::invalid("stream_id должен быть непустым ID потока"))?;
    let session = core.session(session_id).await?;
    if !session["streams"].as_array().is_some_and(|items| {
        items
            .iter()
            .any(|item| item["id"].as_str() == Some(stream_id))
    }) {
        return Err(ApiError::invalid(format!(
            "Поток не принадлежит сеансу: {stream_id}"
        )));
    }
    Ok((session_id.into(), stream_id.into()))
}
fn identified(mut data: Value, key: &hub::Key) -> Value {
    let fields = data.as_object_mut().expect("recorder status is an object");
    fields.insert("session_id".into(), key.0.clone().into());
    fields.insert("stream_id".into(), key.1.clone().into());
    data
}

pub(crate) async fn get(
    core: &Core,
    parts: &[String],
    query: &BTreeMap<String, String>,
    auth: AuthReceipt,
) -> ApiResult<Option<Response>> {
    if !handles(&Method::GET, parts) {
        return Ok(None);
    }
    auth.revalidate()?;
    let (session, family, operation) = route(parts).expect("selected audio route");
    let stream = query.get("stream_id").map(|id| Value::String(id.clone()));
    let key = key(core, session, stream.as_ref()).await.map_err(|error| {
        if family == "audio"
            && operation == "stream"
            && matches!(error.code.as_str(), "invalid_request" | "session_not_found")
        {
            let message = if error.code == "session_not_found" {
                pcm::python_repr(&error.message)
            } else {
                error.message
            };
            ApiError::new(
                if error.status == StatusCode::NOT_FOUND {
                    StatusCode::NOT_FOUND
                } else {
                    StatusCode::CONFLICT
                },
                "audio_stream_unavailable",
                message,
            )
        } else {
            error
        }
    })?;
    let owner = owner(core)?;
    let mut state = owner.state.lock().await;
    owner.closed.check()?;
    auth.revalidate()?;
    let response = match (family, operation) {
        ("phonogram", "status") => {
            crate::http::envelope(StatusCode::OK, identified(state.status(&key), &key))
        }
        ("audio", "status") => crate::http::envelope(StatusCode::OK, state.hub.status(&key)),
        ("phonogram", "wav") => {
            let completed = state
                .recorders
                .get(&key)
                .ok_or_else(|| phonogram("Фонограмма ещё не записана"))
                .and_then(recorder::Recorder::completed)
                .map_err(|error| ApiError::invalid(error.message))?;
            let permit = owner
                .leases
                .clone()
                .try_acquire_owned()
                .map_err(|_| capacity())?;
            // Budget the owner boxes/metadata of every possible chunk view.
            // The lease follows already-yielded Bytes, not just the body.
            let metadata = state.budget.reserve(completed.chunks().len() * 64 + 512)?;
            let lease = Arc::new(WavLease {
                _permit: permit,
                _metadata: metadata,
            });
            wav_response(completed, owner.closed.clone(), auth, lease)
        }
        ("audio", "stream") => {
            state.recorder(&key)?;
            let subscription = state.hub.subscribe(key, &owner.leases, auth.clone())?;
            pcm_response(subscription, owner.closed.clone(), auth)
        }
        _ => unreachable!(),
    };
    Ok(Some(response))
}

pub(crate) async fn post(
    core: &Core,
    parts: &[String],
    payload: Value,
    auth: AuthReceipt,
) -> ApiResult<Option<Response>> {
    if !handles(&Method::POST, parts) {
        return Ok(None);
    }
    auth.revalidate()?;
    let (session, _, operation) = route(parts).expect("selected phonogram route");
    let fields = payload.as_object();
    let valid = fields.is_some_and(|fields| {
        if operation == "frames" {
            fields.len() == 2 && fields.contains_key("stream_id") && fields.contains_key("samples")
        } else {
            fields.len() == 1 && fields.contains_key("stream_id")
        }
    });
    if !valid {
        return Err(ApiError::invalid(if operation == "frames" {
            "Разрешены только поля stream_id и samples"
        } else {
            "Разрешено только поле stream_id"
        }));
    }
    let samples = if operation == "frames" {
        let samples = &payload["samples"];
        if !samples
            .as_array()
            .is_some_and(|items| !items.is_empty() && items.len() <= 4800)
        {
            return Err(ApiError::invalid(
                "samples должен содержать от 1 до 4800 отсчётов",
            ));
        }
        Some(samples)
    } else {
        None
    };
    let key = key(core, session, payload.get("stream_id")).await?;
    let owner = owner(core)?;
    let mut state = owner.state.lock().await;
    owner.closed.check()?;
    auth.revalidate()?;
    // Keep actual conversion/finalization in the admitted coordinator as well
    // as state mutation. Close cannot publish completion while it is running.
    let samples = samples.map(pcm::decode_samples).transpose()?;
    auth.revalidate()?;
    // Test-only latch surrounds the actual admitted operation, not a detached
    // model. Production has no suspension between authority and mutation.
    #[cfg(test)]
    {
        owner.pause_admitted_mutation().await;
        auth.revalidate()?;
    }
    let now = crate::utc_now();
    match operation {
        "frames" => {
            let samples = samples.as_deref().expect("validated uploaded samples");
            if !samples.is_empty() {
                state.recorder(&key)?.push(samples, &now)?;
            }
            state.hub.publish(&key, samples)?;
        }
        "start" => state.recorder(&key)?.start(&now)?,
        "stop" => state
            .recorders
            .get_mut(&key)
            .ok_or_else(|| phonogram("Запись не начата"))?
            .stop(&now)?,
        _ => unreachable!(),
    }
    let status = state.status(&key);
    Ok(Some(crate::http::envelope(
        StatusCode::OK,
        if operation == "frames" {
            status
        } else {
            identified(status, &key)
        },
    )))
}

struct WavLease {
    _permit: tokio::sync::OwnedSemaphorePermit,
    _metadata: budget::Reservation,
}
struct ChunkView {
    chunk: Arc<recorder::Chunk>,
    _lease: Arc<WavLease>,
}
impl AsRef<[u8]> for ChunkView {
    fn as_ref(&self) -> &[u8] {
        self.chunk.bytes()
    }
}
struct WavHeader {
    bytes: [u8; 44],
    _lease: Arc<WavLease>,
}
impl AsRef<[u8]> for WavHeader {
    fn as_ref(&self) -> &[u8] {
        &self.bytes
    }
}
struct PacketView(Arc<hub::Packet>);
impl AsRef<[u8]> for PacketView {
    fn as_ref(&self) -> &[u8] {
        self.0.bytes()
    }
}

fn wav_response(
    completed: Arc<recorder::Completed>,
    closed: Arc<Closed>,
    auth: AuthReceipt,
    lease: Arc<WavLease>,
) -> Response {
    let length = completed.length();
    // The header encoder runs under GET's coordinator too. Body polls only
    // expose encoded immutable memory; they start no encoder or owned read.
    let header = completed.header();
    let stream = async_stream::stream! {
        if closed.check().is_ok() && auth.revalidate().is_ok() {
            yield Ok::<_, Infallible>(Bytes::from_owner(WavHeader { bytes: header, _lease: lease.clone() }));
            for chunk in completed.chunks() {
                if closed.check().is_err() || auth.revalidate().is_err() { break; }
                yield Ok(Bytes::from_owner(ChunkView { chunk: chunk.clone(), _lease: lease.clone() }));
            }
        }
    };
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "audio/wav")
        .header(header::CONTENT_LENGTH, length)
        .header(header::CACHE_CONTROL, "no-store")
        .body(Body::from_stream(stream))
        .expect("static WAV response headers")
}
fn pcm_response(
    subscription: hub::Subscription,
    closed: Arc<Closed>,
    auth: AuthReceipt,
) -> Response {
    let stream = async_stream::stream! {
        let subscription_guard = subscription;
        let mut last_block = tokio::time::Instant::now();
        loop {
            if closed.check().is_err() || auth.revalidate().is_err() { break; }
            let packet = tokio::select! {
                biased;
                _ = closed.cancelled() => None,
                _ = auth.revoked() => None,
                result = tokio::time::timeout_at(last_block + IDLE, subscription_guard.0.receive()) => result.ok().flatten(),
            };
            let Some(packet) = packet else { break; };
            if closed.check().is_err() || auth.revalidate().is_err() { break; }
            last_block = tokio::time::Instant::now();
            yield Ok::<_, Infallible>(Bytes::from_owner(PacketView(packet)));
        }
    };
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "audio/pcm")
        .header("X-Sample-Rate", "48000")
        .header("X-Sample-Format", "pcm16le")
        .header(header::CACHE_CONTROL, "no-store")
        .body(Body::from_stream(stream))
        .expect("static PCM response headers")
}
