//! Private, fixed-size cooperative I/O ledger. It is never a physical I/O limit.
use crate::qos::QosError;
use serde::Serialize;
use std::path::PathBuf;

#[cfg(unix)]
pub(crate) const RATE: u64 = 20 * 1024 * 1024;
pub(crate) const MAX_CHARGE: u64 = 8 * 1024 * 1024;
#[cfg(unix)]
const MAX_DEBT_NS: u64 = 30_000_000_000;

/// A private worker capability, intentionally without Debug or Deserialize.
#[derive(Clone, Serialize)]
pub struct SharedIoSpec {
    version: u8,
    path: PathBuf,
    nonce: String,
    clock: &'static str,
}

#[cfg(unix)]
pub(crate) enum Reservation {
    Busy,
    Until(u64),
}

#[cfg(unix)]
mod platform {
    use super::*;
    use fs2::FileExt;
    use rand::RngCore;
    use std::{
        fs::{self, File, OpenOptions},
        io::{Read, Seek, SeekFrom, Write},
        os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
        path::{Component, Path},
        sync::{
            Mutex, TryLockError,
            atomic::{AtomicBool, AtomicU64, Ordering},
        },
    };

    const MAGIC: &[u8; 8] = b"ORIIOV1\0";
    struct State {
        file: File,
        device: u64,
        inode: u64,
    }
    pub(crate) struct SharedIo {
        spec: SharedIoSpec,
        nonce: [u8; 16],
        directory: PathBuf,
        directory_identity: (u64, u64),
        state: Mutex<State>,
        total: AtomicU64,
        failed: AtomicBool,
        closed: AtomicBool,
    }
    fn invalid<T>() -> Result<T, QosError> {
        Err(QosError::InvalidCharge)
    }
    fn io<T>(value: std::io::Result<T>) -> Result<T, QosError> {
        value.map_err(|_| QosError::InvalidCharge)
    }
    pub(crate) fn now_ns() -> Result<u64, QosError> {
        let mut value = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut value) } != 0
            || value.tv_sec < 0
            || !(0..1_000_000_000).contains(&value.tv_nsec)
        {
            return invalid();
        }
        (value.tv_sec as u64)
            .checked_mul(1_000_000_000)
            .and_then(|n| n.checked_add(value.tv_nsec as u64))
            .ok_or(QosError::InvalidCharge)
    }
    fn private_directory(path: &Path) -> Result<(), QosError> {
        let metadata = io(fs::symlink_metadata(path))?;
        if !metadata.is_dir()
            || metadata.file_type().is_symlink()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o777 != 0o700
        {
            return invalid();
        }
        Ok(())
    }
    fn number(bytes: &[u8], start: usize) -> u64 {
        u64::from_le_bytes(
            bytes[start..start + 8]
                .try_into()
                .expect("fixed ledger offsets"),
        )
    }
    impl SharedIo {
        pub(crate) fn new(root: PathBuf) -> Result<Self, QosError> {
            if !root.is_absolute()
                || root
                    .components()
                    .any(|p| matches!(p, Component::ParentDir | Component::CurDir))
            {
                return invalid();
            }
            let mut ancestor = PathBuf::new();
            for part in root.components() {
                ancestor.push(part);
                if let Ok(meta) = fs::symlink_metadata(&ancestor)
                    && (meta.file_type().is_symlink() || !meta.is_dir())
                {
                    return invalid();
                }
            }
            if !root.exists() {
                io(fs::DirBuilder::new().mode(0o700).create(&root))?;
            }
            private_directory(&root)?;
            let mut nonce = [0; 16];
            rand::rngs::OsRng.fill_bytes(&mut nonce);
            let text = nonce.iter().map(|b| format!("{b:02x}")).collect::<String>();
            let directory = root.join(format!(".ori-io-{text}"));
            io(fs::DirBuilder::new().mode(0o700).create(&directory))?;
            let directory_meta = io(fs::symlink_metadata(&directory))?;
            let directory_identity = (directory_meta.dev(), directory_meta.ino());
            let path = directory.join("budget");
            let mut created_file = None;
            let result = (|| {
                let mut file = io(OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                    .open(&path))?;
                let metadata = io(file.metadata())?;
                created_file = Some((metadata.dev(), metadata.ino()));
                let now = now_ns()?;
                let mut data = [0u8; 128];
                data[..8].copy_from_slice(MAGIC);
                data[8..24].copy_from_slice(&nonce);
                data[24..32].copy_from_slice(&RATE.to_le_bytes());
                data[32..40].copy_from_slice(&now.to_le_bytes());
                data[48..56].copy_from_slice(&now.to_le_bytes());
                data[56..64].copy_from_slice(&1u64.to_le_bytes());
                io(file.write_all(&data))?;
                io(file.sync_all())?;
                let meta = io(file.metadata())?;
                Ok(Self {
                    spec: SharedIoSpec {
                        version: 1,
                        path: path.clone(),
                        nonce: text,
                        clock: "posix-monotonic",
                    },
                    nonce,
                    directory: directory.clone(),
                    directory_identity,
                    state: Mutex::new(State {
                        file,
                        device: meta.dev(),
                        inode: meta.ino(),
                    }),
                    total: AtomicU64::new(0),
                    failed: AtomicBool::new(false),
                    closed: AtomicBool::new(false),
                })
            })();
            if result.is_err()
                && fs::symlink_metadata(&directory)
                    .is_ok_and(|m| (m.dev(), m.ino()) == directory_identity)
            {
                if let Some(identity) = created_file
                    && fs::symlink_metadata(&path).is_ok_and(|m| (m.dev(), m.ino()) == identity)
                {
                    let _ = fs::remove_file(&path);
                }
                let _ = fs::remove_dir(&directory);
            }
            result
        }
        pub(crate) fn spec(&self) -> SharedIoSpec {
            self.spec.clone()
        }
        pub(crate) fn failed(&self) -> bool {
            self.failed.load(Ordering::Acquire)
        }
        pub(crate) fn active(&self) -> bool {
            !self.failed() && !self.closed.load(Ordering::Acquire)
        }
        pub(crate) fn fail(&self) {
            self.failed.store(true, Ordering::Release);
        }
        fn validate(&self, state: &State) -> Result<(), QosError> {
            private_directory(&self.directory)?;
            let directory = io(fs::symlink_metadata(&self.directory))?;
            if (directory.dev(), directory.ino()) != self.directory_identity {
                return invalid();
            }
            let meta = io(fs::symlink_metadata(&self.spec.path))?;
            let opened = io(state.file.metadata())?;
            if !meta.is_file()
                || meta.file_type().is_symlink()
                || meta.nlink() != 1
                || meta.uid() != unsafe { libc::geteuid() }
                || meta.mode() & 0o777 != 0o600
                || meta.len() != 128
                || (meta.dev(), meta.ino()) != (state.device, state.inode)
                || (opened.dev(), opened.ino()) != (state.device, state.inode)
            {
                return invalid();
            }
            Ok(())
        }
        fn read(&self, state: &mut State) -> Result<([u8; 128], u64), QosError> {
            self.validate(state)?;
            let mut data = [0u8; 128];
            io(state.file.seek(SeekFrom::Start(0)))?;
            io(state.file.read_exact(&mut data))?;
            let now = now_ns()?;
            let created = number(&data, 48);
            let next = number(&data, 32);
            let total = number(&data, 40);
            if &data[..8] != MAGIC
                || data[8..24] != self.nonce
                || number(&data, 24) != RATE
                || number(&data, 56) != 1
                || data[64..].iter().any(|n| *n != 0)
                || created == 0
                || created > now
                || next < created
                || next > now.saturating_add(MAX_DEBT_NS)
                || total > i64::MAX as u64
                || total < self.total.load(Ordering::Acquire)
            {
                return invalid();
            }
            self.total.store(total, Ordering::Release);
            Ok((data, now))
        }
        pub(crate) fn reserve(&self, bytes: u64) -> Result<Reservation, QosError> {
            if !self.active() {
                return invalid();
            }
            let mut state = match self.state.try_lock() {
                Ok(state) => state,
                Err(TryLockError::WouldBlock) => return Ok(Reservation::Busy),
                Err(_) => {
                    self.fail();
                    return invalid();
                }
            };
            if let Err(error) = state.file.try_lock_exclusive() {
                if error.kind() == std::io::ErrorKind::WouldBlock {
                    return Ok(Reservation::Busy);
                }
                self.fail();
                return invalid();
            }
            let result = (|| {
                let (mut data, now) = self.read(&mut state)?;
                let duration = (bytes as u128 * 1_000_000_000).div_ceil(RATE as u128) as u64;
                let until = number(&data, 32)
                    .max(now)
                    .checked_add(duration)
                    .ok_or(QosError::InvalidCharge)?;
                let total = number(&data, 40)
                    .checked_add(bytes)
                    .filter(|n| *n <= i64::MAX as u64)
                    .ok_or(QosError::InvalidCharge)?;
                if until > now.saturating_add(MAX_DEBT_NS) {
                    return invalid();
                }
                data[32..40].copy_from_slice(&until.to_le_bytes());
                data[40..48].copy_from_slice(&total.to_le_bytes());
                io(state.file.seek(SeekFrom::Start(0)))?;
                io(state.file.write_all(&data))?;
                self.total.store(total, Ordering::Release);
                Ok(Reservation::Until(until))
            })();
            if FileExt::unlock(&state.file).is_err() {
                self.fail();
                return invalid();
            }
            if result.is_err() {
                self.fail();
            }
            result
        }
        pub(crate) fn total(&self) -> u64 {
            if self.active()
                && let Ok(mut state) = self.state.try_lock()
                && state.file.try_lock_exclusive().is_ok()
            {
                if self.read(&mut state).is_err() {
                    self.fail();
                }
                if FileExt::unlock(&state.file).is_err() {
                    self.fail();
                }
            }
            self.total.load(Ordering::Acquire)
        }
        pub(crate) fn close(&self) -> Result<(), QosError> {
            if !self.closed.swap(true, Ordering::AcqRel) {
                let result = (|| {
                    let state = self.state.lock().map_err(|_| QosError::InvalidCharge)?;
                    self.validate(&state)?;
                    io(state.file.try_lock_exclusive())?;
                    let removed = io(fs::remove_file(&self.spec.path));
                    let unlocked = io(FileExt::unlock(&state.file));
                    removed?;
                    unlocked?;
                    io(fs::remove_dir(&self.directory))
                })();
                if result.is_err() {
                    self.fail();
                }
            }
            if self.failed() { invalid() } else { Ok(()) }
        }
    }
    impl Drop for SharedIo {
        fn drop(&mut self) {
            let _ = self.close();
        }
    }
}

#[cfg(unix)]
pub(crate) use platform::{SharedIo, now_ns};

#[cfg(not(unix))]
pub(crate) struct SharedIo;
#[cfg(not(unix))]
impl SharedIo {
    pub(crate) fn new(_: PathBuf) -> Result<Self, QosError> {
        Err(QosError::InvalidCharge)
    }
    pub(crate) fn spec(&self) -> SharedIoSpec {
        unreachable!("shared I/O is unavailable")
    }
    pub(crate) fn failed(&self) -> bool {
        true
    }
    pub(crate) fn active(&self) -> bool {
        false
    }
    pub(crate) fn total(&self) -> u64 {
        0
    }
    pub(crate) fn close(&self) -> Result<(), QosError> {
        Err(QosError::InvalidCharge)
    }
}
