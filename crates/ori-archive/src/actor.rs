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
        Arc, Mutex,
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
    Close,
}
type ReadResult = Box<dyn Any + Send>;
type ReadFn = Box<dyn FnOnce(&Connection) -> Result<ReadResult> + Send>;
struct ReadCommand {
    run: ReadFn,
    timeout: Duration,
    reply: oneshot::Sender<Result<ReadResult>>,
}

pub(crate) struct Inner {
    pub config: ArchiveConfig,
    pub writer_synchronous: i64,
    writer: Mutex<Option<SyncSender<WriterCommand>>>,
    readers: Mutex<Option<SyncSender<ReadCommand>>>,
    handles: Mutex<Vec<JoinHandle<()>>>,
    bytes: Arc<AtomicUsize>,
    queued: Arc<AtomicUsize>,
    closing: AtomicBool,
    closed: AtomicBool,
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

impl Inner {
    pub fn open(config: ArchiveConfig) -> Result<Arc<Self>> {
        config.validate()?;
        let lease = schema::ProcessWriterLease::acquire(&config.path)?;
        let mut connection = schema::writer(&config)?;
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
        let writer_handle=thread::Builder::new().name("ori-canonical-writer".into()).spawn(move||{
            let _lease=lease;
            while let Ok(command)=writer_rx.recv(){match command{
                WriterCommand::Apply{mutations,reservation,reply}=>{
                    pending.fetch_sub(1,Ordering::AcqRel);
                    let outcome=writer::apply(&mut connection,mutations,capacity,capture_budget).map(|mut value|{value.reservation=Some(reservation);value});
                    if let Ok(committed)=&outcome && let (Some(first),Some(last))=(committed.first_sequence,committed.last_sequence) {
                        // The canonical transaction has already committed. Notices contain no raw payloads.
                        let _=publication.send(CommitNotice{first_sequence:first,last_sequence:last});
                    }
                    let _=reply.send(outcome);
                },
                WriterCommand::Checkpoint{reply}=>{let outcome=connection.query_row("PRAGMA wal_checkpoint(PASSIVE)",[],|row|Ok(serde_json::json!({"busy":row.get::<_,i64>(0)?,"wal_frames":row.get::<_,i64>(1)?,"checkpointed_frames":row.get::<_,i64>(2)?}))).map_err(Into::into);let _=reply.send(outcome);},
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
                            let deadline = Instant::now() + command.timeout;
                            if let Err(error) = connection
                                .progress_handler(10000, Some(move || Instant::now() > deadline))
                            {
                                let _ = command.reply.send(Err(error.into()));
                                continue;
                            }
                            let outcome = connection
                                .execute_batch("BEGIN")
                                .map_err(Into::into)
                                .and_then(|_| (command.run)(&connection));
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
            config,
            writer_synchronous,
            writer: Mutex::new(Some(writer_tx)),
            readers: Mutex::new(Some(reader_tx)),
            handles: Mutex::new(handles),
            bytes,
            queued,
            closing: AtomicBool::new(false),
            closed: AtomicBool::new(false),
            close_notify: Notify::new(),
            backup_busy: AtomicBool::new(false),
            notices,
        }))
    }

    pub async fn apply(&self, mut mutations: Vec<Mutation>) -> Result<CommittedBatch> {
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
        let maximum = self.config.max_response_bytes;
        let (reply, receipt) = oneshot::channel();
        let command = ReadCommand {
            timeout,
            reply,
            run: Box::new(move |db| {
                let value = operation(db)?;
                if !binary {
                    serde_json::to_writer(LimitedCount { remaining: maximum }, &value)
                        .map_err(|_| ArchiveError::QueryBudget)?;
                }
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
    pub async fn checkpoint(&self) -> Result<serde_json::Value> {
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
        if self.closing.swap(true, Ordering::AcqRel) {
            loop {
                let done = self.close_notify.notified();
                if self.closed.load(Ordering::Acquire) {
                    return Ok(());
                }
                done.await;
            }
        }
        let reader_sender = self
            .readers
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?
            .take();
        drop(reader_sender);
        let sender = self
            .writer
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?
            .take();
        let handles = std::mem::take(
            &mut *self
                .handles
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?,
        );
        let result = tokio::task::spawn_blocking(move || {
            // Read snapshots finish before the final writer checkpoint. The writer continues draining meanwhile.
            let mut handles = handles;
            let writer = handles.pop();
            for handle in handles {
                handle.join().map_err(|_| ArchiveError::WorkerStopped)?;
            }
            if let Some(sender) = sender {
                let _ = sender.send(WriterCommand::Close);
                drop(sender);
            }
            if let Some(handle) = writer {
                handle.join().map_err(|_| ArchiveError::WorkerStopped)?;
            }
            Ok(())
        })
        .await
        .unwrap_or(Err(ArchiveError::WorkerStopped));
        self.closed.store(true, Ordering::Release);
        self.close_notify.notify_waiters();
        result
    }
}
