//! Strong identities for disposable sources; unknown replacements are retained.
use crate::{
    HistoryError, Result,
    storage::{Roots, checked, private_file},
};
use std::{
    fs::{self, File, Metadata},
    io::Write,
    path::{Path, PathBuf},
    sync::Arc,
};

struct Entry {
    relative: PathBuf,
    handle: File,
    directory: bool,
}
pub(crate) struct SnapshotOwner {
    temporary: Option<tempfile::TempDir>,
    roots: Arc<Roots>,
    entries: Vec<Entry>,
}
fn io<T>(value: std::io::Result<T>) -> Result<T> {
    value.map_err(|_| HistoryError::UnsafeStorage)
}
fn same(left: &Metadata, right: &Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        left.dev() == right.dev()
            && left.ino() == right.ino()
            && left.file_type() == right.file_type()
    }
    #[cfg(not(unix))]
    {
        let _ = (left, right);
        false // History refuses unsupported child platforms before creating a source.
    }
}
impl SnapshotOwner {
    pub(crate) fn new(roots: Arc<Roots>) -> Result<Self> {
        let mut builder = tempfile::Builder::new();
        builder.prefix(".history-snapshot-");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            builder.permissions(fs::Permissions::from_mode(0o700));
        }
        let temporary = io(builder.tempdir_in(&roots.snapshots))?;
        let mut owner = Self {
            temporary: Some(temporary),
            roots,
            entries: Vec::new(),
        };
        let handle = io(File::open(owner.path()?))?;
        owner.entries.push(Entry {
            relative: PathBuf::new(),
            handle,
            directory: true,
        });
        let mut marker = owner.file(".history-owned")?;
        io(marker.write_all(b"ori-history-owned-v1\n"))?;
        io(marker.sync_all())?;
        Ok(owner)
    }
    pub(crate) fn path(&self) -> Result<&Path> {
        self.temporary
            .as_ref()
            .map(|owned| owned.path())
            .ok_or(HistoryError::Internal)
    }
    pub(crate) fn directory(&mut self, relative: &str) -> Result<()> {
        self.validate()?;
        let path = self.path()?.join(relative);
        let mut builder = fs::DirBuilder::new();
        builder.recursive(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        io(builder.create(&path))?;
        self.entries.push(Entry {
            relative: relative.into(),
            handle: io(File::open(path))?,
            directory: true,
        });
        Ok(())
    }
    pub(crate) fn file(&mut self, relative: &str) -> Result<File> {
        self.validate()?;
        let file = private_file(&self.path()?.join(relative))?;
        self.entries.push(Entry {
            relative: relative.into(),
            handle: io(file.try_clone())?,
            directory: false,
        });
        Ok(file)
    }
    pub(crate) fn rename(&mut self, from: &str, to: &str) -> Result<()> {
        self.validate()?;
        io(fs::rename(self.path()?.join(from), self.path()?.join(to)))?;
        for entry in &mut self.entries {
            if let Ok(suffix) = entry.relative.strip_prefix(from) {
                entry.relative = Path::new(to).join(suffix);
            }
        }
        self.validate()
    }
    fn walk(&self, relative: &Path, count: &mut usize) -> Result<()> {
        for entry in io(fs::read_dir(self.path()?.join(relative)))? {
            let relative = relative.join(io(entry)?.file_name());
            let known = self
                .entries
                .iter()
                .find(|entry| entry.relative == relative)
                .ok_or(HistoryError::UnsafeStorage)?;
            *count += 1;
            if *count > self.entries.len() {
                return Err(HistoryError::UnsafeStorage);
            }
            if known.directory {
                self.walk(&relative, count)?;
            }
        }
        Ok(())
    }
    pub(crate) fn validate(&self) -> Result<()> {
        if self.entries.is_empty() {
            return Err(HistoryError::UnsafeStorage);
        }
        for entry in &self.entries {
            let path = self.path()?.join(&entry.relative);
            checked(&path, entry.directory)?;
            if !same(
                &io(fs::symlink_metadata(path))?,
                &io(entry.handle.metadata())?,
            ) {
                return Err(HistoryError::UnsafeStorage);
            }
        }
        let mut count = 1;
        self.walk(Path::new(""), &mut count)?;
        if count != self.entries.len() {
            return Err(HistoryError::UnsafeStorage);
        }
        Ok(())
    }
}
impl Drop for SnapshotOwner {
    fn drop(&mut self) {
        let valid = self.validate().is_ok();
        if let Some(owned) = self.temporary.take() {
            if !valid {
                // TempDir must not recursively remove a substituted or unknown
                // path, even when its basename and ownership marker look valid.
                let _ = owned.keep();
                self.roots.cleanup_failure();
            } else if owned.close().is_err() {
                self.roots.cleanup_failure();
            }
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::HistoryConfig;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn new_snapshot_is_private_before_its_marker_and_owned_cleanup() {
        let data = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
        let config = HistoryConfig::new(data.path().join("cache"), data.path().join("snapshots"));
        let roots = Arc::new(Roots::open(&config).unwrap());
        let owner = SnapshotOwner::new(roots.clone()).unwrap();
        let path = owner.path().unwrap().to_owned();
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::read(path.join(".history-owned")).unwrap(),
            b"ori-history-owned-v1\n"
        );
        owner.validate().unwrap();
        drop(owner);
        assert!(!path.exists());
        assert!(!roots.cleanup_failed());
    }
}
