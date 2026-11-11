//! Writer-owned lifecycle epochs. No raw state or caller-supplied string is a proof.
use crate::{ArchiveError, Mutation, Result};
use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};

const PENDING: u64 = 1;
const RUNNING: u64 = 2;
const STEP: u64 = 4;
const MAX_CELLS: usize = 1024;
const MAX_KEY_BYTES: usize = 1024 * 1024;

#[derive(Clone, Copy, PartialEq, Eq)]
struct Signature {
    digest: [u8; 32],
    running: bool,
}
impl Signature {
    fn read(db: &Connection, id: &str) -> Result<Option<Self>> {
        Ok(db
            .query_row(
                "SELECT state,created_at,started_at,ended_at FROM sessions WHERE id=?",
                [id],
                |row| {
                    let state = row.get::<_, String>(0)?;
                    let fields = [
                        Some(state.clone()),
                        Some(row.get::<_, String>(1)?),
                        row.get(2)?,
                        row.get(3)?,
                    ];
                    let mut hash = Sha256::new();
                    hash.update((id.len() as u64).to_le_bytes());
                    hash.update(id.as_bytes());
                    for field in fields {
                        match field {
                            Some(value) => {
                                hash.update([1]);
                                hash.update((value.len() as u64).to_le_bytes());
                                hash.update(value.as_bytes());
                            }
                            None => hash.update([0]),
                        }
                    }
                    let mut digest = [0; 32];
                    digest.copy_from_slice(&hash.finalize());
                    Ok(Self {
                        digest,
                        running: state == "running",
                    })
                },
            )
            .optional()?)
    }
}

struct Cell {
    key: Arc<str>,
    owner: Weak<Shared>,
    word: AtomicU64,
}
impl Drop for Cell {
    fn drop(&mut self) {
        if let Some(owner) = self.owner.upgrade()
            && let Ok(mut state) = owner.state.lock()
            && state
                .cells
                .get(self.key.as_ref())
                .is_some_and(|entry| std::ptr::eq(entry.cell.as_ptr(), self))
        {
            state.cells.remove(self.key.as_ref());
            state.key_bytes = state.key_bytes.saturating_sub(self.key.len());
        }
    }
}
struct Entry {
    cell: Weak<Cell>,
    signature: Signature,
}
#[derive(Default)]
struct State {
    cells: BTreeMap<Arc<str>, Entry>,
    key_bytes: usize,
}
struct Shared {
    closed: AtomicBool,
    state: Mutex<State>,
    #[cfg(test)]
    hooks: Mutex<Hooks>,
}

/// An immutable witness captured during setup, not a refreshable policy or I/O lease.
/// Draft enrollment retains its cell but does not authorize a running dispatch.
#[derive(Clone)]
pub struct SessionLifecycleReceipt {
    cell: Arc<Cell>,
    word: u64,
}
impl SessionLifecycleReceipt {
    /// Cache-only point-in-time check. Call at every original execution boundary.
    pub fn check(&self) -> bool {
        let Some(owner) = self.cell.owner.upgrade() else {
            return false;
        };
        !owner.closed.load(Ordering::SeqCst)
            && self.word & (PENDING | RUNNING) == RUNNING
            && self.cell.word.load(Ordering::SeqCst) == self.word
            && !owner.closed.load(Ordering::SeqCst)
    }
}

#[derive(Clone)]
pub(crate) struct Registry {
    shared: Arc<Shared>,
}
impl Default for Registry {
    fn default() -> Self {
        Self {
            shared: Arc::new(Shared {
                closed: AtomicBool::new(false),
                state: Mutex::new(State::default()),
                #[cfg(test)]
                hooks: Mutex::new(Hooks::default()),
            }),
        }
    }
}
impl Registry {
    pub(crate) fn close(&self) {
        self.shared.closed.store(true, Ordering::SeqCst);
    }
    pub(crate) fn is_closed(&self) -> bool {
        self.shared.closed.load(Ordering::SeqCst)
    }
    fn enrolled(&self, id: &str) -> Result<Option<Arc<Cell>>> {
        // Never drop the last upgraded Cell while holding the registry: its
        // destructor removes the matching weak entry under that same lock.
        let weak = self
            .shared
            .state
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?
            .cells
            .get(id)
            .map(|entry| entry.cell.clone());
        Ok(weak.and_then(|weak| weak.upgrade()))
    }
    pub(crate) fn cached(&self, id: &str) -> Option<SessionLifecycleReceipt> {
        if self.is_closed() {
            return None;
        }
        let cell = self.enrolled(id).ok()??;
        let word = cell.word.load(Ordering::SeqCst);
        if word & (PENDING | RUNNING) != RUNNING || self.is_closed() {
            return None;
        }
        Some(SessionLifecycleReceipt { cell, word })
    }
    pub(crate) fn capture(&self, db: &Connection, id: &str) -> Result<SessionLifecycleReceipt> {
        if self.is_closed() {
            return Err(ArchiveError::Closed);
        }
        let signature = Signature::read(db, id)?.ok_or(ArchiveError::NotFound)?;
        if let Some(cell) = self.enrolled(id)? {
            let word = cell.word.load(Ordering::SeqCst);
            if word & PENDING != 0 {
                return Err(ArchiveError::WorkerStopped);
            }
            let matches = self
                .shared
                .state
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?
                .cells
                .get(id)
                .is_some_and(|entry| entry.signature == signature);
            if !matches {
                // An out-of-band SQLite writer is outside Archive ownership.
                // Never bless its change as the old canonical generation.
                cell.word.fetch_or(PENDING, Ordering::SeqCst);
                return Err(ArchiveError::WorkerStopped);
            }
            if self.is_closed() {
                return Err(ArchiveError::Closed);
            }
            return Ok(SessionLifecycleReceipt { cell, word });
        }
        let mut state = self
            .shared
            .state
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?;
        state.cells.retain(|_, entry| entry.cell.strong_count() > 0);
        state.key_bytes = state.cells.keys().map(|id| id.len()).sum();
        if self.is_closed() {
            return Err(ArchiveError::Closed);
        }
        if state.cells.len() >= MAX_CELLS
            || id.len() > MAX_KEY_BYTES.saturating_sub(state.key_bytes)
        {
            return Err(ArchiveError::QueueFull {
                queue: "session lifecycle receipts",
            });
        }
        let key: Arc<str> = id.into();
        let word = STEP | if signature.running { RUNNING } else { 0 };
        let cell = Arc::new(Cell {
            key: key.clone(),
            owner: Arc::downgrade(&self.shared),
            word: AtomicU64::new(word),
        });
        state.key_bytes += key.len();
        state.cells.insert(
            key,
            Entry {
                cell: Arc::downgrade(&cell),
                signature,
            },
        );
        drop(state);
        if self.is_closed() {
            return Err(ArchiveError::Closed);
        }
        Ok(SessionLifecycleReceipt { cell, word })
    }
    pub(crate) fn before(&self, db: &Connection, mutation: &Mutation) -> Result<Option<Before>> {
        let value = match mutation {
            Mutation::CreateSession(value)
            | Mutation::SaveSession { value, .. }
            | Mutation::StopSession { value, .. } => value,
            _ => return Ok(None),
        };
        let id = value["id"].as_str().expect("validated session ID");
        let Some(cell) = self.enrolled(id)? else {
            return Ok(None);
        };
        let signature = Signature::read(db, id)?;
        Ok(Some(Before { cell, signature }))
    }
    pub(crate) fn prepare(&self, changes: Changes) -> Result<Pending> {
        if changes.0.is_empty() {
            return Ok(Pending {
                registry: self.clone(),
                items: Vec::new(),
            });
        }
        let state = self
            .shared
            .state
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?;
        let mut items = Vec::with_capacity(changes.0.len());
        for change in changes.0.into_values() {
            let previous = change.cell.word.load(Ordering::SeqCst);
            if previous & PENDING != 0
                || !state
                    .cells
                    .get(change.cell.key.as_ref())
                    .is_some_and(|entry| Some(entry.signature) == change.original)
            {
                drop(state);
                return Err(ArchiveError::WorkerStopped);
            }
            let Some(generation) = (previous & !(PENDING | RUNNING)).checked_add(STEP) else {
                drop(state);
                return Err(ArchiveError::WorkerStopped);
            };
            let next = generation
                | if change.latest.is_some_and(|signature| signature.running) {
                    RUNNING
                } else {
                    0
                };
            items.push(PendingItem {
                change,
                previous,
                next,
            });
        }
        for item in &items {
            item.change
                .cell
                .word
                .store(item.previous | PENDING, Ordering::SeqCst);
        }
        drop(state);
        Ok(Pending {
            registry: self.clone(),
            items,
        })
    }
}

pub(crate) struct Before {
    cell: Arc<Cell>,
    signature: Option<Signature>,
}
struct Change {
    cell: Arc<Cell>,
    original: Option<Signature>,
    latest: Option<Signature>,
}
#[derive(Default)]
pub(crate) struct Changes(BTreeMap<Arc<str>, Change>);
impl Changes {
    pub(crate) fn after(&mut self, db: &Connection, before: Option<Before>) -> Result<()> {
        let Some(before) = before else { return Ok(()) };
        let latest = Signature::read(db, &before.cell.key)?;
        if before.signature == latest {
            return Ok(());
        }
        // Keep a transition even when a later mutation returns its values to
        // the original state within this same successful transaction.
        self.0
            .entry(before.cell.key.clone())
            .and_modify(|change| change.latest = latest)
            .or_insert(Change {
                cell: before.cell,
                original: before.signature,
                latest,
            });
        Ok(())
    }
}
struct PendingItem {
    change: Change,
    previous: u64,
    next: u64,
}
pub(crate) struct Pending {
    registry: Registry,
    items: Vec<PendingItem>,
}
impl Pending {
    pub(crate) fn publish(self) {
        if self.items.is_empty() {
            return;
        }
        // No allocation/fallible SQL after COMMIT. Poison retains factory
        // failure, but committed revocation must still reach old atomic cells.
        let mut state = self
            .registry
            .shared
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        for item in &self.items {
            if let Some(signature) = item.change.latest
                && let Some(entry) = state.cells.get_mut(item.change.cell.key.as_ref())
            {
                entry.signature = signature;
            }
            item.change.cell.word.store(item.next, Ordering::SeqCst);
        }
        drop(state);
    }
    pub(crate) fn rollback(self, db: &Connection) -> Result<()> {
        if self.items.is_empty() {
            return Ok(());
        }
        if !db.is_autocommit() {
            return Err(ArchiveError::WorkerStopped);
        }
        for item in &self.items {
            if Signature::read(db, &item.change.cell.key)? != item.change.original {
                return Err(ArchiveError::WorkerStopped);
            }
        }
        for item in &self.items {
            let _ = item.change.cell.word.compare_exchange(
                item.previous | PENDING,
                item.previous,
                Ordering::SeqCst,
                Ordering::SeqCst,
            );
        }
        Ok(())
    }
    // Default Drop deliberately does not clear pending: an unknown/panicked
    // COMMIT outcome must not resurrect old authority.
}

pub(crate) struct WriterLifetime(pub(crate) Registry);
impl Drop for WriterLifetime {
    fn drop(&mut self) {
        self.0.close();
    }
}

#[cfg(test)]
type BeforeCommit = Box<dyn FnOnce(&Connection) + Send>;
#[cfg(test)]
#[derive(Default)]
struct Hooks {
    before_apply: Option<Box<dyn FnOnce() + Send>>,
    before_commit: Option<BeforeCommit>,
    after_commit: Option<Box<dyn FnOnce() + Send>>,
}
#[cfg(test)]
impl Registry {
    pub(crate) fn before_apply(&self) {
        let hook = self.shared.hooks.lock().unwrap().before_apply.take();
        if let Some(hook) = hook {
            hook();
        }
    }
    pub(crate) fn before_commit(&self, db: &Connection) {
        let hook = self.shared.hooks.lock().unwrap().before_commit.take();
        if let Some(hook) = hook {
            hook(db);
        }
    }
    pub(crate) fn after_commit(&self) {
        let hook = self.shared.hooks.lock().unwrap().after_commit.take();
        if let Some(hook) = hook {
            hook();
        }
    }
}

#[cfg(test)]
mod tests;
