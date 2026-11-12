//! One owned maintenance executor; the canonical commit hook only wakes it.
use crate::{ArchiveError, Result};
use rusqlite::{Connection, config::DbConfig};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering},
        mpsc,
    },
    thread::{self, JoinHandle, Thread},
    time::{Duration, Instant},
};
use tokio::sync::oneshot;
#[cfg(test)]
mod owner_tests;
#[cfg(test)]
#[path = "vfs_tests.rs"]
mod tests;
mod vfs;

const AUTO_FRAMES: i32 = 1000;
const RETRY: Duration = Duration::from_secs(1);
type Request = oneshot::Sender<Result<Value>>;

/// Admission is attempted once per owned PASSIVE checkpoint. `None` defers
/// maintenance while the embedding's background policy is busy or pressured.
pub trait CheckpointBudget: Send + Sync {
    fn try_begin(&self) -> Result<Option<Arc<dyn CheckpointIoPermit>>>;
}

/// The actual native connection retains this permit until it has closed.
pub trait CheckpointIoPermit: Send + Sync {
    fn account(&self, bytes: u64) -> Result<()>;
    fn cancel(&self);
}

pub(crate) struct Control {
    requests: mpsc::SyncSender<Request>,
    thread: OnceLock<Thread>,
    frames: AtomicI32,
    commits: AtomicU64,
    pub stopping: Arc<AtomicBool>,
    failed: AtomicBool,
    active: Mutex<Option<Arc<dyn CheckpointIoPermit>>>,
}
impl Control {
    pub fn new() -> (Arc<Self>, mpsc::Receiver<Request>) {
        let (requests, received) = mpsc::sync_channel(1);
        (
            Arc::new(Self {
                requests,
                thread: OnceLock::new(),
                frames: AtomicI32::new(0),
                commits: AtomicU64::new(0),
                stopping: Arc::new(AtomicBool::new(false)),
                failed: AtomicBool::new(false),
                active: Mutex::new(None),
            }),
            received,
        )
    }
    pub fn install(self: &Arc<Self>, connection: &mut Connection) -> Result<()> {
        connection.set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true)?;
        connection.pragma_update(None, "wal_autocheckpoint", 0)?;
        let control = self.clone();
        // This replaces SQLite's synchronous auto-checkpoint hook. Never perform
        // file I/O, budget admission, or checkpoint work from this callback.
        connection.wal_hook(Some(move |_: &mut rusqlite::hooks::Wal, frames| {
            control.frames.store(frames, Ordering::Release);
            control.commits.fetch_add(1, Ordering::Release);
            control.wake();
            Ok(())
        }))?;
        Ok(())
    }
    fn wake(&self) {
        if let Some(thread) = self.thread.get() {
            thread.unpark();
        }
    }
    pub fn failed(&self) -> bool {
        self.failed.load(Ordering::Acquire)
    }
    pub fn request_stop(&self) {
        self.stopping.store(true, Ordering::Release);
        let permit = self.active.lock().ok().and_then(|active| active.clone());
        if let Some(permit) = permit
            && std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| permit.cancel())).is_err()
        {
            self.failed.store(true, Ordering::Release);
        }
        self.wake();
    }
    pub fn enqueue(&self, reply: Request) {
        if self.failed() {
            let _ = reply.send(Err(ArchiveError::WorkerStopped));
            return;
        }
        if self.stopping.load(Ordering::Acquire) {
            let _ = reply.send(Err(ArchiveError::Closed));
            return;
        }
        match self.requests.try_send(reply) {
            Ok(()) => self.wake(),
            Err(mpsc::TrySendError::Full(reply)) => {
                let _ = reply.send(Err(ArchiveError::QueueFull {
                    queue: "checkpoint",
                }));
            }
            Err(mpsc::TrySendError::Disconnected(reply)) => {
                let _ = reply.send(Err(ArchiveError::WorkerStopped));
            }
        }
    }
}

pub(crate) struct Owner {
    control: Arc<Control>,
    handle: Option<JoinHandle<()>>,
}
impl Owner {
    pub fn start(
        control: Arc<Control>,
        requests: mpsc::Receiver<Request>,
        path: PathBuf,
        budget: Arc<dyn CheckpointBudget>,
    ) -> Result<Self> {
        let owned = control.clone();
        let handle = thread::Builder::new()
            .name("ori-checkpoint-maintenance".into())
            .spawn(move || {
                let _ = owned.thread.set(thread::current());
                let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    run(&owned, &requests, &path, budget)
                }));
                if outcome.is_err() {
                    owned.failed.store(true, Ordering::Release);
                }
                // The real pass has unwound/closed before its registration drops.
                if let Ok(mut active) = owned.active.lock() {
                    active.take();
                }
                // Refuse every outstanding observer even if the worker panicked.
                while let Ok(reply) = requests.try_recv() {
                    let _ = reply.send(Err(if owned.failed() {
                        ArchiveError::WorkerStopped
                    } else {
                        ArchiveError::Closed
                    }));
                }
            })?;
        Ok(Self {
            control,
            handle: Some(handle),
        })
    }
    /// Called by the canonical writer before its connection/lease can drop.
    pub fn join(mut self) {
        self.control.request_stop();
        if self
            .handle
            .take()
            .is_some_and(|handle| handle.join().is_err())
        {
            self.control.failed.store(true, Ordering::Release);
        }
    }
}
impl Drop for Owner {
    fn drop(&mut self) {
        self.control.request_stop();
        if let Some(handle) = self.handle.take() {
            // Only the writer/startup owner owns this handle, never an async
            // result observer. Its actual JOIN precedes writer lease release.
            if handle.join().is_err() {
                self.control.failed.store(true, Ordering::Release);
            }
        }
    }
}

fn run(
    control: &Arc<Control>,
    requests: &mpsc::Receiver<Request>,
    path: &Path,
    budget: Arc<dyn CheckpointBudget>,
) {
    let mut next = Instant::now();
    let mut completed = None;
    while !control.stopping.load(Ordering::Acquire) {
        let reply = requests.try_recv().ok();
        let generation = control.commits.load(Ordering::Acquire);
        let frames = control.frames.load(Ordering::Acquire);
        if reply.is_none()
            && (control.failed()
                || frames < AUTO_FRAMES
                || completed == Some(generation)
                || Instant::now() < next)
        {
            let delay = if control.failed() || frames < AUTO_FRAMES || completed == Some(generation)
            {
                RETRY
            } else {
                next.saturating_duration_since(Instant::now())
            };
            thread::park_timeout(delay);
            continue;
        }
        if control.failed() {
            if let Some(reply) = reply {
                let _ = reply.send(Err(ArchiveError::WorkerStopped));
            }
            continue;
        }
        let admitted = budget.try_begin();
        let result = match admitted {
            Ok(Some(permit)) => {
                if let Ok(mut active) = control.active.lock() {
                    *active = Some(permit.clone());
                } else {
                    control.failed.store(true, Ordering::Release);
                }
                let result = if control.stopping.load(Ordering::Acquire) {
                    permit.cancel();
                    Err(ArchiveError::Closed)
                } else {
                    passive(path, permit, control.stopping.clone())
                };
                if let Ok(mut active) = control.active.lock() {
                    active.take();
                }
                result
            }
            Ok(None) => {
                if let Some(reply) = reply {
                    let _ = reply.send(Err(ArchiveError::QueryBudget));
                }
                next = Instant::now() + RETRY;
                continue;
            }
            Err(error) => Err(error),
        };
        match &result {
            Ok(value)
                if value["busy"] == 0 && value["wal_frames"] == value["checkpointed_frames"] =>
            {
                // A WAL reset can reuse an earlier frame count. Record the
                // observed commit generation instead of clearing that count,
                // so completion cannot erase a newer hook notification.
                completed = Some(generation);
            }
            Err(ArchiveError::Closed) => {} // pressure/owned close: retryable
            Err(ArchiveError::Sql(rusqlite::Error::SqliteFailure(error, _)))
                if matches!(
                    error.code,
                    rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
                ) => {}
            Err(_) => {
                control.failed.store(true, Ordering::Release);
            }
            _ => {} // a pinned reader legitimately leaves work for later
        }
        next = Instant::now() + RETRY;
        if let Some(reply) = reply {
            let _ = reply.send(result);
        }
    }
}

fn passive(
    path: &Path,
    permit: Arc<dyn CheckpointIoPermit>,
    stopping: Arc<AtomicBool>,
) -> Result<Value> {
    let vfs = vfs::Vfs::new(permit, stopping)?;
    let result = match vfs.connect(path) {
        Ok(connection) => {
            let value = connection
                .query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |row| {
                    Ok(json!({
                        "busy": row.get::<_, i64>(0)?, "wal_frames": row.get::<_, i64>(1)?,
                        "checkpointed_frames": row.get::<_, i64>(2)?
                    }))
                })
                .map_err(ArchiveError::from);
            match connection.close() {
                Ok(()) => value,
                Err((connection, error)) => {
                    drop(connection);
                    Err(error.into())
                }
            }
        }
        Err(error) => Err(error),
    };
    let denied = vfs.take_failure();
    let closed = vfs.finish();
    if let Some(error) = denied {
        if matches!(&error, ArchiveError::Closed) {
            // A real VFS cleanup failure outranks a retryable cancellation.
            closed?;
        }
        return Err(error);
    }
    closed?;
    result
}

pub(crate) struct LocalBudget(Arc<Mutex<Instant>>);
impl Default for LocalBudget {
    fn default() -> Self {
        Self(Arc::new(Mutex::new(Instant::now())))
    }
}
struct LocalPermit {
    timeline: Arc<Mutex<Instant>>,
    cancelled: AtomicBool,
}
impl CheckpointBudget for LocalBudget {
    fn try_begin(&self) -> Result<Option<Arc<dyn CheckpointIoPermit>>> {
        Ok(Some(Arc::new(LocalPermit {
            timeline: self.0.clone(),
            cancelled: AtomicBool::new(false),
        })))
    }
}
impl CheckpointIoPermit for LocalPermit {
    fn account(&self, bytes: u64) -> Result<()> {
        if self.cancelled.load(Ordering::Acquire) {
            return Err(ArchiveError::Closed);
        }
        if bytes > vfs::MAX_IO_BYTES as u64 {
            return Err(ArchiveError::QueryBudget);
        }
        let until = {
            let mut next = self
                .timeline
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?;
            let start = (*next).max(Instant::now());
            *next = start
                .checked_add(Duration::from_nanos(
                    bytes * 1_000_000_000 / (20 * 1024 * 1024),
                ))
                .ok_or(ArchiveError::QueryBudget)?;
            *next
        };
        loop {
            if self.cancelled.load(Ordering::Acquire) {
                return Err(ArchiveError::Closed);
            }
            let remaining = until.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(());
            }
            thread::sleep(remaining.min(Duration::from_millis(10)));
        }
    }
    fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
    }
}
