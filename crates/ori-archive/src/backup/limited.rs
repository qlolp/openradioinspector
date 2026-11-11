//! Explicit policy for one private recovery copy. Legacy policy stays separate.
use super::*;
use std::sync::atomic::AtomicBool;

const RATE: u128 = 20 * 1024 * 1024;
const CANCEL_POLL: Duration = Duration::from_millis(10);

/// Cooperative logical I/O admission. Implementations own their shared policy
/// and must return when cancellation/deadline policy requires it.
pub trait BackupIoBudget: Send + Sync {
    fn account(&self, bytes: u64) -> Result<()>;
}

/// Shared, permanent cancellation for one explicitly admitted recovery backup.
/// Signal cancellation and await the backup task to observe completed cleanup.
#[derive(Clone, Default)]
pub struct BackupCancellation(Arc<AtomicBool>);
impl BackupCancellation {
    /// Request cooperative cancellation; this does not wait for file I/O to drain.
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }
    /// Whether this token or any of its clones requested cancellation.
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}
/// All policy is explicit. This quota covers the SQLite snapshot, not the
/// caller's complete filesystem reservation for indexes/manifests or other jobs.
#[derive(Clone)]
pub struct BackupOptions {
    /// Logical SQLite snapshot bytes, including free pages and stored artifacts.
    pub max_snapshot_bytes: u64,
    /// Snapshot-copy deadline measured from admission, including reader queue wait.
    pub copy_timeout: Duration,
    /// Whole-operation deadline, including validation, packaging and publication.
    pub total_timeout: Duration,
    /// Cancellation is shared with clones retained by the caller.
    pub cancel: BackupCancellation,
}
#[derive(Clone)]
struct Policy {
    max_snapshot_bytes: u64,
    copy_deadline: Instant,
    total_deadline: Instant,
    cancel: BackupCancellation,
    io_budget: Option<Arc<dyn BackupIoBudget>>,
}
impl Policy {
    fn new(options: BackupOptions, admitted: Instant) -> Result<Self> {
        if options.max_snapshot_bytes == 0
            || options.copy_timeout.is_zero()
            || options.total_timeout.is_zero()
            || options.copy_timeout > options.total_timeout
        {
            return Err(error::invalid("backup quota or explicit deadlines"));
        }
        let copy_deadline = admitted
            .checked_add(options.copy_timeout)
            .ok_or_else(|| error::invalid("backup copy deadline overflow"))?;
        let total_deadline = admitted
            .checked_add(options.total_timeout)
            .ok_or_else(|| error::invalid("backup total deadline overflow"))?;
        let policy = Self {
            max_snapshot_bytes: options.max_snapshot_bytes,
            copy_deadline,
            total_deadline,
            cancel: options.cancel,
            io_budget: None,
        };
        policy.check_total()?;
        Ok(policy)
    }
    fn check_total(&self) -> Result<()> {
        if self.cancel.is_cancelled() {
            return Err(ArchiveError::Closed);
        }
        if Instant::now() >= self.total_deadline {
            return Err(ArchiveError::QueryBudget);
        }
        Ok(())
    }
    fn check_copy(&self) -> Result<()> {
        self.check_total()?;
        if Instant::now() >= self.copy_deadline {
            return Err(ArchiveError::QueryBudget);
        }
        Ok(())
    }
    fn admit_size(&self, bytes: u64) -> Result<()> {
        self.check_copy()?;
        if bytes > self.max_snapshot_bytes {
            return Err(ArchiveError::QueryBudget);
        }
        // Integer nanoseconds, rounded up: no overflow or floating epoch math.
        let nanos = (u128::from(bytes) * 2 * 1_000_000_000).div_ceil(RATE);
        if nanos
            > self
                .copy_deadline
                .saturating_duration_since(Instant::now())
                .as_nanos()
        {
            return Err(ArchiveError::QueryBudget);
        }
        Ok(())
    }
    fn normalize(&self, error: ArchiveError) -> ArchiveError {
        if matches!(&error, ArchiveError::Sql(rusqlite::Error::SqliteFailure(sql, _))
            if sql.code == rusqlite::ErrorCode::OperationInterrupted)
        {
            if self.cancel.is_cancelled() {
                ArchiveError::Closed
            } else {
                ArchiveError::QueryBudget
            }
        } else {
            error
        }
    }
}
struct LimitedBudget {
    policy: Policy,
    began: Instant,
    bytes: u64,
    copying: bool,
}
impl LimitedBudget {
    fn new(policy: Policy) -> Self {
        Self {
            policy,
            began: Instant::now(),
            bytes: 0,
            copying: true,
        }
    }
    fn charge_scan(&mut self, bytes: u64) -> Result<()> {
        let mut remaining = bytes;
        while remaining != 0 {
            let chunk = remaining.min(CHUNK as u64);
            self.before_io(chunk)?;
            self.account(chunk)?;
            remaining -= chunk;
        }
        Ok(())
    }
}
impl Accounting for LimitedBudget {
    fn before_io(&mut self, bytes: u64) -> Result<()> {
        self.check()?;
        if bytes != 0
            && let Some(budget) = &self.policy.io_budget
        {
            let result = budget.account(bytes);
            self.check()?;
            result?;
        }
        self.check()
    }
    fn account(&mut self, bytes: u64) -> Result<()> {
        self.check()?;
        self.bytes = self
            .bytes
            .checked_add(bytes)
            .ok_or(ArchiveError::QueryBudget)?;
        let nanos = (u128::from(self.bytes) * 1_000_000_000).div_ceil(RATE);
        let seconds =
            u64::try_from(nanos / 1_000_000_000).map_err(|_| ArchiveError::QueryBudget)?;
        let minimum = Duration::new(seconds, (nanos % 1_000_000_000) as u32);
        let until = self
            .began
            .checked_add(minimum)
            .ok_or(ArchiveError::QueryBudget)?;
        while let Some(wait) = until.checked_duration_since(Instant::now()) {
            self.check()?;
            std::thread::sleep(wait.min(CANCEL_POLL));
        }
        self.check()
    }
    fn check(&self) -> Result<()> {
        if self.copying {
            self.policy.check_copy()
        } else {
            self.policy.check_total()
        }
    }
    fn copy_size(&self, bytes: u64) -> Result<()> {
        if bytes > self.policy.max_snapshot_bytes {
            Err(ArchiveError::QueryBudget)
        } else {
            Ok(())
        }
    }
    fn configure(&self, db: &mut Connection) -> Result<()> {
        self.check()?;
        let policy = self.policy.clone();
        db.progress_handler(1000, Some(move || policy.check_total().is_err()))?;
        db.execute_batch("PRAGMA query_only=ON; PRAGMA cache_size=-4096; PRAGMA mmap_size=0; PRAGMA temp_store=FILE; PRAGMA trusted_schema=OFF")?;
        Ok(())
    }
    fn check_integrity(&mut self, db: &Connection) -> Result<()> {
        // Charge an explicit logical full-file scan for each validation pass.
        // This is application accounting, not a claim of physical disk I/O.
        self.charge_scan(schema::database_bytes(db)?)?;
        integrity(db)?;
        self.check()
    }
}
#[cfg(test)]
type CopyHook = Box<dyn FnOnce(&mut LimitedBudget) + Send>;
#[cfg(test)]
type VerifyHook = Box<dyn FnOnce(&Path) + Send>;

#[derive(Default)]
struct Hooks {
    #[cfg(test)]
    after_admission: Option<Box<dyn FnOnce() + Send>>,
    #[cfg(test)]
    after_copy: Option<CopyHook>,
    #[cfg(test)]
    before_verify: Option<VerifyHook>,
}

fn limited_backup(
    source: &mut Connection,
    destination: &Path,
    policy: Policy,
    owner: &actor::Inner,
    hooks: Hooks,
) -> Result<Value> {
    let mut budget = LimitedBudget::new(policy.clone());
    budget.configure(source)?;
    source.query_row("SELECT name FROM sqlite_master LIMIT 1", [], |row| {
        row.get::<_, String>(0)
    })?;
    policy.admit_size(schema::database_bytes(source)?)?;
    absent(destination)?;
    #[cfg(test)]
    let mut hooks = hooks;
    #[cfg(test)]
    if let Some(hook) = hooks.after_admission.take() {
        hook();
    }
    budget.check()?;
    let parent = parent(destination)?;
    fs::create_dir_all(parent)?;
    let stage = tempfile::Builder::new()
        .prefix(".ori-recovery-")
        .tempdir_in(parent)?;
    let mut published = false;
    let result = fill_and_publish(
        source,
        destination,
        stage.path(),
        &mut budget,
        &mut published,
        owner.config.max_response_bytes,
        hooks,
    );
    // No connection/file handle from fill_and_publish survives this point.
    let mut cleanup_failed = stage.close().is_err();
    let result = result.and_then(|value| {
        policy.check_total()?;
        Ok(value)
    });
    if (result.is_err() || cleanup_failed) && published {
        cleanup_failed |= fs::remove_dir_all(destination).is_err();
        if !cleanup_failed {
            cleanup_failed |= sync_directory(parent).is_err();
        }
    }
    if cleanup_failed {
        owner.record_cleanup_failure();
        return Err(ArchiveError::WorkerStopped);
    }
    result.map_err(|error| policy.normalize(error))
}

fn fill_and_publish(
    source: &Connection,
    destination: &Path,
    stage: &Path,
    budget: &mut LimitedBudget,
    published: &mut bool,
    max_response_bytes: usize,
    hooks: Hooks,
) -> Result<Value> {
    let archive = stage.join("archive.sqlite");
    copy_snapshot(source, &archive, budget.policy.copy_deadline, budget)?;
    #[cfg(test)]
    let mut hooks = hooks;
    #[cfg(test)]
    if let Some(hook) = hooks.after_copy.take() {
        hook(budget);
    }
    // The final step/flush may have completed just as a deadline expired.
    budget.check()?;
    budget.copy_size(fs::metadata(&archive)?.len())?;
    source.execute_batch("ROLLBACK")?;
    budget.copying = false;
    let mut db = db_open(&archive)?;
    budget.configure(&mut db)?;
    budget.check_integrity(&db)?;
    let index_path = stage.join("artifacts.ndjson");
    let mut index = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&index_path)?;
    let artifact_count = artifacts(&db, budget, |entry, budget| {
        write_json_record(&mut index, &entry, false, budget)
    })?;
    index.sync_all()?;
    drop(index);
    private_file(&index_path)?;
    let manifest = json!({"backup_version":1,"purpose":"private-recovery",
        "archive_schema":db.query_row("PRAGMA user_version",[],|row|row.get::<_,i64>(0))?,
        "archive":{"file":"archive.sqlite","sha256":digest_file(&archive,budget)?,"bytes":fs::metadata(&archive)?.len()},
        "artifacts":{"file":"artifacts.ndjson","sha256":digest_file(&index_path,budget)?},
        "counts":counts(&db,artifact_count)?,
        "event_watermark":db.query_row("SELECT COALESCE(MAX(sequence),0) FROM events",[],|row|row.get::<_,i64>(0))?});
    db.close().map_err(|(_, error)| ArchiveError::Sql(error))?;
    write_budgeted_json(&stage.join("manifest.json"), &manifest, budget)?;
    #[cfg(test)]
    if let Some(hook) = hooks.before_verify.take() {
        hook(stage);
    }
    #[cfg(not(test))]
    let _ = hooks;
    verify_with_budget(stage, budget)?;
    actor::check_response_size(&manifest, max_response_bytes)?;
    budget.check()?;
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(destination)?;
    *published = true;
    for file in FILES {
        budget.check()?;
        fs::rename(stage.join(file), destination.join(file))?;
    }
    sync_directory(destination)?;
    sync_directory(parent(destination)?)?;
    budget.check()?;
    Ok(manifest)
}

impl Archive {
    /// Create an explicitly limited backup with an additional shared I/O budget.
    pub async fn backup_with_io_budget(
        &self,
        destination: &Path,
        options: BackupOptions,
        budget: Arc<dyn BackupIoBudget>,
    ) -> Result<Value> {
        self.backup_with_policy(destination, options, Hooks::default(), Some(budget))
            .await
    }

    /// Create a verified private recovery bundle under explicit per-operation limits.
    /// The destination must be absent. Cancellation and deadline errors are returned
    /// only after owned copy/verification/publication work and cleanup have drained.
    /// Retain and await the task even after requesting cancellation.
    pub async fn backup_with_options(
        &self,
        destination: &Path,
        options: BackupOptions,
    ) -> Result<Value> {
        self.backup_with_hooks(destination, options, Hooks::default())
            .await
    }
    async fn backup_with_hooks(
        &self,
        destination: &Path,
        options: BackupOptions,
        hooks: Hooks,
    ) -> Result<Value> {
        self.backup_with_policy(destination, options, hooks, None)
            .await
    }
    async fn backup_with_policy(
        &self,
        destination: &Path,
        options: BackupOptions,
        hooks: Hooks,
        io_budget: Option<Arc<dyn BackupIoBudget>>,
    ) -> Result<Value> {
        self.inner.ensure_cleanup_valid()?;
        let timeout = options.total_timeout;
        let mut policy = Policy::new(options, Instant::now())?;
        policy.io_budget = io_budget;
        if self.inner.backup_busy.swap(true, Ordering::AcqRel) {
            return Err(ArchiveError::QueueFull { queue: "backup" });
        }
        let lease = BackupLease(self.inner.clone());
        let destination = destination.to_owned();
        let owner = self.inner.clone();
        self.inner
            .read_mut_with(timeout, move |db| {
                let _lease = lease;
                let normalizer = policy.clone();
                let result = limited_backup(db, &destination, policy, &owner, hooks);
                // Keep the budget registration through the actual SQL cleanup,
                // even when the caller dropped its receiving future.
                let clear = db.progress_handler(0, None::<fn() -> bool>);
                let rollback = if db.is_autocommit() {
                    Ok(())
                } else {
                    db.execute_batch("ROLLBACK")
                };
                if clear.is_err() || rollback.is_err() {
                    owner.record_cleanup_failure();
                    return Err(ArchiveError::WorkerStopped);
                }
                result.map_err(|error| normalizer.normalize(error))
            })
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::channel;

    async fn seeded(readers: usize) -> (tempfile::TempDir, Archive) {
        let root = tempfile::tempdir().unwrap();
        let mut config = ArchiveConfig::new(root.path().join("archive.sqlite"));
        config.readers = readers;
        let archive = Archive::open(config).unwrap();
        archive
            .create_session(json!({"id":"owned","name":"Owned budget test",
            "created_at":"2026-11-11T00:00:00Z","streams":[]}))
            .await
            .unwrap();
        (root, archive)
    }
    fn options() -> BackupOptions {
        BackupOptions {
            max_snapshot_bytes: 1024 * 1024,
            copy_timeout: Duration::from_secs(2),
            total_timeout: Duration::from_secs(5),
            cancel: BackupCancellation::default(),
        }
    }
    fn no_stages(root: &Path) -> bool {
        fs::read_dir(root).unwrap().all(|entry| {
            !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".ori-recovery-")
        })
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn cancelled_waiter_keeps_backup_slot_and_writer_until_owned_reader_really_drains() {
        let (root, archive) = seeded(2).await;
        let target = root.path().join("not-created/bundle");
        let destination = target.clone();
        let policy = options();
        let cancel = policy.cancel.clone();
        let (entered, entry) = channel();
        let (release, released) = channel();
        let worker = archive.clone();
        let waiter = tokio::spawn(async move {
            worker
                .backup_with_hooks(
                    &destination,
                    policy,
                    Hooks {
                        after_admission: Some(Box::new(move || {
                            entered.send(()).unwrap();
                            released.recv().unwrap();
                        })),
                        ..Default::default()
                    },
                )
                .await
        });
        tokio::task::spawn_blocking(move || entry.recv().unwrap())
            .await
            .unwrap();
        assert!(matches!(
            archive.backup(&root.path().join("parallel")).await,
            Err(ArchiveError::QueueFull { .. })
        ));
        waiter.abort();
        let _ = waiter.await;
        cancel.cancel();
        let closing = archive.clone();
        let close = tokio::spawn(async move { closing.close().await });
        while !archive.queue_status().closing {
            tokio::task::yield_now().await;
        }
        assert!(!close.is_finished());
        assert!(matches!(
            Archive::open(ArchiveConfig::new(root.path().join("archive.sqlite"))),
            Err(ArchiveError::AlreadyOpen)
        ));
        release.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(2), close)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert!(!target.parent().unwrap().exists());
        assert!(no_stages(root.path()));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn absolute_deadline_includes_the_existing_reader_queue_wait() {
        let (root, archive) = seeded(1).await;
        let (entered, entry) = channel();
        let (release, released) = channel();
        let blocker = archive.clone();
        let blocked = tokio::spawn(async move {
            blocker
                .inner
                .read_with(Duration::from_secs(5), false, move |_| {
                    entered.send(()).unwrap();
                    released.recv().unwrap();
                    Ok(())
                })
                .await
        });
        tokio::task::spawn_blocking(move || entry.recv().unwrap())
            .await
            .unwrap();
        let mut policy = options();
        policy.copy_timeout = Duration::from_secs(1);
        policy.total_timeout = Duration::from_secs(1);
        let destination = root.path().join("expired/bundle");
        let target = destination.clone();
        let worker = archive.clone();
        let backup = tokio::spawn(async move { worker.backup_with_options(&target, policy).await });
        while !archive.inner.backup_busy.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
        tokio::time::sleep(Duration::from_millis(1100)).await;
        release.send(()).unwrap();
        blocked.await.unwrap().unwrap();
        assert!(matches!(
            backup.await.unwrap(),
            Err(ArchiveError::QueryBudget)
        ));
        assert!(!destination.parent().unwrap().exists());
        archive.close().await.unwrap();
    }

    #[tokio::test]
    async fn deadline_expiring_after_the_final_copy_step_cannot_publish_a_success() {
        let (root, archive) = seeded(2).await;
        let target = root.path().join("late");
        let result = archive
            .backup_with_hooks(
                &target,
                options(),
                Hooks {
                    // Deterministic expiration after a real complete SQLite copy.
                    after_copy: Some(Box::new(|budget| {
                        budget.policy.copy_deadline = Instant::now() - Duration::from_millis(1);
                    })),
                    ..Default::default()
                },
            )
            .await;
        assert!(matches!(result, Err(ArchiveError::QueryBudget)));
        assert!(!target.exists());
        assert!(no_stages(root.path()));
        archive.close().await.unwrap();
    }

    #[tokio::test]
    async fn verifier_rejects_a_changed_sealed_copy_without_publishing_or_mutating_the_source() {
        let (root, archive) = seeded(2).await;
        let before = archive.events(0, 100).await.unwrap();
        let target = root.path().join("corrupt");
        assert!(
            archive
                .backup_with_hooks(
                    &target,
                    options(),
                    Hooks {
                        before_verify: Some(Box::new(|stage| {
                            let path = stage.join("manifest.json");
                            let mut manifest: Value =
                                serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
                            manifest["archive"]["sha256"] = json!(
                                "0000000000000000000000000000000000000000000000000000000000000000"
                            );
                            fs::write(path, serde_json::to_vec(&manifest).unwrap()).unwrap();
                        })),
                        ..Default::default()
                    }
                )
                .await
                .is_err()
        );
        assert!(!target.exists());
        assert!(no_stages(root.path()));
        assert_eq!(archive.events(0, 100).await.unwrap(), before);
        archive.close().await.unwrap();
    }

    #[tokio::test]
    async fn failed_owned_stage_cleanup_latches_archive_failure_and_never_becomes_success() {
        let (root, archive) = seeded(2).await;
        let moved = root.path().join("retained-owned-stage");
        let retained = moved.clone();
        let target = root.path().join("not-published");
        assert!(matches!(
            archive
                .backup_with_hooks(
                    &target,
                    options(),
                    Hooks {
                        before_verify: Some(Box::new(move |stage| {
                            fs::rename(stage, &moved).unwrap();
                            fs::write(stage, b"owned replacement; cleanup must fail visibly")
                                .unwrap();
                        })),
                        ..Default::default()
                    }
                )
                .await,
            Err(ArchiveError::WorkerStopped)
        ));
        assert!(retained.join("archive.sqlite").is_file());
        assert!(!target.exists());
        assert!(matches!(
            archive
                .backup_with_options(&root.path().join("retry"), options())
                .await,
            Err(ArchiveError::WorkerStopped)
        ));
        assert!(archive.close().await.is_err());
        assert!(archive.close().await.is_err());
    }
}
