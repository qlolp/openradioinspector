//! Completion belongs to a reaper created before any reader I/O can start.
use super::{Failure, Ready, Reply};
use crate::{ArchiveError, Result, actor};
use std::{
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc::SyncSender,
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};
use tokio::sync::Notify;

enum Start {
    Pending,
    Reader(JoinHandle<()>),
    NoReader,
}

/// Archive stores a Weak to this lifecycle, not to the public client handle.
/// The reaper retains it even after the last public handle has been dropped.
pub(crate) struct Shared {
    pub(super) ready: Mutex<Option<std::result::Result<Ready, Failure>>>,
    pub(super) ready_changed: Notify,
    pub(super) closing: AtomicBool,
    pub(super) cancelled: AtomicBool,
    pub(super) closed: AtomicBool,
    pub(super) failed: AtomicBool,
    pub(super) closed_changed: Notify,
    pub(super) deadline: Mutex<Instant>,
    pub(super) sender: Mutex<Option<SyncSender<Reply>>>,
    archive_lease: Mutex<Option<Arc<actor::Inner>>>,
    start: Mutex<Start>,
    started: Condvar,
    joined: Mutex<bool>,
    joined_changed: Condvar,
    #[cfg(test)]
    pub(super) after_snapshot: Mutex<Option<Box<dyn FnOnce() + Send>>>,
}
impl Shared {
    pub(super) fn new(
        timeout: Duration,
        sender: SyncSender<Reply>,
        archive_lease: Option<Arc<actor::Inner>>,
    ) -> Arc<Self> {
        Arc::new(Self {
            ready: Mutex::new(None),
            ready_changed: Notify::new(),
            closing: AtomicBool::new(false),
            cancelled: AtomicBool::new(false),
            closed: AtomicBool::new(false),
            failed: AtomicBool::new(false),
            closed_changed: Notify::new(),
            deadline: Mutex::new(Instant::now() + timeout),
            sender: Mutex::new(Some(sender)),
            archive_lease: Mutex::new(archive_lease),
            start: Mutex::new(Start::Pending),
            started: Condvar::new(),
            joined: Mutex::new(false),
            joined_changed: Condvar::new(),
            #[cfg(test)]
            after_snapshot: Mutex::new(None),
        })
    }
    pub(super) fn publish(&self, result: std::result::Result<Ready, Failure>) {
        if let Ok(mut ready) = self.ready.lock()
            && ready.is_none()
        {
            *ready = Some(result);
        }
        self.ready_changed.notify_waiters();
    }
    pub(crate) fn is_joined(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }
    pub(crate) fn request_close(&self) {
        self.closing.store(true, Ordering::Release);
        self.cancelled.store(true, Ordering::Release);
        self.sender.lock().unwrap_or_else(|e| e.into_inner()).take();
        self.ready_changed.notify_waiters();
    }
    pub(super) fn install_reader(&self, handle: JoinHandle<()>) {
        *self.start.lock().unwrap_or_else(|e| e.into_inner()) = Start::Reader(handle);
        self.started.notify_one();
    }
    pub(super) fn no_reader(&self) {
        *self.start.lock().unwrap_or_else(|e| e.into_inner()) = Start::NoReader;
        self.started.notify_one();
    }
    pub(super) fn reap(self: Arc<Self>) {
        let reader = {
            let mut start = self.start.lock().unwrap_or_else(|e| e.into_inner());
            while matches!(*start, Start::Pending) {
                start = self.started.wait(start).unwrap_or_else(|e| e.into_inner());
            }
            std::mem::replace(&mut *start, Start::NoReader)
        };
        let failed = match reader {
            Start::Reader(handle) => handle.join().is_err(),
            Start::NoReader => false,
            Start::Pending => unreachable!(),
        };
        self.finish(failed);
    }
    /// Only the reaper (or a failed reaper launch with no child) calls finish.
    pub(super) fn finish(&self, failed: bool) {
        if failed {
            self.failed.store(true, Ordering::Release);
            self.publish(Err(Failure::Worker));
        }
        let lease = self
            .archive_lease
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();
        if self.failed.load(Ordering::Acquire)
            && let Some(archive) = &lease
        {
            archive.record_cleanup_failure();
        }
        // Publish completion before releasing the final possible Archive Arc.
        // Any reentrant Archive cleanup sees an already joined reader, never
        // tries to join this reaper, and cannot release a writer ahead of I/O.
        *self.joined.lock().unwrap_or_else(|e| e.into_inner()) = true;
        self.closed.store(true, Ordering::Release);
        self.joined_changed.notify_all();
        self.closed_changed.notify_waiters();
        self.ready_changed.notify_waiters();
        drop(lease);
    }
    pub(crate) fn wait_joined_blocking(&self) -> Result<()> {
        let mut joined = self
            .joined
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?;
        while !*joined {
            joined = self
                .joined_changed
                .wait(joined)
                .map_err(|_| ArchiveError::WorkerStopped)?;
        }
        self.outcome()
    }
    fn outcome(&self) -> Result<()> {
        if self.failed.load(Ordering::Acquire) {
            Err(ArchiveError::WorkerStopped)
        } else {
            Ok(())
        }
    }
    pub(super) async fn close(&self) -> Result<()> {
        self.request_close();
        loop {
            let changed = self.closed_changed.notified();
            if self.closed.load(Ordering::Acquire) {
                return self.outcome();
            }
            changed.await;
        }
    }
}
