//! Pull-based raw cursors: no whole archive copy and no heavy research permit.
use crate::{
    Core, CoreError,
    error::{ApiError, ApiResult},
    security::AuthReceipt,
};
use axum::{
    body::{Body, Bytes},
    http::{StatusCode, header},
    response::Response,
};
use ori_archive::{
    ArchiveError, SnapshotExport, SnapshotExportInfo, SnapshotExportKind, SnapshotExportLimits,
    SnapshotExportQuery,
};
use ori_runtime::{qos::BackgroundWorkGuard, worker::Cancellation};
use serde_json::json;
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::sync::{Semaphore, mpsc, oneshot, watch};

#[derive(Clone, Copy, Debug)]
pub(crate) enum Failure {
    Missing,
    Invalid,
    Busy,
    Budget,
    Closed,
    Failed,
}
impl Failure {
    fn from(error: ArchiveError) -> Self {
        match error {
            ArchiveError::NotFound => Self::Missing,
            ArchiveError::Invalid(_) => Self::Invalid,
            ArchiveError::QueueFull { .. } => Self::Busy,
            ArchiveError::QueryBudget => Self::Budget,
            ArchiveError::Closed => Self::Closed,
            _ => Self::Failed,
        }
    }
    fn api(self) -> ApiError {
        match self {
            Self::Missing => ApiError::missing("session_not_found"),
            Self::Invalid => ApiError::new(
                StatusCode::NOT_FOUND,
                "stream_not_found",
                "Stream not found in session",
            ),
            Self::Busy => ApiError::unavailable("archive_queue_full"),
            Self::Budget => ApiError::unavailable("archive_query_budget"),
            Self::Closed => ApiError::unavailable("archive_export_closed"),
            Self::Failed => ApiError::unavailable("archive_export_failed"),
        }
    }
}
struct Frame {
    kind: SnapshotExportKind,
    bytes: Vec<u8>,
}
type Next = oneshot::Sender<std::result::Result<Option<Frame>, Failure>>;
type Completion = watch::Receiver<Option<std::result::Result<(), Failure>>>;
struct Signals {
    ready: watch::Sender<Option<std::result::Result<SnapshotExportInfo, Failure>>>,
    done: watch::Sender<Option<std::result::Result<(), Failure>>>,
}
struct Cursor {
    cancel: Cancellation,
    auth: AuthReceipt,
    next: mpsc::Sender<Next>,
    ready: watch::Receiver<Option<std::result::Result<SnapshotExportInfo, Failure>>>,
    done: Completion,
    quarantine: Mutex<Option<BackgroundWorkGuard>>,
}
impl Cursor {
    fn authorized(&self) -> bool {
        !self.cancel.is_cancelled() && self.auth.revalidate().is_ok()
    }
    async fn ready(&self) -> std::result::Result<SnapshotExportInfo, Failure> {
        let mut receiver = self.ready.clone();
        loop {
            if let Some(outcome) = receiver.borrow().clone() {
                return outcome;
            }
            receiver.changed().await.map_err(|_| Failure::Failed)?;
        }
    }
    async fn next(&self) -> std::result::Result<Option<Frame>, Failure> {
        if !self.authorized() {
            return Err(Failure::Closed);
        }
        let (sent, receiver) = oneshot::channel();
        self.next.try_send(sent).map_err(|_| Failure::Closed)?;
        receiver.await.map_err(|_| Failure::Closed)?
    }
    async fn close(&self) -> std::result::Result<(), Failure> {
        self.cancel.cancel();
        wait(self.done.clone()).await
    }
}
struct BodyOwner(Arc<Cursor>);
impl Drop for BodyOwner {
    fn drop(&mut self) {
        self.0.cancel.cancel();
    }
}
pub(crate) struct RawExports {
    cursors: Mutex<BTreeMap<u64, Arc<Cursor>>>,
    serial: AtomicU64,
    closing: AtomicBool,
    encoder: Arc<Semaphore>,
    runtime: tokio::runtime::Handle,
    shutdown: OnceLock<Completion>,
}
pub(crate) fn initialize(core: &Arc<Core>) -> crate::Result<()> {
    if core.archive.is_none() {
        return Ok(());
    }
    core.raw_exports
        .set(Arc::new(RawExports {
            cursors: Mutex::new(BTreeMap::new()),
            serial: AtomicU64::new(0),
            closing: AtomicBool::new(false),
            encoder: Arc::new(Semaphore::new(1)),
            runtime: tokio::runtime::Handle::current(),
            shutdown: OnceLock::new(),
        }))
        .map_err(|_| CoreError::Invalid("raw export owner already initialized"))?;
    Ok(())
}
impl RawExports {
    fn open(
        self: &Arc<Self>,
        core: &Core,
        query: SnapshotExportQuery,
        auth: AuthReceipt,
    ) -> ApiResult<BodyOwner> {
        auth.revalidate()?;
        let mut cursors = self
            .cursors
            .lock()
            .map_err(|_| ApiError::unavailable("archive_export_failed"))?;
        if self.closing.load(Ordering::Acquire) {
            return Err(Failure::Closed.api());
        }
        if cursors.len() >= 2 {
            return Err(Failure::Busy.api());
        }
        let background = core
            .background
            .as_ref()
            .ok_or_else(|| Failure::Closed.api())?;
        let cancel = Cancellation::default();
        let guard = background.qos.register(cancel.clone()).map_err(|error| {
            ApiError::unavailable(match error {
                ori_runtime::qos::QosError::Busy => "archive_export_busy",
                ori_runtime::qos::QosError::Pressure => "acquisition_pressure",
                ori_runtime::qos::QosError::Cancelled => "archive_export_closed",
                ori_runtime::qos::QosError::InvalidCharge => "background_io_unavailable",
            })
        })?;
        let id = self
            .serial
            .try_update(Ordering::AcqRel, Ordering::Acquire, |v| v.checked_add(1))
            .map_err(|_| Failure::Busy.api())?;
        let reader = SnapshotExport::open_live(
            core.archive()?.clone(),
            query,
            SnapshotExportLimits::default(),
        )?;
        let (sent, commands) = mpsc::channel(1);
        let (ready, ready_received) = watch::channel(None);
        let (done, done_received) = watch::channel(None);
        let cursor = Arc::new(Cursor {
            cancel,
            auth,
            next: sent,
            ready: ready_received,
            done: done_received,
            quarantine: Mutex::new(None),
        });
        cursors.insert(id, cursor.clone());
        let own = self.clone();
        let entry = cursor.clone();
        self.runtime.spawn(async move {
            own.drive(id, entry, reader, commands, Signals { ready, done }, guard)
                .await;
        });
        Ok(BodyOwner(cursor))
    }
    async fn drive(
        self: Arc<Self>,
        id: u64,
        cursor: Arc<Cursor>,
        reader: SnapshotExport,
        mut commands: mpsc::Receiver<Next>,
        signals: Signals,
        guard: BackgroundWorkGuard,
    ) {
        let Signals { ready, done } = signals;
        let status = tokio::select! {
            biased;
            _=interrupted(&cursor)=>Err(Failure::Closed),
            ready=reader.ready()=>ready.map_err(Failure::from),
        };
        let available = status.is_ok();
        ready.send_replace(Some(status));
        if available {
            loop {
                let request = tokio::select! {
                    biased;
                    _=interrupted(&cursor)=>break,
                    request=commands.recv()=>match request{Some(request)=>request,None=>break},
                };
                if !cursor.authorized() {
                    let _ = request.send(Err(Failure::Closed));
                    break;
                }
                let record = tokio::select! {
                    biased;
                    _=interrupted(&cursor)=>{let _=request.send(Err(Failure::Closed));break;},
                    record=reader.next_record()=>record,
                };
                let outcome = match record {
                    Ok(Some(record)) => {
                        let encoding = tokio::select! {
                            biased;
                            _=interrupted(&cursor)=>{let _=request.send(Err(Failure::Closed));break;},
                            encoding=self.encoder.clone().acquire_owned()=>encoding,
                        };
                        match encoding {
                            Ok(encoding) => {
                                if !cursor.authorized() {
                                    let _ = request.send(Err(Failure::Closed));
                                    break;
                                }
                                // The supervisor owns this blocking JOIN. A dropped body
                                // cannot leave serialization detached from its budget.
                                tokio::task::spawn_blocking(move || {
                                    let _encoding = encoding;
                                    ori_contracts::to_json_vec(&record.value)
                                        .map(|bytes| {
                                            Some(Frame {
                                                kind: record.kind,
                                                bytes,
                                            })
                                        })
                                        .map_err(|_| Failure::Failed)
                                })
                                .await
                                .unwrap_or(Err(Failure::Failed))
                            }
                            Err(_) => Err(Failure::Closed),
                        }
                    }
                    Ok(None) => Ok(None),
                    Err(error) => Err(Failure::from(error)),
                };
                // Encoding is JOINed even after revocation. Never publish its
                // prepared bytes under an expired originating credential.
                let outcome = if cursor.authorized() {
                    outcome
                } else {
                    Err(Failure::Closed)
                };
                let finished = !matches!(&outcome, Ok(Some(_)));
                if request.send(outcome).is_err() || finished {
                    break;
                }
            }
        }
        let outcome = reader.close().await.map_err(Failure::from);
        if outcome.is_ok() {
            drop(guard);
            if let Ok(mut cursors) = self.cursors.lock() {
                cursors.remove(&id);
            }
        } else if let Ok(mut held) = cursor.quarantine.lock() {
            *held = Some(guard);
        }
        done.send_replace(Some(outcome));
    }
    pub fn begin_shutdown(self: &Arc<Self>) -> Completion {
        self.shutdown
            .get_or_init(|| {
                // Admission closes synchronously, including calls from a standard
                // stdin lifeline thread. The originating runtime owns all joins.
                self.closing.store(true, Ordering::Release);
                let (sent, receiver) = watch::channel(None);
                let own = self.clone();
                self.runtime.spawn(async move {
                    let entries = own
                        .cursors
                        .lock()
                        .map(|v| v.values().cloned().collect::<Vec<_>>())
                        .map_err(|_| Failure::Failed);
                    let outcome = match entries {
                        Err(error) => Err(error),
                        Ok(entries) => {
                            for entry in &entries {
                                entry.cancel.cancel();
                            }
                            let mut failed = None;
                            for entry in entries {
                                if let Err(error) = entry.close().await {
                                    failed = Some(error);
                                }
                            }
                            failed.map_or(Ok(()), Err)
                        }
                    };
                    sent.send_replace(Some(outcome));
                });
                receiver
            })
            .clone()
    }
    pub async fn close(self: &Arc<Self>) -> crate::Result<()> {
        wait(self.begin_shutdown())
            .await
            .map_err(|_| CoreError::Invalid("raw export reader cleanup is unconfirmed"))
    }
}
async fn wait(mut receiver: Completion) -> std::result::Result<(), Failure> {
    loop {
        if let Some(outcome) = *receiver.borrow() {
            return outcome;
        }
        receiver.changed().await.map_err(|_| Failure::Failed)?;
    }
}
async fn cancelled(cancel: &Cancellation) {
    while !cancel.is_cancelled() {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}
async fn interrupted(cursor: &Cursor) {
    tokio::select! {
        _=cursor.auth.revoked()=>{},
        _=cancelled(&cursor.cancel)=>{},
    }
    cursor.cancel.cancel();
}
fn body_authorized(auth: &AuthReceipt, cancel: &Cancellation) -> std::io::Result<()> {
    if cancel.is_cancelled() || auth.revalidate().is_err() {
        Err(std::io::Error::other("raw export request authority ended"))
    } else {
        Ok(())
    }
}
pub(crate) async fn get(
    core: &Core,
    parts: &[String],
    query: &BTreeMap<String, String>,
    auth: AuthReceipt,
) -> ApiResult<Option<Response>> {
    let route = parts.iter().map(String::as_str).collect::<Vec<_>>();
    let [
        "api",
        "v1",
        "sessions",
        session,
        "archive",
        format @ ("export" | "export.ndjson"),
    ] = route.as_slice()
    else {
        return Ok(None);
    };
    let value = core.session(session).await?;
    let stream = query.get("stream_id").filter(|s| !s.is_empty()).cloned();
    if let Some(stream) = &stream
        && value["streams"]
            .as_array()
            .is_none_or(|items| !items.iter().any(|item| item["id"] == *stream))
    {
        return Err(ApiError::invalid(format!(
            "Поток не принадлежит сеансу: {stream}"
        )));
    }
    auth.revalidate()?;
    let registry = core
        .raw_exports
        .get()
        .ok_or_else(|| Failure::Closed.api())?;
    let owner = registry.open(
        core,
        SnapshotExportQuery {
            session_id: (*session).into(),
            stream_id: stream,
        },
        auth.clone(),
    )?;
    let info = match owner.0.ready().await {
        Ok(info) => info,
        Err(error) => {
            let _ = owner.0.close().await;
            return Err(error.api());
        }
    };
    auth.revalidate()?;
    let ndjson = *format == "export.ndjson";
    let qos = core
        .background
        .as_ref()
        .expect("owned raw background")
        .qos
        .clone();
    let mut shutdown = core.shutdown.subscribe();
    let header = if ndjson {
        let mut header=ori_contracts::to_json_vec(&json!({"format":"openradioinspector-archive-ndjson","format_version":1,"session_id":info.session_id})).map_err(|_|Failure::Failed.api())?;
        header.push(b'\n');
        Bytes::from(header)
    } else {
        Bytes::from_static(b"{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":{\"format\":\"openradioinspector-archive\",\"format_version\":1,\"sweeps\":[")
    };
    let stream: std::pin::Pin<Box<dyn futures_util::Stream<Item = std::io::Result<Bytes>> + Send>> =
        Box::pin(async_stream::try_stream! {
            let held=owner;
            body_authorized(&auth,&held.0.cancel)?;
            yield header;
            let mut observations=false;let mut first=true;
            loop {
                let frame=tokio::select! {
                    biased;
                    _=auth.revoked()=>Err(std::io::Error::other("request authority revoked")),
                    _=async{while !*shutdown.borrow(){if shutdown.changed().await.is_err(){break;}}}=>Err(std::io::Error::other("core shutting down")),
                    value=held.0.next()=>value.map_err(|_|std::io::Error::other("raw export read failed")),
                }?;
                let Some(frame)=frame else{break;};
                auth.revalidate().map_err(|_|std::io::Error::other("request authority revoked"))?;
                // This is HTTP delivery pacing, not evidence that native SQLite
                // page reads or physical disk traffic are completely accounted.
                for chunk in frame.bytes.chunks(8*1024*1024) {
                    qos.account(chunk.len() as u64,&held.0.cancel).await.map_err(|_|std::io::Error::other("raw export delivery paused"))?;
                }
                if ndjson {
                    body_authorized(&auth,&held.0.cancel)?;
                    yield Bytes::from_static(if matches!(frame.kind,SnapshotExportKind::Sweep){b"{\"kind\":\"sweep\",\"measurement\":"}else{b"{\"kind\":\"observation\",\"measurement\":"});
                    body_authorized(&auth,&held.0.cancel)?;
                    yield Bytes::from(frame.bytes);
                    body_authorized(&auth,&held.0.cancel)?;
                    yield Bytes::from_static(b"}\n");
                } else {
                    if matches!(frame.kind,SnapshotExportKind::Observation) && !observations {
                        observations=true;first=true;
                        body_authorized(&auth,&held.0.cancel)?;
                        yield Bytes::from_static(b"],\"observations\":[");
                    }
                    if !first{
                        body_authorized(&auth,&held.0.cancel)?;
                        yield Bytes::from_static(b",");
                    }
                    first=false;
                    body_authorized(&auth,&held.0.cancel)?;
                    yield Bytes::from(frame.bytes);
                }
            }
            // EOF already makes the owned reader close and JOIN. Do not mark
            // a successful cursor cancelled before publishing its JSON tail.
            wait(held.0.done.clone()).await.map_err(|_|std::io::Error::other("raw export cleanup failed"))?;
            if !ndjson {
                if !observations{
                    body_authorized(&auth,&held.0.cancel)?;
                    yield Bytes::from_static(b"],\"observations\":[");
                }
                body_authorized(&auth,&held.0.cancel)?;
                yield Bytes::from_static(b"]}}");
            }
        });
    let response = Response::builder()
        .status(StatusCode::OK)
        .header(
            header::CONTENT_TYPE,
            if ndjson {
                "application/x-ndjson; charset=utf-8"
            } else {
                "application/json; charset=utf-8"
            },
        )
        .header(header::CACHE_CONTROL, "no-store")
        .header("X-Archive-Sweeps", info.sweeps)
        .header("X-Archive-Observations", info.observations)
        .header("X-Archive-Boundary", info.boundary)
        .body(Body::from_stream(stream))
        .map_err(|_| Failure::Failed.api())?;
    Ok(Some(response))
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn core(data: &std::path::Path) -> Arc<Core> {
        let core = Core::open(crate::CoreConfig::replay(data)).await.unwrap();
        let fixture = ori_contracts::parse_json_value(
            include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
            65536,
        )
        .unwrap();
        let mut session = fixture["session"].clone();
        session["created_at"] = json!("2026-11-11T00:00:00.000000Z");
        core.archive()
            .unwrap()
            .create_session(session)
            .await
            .unwrap();
        core.archive()
            .unwrap()
            .append_sweeps(vec![fixture["sweep"].clone()])
            .await
            .unwrap();
        core
    }
    fn query() -> SnapshotExportQuery {
        SnapshotExportQuery {
            session_id: "fixture-сессия".into(),
            stream_id: None,
        }
    }
    async fn auth(core: &Core) -> AuthReceipt {
        core.security
            .check(
                &axum::http::Method::GET,
                "/api/v1/health",
                &axum::http::HeaderMap::new(),
                crate::security::ListenerRole::Plain,
            )
            .await
            .unwrap()
            .receipt()
    }
    #[tokio::test]
    async fn two_live_raw_readers_leave_research_available_and_join_before_writer_release() {
        let data = tempfile::tempdir().unwrap();
        let core = core(data.path()).await;
        let registry = core.raw_exports.get().unwrap();
        let auth = auth(&core).await;
        let one = registry.open(&core, query(), auth.clone()).unwrap();
        one.0.ready().await.unwrap();
        let two = registry.open(&core, query(), auth.clone()).unwrap();
        two.0.ready().await.unwrap();
        let third = registry.open(&core, query(), auth).err().unwrap();
        let heavy = core.research_pool.try_reserve();
        let available = heavy.is_ok();
        drop(heavy);
        let registered = core
            .background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work;
        core.close().await.unwrap();
        let empty = registry.cursors.lock().unwrap().is_empty();
        let released = ori_runtime::lease::WriterLease::acquire(data.path()).is_ok();
        drop((one, two));
        assert_eq!(third.code, "archive_queue_full");
        assert_eq!(registered, 2);
        assert!(available && empty && released);
    }
    #[tokio::test]
    async fn cancelled_queued_record_does_not_wait_for_an_unavailable_encoder() {
        let data = tempfile::tempdir().unwrap();
        let core = core(data.path()).await;
        let registry = core.raw_exports.get().unwrap();
        let body = registry.open(&core, query(), auth(&core).await).unwrap();
        body.0.ready().await.unwrap();
        let encoder = registry.encoder.clone().acquire_owned().await.unwrap();
        let (sent, received) = oneshot::channel();
        body.0.next.try_send(sent).unwrap();
        while body.0.next.capacity() == 0 {
            tokio::task::yield_now().await;
        }
        let close = tokio::time::timeout(Duration::from_secs(3), registry.close()).await;
        drop(encoder);
        let cancelled = !matches!(received.await, Ok(Ok(Some(_))));
        core.close().await.unwrap();
        close.unwrap().unwrap();
        assert!(cancelled);
    }
}
