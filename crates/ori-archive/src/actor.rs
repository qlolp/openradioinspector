use crate::{
    ArchiveConfig, ArchiveError, CommitNotice, CommittedBatch, Mutation, Result, schema, validate,
    writer,
};
use rusqlite::Connection;
use serde::Serialize;
use std::{
    any::Any,
    io::Write,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc::{self, SyncSender},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tokio::sync::{Notify, broadcast, oneshot};

#[derive(Debug)]
pub(crate) struct ByteReservation {
    bytes: usize,
    counter: Arc<AtomicUsize>,
}
impl Drop for ByteReservation {
    fn drop(&mut self) {
        self.counter.fetch_sub(self.bytes, Ordering::AcqRel);
    }
}
enum WriterCommand {
    Apply {
        mutations: Vec<Mutation>,
        reservation: Arc<ByteReservation>,
        reply: oneshot::Sender<Result<CommittedBatch>>,
    },
    Checkpoint {
        reply: oneshot::Sender<Result<serde_json::Value>>,
    },
    Outbox {
        operation: crate::outbox::OutboxOperation,
        reservation: Arc<ByteReservation>,
        timeout: Duration,
        reply: oneshot::Sender<Result<crate::outbox::OutboxReply>>,
    },
    CaptureLifecycle {
        id: String,
        reservation: Arc<ByteReservation>,
        reply: oneshot::Sender<Result<crate::SessionLifecycleReceipt>>,
    },
    Close,
}
type ReadResult = Box<dyn Any + Send>;
type ReadFn = Box<dyn FnOnce(&mut Connection) -> Result<ReadResult> + Send>;
struct ReadCommand {
    run: ReadFn,
    timeout: Duration,
    reply: oneshot::Sender<Result<ReadResult>>,
}

pub(crate) struct Inner {
    pub config: ArchiveConfig,
    pub domain_coordinator: Arc<tokio::sync::Mutex<()>>,
    pub(crate) lifecycle: crate::session_lifecycle::Registry,
    pub writer_synchronous: i64,
    pub live_export_path: std::path::PathBuf,
    live_exports: Mutex<Vec<Weak<crate::snapshot_export::LiveExportOwner>>>,
    writer: Mutex<Option<SyncSender<WriterCommand>>>,
    readers: Mutex<Option<SyncSender<ReadCommand>>>,
    handles: Mutex<Vec<JoinHandle<()>>>,
    bytes: Arc<AtomicUsize>,
    queued: Arc<AtomicUsize>,
    closing: AtomicBool,
    closed: AtomicBool,
    close_failed: AtomicBool,
    close_notify: Notify,
    pub backup_busy: AtomicBool,
    pub notices: broadcast::Sender<CommitNotice>,
}

struct LimitedCount {
    remaining: usize,
}
impl Write for LimitedCount {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > self.remaining {
            return Err(std::io::Error::other("response budget"));
        }
        self.remaining -= bytes.len();
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

pub(crate) fn check_response_size(value: &impl Serialize, maximum: usize) -> Result<()> {
    serde_json::to_writer(LimitedCount { remaining: maximum }, value)
        .map_err(|_| ArchiveError::QueryBudget)
}

fn run_outbox(
    connection: &mut Connection,
    operation: crate::outbox::OutboxOperation,
    maximum: usize,
    timeout: Duration,
) -> Result<crate::outbox::OutboxReply> {
    let deadline = Instant::now() + timeout;
    connection.progress_handler(10000, Some(move || Instant::now() > deadline))?;
    let outcome = writer::outbox(connection, operation, maximum);
    let clear = connection.progress_handler(0, None::<fn() -> bool>);
    match outcome {
        Err(ArchiveError::Sql(rusqlite::Error::SqliteFailure(error, _)))
            if error.code == rusqlite::ErrorCode::OperationInterrupted =>
        {
            Err(ArchiveError::QueryBudget)
        }
        Ok(_) if clear.is_err() => Err(ArchiveError::WorkerStopped),
        other => other,
    }
}

impl Inner {
    pub fn open(config: ArchiveConfig) -> Result<Arc<Self>> {
        config.validate()?;
        let lease = schema::ProcessWriterLease::acquire(&config.path)?;
        let mut connection = schema::writer(&config)?;
        let live_export_path = config.path.canonicalize()?;
        let writer_synchronous =
            connection.query_row("PRAGMA synchronous", [], |row| row.get::<_, i64>(0))?;
        let mut read_connections = Vec::new();
        for _ in 0..config.readers {
            read_connections.push(schema::reader(&config)?);
        }
        let (writer_tx, writer_rx) =
            mpsc::sync_channel::<WriterCommand>(config.writer_queue_capacity);
        let (reader_tx, reader_rx) =
            mpsc::sync_channel::<ReadCommand>(config.reader_queue_capacity);
        let reader_rx = Arc::new(Mutex::new(reader_rx));
        let bytes = Arc::new(AtomicUsize::new(0));
        let queued = Arc::new(AtomicUsize::new(0));
        let (notices, _) = broadcast::channel(64);
        let publication = notices.clone();
        let pending = queued.clone();
        let capacity = config.max_database_bytes;
        let capture_budget = config.max_response_bytes;
        let lifecycle = crate::session_lifecycle::Registry::default();
        let writer_lifecycle = lifecycle.clone();
        let writer_handle=thread::Builder::new().name("ori-canonical-writer".into()).spawn(move||{
            let _lease=lease;
            let _lifecycle = crate::session_lifecycle::WriterLifetime(writer_lifecycle.clone());
            while let Ok(command)=writer_rx.recv(){match command{
                WriterCommand::Apply{mutations,reservation,reply}=>{
                    pending.fetch_sub(1,Ordering::AcqRel);
                    let outcome=writer::apply(&mut connection,mutations,capacity,capture_budget,&writer_lifecycle).map(|mut value|{value.reservation=Some(reservation);value});
                    if let Ok(committed)=&outcome && let (Some(first),Some(last))=(committed.first_sequence,committed.last_sequence) {
                        // The canonical transaction has already committed. Notices contain no raw payloads.
                        let _=publication.send(CommitNotice{first_sequence:first,last_sequence:last});
                    }
                    let _=reply.send(outcome);
                },
                WriterCommand::Checkpoint{reply}=>{let outcome=connection.query_row("PRAGMA wal_checkpoint(PASSIVE)",[],|row|Ok(serde_json::json!({"busy":row.get::<_,i64>(0)?,"wal_frames":row.get::<_,i64>(1)?,"checkpointed_frames":row.get::<_,i64>(2)?}))).map_err(Into::into);let _=reply.send(outcome);},
                WriterCommand::Outbox{operation,reservation,timeout,reply}=>{
                    pending.fetch_sub(1,Ordering::AcqRel);
                    let outcome=run_outbox(&mut connection,operation,capture_budget,timeout);
                    drop(reservation);
                    let _=reply.send(outcome);
                },
                WriterCommand::CaptureLifecycle{id,reservation,reply}=>{
                    pending.fetch_sub(1,Ordering::AcqRel);
                    let outcome=writer_lifecycle.capture(&connection,&id);
                    drop(reservation);
                    let _=reply.send(outcome);
                },
                WriterCommand::Close=>{let _=connection.execute_batch("PRAGMA wal_checkpoint(PASSIVE)");break;},
            }}
            drop(connection);drop(_lease);
        })?;
        let mut handles = Vec::new();
        for (index, mut connection) in read_connections.into_iter().enumerate() {
            let receiver = reader_rx.clone();
            handles.push(
                thread::Builder::new()
                    .name(format!("ori-archive-reader-{index}"))
                    .spawn(move || {
                        loop {
                            let command = match receiver.lock() {
                                Ok(receiver) => receiver.recv(),
                                Err(_) => return,
                            };
                            let Ok(command) = command else { return };
                            let Some(deadline) = Instant::now().checked_add(command.timeout) else {
                                let _ = command.reply.send(Err(crate::error::invalid(
                                    "archive reader deadline overflow",
                                )));
                                continue;
                            };
                            if let Err(error) = connection
                                .progress_handler(10000, Some(move || Instant::now() > deadline))
                            {
                                let _ = command.reply.send(Err(error.into()));
                                continue;
                            }
                            let outcome = connection
                                .execute_batch("BEGIN")
                                .map_err(Into::into)
                                .and_then(|_| (command.run)(&mut connection));
                            let clear = connection.progress_handler(0, None::<fn() -> bool>);
                            let rollback = if connection.is_autocommit() {
                                Ok(())
                            } else {
                                connection.execute_batch("ROLLBACK")
                            };
                            let outcome = match outcome {
                                Err(ArchiveError::Sql(rusqlite::Error::SqliteFailure(
                                    error,
                                    _,
                                ))) if error.code == rusqlite::ErrorCode::OperationInterrupted => {
                                    Err(ArchiveError::QueryBudget)
                                }
                                Ok(_) if rollback.is_err() || clear.is_err() => {
                                    Err(ArchiveError::WorkerStopped)
                                }
                                other => other,
                            };
                            let _ = command.reply.send(outcome);
                        }
                    })?,
            );
        }
        handles.push(writer_handle);
        Ok(Arc::new(Self {
            domain_coordinator: Arc::new(tokio::sync::Mutex::new(())),
            lifecycle,
            config,
            writer_synchronous,
            live_export_path,
            live_exports: Mutex::new(Vec::new()),
            writer: Mutex::new(Some(writer_tx)),
            readers: Mutex::new(Some(reader_tx)),
            handles: Mutex::new(handles),
            bytes,
            queued,
            closing: AtomicBool::new(false),
            closed: AtomicBool::new(false),
            close_failed: AtomicBool::new(false),
            close_notify: Notify::new(),
            backup_busy: AtomicBool::new(false),
            notices,
        }))
    }

    pub async fn apply(&self, mut mutations: Vec<Mutation>) -> Result<CommittedBatch> {
        if self.closing.load(Ordering::Acquire) {
            return Err(ArchiveError::Closed);
        }
        validate::mutations(&mut mutations)?;
        // Stream the estimate without a second JSON allocation. Closed binary payloads are
        // retained as bytes, never expanded into a JSON array for admission accounting.
        let mut counter = LimitedCount {
            remaining: self.config.max_batch_bytes,
        };
        for mutation in &mutations {
            let counted = match mutation {
                Mutation::Artifact(item) => {
                    counter.remaining = counter
                        .remaining
                        .checked_sub(item.payload.len())
                        .ok_or_else(|| crate::error::invalid("source batch byte budget"))?;
                    serde_json::to_writer(
                        &mut counter,
                        &(
                            &item.id,
                            &item.session_id,
                            &item.stream_id,
                            item.schema_version,
                            &item.kind,
                            &item.mime_type,
                            &item.filename,
                            &item.sha256,
                            &item.created_at,
                            &item.metadata,
                        ),
                    )
                }
                other => serde_json::to_writer(&mut counter, other),
            };
            counted.map_err(|_| crate::error::invalid("source batch byte budget"))?;
        }
        let byte_count = self.config.max_batch_bytes - counter.remaining;
        let (reply, receipt) = oneshot::channel();
        {
            let sender = self
                .writer
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?;
            let sender = sender.as_ref().ok_or(ArchiveError::Closed)?;
            self.bytes
                .try_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                    used.checked_add(byte_count)
                        .filter(|total| *total <= self.config.writer_queue_bytes)
                })
                .map_err(|_| ArchiveError::QueueFull { queue: "writer" })?;
            let reservation = Arc::new(ByteReservation {
                bytes: byte_count,
                counter: self.bytes.clone(),
            });
            self.queued.fetch_add(1, Ordering::AcqRel);
            if let Err(error) = sender.try_send(WriterCommand::Apply {
                mutations,
                reservation,
                reply,
            }) {
                self.queued.fetch_sub(1, Ordering::AcqRel);
                return Err(match error {
                    mpsc::TrySendError::Full(_) => ArchiveError::QueueFull { queue: "writer" },
                    mpsc::TrySendError::Disconnected(_) => ArchiveError::Closed,
                });
            }
        }
        receipt.await.map_err(|_| ArchiveError::WorkerStopped)?
    }

    pub(crate) async fn capture_session_lifecycle(
        &self,
        id: &str,
    ) -> Result<crate::SessionLifecycleReceipt> {
        if self.closing.load(Ordering::Acquire) || self.lifecycle.is_closed() {
            return Err(ArchiveError::Closed);
        }
        let bytes = id
            .len()
            .checked_add(64)
            .filter(|bytes| *bytes <= self.config.max_batch_bytes)
            .ok_or_else(|| crate::error::invalid("lifecycle capture byte budget"))?;
        let (reply, receipt) = oneshot::channel();
        {
            let sender = self
                .writer
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?;
            let sender = sender.as_ref().ok_or(ArchiveError::Closed)?;
            self.bytes
                .try_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                    used.checked_add(bytes)
                        .filter(|total| *total <= self.config.writer_queue_bytes)
                })
                .map_err(|_| ArchiveError::QueueFull { queue: "writer" })?;
            let reservation = Arc::new(ByteReservation {
                bytes,
                counter: self.bytes.clone(),
            });
            self.queued.fetch_add(1, Ordering::AcqRel);
            if let Err(error) = sender.try_send(WriterCommand::CaptureLifecycle {
                id: id.to_owned(),
                reservation,
                reply,
            }) {
                self.queued.fetch_sub(1, Ordering::AcqRel);
                return Err(match error {
                    mpsc::TrySendError::Full(_) => ArchiveError::QueueFull { queue: "writer" },
                    mpsc::TrySendError::Disconnected(_) => ArchiveError::Closed,
                });
            }
        }
        let value = receipt.await.map_err(|_| ArchiveError::WorkerStopped)??;
        if self.lifecycle.is_closed() {
            return Err(ArchiveError::Closed);
        }
        Ok(value)
    }

    pub(crate) async fn outbox(
        &self,
        mut operation: crate::outbox::OutboxOperation,
    ) -> Result<crate::outbox::OutboxReply> {
        crate::outbox::validate_operation(&mut operation)?;
        let mut counter = LimitedCount {
            remaining: self.config.max_batch_bytes,
        };
        serde_json::to_writer(&mut counter, &operation)
            .map_err(|_| crate::error::invalid("outbox command byte budget"))?;
        let byte_count = self.config.max_batch_bytes - counter.remaining;
        let (reply, receipt) = oneshot::channel();
        {
            let sender = self
                .writer
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?;
            let sender = sender.as_ref().ok_or(ArchiveError::Closed)?;
            self.bytes
                .try_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                    used.checked_add(byte_count)
                        .filter(|total| *total <= self.config.writer_queue_bytes)
                })
                .map_err(|_| ArchiveError::QueueFull { queue: "writer" })?;
            let reservation = Arc::new(ByteReservation {
                bytes: byte_count,
                counter: self.bytes.clone(),
            });
            self.queued.fetch_add(1, Ordering::AcqRel);
            if let Err(error) = sender.try_send(WriterCommand::Outbox {
                operation,
                reservation,
                timeout: self.config.read_timeout,
                reply,
            }) {
                self.queued.fetch_sub(1, Ordering::AcqRel);
                return Err(match error {
                    mpsc::TrySendError::Full(_) => ArchiveError::QueueFull { queue: "writer" },
                    mpsc::TrySendError::Disconnected(_) => ArchiveError::Closed,
                });
            }
        }
        receipt.await.map_err(|_| ArchiveError::WorkerStopped)?
    }

    pub async fn read<T, F>(&self, operation: F) -> Result<T>
    where
        T: Serialize + Send + 'static,
        F: FnOnce(&Connection) -> Result<T> + Send + 'static,
    {
        self.read_with(self.config.read_timeout, false, operation)
            .await
    }
    pub async fn read_with<T, F>(&self, timeout: Duration, binary: bool, operation: F) -> Result<T>
    where
        T: Serialize + Send + 'static,
        F: FnOnce(&Connection) -> Result<T> + Send + 'static,
    {
        if self.closing.load(Ordering::Acquire) {
            return Err(ArchiveError::Closed);
        }
        let maximum = self.config.max_response_bytes;
        self.read_private(timeout, move |db| {
            let value = operation(db)?;
            if !binary {
                check_response_size(&value, maximum)?;
            }
            Ok(value)
        })
        .await
    }

    /// Only non-serializable credential getters use this path; their closure
    /// explicitly charges secret string bytes before returning an owned value.
    pub(crate) async fn read_secret<T, F>(&self, operation: F) -> Result<T>
    where
        T: Send + 'static,
        F: FnOnce(&Connection) -> Result<T> + Send + 'static,
    {
        self.read_private(self.config.read_timeout, move |db| operation(db))
            .await
    }

    /// An exclusively owned READONLY connection can reconfigure its progress
    /// handler for explicit operation cancellation/deadlines. No public SQL API.
    pub(crate) async fn read_mut_with<T, F>(&self, timeout: Duration, operation: F) -> Result<T>
    where
        T: Serialize + Send + 'static,
        F: FnOnce(&mut Connection) -> Result<T> + Send + 'static,
    {
        if self.closing.load(Ordering::Acquire) {
            return Err(ArchiveError::Closed);
        }
        let maximum = self.config.max_response_bytes;
        self.read_private(timeout, move |db| {
            let value = operation(db)?;
            check_response_size(&value, maximum)?;
            Ok(value)
        })
        .await
    }

    async fn read_private<T, F>(&self, timeout: Duration, operation: F) -> Result<T>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection) -> Result<T> + Send + 'static,
    {
        let (reply, receipt) = oneshot::channel();
        let command = ReadCommand {
            timeout,
            reply,
            run: Box::new(move |db| {
                let value = operation(db)?;
                Ok(Box::new(value))
            }),
        };
        {
            let sender = self
                .readers
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?;
            sender
                .as_ref()
                .ok_or(ArchiveError::Closed)?
                .try_send(command)
                .map_err(|error| match error {
                    mpsc::TrySendError::Full(_) => ArchiveError::QueueFull { queue: "reader" },
                    mpsc::TrySendError::Disconnected(_) => ArchiveError::Closed,
                })?;
        }
        receipt
            .await
            .map_err(|_| ArchiveError::WorkerStopped)??
            .downcast::<T>()
            .map(|value| *value)
            .map_err(|_| ArchiveError::WorkerStopped)
    }

    pub fn queue_status(&self) -> crate::QueueStatus {
        crate::QueueStatus {
            queued_operations: self.queued.load(Ordering::Acquire),
            retained_bytes: self.bytes.load(Ordering::Acquire),
            writer_capacity: self.config.writer_queue_capacity,
            writer_byte_limit: self.config.writer_queue_bytes,
            closing: self.closing.load(Ordering::Acquire),
        }
    }
    pub(crate) fn register_live_export(
        &self,
        reader: Weak<crate::snapshot_export::LiveExportOwner>,
    ) -> Result<()> {
        let mut readers = self
            .live_exports
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?;
        if self.closing.load(Ordering::Acquire) {
            return Err(ArchiveError::Closed);
        }
        if self.close_failed.load(Ordering::Acquire) {
            return Err(ArchiveError::WorkerStopped);
        }
        readers.retain(|reader| reader.upgrade().is_some_and(|reader| !reader.is_joined()));
        if readers.len() >= 2 {
            return Err(ArchiveError::QueueFull {
                queue: "live snapshot export",
            });
        }
        readers.push(reader);
        Ok(())
    }
    pub(crate) fn record_cleanup_failure(&self) {
        self.close_failed.store(true, Ordering::Release);
    }
    pub(crate) fn ensure_cleanup_valid(&self) -> Result<()> {
        if self.close_failed.load(Ordering::Acquire) {
            Err(ArchiveError::WorkerStopped)
        } else {
            Ok(())
        }
    }
    pub async fn checkpoint(&self) -> Result<serde_json::Value> {
        if self.closing.load(Ordering::Acquire) {
            return Err(ArchiveError::Closed);
        }
        let (reply, receipt) = oneshot::channel();
        {
            let sender = self
                .writer
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?;
            sender
                .as_ref()
                .ok_or(ArchiveError::Closed)?
                .try_send(WriterCommand::Checkpoint { reply })
                .map_err(|_| ArchiveError::QueueFull { queue: "writer" })?;
        }
        receipt.await.map_err(|_| ArchiveError::WorkerStopped)?
    }
    pub async fn close(self: &Arc<Self>) -> Result<()> {
        self.lifecycle.close();
        if self.closing.swap(true, Ordering::AcqRel) {
            loop {
                let done = self.close_notify.notified();
                if self.closed.load(Ordering::Acquire) {
                    return if self.close_failed.load(Ordering::Acquire) {
                        Err(ArchiveError::WorkerStopped)
                    } else {
                        Ok(())
                    };
                }
                done.await;
            }
        }
        // The drain job owns completion even if its async caller is cancelled.
        let completion = self.clone();
        let result = tokio::task::spawn_blocking(move || {
            let result =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
                    // Registration checks closing while holding this same lock.
                    // Thus pending starts are either refused or included here.
                    let exports = {
                        let mut readers = completion
                            .live_exports
                            .lock()
                            .map_err(|_| ArchiveError::WorkerStopped)?;
                        std::mem::take(&mut *readers)
                            .into_iter()
                            .filter_map(|reader| reader.upgrade())
                            .collect::<Vec<_>>()
                    };
                    for export in &exports {
                        export.request_close();
                    }
                    let mut failed = completion.close_failed.load(Ordering::Acquire);
                    for export in exports {
                        failed |= export.wait_joined_blocking().is_err();
                    }
                    let reader_sender = completion
                        .readers
                        .lock()
                        .map_err(|_| ArchiveError::WorkerStopped)?
                        .take();
                    drop(reader_sender);
                    let sender = completion
                        .writer
                        .lock()
                        .map_err(|_| ArchiveError::WorkerStopped)?
                        .take();
                    let mut handles = std::mem::take(
                        &mut *completion
                            .handles
                            .lock()
                            .map_err(|_| ArchiveError::WorkerStopped)?,
                    );
                    let writer = handles.pop();
                    for handle in handles {
                        failed |= handle.join().is_err();
                    }
                    if let Some(sender) = sender {
                        let _ = sender.send(WriterCommand::Close);
                        drop(sender);
                    }
                    if let Some(handle) = writer {
                        failed |= handle.join().is_err();
                    }
                    if failed || completion.close_failed.load(Ordering::Acquire) {
                        Err(ArchiveError::WorkerStopped)
                    } else {
                        Ok(())
                    }
                }))
                .unwrap_or(Err(ArchiveError::WorkerStopped));
            completion.finish_close(result.is_err());
            result
        })
        .await;
        match result {
            Ok(value) => value,
            Err(_) => {
                self.finish_close(true);
                Err(ArchiveError::WorkerStopped)
            }
        }
    }
    fn finish_close(&self, failed: bool) {
        if failed {
            self.close_failed.store(true, Ordering::Release);
        }
        self.closed.store(true, Ordering::Release);
        self.close_notify.notify_waiters();
    }
}

impl Drop for Inner {
    fn drop(&mut self) {
        self.lifecycle.close();
    }
}

#[cfg(test)]
mod close_tests {
    use super::*;
    #[tokio::test]
    async fn unrepresentable_reader_deadline_returns_an_error_without_stopping_the_reader() {
        let dir = tempfile::tempdir().unwrap();
        let mut config = ArchiveConfig::new(dir.path().join("archive.sqlite"));
        config.readers = 1;
        let inner = Inner::open(config).unwrap();
        assert!(matches!(
            inner.read_mut_with(Duration::MAX, |_| Ok(())).await,
            Err(ArchiveError::Invalid(_))
        ));
        assert_eq!(inner.read(|_| Ok(7_u64)).await.unwrap(), 7);
        inner.close().await.unwrap();
    }

    #[tokio::test]
    async fn repeat_close_never_turns_an_owned_cleanup_failure_into_success() {
        let dir = tempfile::tempdir().unwrap();
        let inner = Inner::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
        let failure = thread::Builder::new()
            .name("ori-owned-synthetic-cleanup-failure".into())
            .spawn(|| panic!("owned synthetic cleanup failure"))
            .unwrap();
        inner.handles.lock().unwrap().insert(0, failure);
        assert!(inner.close().await.is_err());
        assert!(
            inner.close().await.is_err(),
            "a previous cleanup failure was hidden"
        );
    }
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn cancelled_close_caller_does_not_orphan_the_drain_completion() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("archive.sqlite");
        let inner = Inner::open(ArchiveConfig::new(&path)).unwrap();
        let blocker = Connection::open(path).unwrap();
        blocker.execute_batch("BEGIN IMMEDIATE").unwrap();
        let writer = inner.clone();
        let admitted = tokio::spawn(async move {
            writer.apply(vec![Mutation::CreateSession(serde_json::json!({"id":"owned-source","name":"Synthetic closure","created_at":"2026-11-11T00:00:00Z","streams":[]}))]).await
        });
        while inner.queue_status().retained_bytes == 0 {
            tokio::task::yield_now().await;
        }
        let closing = inner.clone();
        let caller = tokio::spawn(async move { closing.close().await });
        while !inner.closing.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
        assert!(matches!(
            inner.apply(vec![Mutation::ResetRuntimeStreamStates]).await,
            Err(ArchiveError::Closed)
        ));
        assert!(matches!(
            inner.read(|_| Ok(1_i64)).await,
            Err(ArchiveError::Closed)
        ));
        caller.abort();
        let _ = caller.await;
        blocker.execute_batch("COMMIT").unwrap();
        admitted.await.unwrap().unwrap();
        tokio::time::timeout(Duration::from_secs(2), inner.close())
            .await
            .expect("cancelled caller orphaned the close completion")
            .unwrap();
    }
}
