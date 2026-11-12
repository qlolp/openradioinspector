//! Cooperative OS leases. Unmodified external legacy programs do not participate.
use crate::{Error, Result};
use fs2::FileExt;
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
};

pub(crate) fn validate_path(path: &Path) -> Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|p| matches!(p, Component::ParentDir | Component::CurDir))
    {
        return Err(Error::Unsafe(
            "an absolute normalized local path is required".into(),
        ));
    }
    let mut cursor = PathBuf::new();
    for component in path.components() {
        cursor.push(component);
        match fs::symlink_metadata(&cursor) {
            Ok(meta) if meta.file_type().is_symlink() => {
                return Err(Error::Unsafe("symlink in private path".into()));
            }
            #[cfg(unix)]
            Ok(meta) => {
                use std::os::unix::fs::MetadataExt;
                if meta.is_dir() && meta.mode() & 0o022 != 0 && meta.mode() & 0o1000 == 0 {
                    return Err(Error::Unsafe(
                        "untrusted writable ancestor in private path".into(),
                    ));
                }
            }
            #[cfg(not(unix))]
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}

pub(crate) fn private_directory(path: &Path) -> Result<()> {
    validate_path(path)?;
    #[cfg(windows)]
    return Err(Error::Unsafe("private LAN state requires a verified owner-only Windows DACL; this build supports explicit loopback mode only".into()));
    #[cfg(not(windows))]
    {
        if !path.exists() {
            let mut builder = fs::DirBuilder::new();
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                builder.mode(0o700);
            }
            builder.create(path)?;
        }
        check_private(path, true)
    }
}

pub(crate) fn check_private(path: &Path, directory: bool) -> Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if meta.file_type().is_symlink()
        || (directory && !meta.is_dir())
        || (!directory && !meta.is_file())
    {
        return Err(Error::Unsafe(
            "private state has an unsafe file type".into(),
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.uid() != unsafe { libc::geteuid() }
            || meta.mode() & 0o077 != 0
            || (!directory && meta.nlink() != 1)
        {
            return Err(Error::Unsafe("private state must be owned by this user with owner-only permissions and no hard links".into()));
        }
    }
    Ok(())
}

pub(crate) fn open_private(path: &Path) -> Result<File> {
    validate_path(path)?;
    if path.exists() {
        check_private(path, false)?;
    }
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    let file = options.open(path)?;
    check_private(path, false)?;
    Ok(file)
}

pub(crate) fn read_private(path: &Path, limit: u64) -> Result<Vec<u8>> {
    check_private(path, false)?;
    let file = open_private(path)?;
    if file.metadata()?.len() > limit {
        return Err(Error::Unsafe("private file exceeds size bound".into()));
    }
    let mut bytes = Vec::new();
    file.take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err(Error::Unsafe("private file exceeds size bound".into()));
    }
    Ok(bytes)
}

pub(crate) fn atomic_private(path: &Path, bytes: &[u8]) -> Result<()> {
    if path.exists() {
        check_private(path, false)?;
    }
    let parent = path
        .parent()
        .ok_or_else(|| Error::Unsafe("missing private parent".into()))?;
    let temporary = parent.join(format!(".write-{}", crate::security::random_secret()));
    let result = (|| {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
        }
        let mut file = options.open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        #[cfg(unix)]
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}

pub(crate) struct Lease {
    _file: File,
}
impl Lease {
    pub(crate) fn acquire(path: &Path) -> Result<Self> {
        let file = open_private(path)?;
        FileExt::try_lock_exclusive(&file).map_err(|e| {
            if e.kind() == std::io::ErrorKind::WouldBlock {
                Error::Busy
            } else {
                Error::Io(e)
            }
        })?;
        Ok(Self { _file: file })
    }
}

pub fn state_directory(data: &Path) -> Result<PathBuf> {
    validate_path(data)?;
    if !data.is_dir() {
        return Err(Error::Unsafe("data directory must already exist".into()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let metadata = fs::metadata(data)?;
        if metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o022 != 0 {
            return Err(Error::Unsafe(
                "data directory must be user-owned and not writable by other users".into(),
            ));
        }
    }
    let path = data.join(".ori-runtime");
    // Leases are useful in loopback mode even on Windows; private LAN state remains fail-closed.
    #[cfg(windows)]
    if !path.exists() {
        fs::create_dir(&path)?;
    }
    #[cfg(not(windows))]
    private_directory(&path)?;
    Ok(path)
}

pub struct InstanceLease {
    _lease: Lease,
}
pub struct WriterLease {
    _lease: Lease,
}
impl InstanceLease {
    pub fn acquire(data: &Path) -> Result<Self> {
        Ok(Self {
            _lease: Lease::acquire(&state_directory(data)?.join("instance.lock"))?,
        })
    }
}
impl WriterLease {
    pub fn acquire(data: &Path) -> Result<Self> {
        Ok(Self {
            _lease: Lease::acquire(&state_directory(data)?.join("writer.lock"))?,
        })
    }
}
