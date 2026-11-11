//! One SQLite thread per selected legacy file. It never initializes a schema.
use crate::*;
use rusqlite::{
    Connection, OpenFlags,
    backup::{Backup, StepResult},
    params,
};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};
use tokio::sync::oneshot;

pub(crate) struct Snapshot {
    pub directory: tempfile::TempDir,
    pub nonce: String,
    pub data_version: i64,
    // Fields drop in declaration order: remove private files before releasing
    // the original job's shared I/O registration.
    _io_budget: Option<Arc<dyn LegacyIoBudget>>,
}
struct SnapshotParameters {
    scratch: PathBuf,
    limits: LegacyLimits,
    timeout: Duration,
    rate: u64,
    expected: Option<i64>,
    sidecar: bool,
    cancel: LegacyCancellation,
    io_budget: Option<Arc<dyn LegacyIoBudget>>,
}
enum Command {
    Snapshot {
        parameters: SnapshotParameters,
        reply: oneshot::Sender<Result<Snapshot>>,
    },
    Check(i64, oneshot::Sender<Result<()>>),
    Threshold {
        expected: i64,
        frequencies: Vec<u8>,
        levels: Vec<u8>,
        cancel: LegacyCancellation,
        io_budget: Option<Arc<dyn LegacyIoBudget>>,
        timeout: Duration,
        reply: oneshot::Sender<Result<i64>>,
    },
    Close(Option<oneshot::Sender<Result<()>>>),
}
struct Inner {
    sender: mpsc::SyncSender<Command>,
    join: Mutex<Option<thread::JoinHandle<()>>>,
    lease: Arc<File>,
}
impl Drop for Inner {
    fn drop(&mut self) {
        if let Some(join) = self.join.get_mut().ok().and_then(Option::take) {
            let sender = self.sender.clone();
            let lease = self.lease.clone();
            // Drop never unlocks a file while its SQL thread might still run.
            thread::spawn(move || {
                let _ = sender.send(Command::Close(None));
                if join.join().is_err() {
                    std::mem::forget(lease);
                }
            });
        }
    }
}
pub(crate) struct PanOwner {
    pub path: PathBuf,
    inner: Arc<Inner>,
}
impl PanOwner {
    pub fn lease(&self) -> Arc<File> {
        self.inner.lease.clone()
    }
    fn unchanged_file(&self) -> Result<()> {
        let current = File::open(&self.path).map_err(|_| LegacyError::Stale)?;
        if file_identity(&current)? != file_identity(&self.inner.lease)? {
            return Err(LegacyError::Stale);
        }
        Ok(())
    }
    pub async fn open(path: PathBuf, maximum_bytes: u64) -> Result<Self> {
        let lease = Arc::new(OpenOptions::new().read(true).open(&path)?);
        if lease.metadata()?.len() > maximum_bytes {
            return Err(LegacyError::ResourceLimit(
                "Legacy source file budget exceeded".into(),
            ));
        }
        lease.try_lock().map_err(|_| LegacyError::Busy)?;
        let (sender, receiver) = mpsc::sync_channel(4);
        let (ready_tx, ready) = oneshot::channel();
        let source = path.clone();
        let join = thread::spawn(move || {
            let opened = Connection::open_with_flags(
                &source,
                OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )
            .or_else(|_| {
                Connection::open_with_flags(
                    &source,
                    OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
                )
            });
            let mut db = match opened.and_then(|db| {
                db.busy_timeout(Duration::from_secs(2))?;
                Ok(db)
            }) {
                Ok(value) => value,
                Err(error) => {
                    let _ = ready_tx.send(Err(error.into()));
                    return;
                }
            };
            if let Err(error) = validate_schema(&db) {
                let _ = ready_tx.send(Err(error));
                return;
            }
            let _ = ready_tx.send(Ok(()));
            while let Ok(command) = receiver.recv() {
                match command {
                    Command::Snapshot { parameters, reply } => {
                        let result = snapshot(&mut db, &source, parameters);
                        let _ = reply.send(result);
                    }
                    Command::Check(expected, reply) => {
                        let _ = reply.send(data_version(&db).and_then(|actual| {
                            if actual == expected {
                                Ok(())
                            } else {
                                Err(LegacyError::Stale)
                            }
                        }));
                    }
                    Command::Threshold {
                        expected,
                        frequencies,
                        levels,
                        cancel,
                        io_budget,
                        timeout,
                        reply,
                    } => {
                        let _ = reply.send(save_threshold(
                            &mut db,
                            expected,
                            &frequencies,
                            &levels,
                            &cancel,
                            io_budget.as_deref(),
                            timeout,
                        ));
                    }
                    Command::Close(reply) => match db.close() {
                        Ok(()) => {
                            if let Some(reply) = reply {
                                let _ = reply.send(Ok(()));
                            }
                            return;
                        }
                        Err((connection, _)) => {
                            db = connection;
                            if let Some(reply) = reply {
                                let _ = reply.send(Err(LegacyError::CleanupPending));
                            }
                        }
                    },
                }
            }
        });
        let owner = Self {
            path,
            inner: Arc::new(Inner {
                sender,
                join: Mutex::new(Some(join)),
                lease,
            }),
        };
        ready.await.map_err(|_| LegacyError::CleanupPending)??;
        Ok(owner)
    }
    fn send(&self, command: Command) -> Result<()> {
        self.inner
            .sender
            .try_send(command)
            .map_err(|error| match error {
                mpsc::TrySendError::Full(_) => LegacyError::Busy,
                mpsc::TrySendError::Disconnected(_) => LegacyError::CleanupPending,
            })
    }
    pub async fn snapshot(
        &self,
        options: &LegacyOptions,
        expected: Option<i64>,
        sidecar: bool,
        cancel: LegacyCancellation,
        io_budget: Option<Arc<dyn LegacyIoBudget>>,
    ) -> Result<Snapshot> {
        self.unchanged_file()?;
        let (reply, result) = oneshot::channel();
        self.send(Command::Snapshot {
            parameters: SnapshotParameters {
                scratch: options.scratch_directory.clone(),
                limits: options.limits.clone(),
                timeout: options.sql_timeout,
                rate: options.io_bytes_per_second,
                expected,
                sidecar,
                cancel,
                io_budget,
            },
            reply,
        })?;
        result.await.map_err(|_| LegacyError::CleanupPending)?
    }
    pub async fn check(&self, expected: i64) -> Result<()> {
        self.unchanged_file()?;
        let (reply, result) = oneshot::channel();
        self.send(Command::Check(expected, reply))?;
        result.await.map_err(|_| LegacyError::CleanupPending)?
    }
    pub async fn threshold(
        &self,
        expected: i64,
        frequencies: Vec<u8>,
        levels: Vec<u8>,
        cancel: LegacyCancellation,
        io_budget: Option<Arc<dyn LegacyIoBudget>>,
        timeout: Duration,
    ) -> Result<i64> {
        self.unchanged_file()?;
        let (reply, result) = oneshot::channel();
        self.send(Command::Threshold {
            expected,
            frequencies,
            levels,
            cancel,
            io_budget,
            timeout,
            reply,
        })?;
        result.await.map_err(|_| LegacyError::CleanupPending)?
    }
    pub async fn close(&self) -> Result<()> {
        let (reply, result) = oneshot::channel();
        self.send(Command::Close(Some(reply)))?;
        result.await.map_err(|_| LegacyError::CleanupPending)??;
        let join = self
            .inner
            .join
            .lock()
            .map_err(|_| LegacyError::CleanupPending)?
            .take();
        if let Some(join) = join {
            let lease = self.inner.lease.clone();
            let (done, result) = oneshot::channel();
            // Independent of the caller future/runtime: the keeper owns the
            // lock until a confirmed join, leaking it closed on a panic.
            thread::spawn(move || {
                let result = join.join().map_err(|_| LegacyError::CleanupPending);
                if result.is_err() {
                    std::mem::forget(lease);
                } else {
                    drop(lease);
                }
                let _ = done.send(result);
            });
            result.await.map_err(|_| LegacyError::CleanupPending)??;
        }
        Ok(())
    }
}

#[cfg(unix)]
fn file_identity(file: &File) -> Result<(u64, u64)> {
    use std::os::unix::fs::MetadataExt;
    let info = file.metadata()?;
    Ok((info.dev(), info.ino()))
}
#[cfg(windows)]
fn file_identity(file: &File) -> Result<(u64, u64)> {
    use std::{ffi::c_void, os::windows::io::AsRawHandle};
    #[repr(C)]
    struct Information {
        attributes: u32,
        creation: [u32; 2],
        access: [u32; 2],
        write: [u32; 2],
        volume: u32,
        size: [u32; 2],
        links: u32,
        index: [u32; 2],
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        #[link_name = "GetFileInformationByHandle"]
        fn information(handle: *mut c_void, output: *mut Information) -> i32;
    }
    let mut info = Information {
        attributes: 0,
        creation: [0; 2],
        access: [0; 2],
        write: [0; 2],
        volume: 0,
        size: [0; 2],
        links: 0,
        index: [0; 2],
    };
    // The repr(C) structure is BY_HANDLE_FILE_INFORMATION; the borrowed handle
    // remains valid for the call and is never closed or modified here.
    if unsafe { information(file.as_raw_handle(), &mut info) } == 0 {
        return Err(LegacyError::Stale);
    }
    Ok((
        u64::from(info.volume),
        (u64::from(info.index[0]) << 32) | u64::from(info.index[1]),
    ))
}
#[cfg(not(any(unix, windows)))]
fn file_identity(_file: &File) -> Result<(u64, u64)> {
    Err(LegacyError::Request(
        "This platform cannot verify legacy source identity".into(),
    ))
}

fn validate_schema(db: &Connection) -> Result<()> {
    for (table, columns) in [
        (
            "panorama",
            &[
                "id",
                "recorded_at",
                "latitude",
                "longitude",
                "altitude",
                "speed",
                "heading",
                "levels",
                "start_hz",
                "stop_hz",
                "step_hz",
                "unit",
            ][..],
        ),
        ("panorama_meta", &["key", "value"][..]),
        (
            "threshold_line",
            &["line_index", "frequencies", "levels"][..],
        ),
        (
            "signal_event",
            &[
                "id",
                "panorama_id",
                "recorded_at",
                "frequency_hz",
                "bandwidth_hz",
                "power_over_w",
                "level_db",
            ][..],
        ),
    ] {
        let count: i64 = db.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE",
            [table],
            |r| r.get(0),
        )?;
        if count != 1 {
            return Err(LegacyError::Request(
                "Unsupported existing .pan schema; no initialization or migration was performed"
                    .into(),
            ));
        }
        let names = db
            .prepare(&format!("PRAGMA table_info({table})"))?
            .query_map([], |r| r.get::<_, String>(1))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        if columns
            .iter()
            .any(|column| !names.iter().any(|name| name.eq_ignore_ascii_case(column)))
        {
            return Err(LegacyError::Request(
                "Unsupported existing .pan columns".into(),
            ));
        }
    }
    Ok(())
}
fn data_version(db: &Connection) -> Result<i64> {
    Ok(db.query_row("PRAGMA data_version", [], |row| row.get(0))?)
}
fn private_file(path: &Path) -> Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    Ok(options.open(path)?)
}
fn cancelled(cancel: &LegacyCancellation, deadline: Instant) -> Result<()> {
    if cancel.is_cancelled() {
        Err(LegacyError::Cancelled)
    } else if Instant::now() >= deadline {
        Err(LegacyError::ResourceLimit(
            "Legacy snapshot deadline exceeded".into(),
        ))
    } else {
        Ok(())
    }
}
fn account_io(
    budget: Option<&dyn LegacyIoBudget>,
    bytes: u64,
    cancel: &LegacyCancellation,
    deadline: Instant,
) -> Result<()> {
    cancelled(cancel, deadline)?;
    if let Some(budget) = budget {
        let mut remaining = bytes;
        while remaining != 0 {
            let chunk = remaining.min(512 * 1024);
            cancelled(cancel, deadline)?;
            let result = budget.account(chunk, cancel);
            cancelled(cancel, deadline)?;
            result?;
            remaining -= chunk;
        }
    }
    cancelled(cancel, deadline)
}
fn snapshot(
    db: &mut Connection,
    source: &Path,
    parameters: SnapshotParameters,
) -> Result<Snapshot> {
    let SnapshotParameters {
        scratch,
        limits,
        timeout,
        rate,
        expected,
        sidecar,
        cancel,
        io_budget,
    } = parameters;
    let deadline = Instant::now() + timeout;
    cancelled(&cancel, deadline)?;
    let flag = cancel.clone();
    db.progress_handler(
        10_000,
        Some(move || flag.is_cancelled() || Instant::now() >= deadline),
    )?;
    let result = (|| {
        validate_schema(db)?;
        let tx = db.transaction()?;
        let pages = nonnegative(tx.query_row("PRAGMA page_count", [], |r| r.get::<_, i64>(0))?)?;
        let page_size = nonnegative(tx.query_row("PRAGMA page_size", [], |r| r.get::<_, i64>(0))?)?;
        if pages.saturating_mul(page_size) > limits.max_database_bytes {
            return Err(LegacyError::ResourceLimit(
                "Legacy source budget exceeded".into(),
            ));
        }
        let (rows,bytes,largest):(i64,i64,i64)=tx.query_row("SELECT COUNT(*),COALESCE(SUM(length(levels)),0),COALESCE(MAX(length(levels)),0) FROM panorama",[],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?)))?;
        let (rows, bytes, largest) = (
            nonnegative(rows)?,
            nonnegative(bytes)?,
            nonnegative(largest)?,
        );
        let revision = data_version(&tx)?;
        if expected.is_some_and(|expected| expected != revision) {
            return Err(LegacyError::Stale);
        }
        if rows > limits.max_rows
            || bytes > limits.max_total_points * 8
            || largest > limits.max_record_bytes
            || pages.saturating_mul(page_size) > limits.max_database_bytes
        {
            return Err(LegacyError::ResourceLimit(
                "Legacy source budget exceeded".into(),
            ));
        }
        fs::create_dir_all(&scratch)?;
        let scratch = scratch.canonicalize()?;
        let directory = tempfile::Builder::new()
            .prefix(".ori-legacy-")
            .tempdir_in(scratch)?;
        let destination = directory.path().join("workspace.pan");
        drop(private_file(&destination)?);
        let mut writer = Connection::open_with_flags(
            &destination,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        // rusqlite appends the filename to CannotOpen errors. Staging paths
        // are internal even when temporary storage becomes unavailable.
        .map_err(|error| LegacyError::Storage(error.to_string()))?;
        let start = Instant::now();
        {
            let backup = Backup::new(&tx, &mut writer)?;
            let step_pages = if io_budget.is_some() {
                (512 * 1024 / page_size).clamp(1, 128) as i32
            } else {
                128 // Keep old providers' copy/pacing granularity unchanged.
            };
            let mut copied_pages = 0_u64;
            loop {
                let chunk = pages.saturating_sub(copied_pages).min(step_pages as u64) * page_size;
                account_io(io_budget.as_deref(), chunk, &cancel, deadline)?;
                account_io(io_budget.as_deref(), chunk, &cancel, deadline)?;
                let step = backup.step(step_pages)?;
                let progress = backup.progress();
                if progress.pagecount.max(0) as u64 * page_size > limits.max_database_bytes {
                    return Err(LegacyError::ResourceLimit(
                        "Legacy snapshot byte budget exceeded".into(),
                    ));
                }
                let copied = (progress.pagecount - progress.remaining).max(0) as u64 * page_size;
                copied_pages = (progress.pagecount - progress.remaining).max(0) as u64;
                let due = Duration::from_secs_f64((copied.saturating_mul(2)) as f64 / rate as f64);
                while start.elapsed() < due {
                    cancelled(&cancel, deadline)?;
                    thread::sleep(
                        due.saturating_sub(start.elapsed())
                            .min(Duration::from_millis(20)),
                    );
                }
                match step {
                    StepResult::Done => break,
                    StepResult::More => {}
                    StepResult::Busy | StepResult::Locked => {
                        thread::sleep(Duration::from_millis(5))
                    }
                    _ => return Err(LegacyError::Protocol("backup state")),
                }
            }
        }
        writer.close().map_err(|_| LegacyError::CleanupPending)?;
        tx.commit()?;
        if data_version(db)? != revision {
            return Err(LegacyError::Stale);
        }
        let signal_source = source.with_file_name("signals.json");
        let mut has_signals = false;
        if sidecar && signal_source.exists() {
            let file = File::open(signal_source)?;
            let file_bytes = file.metadata()?.len();
            if file_bytes > limits.max_sidecar_bytes {
                return Err(LegacyError::ResourceLimit(
                    "Legacy signals sidecar budget exceeded".into(),
                ));
            }
            let mut reader = file;
            let mut target = private_file(&directory.path().join("signals.json"))?;
            let mut copied = 0;
            let mut chunk = [0_u8; 65536];
            let copied_start = Instant::now();
            let mut remaining = file_bytes;
            while remaining != 0 {
                let count = remaining.min(chunk.len() as u64) as usize;
                account_io(io_budget.as_deref(), count as u64, &cancel, deadline)?;
                reader.read_exact(&mut chunk[..count])?;
                copied += count as u64;
                if copied > limits.max_sidecar_bytes {
                    return Err(LegacyError::ResourceLimit(
                        "Legacy signals sidecar budget exceeded".into(),
                    ));
                }
                account_io(io_budget.as_deref(), count as u64, &cancel, deadline)?;
                target.write_all(&chunk[..count])?;
                cancelled(&cancel, deadline)?;
                remaining -= count as u64;
                let due = Duration::from_secs_f64(copied as f64 * 2. / rate as f64);
                while copied_start.elapsed() < due {
                    cancelled(&cancel, deadline)?;
                    thread::sleep(
                        due.saturating_sub(copied_start.elapsed())
                            .min(Duration::from_millis(20)),
                    );
                }
            }
            if reader.metadata()?.len() != file_bytes {
                return Err(LegacyError::Stale);
            }
            has_signals = true;
        }
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        let descriptor = ori_contracts::to_json_vec(
            &serde_json::json!({"version":1,"nonce":nonce,"panorama_file":"workspace.pan","signals_file":if has_signals{Some("signals.json")}else{None}}),
        )?;
        account_io(
            io_budget.as_deref(),
            descriptor.len() as u64,
            &cancel,
            deadline,
        )?;
        private_file(&directory.path().join("snapshot.json"))?.write_all(&descriptor)?;
        cancelled(&cancel, deadline)?;
        Ok(Snapshot {
            directory,
            nonce,
            data_version: revision,
            _io_budget: io_budget.clone(),
        })
    })();
    db.progress_handler(0, None::<fn() -> bool>)?;
    result
}

fn nonnegative(value: i64) -> Result<u64> {
    u64::try_from(value).map_err(|_| LegacyError::Request("Invalid legacy size metadata".into()))
}

fn save_threshold(
    db: &mut Connection,
    expected: i64,
    frequencies: &[u8],
    levels: &[u8],
    cancel: &LegacyCancellation,
    io_budget: Option<&dyn LegacyIoBudget>,
    timeout: Duration,
) -> Result<i64> {
    let deadline = Instant::now() + timeout;
    cancelled(cancel, deadline)?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    if data_version(&tx)? != expected {
        return Err(LegacyError::Stale);
    }
    let triggers: i64 = tx.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'",
        [],
        |r| r.get(0),
    )?;
    if triggers != 0 {
        return Err(LegacyError::Request(
            "Threshold writes with custom triggers are not permitted".into(),
        ));
    }
    let references: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master AS m, pragma_foreign_key_list(m.name) AS f WHERE m.type='table' AND f.\"table\"='threshold_line' COLLATE NOCASE)",
        [],
        |row| row.get(0),
    )?;
    if references {
        return Err(LegacyError::Request(
            "Threshold writes with custom foreign-key relations are not permitted".into(),
        ));
    }
    account_io(io_budget, frequencies.len() as u64, cancel, deadline)?;
    account_io(io_budget, levels.len() as u64, cancel, deadline)?;
    tx.execute("INSERT INTO threshold_line(line_index,frequencies,levels) VALUES(0,?,?) ON CONFLICT(line_index) DO UPDATE SET frequencies=excluded.frequencies,levels=excluded.levels",params![frequencies,levels])?;
    cancelled(cancel, deadline)?;
    tx.commit()?;
    data_version(db)
}
