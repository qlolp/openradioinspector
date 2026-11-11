//! Cooperative derivative-work pressure and aggregate logical I/O accounting.
//! This never pauses acquisition and does not claim an OS physical I/O limit.
pub use crate::shared_io::SharedIoSpec;
use crate::shared_io::{self, MAX_CHARGE, Reservation, SharedIo};
use crate::worker::Cancellation;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{sync::Notify, time::Instant};

const RATE: u64 = 20 * 1024 * 1024;
const MAX_REGISTRATIONS: usize = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum QosError {
    #[error("acquisition pressure pauses derivative work")]
    Pressure,
    #[error("background registration capacity is busy")]
    Busy,
    #[error("background operation was cancelled or superseded")]
    Cancelled,
    #[error("invalid logical I/O charge")]
    InvalidCharge,
}
#[derive(Clone, Copy, Debug, serde::Serialize)]
pub struct QosStatus {
    pub acquisition_pressure: bool,
    pub registered_work: usize,
    pub logical_bytes: u64,
    pub logical_bytes_per_second: u64,
    pub physical_io_enforced: bool,
    pub cross_process_logical_io: bool,
    pub cleanup_failed: bool,
}
struct State {
    pressure: bool,
    generation: u64,
    serial: u64,
    active: BTreeMap<u64, Cancellation>,
    logical_bytes: u64,
    next_io: Instant,
}
struct Inner {
    state: Mutex<State>,
    changed: Notify,
    shared: Option<Arc<SharedIo>>,
}
#[derive(Clone)]
pub struct BackgroundQos(Arc<Inner>);
impl Default for BackgroundQos {
    fn default() -> Self {
        Self(Arc::new(Inner {
            state: Mutex::new(State {
                pressure: false,
                generation: 0,
                serial: 0,
                active: BTreeMap::new(),
                logical_bytes: 0,
                next_io: Instant::now(),
            }),
            changed: Notify::new(),
            shared: None,
        }))
    }
}
/// Keep this guard until an owned child/task has been JOINED, not only signalled.
pub struct BackgroundWorkGuard {
    inner: Arc<Inner>,
    id: u64,
}
impl Drop for BackgroundWorkGuard {
    fn drop(&mut self) {
        if let Ok(mut state) = self.inner.state.lock() {
            state.active.remove(&self.id);
        }
        self.inner.changed.notify_waiters();
    }
}
impl BackgroundQos {
    /// Create one private cross-process ledger for trusted derivative workers.
    /// Default remains in-memory; unsupported platforms fail this constructor.
    pub fn with_shared_io(private_root: std::path::PathBuf) -> Result<Self, QosError> {
        let shared = Arc::new(SharedIo::new(private_root)?);
        let mut qos = Self::default();
        Arc::get_mut(&mut qos.0)
            .expect("new exclusive QoS instance")
            .shared = Some(shared);
        Ok(qos)
    }
    pub fn worker_io(&self) -> Option<SharedIoSpec> {
        self.0.shared.as_ref().map(|io| io.spec())
    }
    /// Call only after service children/tasks can be cancelled and joined.
    /// Cancellation of a waiter cannot undo pressure or detach file cleanup.
    pub async fn close_shared_io(&self) -> Result<(), QosError> {
        let Some(shared) = self.0.shared.clone() else {
            return Ok(());
        };
        self.pressure(true).await?;
        tokio::task::spawn_blocking(move || shared.close())
            .await
            .map_err(|_| QosError::InvalidCharge)?
    }
    fn check(&self, cancel: &Cancellation) -> Result<(), QosError> {
        if cancel.is_cancelled() {
            return Err(QosError::Cancelled);
        }
        if self
            .0
            .state
            .lock()
            .map_err(|_| QosError::Cancelled)?
            .pressure
        {
            return Err(QosError::Pressure);
        }
        if self.0.shared.as_ref().is_some_and(|io| !io.active()) {
            return Err(QosError::InvalidCharge);
        }
        Ok(())
    }
    pub fn register(&self, cancel: Cancellation) -> Result<BackgroundWorkGuard, QosError> {
        if self.0.shared.as_ref().is_some_and(|io| !io.active()) {
            return Err(QosError::InvalidCharge);
        }
        let mut state = self.0.state.lock().map_err(|_| QosError::Cancelled)?;
        if state.pressure {
            return Err(QosError::Pressure);
        }
        if state.active.len() >= MAX_REGISTRATIONS {
            return Err(QosError::Busy);
        }
        state.serial = state.serial.checked_add(1).ok_or(QosError::Busy)?;
        let id = state.serial;
        state.active.insert(id, cancel);
        Ok(BackgroundWorkGuard {
            inner: self.0.clone(),
            id,
        })
    }
    pub async fn pressure(&self, active: bool) -> Result<(), QosError> {
        let (generation, cancellations) = {
            let mut state = self.0.state.lock().map_err(|_| QosError::Cancelled)?;
            if state.pressure != active {
                state.pressure = active;
                state.generation = state.generation.wrapping_add(1);
            }
            (
                state.generation,
                state.active.values().cloned().collect::<Vec<_>>(),
            )
        };
        self.0.changed.notify_waiters();
        if !active {
            return Ok(());
        }
        for cancel in cancellations {
            cancel.cancel();
        }
        loop {
            let changed = self.0.changed.notified();
            {
                let state = self.0.state.lock().map_err(|_| QosError::Cancelled)?;
                if state.generation != generation {
                    return Err(QosError::Cancelled);
                }
                if state.active.is_empty() {
                    return Ok(());
                }
            }
            changed.await;
        }
    }
    pub fn status(&self) -> QosStatus {
        match self.0.state.lock() {
            Ok(state) => QosStatus {
                acquisition_pressure: state.pressure,
                registered_work: state.active.len(),
                logical_bytes: self
                    .0
                    .shared
                    .as_ref()
                    .map_or(state.logical_bytes, |io| io.total()),
                logical_bytes_per_second: RATE,
                physical_io_enforced: false,
                cross_process_logical_io: self.0.shared.as_ref().is_some_and(|io| io.active()),
                cleanup_failed: self.0.shared.as_ref().is_some_and(|io| io.failed()),
            },
            Err(_) => QosStatus {
                acquisition_pressure: true,
                registered_work: MAX_REGISTRATIONS,
                logical_bytes: 0,
                logical_bytes_per_second: RATE,
                physical_io_enforced: false,
                cross_process_logical_io: false,
                cleanup_failed: self.0.shared.as_ref().is_some_and(|io| io.failed()),
            },
        }
    }
    /// Call before a bounded logical read/write. Cancelled reservations remain
    /// charged conservatively; pressure never causes a catch-up I/O burst.
    pub async fn account(&self, bytes: u64, cancel: &Cancellation) -> Result<(), QosError> {
        if bytes > MAX_CHARGE {
            return Err(QosError::InvalidCharge);
        }
        if let Some(shared) = self.0.shared.clone() {
            let lock_deadline = std::time::Instant::now() + Duration::from_secs(2);
            let until = loop {
                self.check(cancel)?;
                let ledger = shared.clone();
                match tokio::task::spawn_blocking(move || ledger.reserve(bytes))
                    .await
                    .map_err(|_| QosError::InvalidCharge)??
                {
                    Reservation::Until(until) => break until,
                    Reservation::Busy => {
                        if std::time::Instant::now() >= lock_deadline {
                            shared.fail();
                            return Err(QosError::InvalidCharge);
                        }
                        tokio::time::sleep(Duration::from_millis(10)).await;
                    }
                }
            };
            loop {
                self.check(cancel)?;
                let now = shared_io::now_ns()?;
                if now >= until {
                    return Ok(());
                }
                tokio::time::sleep(
                    Duration::from_nanos(until - now).min(Duration::from_millis(10)),
                )
                .await;
            }
        }
        if cancel.is_cancelled() {
            return Err(QosError::Cancelled);
        }
        let end = {
            let mut state = self.0.state.lock().map_err(|_| QosError::Cancelled)?;
            if state.pressure {
                return Err(QosError::Pressure);
            }
            let start = state.next_io.max(Instant::now());
            let duration = Duration::from_nanos(bytes.saturating_mul(1_000_000_000).div_ceil(RATE));
            state.next_io = start + duration;
            state.logical_bytes = state.logical_bytes.saturating_add(bytes);
            state.next_io
        };
        loop {
            if cancel.is_cancelled() {
                return Err(QosError::Cancelled);
            }
            if self.status().acquisition_pressure {
                return Err(QosError::Pressure);
            }
            let now = Instant::now();
            if now >= end {
                return Ok(());
            }
            tokio::time::sleep((end - now).min(Duration::from_millis(10))).await;
        }
    }
    /// The same budget for dedicated owned SQL/file threads, never an async
    /// executor thread. Blocking flock is intentionally never used.
    pub fn account_blocking(&self, bytes: u64, cancel: &Cancellation) -> Result<(), QosError> {
        if bytes > MAX_CHARGE {
            return Err(QosError::InvalidCharge);
        }
        if let Some(shared) = &self.0.shared {
            let lock_deadline = std::time::Instant::now() + Duration::from_secs(2);
            let until = loop {
                self.check(cancel)?;
                match shared.reserve(bytes)? {
                    Reservation::Until(until) => break until,
                    Reservation::Busy => {
                        if std::time::Instant::now() >= lock_deadline {
                            shared.fail();
                            return Err(QosError::InvalidCharge);
                        }
                        std::thread::sleep(Duration::from_millis(10));
                    }
                }
            };
            loop {
                self.check(cancel)?;
                let now = shared_io::now_ns()?;
                if now >= until {
                    return Ok(());
                }
                std::thread::sleep(
                    Duration::from_nanos(until - now).min(Duration::from_millis(10)),
                );
            }
        }
        self.check(cancel)?;
        let end = {
            let mut state = self.0.state.lock().map_err(|_| QosError::Cancelled)?;
            let start = state.next_io.max(Instant::now());
            state.next_io =
                start + Duration::from_nanos(bytes.saturating_mul(1_000_000_000).div_ceil(RATE));
            state.logical_bytes = state.logical_bytes.saturating_add(bytes);
            state.next_io.into_std()
        };
        loop {
            self.check(cancel)?;
            let now = std::time::Instant::now();
            if now >= end {
                return Ok(());
            }
            std::thread::sleep((end - now).min(Duration::from_millis(10)));
        }
    }
}
