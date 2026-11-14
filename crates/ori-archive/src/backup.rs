mod limited;
use crate::*;
pub use limited::{BackupCancellation, BackupIoBudget, BackupOptions};
use rusqlite::{
    Connection, OpenFlags, OptionalExtension,
    backup::{Backup, StepResult},
    params,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{BufWriter, Read, Write},
    path::{Path, PathBuf},
    sync::{Arc, atomic::Ordering},
    time::{Duration, Instant},
};
const CHUNK: usize = 1024 * 1024;
const FILES: [&str; 3] = ["archive.sqlite", "artifacts.ndjson", "manifest.json"];

struct Budget {
    began: Instant,
    bytes: u64,
}
impl Budget {
    fn new() -> Self {
        Self {
            began: Instant::now(),
            bytes: 0,
        }
    }
    fn account(&mut self, bytes: u64) {
        self.bytes = self.bytes.saturating_add(bytes);
        let minimum = Duration::from_secs_f64(self.bytes as f64 / (20.0 * 1024.0 * 1024.0));
        if let Some(wait) = minimum.checked_sub(self.began.elapsed()) {
            std::thread::sleep(wait);
        }
    }
}
/// Keep legacy accounting unchanged while explicit operations add cancellation,
/// absolute deadlines and a source quota to the same production copy/validator.
trait Accounting {
    fn account(&mut self, bytes: u64) -> Result<()>;
    fn before_io(&mut self, _bytes: u64) -> Result<()> {
        self.check()
    }
    fn check(&self) -> Result<()> {
        Ok(())
    }
    fn copy_size(&self, _bytes: u64) -> Result<()> {
        Ok(())
    }
    fn configure(&self, _db: &mut Connection) -> Result<()> {
        Ok(())
    }
    fn check_integrity(&mut self, db: &Connection) -> Result<()> {
        integrity(db)
    }
}
impl Accounting for Budget {
    fn account(&mut self, bytes: u64) -> Result<()> {
        Budget::account(self, bytes);
        Ok(())
    }
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn digest_file(path: &Path, budget: &mut impl Accounting) -> Result<String> {
    budget.check()?;
    let mut source = File::open(path)?;
    let mut digest = Sha256::new();
    let mut block = vec![0; CHUNK];
    let original_bytes = source.metadata()?.len();
    let mut remaining = original_bytes;
    while remaining != 0 {
        let count = remaining.min(CHUNK as u64) as usize;
        budget.before_io(count as u64)?;
        source.read_exact(&mut block[..count])?;
        digest.update(&block[..count]);
        budget.account(count as u64)?;
        remaining -= count as u64;
    }
    if source.metadata()?.len() != original_bytes {
        return Err(ArchiveError::Corrupt(
            "recovery file changed while hashing".into(),
        ));
    }
    budget.check()?;
    Ok(hex(&digest.finalize()))
}
fn private_file(_path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(_path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}
fn sync_directory(_path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        File::open(_path)?.sync_all()?;
    }
    Ok(())
}
fn sync_file(path: &Path) -> Result<()> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)?
        .sync_all()?;
    Ok(())
}
fn write_json(path: &Path, value: &Value) -> Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    serde_json::to_writer_pretty(&mut file, value)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    private_file(path)
}

struct AccountedWriter<'a, A> {
    output: &'a mut File,
    budget: &'a mut A,
    error: Option<ArchiveError>,
}
impl<A: Accounting> Write for AccountedWriter<'_, A> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        let chunk = &bytes[..bytes.len().min(CHUNK)];
        if chunk.is_empty() {
            return Ok(0);
        }
        let result = (|| {
            self.budget.before_io(chunk.len() as u64)?;
            self.output.write_all(chunk)?;
            self.budget.check()
        })();
        if let Err(error) = result {
            self.error = Some(error);
            return Err(std::io::Error::other("recovery I/O admission failed"));
        }
        Ok(chunk.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.output.flush()
    }
}
fn write_json_record<A: Accounting>(
    output: &mut File,
    value: &Value,
    pretty: bool,
    budget: &mut A,
) -> Result<()> {
    let mut writer = BufWriter::with_capacity(
        65536,
        AccountedWriter {
            output,
            budget,
            error: None,
        },
    );
    let result = (|| -> Result<()> {
        if pretty {
            serde_json::to_writer_pretty(&mut writer, value)?;
        } else {
            serde_json::to_writer(&mut writer, value)?;
        }
        writer.write_all(b"\n")?;
        writer.flush()?;
        Ok(())
    })();
    // Never retry a refused write while dropping the serialization buffer.
    let (writer, _) = writer.into_parts();
    match writer.error {
        Some(error) => Err(error),
        None => result,
    }
}
fn write_budgeted_json(path: &Path, value: &Value, budget: &mut impl Accounting) -> Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    write_json_record(&mut file, value, true, budget)?;
    file.sync_all()?;
    private_file(path)
}

struct IndexReader {
    file: File,
    buffer: Vec<u8>,
    cursor: usize,
    remaining: u64,
    original_bytes: u64,
}
impl IndexReader {
    fn open(path: &Path) -> Result<Self> {
        let file = File::open(path)?;
        let bytes = file.metadata()?.len();
        Ok(Self {
            file,
            buffer: Vec::new(),
            cursor: 0,
            remaining: bytes,
            original_bytes: bytes,
        })
    }
    fn line(&mut self, budget: &mut impl Accounting) -> Result<Vec<u8>> {
        let mut line = Vec::new();
        loop {
            if self.cursor == self.buffer.len() {
                if self.remaining == 0 {
                    return Ok(line);
                }
                let count = self.remaining.min(CHUNK as u64) as usize;
                budget.before_io(count as u64)?;
                self.buffer.resize(count, 0);
                self.file.read_exact(&mut self.buffer)?;
                self.remaining -= count as u64;
                self.cursor = 0;
            }
            let available = &self.buffer[self.cursor..];
            let count = available
                .iter()
                .position(|byte| *byte == b'\n')
                .map_or(available.len(), |index| index + 1);
            if line.len() + count > CHUNK {
                return Err(ArchiveError::Corrupt("recovery artifact index".into()));
            }
            line.extend_from_slice(&available[..count]);
            self.cursor += count;
            if line.ends_with(b"\n") {
                return Ok(line);
            }
        }
    }
    fn has_tail(&self) -> Result<bool> {
        if self.file.metadata()?.len() != self.original_bytes {
            return Err(ArchiveError::Corrupt(
                "recovery artifact index changed".into(),
            ));
        }
        Ok(self.cursor != self.buffer.len() || self.remaining != 0)
    }
}
fn absent(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(_) => Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "archive destination already exists",
        )
        .into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}
fn restore_destination(path: &Path) -> Result<()> {
    absent(path)?;
    for suffix in ["-wal", "-shm", "-journal"] {
        absent(&PathBuf::from(format!("{}{suffix}", path.display())))?;
    }
    Ok(())
}
fn parent(path: &Path) -> Result<&Path> {
    path.parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map_or(Ok(Path::new(".")), Ok)
}
fn db_open(path: &Path) -> Result<Connection> {
    let db = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    schema::functions(&db)?;
    Ok(db)
}
fn has_table(db: &Connection, name: &str) -> Result<bool> {
    Ok(db
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            [name],
            |r| r.get::<_, i64>(0),
        )
        .optional()?
        .is_some())
}
fn integrity(db: &Connection) -> Result<()> {
    crate::candidate_precision::validate_rows(db)?;
    if db.query_row("PRAGMA integrity_check(1)", [], |row| {
        row.get::<_, String>(0)
    })? != "ok"
    {
        return Err(ArchiveError::Corrupt("backup SQLite integrity".into()));
    }
    if db
        .prepare("PRAGMA foreign_key_check")?
        .query([])?
        .next()?
        .is_some()
    {
        return Err(ArchiveError::Corrupt("backup foreign key integrity".into()));
    }
    for table in ["sessions", "streams", "sweeps", "observations", "events"] {
        if !has_table(db, table)? {
            return Err(ArchiveError::Corrupt(
                "backup canonical tables missing".into(),
            ));
        }
    }
    schema::versions(db)?;
    if has_table(db, "ori_observation_level_bits")? {
        let mut statement=db.prepare("SELECT p.level_bits_le,o.level FROM ori_observation_level_bits p LEFT JOIN observations o ON o.id=p.observation_id")?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            let bytes: Vec<u8> = row.get(0)?;
            let bits: [u8; 8] = bytes
                .try_into()
                .map_err(|_| ArchiveError::Corrupt("backup precision length".into()))?;
            let value = f64::from_le_bytes(bits);
            let stored: Option<f64> = row.get(1)?;
            if !value.is_finite() || stored != Some(value) {
                return Err(ArchiveError::Corrupt("backup precision value".into()));
            }
        }
    }
    Ok(())
}

fn artifacts<A: Accounting>(
    db: &Connection,
    budget: &mut A,
    mut consume: impl FnMut(Value, &mut A) -> Result<()>,
) -> Result<u64> {
    if !has_table(db, "instrument_artifacts")? {
        return Ok(0);
    }
    let mut statement = db.prepare(
        "SELECT id,kind,mime_type,sha256,length(payload) FROM instrument_artifacts ORDER BY id",
    )?;
    let mut rows = statement.query([])?;
    let mut count = 0;
    while let Some(row) = rows.next()? {
        budget.check()?;
        let id: String = row.get(0)?;
        let kind: String = row.get(1)?;
        let mime: String = row.get(2)?;
        let sha: String = row.get(3)?;
        let size: i64 = row.get(4)?;
        if !(0..=32 * 1024 * 1024).contains(&size) {
            return Err(ArchiveError::Corrupt("closed artifact size".into()));
        }
        let mut digest = Sha256::new();
        let mut offset = 0;
        while offset < size {
            budget.before_io((size - offset).min(CHUNK as i64) as u64)?;
            let block: Vec<u8> = db.query_row(
                "SELECT substr(payload,?,?) FROM instrument_artifacts WHERE id=?",
                params![offset + 1, CHUNK as i64, id],
                |row| row.get(0),
            )?;
            if block.is_empty() {
                return Err(ArchiveError::Corrupt(
                    "closed artifact was truncated".into(),
                ));
            }
            digest.update(&block);
            offset += block.len() as i64;
            budget.account(block.len() as u64)?;
        }
        if hex(&digest.finalize()) != sha {
            return Err(ArchiveError::Corrupt(
                "closed artifact SHA-256 mismatch".into(),
            ));
        }
        consume(
            json!({"id":id,"kind":kind,"mime_type":mime,"sha256":sha,"bytes":size}),
            budget,
        )?;
        count += 1;
    }
    Ok(count)
}
fn counts(db: &Connection, artifacts: u64) -> Result<Value> {
    let mut output = json!({"artifacts":artifacts});
    for table in ["sweeps", "observations", "events"] {
        output[table] = json!(db.query_row(
            &format!("SELECT COUNT(*) FROM {table}"),
            [],
            |row| row.get::<_, i64>(0)
        )?);
    }
    Ok(output)
}
fn copy_snapshot(
    source: &Connection,
    path: &Path,
    deadline: Instant,
    budget: &mut impl Accounting,
) -> Result<()> {
    budget.check()?;
    source.query_row("SELECT name FROM sqlite_master LIMIT 1", [], |row| {
        row.get::<_, String>(0)
    })?; // Acquire the immutable read snapshot before copying a live WAL.
    let mut target = Connection::open(path)?;
    target.pragma_update(None, "synchronous", "FULL")?;
    let page_size: i64 = source.query_row("PRAGMA page_size", [], |row| row.get(0))?;
    let pages: i64 = source.query_row("PRAGMA page_count", [], |row| row.get(0))?;
    let step = (512 * 1024 / page_size).clamp(1, 128) as i32;
    {
        let backup = Backup::new(source, &mut target)?;
        let mut copied = 0_i64;
        loop {
            budget.check()?;
            if Instant::now() > deadline {
                return Err(ArchiveError::QueryBudget);
            }
            let next =
                pages.saturating_sub(copied).max(0).min(i64::from(step)) as u64 * page_size as u64;
            budget.before_io(next)?; // Source read and destination write are distinct logical I/O.
            budget.before_io(next)?;
            let result = backup.step(step)?;
            let progress = backup.progress();
            budget.copy_size(progress.pagecount as u64 * page_size as u64)?;
            let now = (progress.pagecount - progress.remaining) as i64;
            budget.account(now.saturating_sub(copied).max(0) as u64 * page_size as u64 * 2)?;
            budget.check()?;
            copied = now;
            match result {
                StepResult::Done => break,
                StepResult::More => {}
                StepResult::Busy | StepResult::Locked => {
                    std::thread::sleep(Duration::from_millis(5))
                }
                _ => return Err(ArchiveError::WorkerStopped),
            }
        }
    }
    target.query_row("PRAGMA journal_mode=DELETE", [], |row| {
        row.get::<_, String>(0)
    })?;
    target
        .close()
        .map_err(|(_, error)| ArchiveError::Sql(error))?;
    private_file(path)?;
    sync_file(path)?;
    budget.check()?;
    Ok(())
}

fn make_backup(source: &Connection, destination: &Path, timeout: Duration) -> Result<Value> {
    absent(destination)?;
    let parent = parent(destination)?;
    fs::create_dir_all(parent)?;
    let mut builder = tempfile::Builder::new();
    builder.prefix(".ori-recovery-");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(fs::Permissions::from_mode(0o700));
    }
    let stage = builder.tempdir_in(parent)?;
    let archive = stage.path().join("archive.sqlite");
    let mut budget = Budget::new();
    copy_snapshot(source, &archive, Instant::now() + timeout, &mut budget)?;
    source.execute_batch("ROLLBACK")?;
    let db = db_open(&archive)?;
    integrity(&db)?;
    let path = stage.path().join("artifacts.ndjson");
    let mut index = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)?;
    let artifact_count = artifacts(&db, &mut budget, |entry, budget| {
        write_json_record(&mut index, &entry, false, budget)
    })?;
    index.sync_all()?;
    private_file(&path)?;
    let manifest = json!({"backup_version":1,"purpose":"private-recovery","archive_schema":db.query_row("PRAGMA user_version",[],|row|row.get::<_,i64>(0))?,"archive":{"file":"archive.sqlite","sha256":digest_file(&archive,&mut budget)?,"bytes":fs::metadata(&archive)?.len()},"artifacts":{"file":"artifacts.ndjson","sha256":digest_file(&path,&mut budget)?},"counts":counts(&db,artifact_count)?,"event_watermark":db.query_row("SELECT COALESCE(MAX(sequence),0) FROM events",[],|row|row.get::<_,i64>(0))?});
    drop(db);
    write_json(&stage.path().join("manifest.json"), &manifest)?;
    verify_backup(stage.path())?;
    let mut builder = fs::DirBuilder::new();
    builder.recursive(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(destination)?;
    let publication = (|| -> Result<()> {
        for file in FILES {
            fs::rename(stage.path().join(file), destination.join(file))?;
        }
        sync_directory(destination)?;
        sync_directory(parent)?;
        Ok(())
    })();
    if let Err(error) = publication {
        let _ = fs::remove_dir_all(destination);
        return Err(error);
    }
    Ok(manifest)
}

pub fn verify_backup(directory: &Path) -> Result<Value> {
    verify_with_budget(directory, &mut Budget::new())
}
fn verify_with_budget(directory: &Path, budget: &mut impl Accounting) -> Result<Value> {
    budget.check()?;
    for file in FILES {
        let info = fs::symlink_metadata(directory.join(file))?;
        if !info.is_file() || info.file_type().is_symlink() {
            return Err(ArchiveError::Corrupt(
                "recovery bundle file is linked or missing".into(),
            ));
        }
    }
    let manifest_path = directory.join("manifest.json");
    if fs::metadata(&manifest_path)?.len() > 65536 {
        return Err(ArchiveError::Corrupt("recovery manifest size".into()));
    }
    budget.before_io(fs::metadata(&manifest_path)?.len())?;
    let manifest = parse_json_value(&fs::read(manifest_path)?, 65536)?;
    if manifest["backup_version"] != 1
        || manifest["purpose"] != "private-recovery"
        || manifest["archive"]["file"] != "archive.sqlite"
        || manifest["artifacts"]["file"] != "artifacts.ndjson"
    {
        return Err(ArchiveError::Corrupt("recovery manifest format".into()));
    }
    let archive = directory.join("archive.sqlite");
    for suffix in ["-wal", "-shm", "-journal"] {
        if fs::symlink_metadata(format!("{}{suffix}", archive.display())).is_ok() {
            return Err(ArchiveError::Corrupt(
                "recovery database is not sealed".into(),
            ));
        }
    }
    for (key, file) in [
        ("archive", "archive.sqlite"),
        ("artifacts", "artifacts.ndjson"),
    ] {
        if manifest[key]["sha256"] != digest_file(&directory.join(file), budget)? {
            return Err(ArchiveError::Corrupt(
                "recovery file SHA-256 mismatch".into(),
            ));
        }
    }
    if manifest["archive"]["bytes"].as_u64() != Some(fs::metadata(&archive)?.len()) {
        return Err(ArchiveError::Corrupt("recovery database size".into()));
    }
    let mut db = db_open(&archive)?;
    budget.configure(&mut db)?;
    budget.check_integrity(&db)?;
    let mut index = IndexReader::open(&directory.join("artifacts.ndjson"))?;
    let artifact_count = artifacts(&db, budget, |entry, budget| {
        let line = index.line(budget)?;
        if line.len() > CHUNK || !line.ends_with(b"\n") || parse_json_value(&line, CHUNK)? != entry
        {
            return Err(ArchiveError::Corrupt("recovery artifact index".into()));
        }
        Ok(())
    })?;
    if index.has_tail()? {
        return Err(ArchiveError::Corrupt(
            "extra recovery artifact entry".into(),
        ));
    }
    let watermark: i64 =
        db.query_row("SELECT COALESCE(MAX(sequence),0) FROM events", [], |row| {
            row.get(0)
        })?;
    let version: i64 = db.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    if manifest["counts"] != counts(&db, artifact_count)?
        || manifest["event_watermark"] != watermark
        || manifest["archive_schema"] != version
    {
        return Err(ArchiveError::Corrupt("recovery snapshot metadata".into()));
    }
    budget.check()?;
    Ok(manifest)
}

pub fn restore_backup(directory: &Path, destination: &Path) -> Result<Value> {
    restore_destination(destination)?;
    let manifest = verify_backup(directory)?;
    let parent = parent(destination)?;
    fs::create_dir_all(parent)?;
    let mut builder = tempfile::Builder::new();
    builder.prefix(".ori-restore-");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(fs::Permissions::from_mode(0o700));
    }
    let staging = builder.tempdir_in(parent)?;
    let target = staging.path().join("archive.sqlite");
    let mut input = File::open(directory.join("archive.sqlite"))?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&target)?;
    let mut block = vec![0; CHUNK];
    let mut budget = Budget::new();
    loop {
        let n = input.read(&mut block)?;
        if n == 0 {
            break;
        }
        output.write_all(&block[..n])?;
        budget.account(n as u64 * 2);
    }
    output.sync_all()?;
    private_file(&target)?;
    if manifest["archive"]["sha256"] != digest_file(&target, &mut budget)? {
        return Err(ArchiveError::Corrupt(
            "recovery source changed during restore".into(),
        ));
    }
    restore_destination(destination)?;
    fs::hard_link(&target, destination)?;
    sync_directory(parent)?;
    Ok(manifest)
}

fn portable(source: &Connection, destination: &Path, timeout: Duration) -> Result<Value> {
    absent(destination)?;
    let parent = parent(destination)?;
    fs::create_dir_all(parent)?;
    let mut builder = tempfile::Builder::new();
    builder.prefix(".ori-portable-");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(fs::Permissions::from_mode(0o700));
    }
    let stage = builder.tempdir_in(parent)?;
    let path = stage.path().join("archive.sqlite");
    let mut budget = Budget::new();
    let deadline = Instant::now() + timeout;
    copy_snapshot(source, &path, deadline, &mut budget)?;
    source.execute_batch("ROLLBACK")?;
    let mut db = Connection::open(&path)?;
    schema::functions(&db)?;
    db.progress_handler(10000, Some(move || Instant::now() > deadline))?;
    db.pragma_update(None, "secure_delete", true)?;
    {
        let tx = db.transaction()?;
        tx.execute("DELETE FROM monitoring_webhooks", [])?;
        let mut after = 0_i64;
        loop {
            let values=tx.prepare("SELECT rowid,id,payload FROM monitoring_records WHERE kind='project' AND rowid>? ORDER BY rowid LIMIT 100")?.query_map([after],|row|Ok((row.get::<_,i64>(0)?,row.get::<_,String>(1)?,row.get::<_,String>(2)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;
            if values.is_empty() {
                break;
            }
            for (rowid, id, raw) in values {
                let mut project = rows::object(raw)?;
                project["autostart"] = json!(false);
                project["resume_requested"] = json!(false);
                project["webhook_enabled"] = json!(false);
                project["coverage"] = json!("stopped");
                if ["armed", "searching", "degraded", "collecting"]
                    .contains(&project["state"].as_str().unwrap_or(""))
                {
                    project["state"] = json!("interrupted");
                }
                if project["preparation"].is_object() {
                    project["preparation"]["receive_path_confirmed"] = json!(false);
                    project["preparation"]["location_confirmed"] = json!(false);
                }
                tx.execute(
                    "UPDATE monitoring_records SET payload=? WHERE kind='project' AND id=?",
                    params![serde_json::to_string(&project)?, id],
                )?;
                after = rowid;
            }
        }
        tx.execute("UPDATE monitoring_notifications SET state='disabled',lease_until=NULL,next_at=NULL WHERE state IN ('pending','delivering')",[])?;
        tx.commit()?;
    }
    db.execute_batch("VACUUM")?;
    db.progress_handler(0, None::<fn() -> bool>)?;
    integrity(&db)?;
    db.close().map_err(|(_, error)| ArchiveError::Sql(error))?;
    private_file(&path)?;
    sync_file(&path)?;
    fs::hard_link(&path, destination)?;
    sync_directory(parent)?;
    Ok(json!({"bytes":fs::metadata(destination)?.len(),"automatic_actions_enabled":false}))
}

struct BackupLease(Arc<actor::Inner>);
impl Drop for BackupLease {
    fn drop(&mut self) {
        self.0.backup_busy.store(false, Ordering::Release);
    }
}
impl Archive {
    pub async fn backup(&self, destination: &Path) -> Result<Value> {
        if self.inner.backup_busy.swap(true, Ordering::AcqRel) {
            return Err(ArchiveError::QueueFull { queue: "backup" });
        }
        let lease = BackupLease(self.inner.clone());
        let destination = destination.to_owned();
        let timeout = self.inner.config.backup_timeout;
        self.inner
            .read_with(timeout, false, move |db| {
                let _lease = lease;
                make_backup(db, &destination, timeout)
            })
            .await
    }
    pub async fn export_portable(&self, destination: &Path) -> Result<Value> {
        if self.inner.backup_busy.swap(true, Ordering::AcqRel) {
            return Err(ArchiveError::QueueFull { queue: "backup" });
        }
        let lease = BackupLease(self.inner.clone());
        let destination = destination.to_owned();
        let timeout = self.inner.config.backup_timeout;
        self.inner
            .read_with(timeout, false, move |db| {
                let _lease = lease;
                portable(db, &destination, timeout)
            })
            .await
    }
}
