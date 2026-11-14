//! Owned, read-only cursors over a completed backup or a live WAL snapshot.
mod lifecycle;
mod scoped;
use crate::{Archive, ArchiveError, Result, error::invalid, rows, schema};
pub(crate) use lifecycle::Shared as LiveExportOwner;
use lifecycle::Shared;
use rusqlite::{Connection, OpenFlags, OptionalExtension, limits::Limit, params};
pub use scoped::{
    ScopedSnapshotExport, ScopedSnapshotInfo, ScopedSnapshotKind, ScopedSnapshotQuery,
    ScopedSnapshotRecord,
};
use serde::Serialize;
use serde_json::Value;
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::Ordering,
        mpsc::{self, TrySendError},
    },
    thread,
    time::{Duration, Instant},
};
use tempfile::TempDir;
use tokio::sync::oneshot;

const MAX_RECORD: usize = 16 * 1024 * 1024;
const MAX_READ_TIME: Duration = Duration::from_secs(5);

#[derive(Clone, Debug)]
pub struct SnapshotExportQuery {
    pub session_id: String,
    pub stream_id: Option<String>,
}
#[derive(Clone, Copy, Debug)]
pub struct SnapshotExportLimits {
    pub max_record_bytes: usize,
    pub read_timeout: Duration,
}
impl Default for SnapshotExportLimits {
    fn default() -> Self {
        Self {
            max_record_bytes: MAX_RECORD,
            read_timeout: MAX_READ_TIME,
        }
    }
}
#[derive(Clone, Debug, Serialize)]
pub struct SnapshotExportInfo {
    pub session_id: String,
    pub stream_id: Option<String>,
    pub sweeps: u64,
    pub observations: u64,
    pub boundary: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SnapshotExportKind {
    Sweep,
    Observation,
}
impl SnapshotExportKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Sweep => "sweep",
            Self::Observation => "observation",
        }
    }
}
#[derive(Debug)]
pub struct SnapshotExportRecord {
    pub kind: SnapshotExportKind,
    pub value: Value,
}

#[derive(Clone, Copy)]
enum Failure {
    Missing,
    ForeignStream,
    Budget,
    Closed,
    Corrupt,
    Worker,
}
impl Failure {
    fn from_error(error: &ArchiveError) -> Self {
        match error {
            ArchiveError::NotFound => Self::Missing,
            ArchiveError::Invalid(_) => Self::ForeignStream,
            ArchiveError::QueryBudget => Self::Budget,
            ArchiveError::Closed => Self::Closed,
            ArchiveError::WorkerStopped => Self::Worker,
            ArchiveError::Sql(rusqlite::Error::SqliteFailure(error, _))
                if matches!(
                    error.code,
                    rusqlite::ErrorCode::OperationInterrupted
                        | rusqlite::ErrorCode::TooBig
                        | rusqlite::ErrorCode::DatabaseBusy
                        | rusqlite::ErrorCode::DatabaseLocked
                ) =>
            {
                Self::Budget
            }
            _ => Self::Corrupt,
        }
    }
    fn error(self) -> ArchiveError {
        match self {
            Self::Missing => ArchiveError::NotFound,
            Self::ForeignStream => invalid("export stream does not belong to this session"),
            Self::Budget => ArchiveError::QueryBudget,
            Self::Closed => ArchiveError::Closed,
            Self::Corrupt => ArchiveError::Corrupt("read-only export snapshot".into()),
            Self::Worker => ArchiveError::WorkerStopped,
        }
    }
}
#[derive(Clone)]
enum Ready {
    Legacy(SnapshotExportInfo),
    Scoped(ScopedSnapshotInfo),
}
enum Record {
    Legacy(SnapshotExportRecord),
    Scoped(ScopedSnapshotRecord),
}
enum Query {
    Legacy(SnapshotExportQuery),
    Scoped(ScopedSnapshotQuery),
}
type Reply = oneshot::Sender<Result<Option<Record>>>;
struct Inner {
    shared: Arc<Shared>,
}
impl Drop for Inner {
    fn drop(&mut self) {
        // The already running reaper owns JOIN and the Archive lease. Dropping
        // the last public handle must never join a thread on an async executor.
        self.shared.request_close();
    }
}
enum Source {
    Staged(TempDir),
    Live(PathBuf),
}
impl Source {
    fn cleanup(self, shared: &Shared) {
        if let Self::Staged(root) = self
            && root.close().is_err()
        {
            shared.failed.store(true, Ordering::Release);
        }
    }
}
#[derive(Default)]
struct Launch {
    #[cfg(test)]
    fail_reaper: bool,
    #[cfg(test)]
    fail_reader: bool,
    #[cfg(test)]
    before_io: Option<Box<dyn FnOnce() + Send>>,
    #[cfg(test)]
    before_launch: Option<Box<dyn FnOnce() + Send>>,
    #[cfg(test)]
    after_snapshot: Option<Box<dyn FnOnce() + Send>>,
}
/// Clone shares one cursor. A body owner must await close before releasing its
/// admission/QoS guards. Last Drop signals an already owned, nonblocking join.
#[derive(Clone)]
pub struct SnapshotExport(Arc<Inner>);
impl SnapshotExport {
    /// `owned_root/snapshot` must be a completed Archive.backup bundle. The
    /// caller must await the backup receipt even if its HTTP request is cancelled.
    /// This function does no SQLite work on the calling thread.
    pub fn open(
        root: TempDir,
        query: SnapshotExportQuery,
        limits: SnapshotExportLimits,
    ) -> Result<Self> {
        Self::launch(
            Source::Staged(root),
            None,
            Query::Legacy(query),
            limits,
            Launch::default(),
        )
    }
    /// Open a frozen read-only WAL transaction on the Archive's trusted file.
    /// Admission is atomic with Archive.close; at most two live readers exist.
    /// No SQLite work or reader JOIN runs on the calling thread.
    pub fn open_live(
        archive: Archive,
        query: SnapshotExportQuery,
        limits: SnapshotExportLimits,
    ) -> Result<Self> {
        Self::open_live_with(archive, query, limits, Launch::default())
    }
    fn open_live_with(
        archive: Archive,
        query: SnapshotExportQuery,
        limits: SnapshotExportLimits,
        launch: Launch,
    ) -> Result<Self> {
        let source = Source::Live(archive.inner.live_export_path.clone());
        Self::launch(source, Some(archive), Query::Legacy(query), limits, launch)
    }
    fn launch(
        source: Source,
        archive: Option<Archive>,
        query: Query,
        limits: SnapshotExportLimits,
        launch: Launch,
    ) -> Result<Self> {
        // Match Archive's established text/ID contract. These values are bound
        // SQL parameters and never become filesystem paths or HTTP headers.
        let id_ok = |s: &str| !s.trim().is_empty();
        let valid_query = match &query {
            Query::Legacy(query) => {
                id_ok(&query.session_id) && query.stream_id.as_deref().is_none_or(id_ok)
            }
            Query::Scoped(query) => query.valid(),
        };
        if !valid_query
            || limits.max_record_bytes == 0
            || limits.max_record_bytes > MAX_RECORD
            || limits.read_timeout.is_zero()
            || limits.read_timeout > MAX_READ_TIME
        {
            return Err(invalid("snapshot export context or lowering-only budget"));
        }
        let (sender, receiver) = mpsc::sync_channel::<Reply>(1);
        let shared = Shared::new(
            limits.read_timeout,
            sender,
            archive.as_ref().map(|a| a.inner.clone()),
        );
        let client = Self(Arc::new(Inner {
            shared: shared.clone(),
        }));
        if let Some(archive) = archive {
            archive
                .inner
                .register_live_export(Arc::downgrade(&shared))?;
        }
        #[cfg(test)]
        let mut launch = launch;
        #[cfg(test)]
        {
            *shared.after_snapshot.lock().unwrap() = launch.after_snapshot.take();
        }
        #[cfg(test)]
        if let Some(before_launch) = launch.before_launch.take() {
            before_launch();
        }
        let state = shared.clone();
        // Establish owned completion BEFORE launching a thread capable of I/O.
        // Even last Drop or Archive.close during startup sees this lifecycle.
        #[cfg(test)]
        let reaper = if launch.fail_reaper {
            Err(std::io::Error::other("injected reaper launch failure"))
        } else {
            thread::Builder::new()
                .name("ori-export-reaper".into())
                .spawn(move || state.reap())
        };
        #[cfg(not(test))]
        let reaper = thread::Builder::new()
            .name("ori-export-reaper".into())
            .spawn(move || state.reap());
        if let Err(error) = reaper {
            source.cleanup(&shared);
            shared.finish(false); // No reader thread was ever launched.
            return Err(error.into());
        }
        let source = Arc::new(Mutex::new(Some(source)));
        let worker_source = source.clone();
        let state = shared.clone();
        #[cfg(test)]
        let fail_reader = launch.fail_reader;
        #[cfg(test)]
        let mut before_io = launch.before_io;
        #[cfg(not(test))]
        let _ = launch;
        let worker = move || {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let source = worker_source
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .take()
                    .expect("one owned reader source");
                #[cfg(test)]
                if let Some(before_io) = before_io.take() {
                    before_io();
                }
                let result = if state.cancelled.load(Ordering::Acquire) {
                    Err(ArchiveError::Closed)
                } else {
                    run(&source, query, limits, &state, receiver)
                };
                source.cleanup(&state);
                result
            }));
            match result {
                Ok(Err(error)) => state.publish(Err(Failure::from_error(&error))),
                Err(_) => {
                    state.failed.store(true, Ordering::Release);
                    state.publish(Err(Failure::Worker));
                }
                _ => {}
            }
        };
        #[cfg(test)]
        let handle = if fail_reader {
            Err(std::io::Error::other("injected reader launch failure"))
        } else {
            thread::Builder::new()
                .name("ori-snapshot-export".into())
                .spawn(worker)
        };
        #[cfg(not(test))]
        let handle = thread::Builder::new()
            .name("ori-snapshot-export".into())
            .spawn(worker);
        match handle {
            Ok(handle) => shared.install_reader(handle),
            Err(error) => {
                if let Some(source) = source.lock().unwrap_or_else(|e| e.into_inner()).take() {
                    source.cleanup(&shared);
                }
                shared.no_reader();
                return Err(error.into());
            }
        }
        Ok(client)
    }
    pub async fn ready(&self) -> Result<SnapshotExportInfo> {
        match self.ready_value().await? {
            Ready::Legacy(info) => Ok(info),
            Ready::Scoped(_) => Err(ArchiveError::WorkerStopped),
        }
    }
    async fn ready_value(&self) -> Result<Ready> {
        loop {
            let changed = self.0.shared.ready_changed.notified();
            if self.0.shared.closing.load(Ordering::Acquire) {
                return Err(ArchiveError::Closed);
            }
            if let Some(value) = self
                .0
                .shared
                .ready
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?
                .as_ref()
            {
                return value.clone().map_err(Failure::error);
            }
            changed.await;
        }
    }
    pub async fn next_record(&self) -> Result<Option<SnapshotExportRecord>> {
        self.next_value()
            .await?
            .map(|record| match record {
                Record::Legacy(record) => Ok(record),
                Record::Scoped(_) => Err(ArchiveError::WorkerStopped),
            })
            .transpose()
    }
    async fn next_value(&self) -> Result<Option<Record>> {
        self.ready_value().await?;
        let (reply, result) = oneshot::channel();
        {
            let sender = self
                .0
                .shared
                .sender
                .lock()
                .map_err(|_| ArchiveError::WorkerStopped)?;
            let sender = sender.as_ref().ok_or(ArchiveError::Closed)?;
            sender.try_send(reply).map_err(|error| match error {
                TrySendError::Full(_) => ArchiveError::QueueFull {
                    queue: "snapshot export",
                },
                TrySendError::Disconnected(_) => ArchiveError::WorkerStopped,
            })?;
        }
        result.await.map_err(|_| ArchiveError::WorkerStopped)?
    }
    pub async fn close(&self) -> Result<()> {
        self.0.shared.close().await
    }
}

fn regular(path: &Path, directory: bool) -> Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if meta.file_type().is_symlink()
        || if directory {
            !meta.is_dir()
        } else {
            !meta.is_file()
        }
    {
        return Err(ArchiveError::Corrupt("snapshot path type".into()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if (!directory && meta.nlink() != 1) || (directory && meta.mode() & 0o077 != 0) {
            return Err(ArchiveError::Corrupt("snapshot path ownership".into()));
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & 0x400 != 0 {
            return Err(ArchiveError::Corrupt("snapshot reparse path".into()));
        }
    }
    Ok(())
}
fn deadline(limits: SnapshotExportLimits, shared: &Shared) -> Result<()> {
    *shared
        .deadline
        .lock()
        .map_err(|_| ArchiveError::WorkerStopped)? = Instant::now() + limits.read_timeout;
    Ok(())
}
fn run(
    source: &Source,
    query: Query,
    limits: SnapshotExportLimits,
    shared: &Arc<Shared>,
    receiver: mpsc::Receiver<Reply>,
) -> Result<()> {
    let (path, boundary) = match source {
        Source::Staged(root) => {
            regular(root.path(), true)?;
            let directory = root.path().join("snapshot");
            regular(&directory, true)?;
            let path = directory.join("archive.sqlite");
            regular(&path, false)?;
            let manifest_path = directory.join("manifest.json");
            regular(&manifest_path, false)?;
            if fs::metadata(&manifest_path)?.len() > 65_536 {
                return Err(ArchiveError::QueryBudget);
            }
            let manifest = crate::parse_json_value(&fs::read(manifest_path)?, 65_536)?;
            if manifest["backup_version"] != 1
                || manifest["purpose"] != "private-recovery"
                || manifest["archive"]["file"] != "archive.sqlite"
                || manifest["archive"]["bytes"].as_u64() != Some(fs::metadata(&path)?.len())
            {
                return Err(ArchiveError::Corrupt("completed backup manifest".into()));
            }
            let boundary = manifest["event_watermark"]
                .as_u64()
                .ok_or_else(|| ArchiveError::Corrupt("snapshot event boundary".into()))?;
            (path, Some(boundary))
        }
        Source::Live(path) => {
            regular(path, false)?;
            (path.clone(), None)
        }
    };
    let mut db = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    let result = match query {
        Query::Legacy(query) => read_snapshot(&mut db, boundary, query, limits, shared, receiver),
        Query::Scoped(query) => scoped::read_snapshot(&mut db, query, limits, shared, receiver),
    };
    // An interrupted read must not make transaction cleanup fail merely because
    // the same cancellation progress handler is still armed.
    let clear = db.progress_handler(0, None::<fn() -> bool>);
    let rollback = if db.is_autocommit() {
        Ok(())
    } else {
        db.execute_batch("ROLLBACK")
    };
    let closed = db.close().map_err(|(_, error)| error);
    if clear.is_err() || rollback.is_err() || closed.is_err() {
        shared.failed.store(true, Ordering::Release);
        return Err(ArchiveError::WorkerStopped);
    }
    result
}
fn read_snapshot(
    db: &mut Connection,
    boundary: Option<u64>,
    query: SnapshotExportQuery,
    limits: SnapshotExportLimits,
    shared: &Arc<Shared>,
    receiver: mpsc::Receiver<Reply>,
) -> Result<()> {
    let state = shared.clone();
    db.progress_handler(
        1000,
        Some(move || {
            state.cancelled.load(Ordering::Acquire)
                || state
                    .deadline
                    .lock()
                    .map_or(true, |until| Instant::now() >= *until)
        }),
    )?;
    db.busy_timeout(limits.read_timeout)?;
    db.execute_batch("PRAGMA query_only=ON; PRAGMA cache_size=-4096; PRAGMA temp_store=FILE; PRAGMA mmap_size=0; PRAGMA trusted_schema=OFF; BEGIN")?;
    schema::functions(db)?;
    deadline(limits, shared)?;
    schema::versions(db)?;
    if !rows::exists(db, "sessions", &query.session_id)? {
        return Err(ArchiveError::NotFound);
    }
    if let Some(stream) = &query.stream_id {
        let owner: Option<String> = db
            .query_row("SELECT session_id FROM streams WHERE id=?", [stream], |r| {
                r.get(0)
            })
            .optional()?;
        if owner.as_deref() != Some(&query.session_id) {
            return Err(invalid("foreign export stream"));
        }
    }
    let count = |table: &str| -> Result<u64> {
        Ok(db.query_row(
            &format!(
                "SELECT COUNT(*) FROM {table} WHERE session_id=?1 AND (?2 IS NULL OR stream_id=?2)"
            ),
            params![query.session_id, query.stream_id],
            |r| r.get::<_, i64>(0),
        )? as u64)
    };
    let info = SnapshotExportInfo {
        session_id: query.session_id.clone(),
        stream_id: query.stream_id.clone(),
        sweeps: count("sweeps")?,
        observations: count("observations")?,
        boundary: db.query_row("SELECT COALESCE(MAX(sequence),0) FROM events", [], |r| {
            r.get::<_, i64>(0)
        })? as u64,
    };
    if boundary.is_some_and(|boundary| boundary != info.boundary) {
        return Err(ArchiveError::Corrupt("snapshot event boundary".into()));
    }
    // Sort only IDs, never raw point arrays. SQLite's sort spills to disk.
    let mut sweeps = db.prepare("SELECT id FROM sweeps WHERE session_id=?1 AND (?2 IS NULL OR stream_id=?2) ORDER BY context_utc(started_at),context_utf8(id)")?;
    let mut observations = db.prepare("SELECT id FROM observations WHERE session_id=?1 AND (?2 IS NULL OR stream_id=?2) ORDER BY context_utc(started_at),context_utf8(id)")?;
    let mut sweep_rows = sweeps.query(params![query.session_id, query.stream_id])?;
    let mut observation_rows = observations.query(params![query.session_id, query.stream_id])?;
    db.set_limit(Limit::SQLITE_LIMIT_LENGTH, limits.max_record_bytes as i32)?;
    shared.publish(Ok(Ready::Legacy(info)));
    #[cfg(test)]
    if let Some(after_snapshot) = shared.after_snapshot.lock().unwrap().take() {
        after_snapshot();
    }
    let mut sweep_done = false;
    let mut failed = None;
    while let Ok(reply) = receiver.recv() {
        if shared.cancelled.load(Ordering::Acquire) {
            let _ = reply.send(Err(ArchiveError::Closed));
            continue;
        }
        if let Some(failure) = failed {
            let _ = reply.send(Err(Failure::error(failure)));
            continue;
        }
        deadline(limits, shared)?;
        let value = (|| -> Result<Option<SnapshotExportRecord>> {
            if !sweep_done {
                if let Some(row) = sweep_rows.next()? {
                    let value = rows::get_sweep(db, &row.get::<_, String>(0)?)?
                        .ok_or(ArchiveError::NotFound)?;
                    bounded(&value, limits.max_record_bytes)?;
                    return Ok(Some(SnapshotExportRecord {
                        kind: SnapshotExportKind::Sweep,
                        value,
                    }));
                }
                sweep_done = true;
            }
            let Some(row) = observation_rows.next()? else {
                return Ok(None);
            };
            let value = rows::get_observation(db, &row.get::<_, String>(0)?)?
                .ok_or(ArchiveError::NotFound)?;
            bounded(&value, limits.max_record_bytes)?;
            Ok(Some(SnapshotExportRecord {
                kind: SnapshotExportKind::Observation,
                value,
            }))
        })();
        let value = value
            .map(|value| value.map(Record::Legacy))
            .map_err(|error| {
                let failure = Failure::from_error(&error);
                failed = Some(failure);
                failure.error()
            });
        let _ = reply.send(value);
    }
    Ok(())
}
struct ByteBudget(usize);
impl Write for ByteBudget {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > self.0 {
            return Err(std::io::Error::other("record budget"));
        }
        self.0 -= bytes.len();
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn bounded(value: &Value, bytes: usize) -> Result<()> {
    serde_json::to_writer(ByteBudget(bytes), value).map_err(|_| ArchiveError::QueryBudget)
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;
    use crate::ArchiveConfig;
    use serde_json::json;
    use std::sync::mpsc::{channel, sync_channel};

    async fn seeded() -> (TempDir, Archive, SnapshotExportQuery) {
        let directory = tempfile::tempdir().unwrap();
        let archive =
            Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))).unwrap();
        archive
            .create_session(json!({"id":"owned","name":"Owned lifecycle test",
            "created_at":"2026-11-11T00:00:00Z","streams":[]}))
            .await
            .unwrap();
        let query = SnapshotExportQuery {
            session_id: "owned".into(),
            stream_id: None,
        };
        (directory, archive, query)
    }
    async fn until_closing(archive: &Archive) {
        tokio::time::timeout(Duration::from_secs(2), async {
            while !archive.queue_status().closing {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn registered_pending_start_is_drained_even_when_archive_close_waiter_is_cancelled() {
        let (directory, archive, query) = seeded().await;
        let (entered, entry) = channel();
        let (release, released) = channel();
        let starting = archive.clone();
        let starter = tokio::task::spawn_blocking(move || {
            SnapshotExport::open_live_with(
                starting,
                query,
                Default::default(),
                Launch {
                    before_launch: Some(Box::new(move || {
                        entered.send(()).unwrap();
                        released.recv().unwrap();
                    })),
                    ..Default::default()
                },
            )
        });
        tokio::task::spawn_blocking(move || entry.recv().unwrap())
            .await
            .unwrap();
        let closing = archive.clone();
        let close = tokio::spawn(async move { closing.close().await });
        until_closing(&archive).await;
        assert!(
            matches!(
                Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))),
                Err(ArchiveError::AlreadyOpen)
            ),
            "writer lease escaped a registered pending start"
        );
        assert!(!close.is_finished());
        close.abort();
        let _ = close.await;
        release.send(()).unwrap();
        let reader = starter.await.unwrap().unwrap();
        tokio::time::timeout(Duration::from_secs(2), archive.close())
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(reader.ready().await, Err(ArchiveError::Closed)));
        reader.close().await.unwrap();
        let reopened =
            Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))).unwrap();
        reopened.close().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn last_drop_is_nonblocking_and_archive_still_owns_the_blocked_reader() {
        let (directory, archive, query) = seeded().await;
        let (entered, entry) = channel();
        let (release, released) = channel();
        let reader = SnapshotExport::open_live_with(
            archive.clone(),
            query,
            Default::default(),
            Launch {
                before_io: Some(Box::new(move || {
                    entered.send(()).unwrap();
                    released.recv().unwrap();
                })),
                ..Default::default()
            },
        )
        .unwrap();
        tokio::task::spawn_blocking(move || entry.recv().unwrap())
            .await
            .unwrap();
        let state = reader.0.shared.clone();
        // A watchdog releases a broken implementation without hanging the test.
        // Success requires last Drop to return BEFORE this release is sent.
        let (continued, continuation) = sync_channel(1);
        let watchdog = thread::spawn(move || {
            let not_blocked = continuation.recv_timeout(Duration::from_secs(1)).is_ok();
            release.send(()).unwrap();
            not_blocked
        });
        drop(reader);
        continued.send(()).unwrap();
        assert!(matches!(
            Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))),
            Err(ArchiveError::AlreadyOpen)
        ));
        assert!(
            tokio::task::spawn_blocking(move || watchdog.join().unwrap())
                .await
                .unwrap(),
            "last Drop joined the blocked reader on the async executor"
        );
        archive.close().await.unwrap();
        assert!(state.is_joined());
    }

    #[tokio::test]
    async fn launch_failures_leave_no_io_or_stranded_registration_and_are_recoverable() {
        let (_directory, archive, query) = seeded().await;
        for fail_reaper in [true, false] {
            let (called, calls) = channel::<()>();
            let failed = SnapshotExport::open_live_with(
                archive.clone(),
                query.clone(),
                Default::default(),
                Launch {
                    fail_reaper,
                    fail_reader: !fail_reaper,
                    before_io: Some(Box::new(move || {
                        called.send(()).unwrap();
                    })),
                    ..Default::default()
                },
            );
            assert!(matches!(failed, Err(ArchiveError::Io(_))));
            assert!(
                calls.try_recv().is_err(),
                "a failed launch must never reach reader I/O"
            );
        }
        // Two slots can be occupied after both real startup-failure paths drain.
        tokio::task::yield_now().await;
        let a =
            SnapshotExport::open_live(archive.clone(), query.clone(), Default::default()).unwrap();
        let b = tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                match SnapshotExport::open_live(archive.clone(), query.clone(), Default::default())
                {
                    Ok(reader) => break reader,
                    Err(ArchiveError::QueueFull { .. }) => tokio::task::yield_now().await,
                    Err(error) => panic!("launch failure damaged archive: {error:?}"),
                }
            }
        })
        .await
        .expect("failed startup stranded an admission slot");
        a.ready().await.unwrap();
        b.ready().await.unwrap();
        a.close().await.unwrap();
        b.close().await.unwrap();
        archive.close().await.unwrap();
    }

    #[tokio::test]
    async fn failed_reader_join_is_sticky_for_reader_and_archive_after_registry_expiry() {
        let (_directory, archive, query) = seeded().await;
        let reader = SnapshotExport::open_live_with(
            archive.clone(),
            query,
            Default::default(),
            Launch {
                before_io: Some(Box::new(|| panic!("owned synthetic reader failure"))),
                ..Default::default()
            },
        )
        .unwrap();
        assert!(matches!(
            reader.ready().await,
            Err(ArchiveError::WorkerStopped)
        ));
        assert!(reader.close().await.is_err());
        assert!(reader.close().await.is_err());
        drop(reader);
        assert!(
            archive.close().await.is_err(),
            "expired weak registration must not hide a failed drain"
        );
        assert!(archive.close().await.is_err());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn active_wal_snapshot_is_joined_before_close_can_release_the_writer() {
        let (directory, archive, query) = seeded().await;
        let (release, released) = channel();
        let reader = SnapshotExport::open_live_with(
            archive.clone(),
            query,
            Default::default(),
            Launch {
                after_snapshot: Some(Box::new(move || {
                    released.recv().unwrap();
                })),
                ..Default::default()
            },
        )
        .unwrap();
        let frozen = reader.ready().await.unwrap();
        assert_eq!(frozen.boundary, 1);
        archive
            .create_session(json!({"id":"committed-later","name":"Later session",
            "created_at":"2026-11-11T00:00:01Z","streams":[]}))
            .await
            .unwrap();
        let checkpoint = archive.checkpoint().await.unwrap();
        assert!(
            checkpoint["checkpointed_frames"].as_i64().unwrap()
                < checkpoint["wal_frames"].as_i64().unwrap(),
            "the real read transaction must retain its original WAL pages"
        );
        let closing = archive.clone();
        let close = tokio::spawn(async move { closing.close().await });
        until_closing(&archive).await;
        assert!(!close.is_finished());
        assert!(matches!(
            Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))),
            Err(ArchiveError::AlreadyOpen)
        ));
        close.abort();
        let _ = close.await;
        release.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(2), archive.close())
            .await
            .unwrap()
            .unwrap();
        reader.close().await.unwrap();
        let recovered =
            Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))).unwrap();
        assert!(
            recovered
                .get_session("committed-later")
                .await
                .unwrap()
                .is_some()
        );
        recovered.close().await.unwrap();
    }
}
