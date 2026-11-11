//! Dedicated disposable roots. Request DTOs accept no source/cache filesystem path.
use crate::{HistoryConfig, HistoryError, Result, types::MAX_RESULT_BYTES};
use fs2::FileExt;
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, Ordering},
    },
};

const OWNER: &[u8] = b"ori-history-owned-v1\n";
const WALK_ENTRIES: usize = 8192;
pub(crate) struct Roots {
    pub cache: PathBuf,
    pub worker: PathBuf,
    pub views: PathBuf,
    pub snapshots: PathBuf,
    pub maximum: u64,
    pub readers: Mutex<Vec<Weak<crate::export::ReaderInner>>>,
    _locks: Vec<File>,
    cleanup_failed: AtomicBool,
}
pub(crate) struct SealedSnapshot {
    pub directory: PathBuf,
    pub boundary: u64,
    pub sha256: String,
    pub _owned: crate::snapshot_owner::SnapshotOwner,
    pub _roots: Arc<Roots>,
}
fn io<T>(value: std::io::Result<T>) -> Result<T> {
    value.map_err(|_| HistoryError::UnsafeStorage)
}

pub(crate) fn checked(path: &Path, directory: bool) -> Result<()> {
    let meta = io(fs::symlink_metadata(path))?;
    if meta.file_type().is_symlink()
        || (directory && !meta.is_dir())
        || (!directory && !meta.is_file())
    {
        return Err(HistoryError::UnsafeStorage);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.uid() != unsafe { libc::geteuid() }
            || (directory && meta.mode() & 0o077 != 0)
            || (!directory && meta.nlink() != 1)
        {
            return Err(HistoryError::UnsafeStorage);
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & 0x400 != 0 {
            return Err(HistoryError::UnsafeStorage);
        }
    }
    Ok(())
}
fn normalized(path: &Path) -> Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
    {
        return Err(HistoryError::UnsafeStorage);
    }
    let mut cursor = PathBuf::new();
    for part in path.components() {
        cursor.push(part);
        match fs::symlink_metadata(&cursor) {
            Ok(meta) => {
                if meta.file_type().is_symlink() {
                    return Err(HistoryError::UnsafeStorage);
                }
                #[cfg(unix)]
                {
                    use std::os::unix::fs::MetadataExt;
                    if meta.is_dir()
                        && (meta.uid() != 0 && meta.uid() != unsafe { libc::geteuid() }
                            || meta.mode() & 0o022 != 0 && meta.mode() & 0o1000 == 0)
                    {
                        return Err(HistoryError::UnsafeStorage);
                    }
                }
                #[cfg(windows)]
                {
                    use std::os::windows::fs::MetadataExt;
                    if meta.file_attributes() & 0x400 != 0 {
                        return Err(HistoryError::UnsafeStorage);
                    }
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(HistoryError::UnsafeStorage),
        }
    }
    Ok(())
}
pub(crate) fn private_file(path: &Path) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    io(options.open(path))
}
fn directory(path: &Path) -> Result<()> {
    normalized(path)?;
    if !path.exists() {
        let mut builder = fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        io(builder.create(path))?;
    }
    checked(path, true)
}
fn claim(path: &Path) -> Result<File> {
    directory(path)?;
    let marker = path.join(".history-owner");
    if !marker.exists() {
        if io(fs::read_dir(path))?.next().is_some() {
            return Err(HistoryError::UnsafeStorage);
        }
        let mut file = private_file(&marker)?;
        io(file.write_all(OWNER))?;
        io(file.sync_all())?;
    }
    checked(&marker, false)?;
    if io(fs::metadata(&marker))?.len() != OWNER.len() as u64 {
        return Err(HistoryError::UnsafeStorage);
    }
    if io(fs::read(&marker))? != OWNER {
        return Err(HistoryError::UnsafeStorage);
    }
    let lock = path.join(".history.lock");
    let file = if lock.exists() {
        checked(&lock, false)?;
        io(OpenOptions::new().read(true).write(true).open(&lock))?
    } else {
        private_file(&lock)?
    };
    file.try_lock_exclusive().map_err(|_| HistoryError::Busy)?;
    Ok(file)
}
fn usage(path: &Path, count: &mut usize, depth: usize) -> Result<u64> {
    if depth > 6 {
        return Err(HistoryError::ResourceLimit);
    }
    let mut bytes = 0_u64;
    for entry in io(fs::read_dir(path))? {
        let entry = io(entry)?;
        *count += 1;
        if *count > WALK_ENTRIES {
            return Err(HistoryError::ResourceLimit);
        }
        let meta = io(fs::symlink_metadata(entry.path()))?;
        if meta.file_type().is_symlink() || (!meta.is_dir() && !meta.is_file()) {
            return Err(HistoryError::UnsafeStorage);
        }
        if meta.is_dir() {
            bytes = bytes
                .checked_add(usage(&entry.path(), count, depth + 1)?)
                .ok_or(HistoryError::ResourceLimit)?;
        } else {
            checked(&entry.path(), false)?;
            bytes = bytes
                .checked_add(meta.len())
                .ok_or(HistoryError::ResourceLimit)?;
        }
    }
    Ok(bytes)
}
impl Roots {
    pub fn open(config: &HistoryConfig) -> Result<Self> {
        normalized(&config.cache_directory)?;
        normalized(&config.snapshot_directory)?;
        if config
            .cache_directory
            .starts_with(&config.snapshot_directory)
            || config
                .snapshot_directory
                .starts_with(&config.cache_directory)
        {
            return Err(HistoryError::UnsafeStorage);
        }
        let mut ordered = [
            config.cache_directory.clone(),
            config.snapshot_directory.clone(),
        ];
        ordered.sort();
        let mut locks = Vec::new();
        for path in &ordered {
            locks.push(claim(path)?);
        }
        let root = Self {
            cache: config.cache_directory.clone(),
            worker: config.cache_directory.join("research"),
            views: config.cache_directory.join("views"),
            snapshots: config.snapshot_directory.clone(),
            maximum: config.max_cache_bytes,
            _locks: locks,
            readers: Mutex::new(Vec::new()),
            cleanup_failed: AtomicBool::new(false),
        };
        directory(&root.worker)?;
        directory(&root.views)?;
        root.total()?;
        Ok(root)
    }
    pub fn total(&self) -> Result<u64> {
        let mut count = 0;
        let total = usage(&self.cache, &mut count, 0)?
            .checked_add(usage(&self.snapshots, &mut count, 0)?)
            .ok_or(HistoryError::ResourceLimit)?;
        if total > self.maximum {
            return Err(HistoryError::ResourceLimit);
        }
        Ok(total)
    }
    pub(crate) fn cleanup_failure(&self) {
        self.cleanup_failed.store(true, Ordering::Release);
    }
    pub(crate) fn cleanup_failed(&self) -> bool {
        self.cleanup_failed.load(Ordering::Acquire)
    }
    pub fn reader_count(&self) -> Result<usize> {
        let mut readers = self.readers.lock().map_err(|_| HistoryError::Internal)?;
        readers.retain(|r| r.upgrade().is_some_and(|r| r.is_open()));
        Ok(readers.len())
    }
    pub fn close_readers(&self) -> Result<()> {
        let readers = self
            .readers
            .lock()
            .map_err(|_| HistoryError::Internal)?
            .iter()
            .filter_map(Weak::upgrade)
            .collect::<Vec<_>>();
        for reader in readers {
            reader.close();
        }
        Ok(())
    }
    pub fn worker_budget(&self) -> Result<(u64, u64)> {
        let total = self.total()?;
        let worker = usage(&self.worker, &mut 0, 0)?;
        let available = self.maximum.saturating_sub(total.saturating_sub(worker));
        let cache = available * 3 / 4;
        let temporary = available - cache;
        if cache < 1024 || temporary < 1024 {
            return Err(HistoryError::ResourceLimit);
        }
        if worker > cache {
            return Err(HistoryError::ResourceLimit);
        }
        Ok((cache, temporary))
    }
    pub fn view(&self, key: &str) -> Result<Option<Vec<u8>>> {
        let path = self.views.join(format!("vp-{key}.json"));
        if !path.exists() {
            return Ok(None);
        }
        checked(&path, false)?;
        let file = io(File::open(path))?;
        if io(file.metadata())?.len() > MAX_RESULT_BYTES as u64 {
            return Err(HistoryError::ResourceLimit);
        }
        let mut bytes = Vec::new();
        io(file
            .take(MAX_RESULT_BYTES as u64 + 1)
            .read_to_end(&mut bytes))?;
        if bytes.len() > MAX_RESULT_BYTES {
            return Err(HistoryError::ResourceLimit);
        }
        Ok(Some(bytes))
    }
    pub fn write_view(&self, key: &str, bytes: &[u8]) -> Result<()> {
        if bytes.len() > MAX_RESULT_BYTES
            || self.total()?.saturating_add(bytes.len() as u64) > self.maximum
        {
            return Err(HistoryError::ResourceLimit);
        }
        let path = self.views.join(format!("vp-{key}.json"));
        if path.exists() {
            checked(&path, false)?;
            return Ok(());
        }
        let mut file = private_file(&path)?;
        if io(file.write_all(bytes)).is_err() {
            let _ = fs::remove_file(path);
            return Err(HistoryError::ResourceLimit);
        }
        io(file.sync_all())
    }
}
