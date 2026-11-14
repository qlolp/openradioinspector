//! Private forwarding VFS for the maintenance connection only. The default
//! SQLite VFS and every ingestion/reader connection remain untouched.
use super::CheckpointIoPermit;
use crate::{ArchiveError, Result};
use rusqlite::{Connection, OpenFlags, config::DbConfig, ffi};
use std::{
    ffi::{CString, c_char, c_int, c_void},
    path::Path,
    ptr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    time::Duration,
};

pub(super) const MAX_IO_BYTES: usize = 64 * 1024;
static SERIAL: AtomicU64 = AtomicU64::new(0);

struct Context {
    parent: *mut ffi::sqlite3_vfs,
    permit: Arc<dyn CheckpointIoPermit>,
    stopping: Arc<AtomicBool>,
    failure: Mutex<Option<ArchiveError>>,
    files: AtomicUsize,
}
impl Context {
    fn remember(&self, error: ArchiveError) {
        if let Ok(mut failure) = self.failure.lock()
            && (failure.is_none()
                || (matches!(failure.as_ref(), Some(ArchiveError::Closed))
                    && !matches!(&error, ArchiveError::Closed)))
        {
            // Cancellation is retryable; it cannot hide failed native cleanup.
            // Once a real error exists, preserve it across later cancellation.
            *failure = Some(error);
        }
    }
    fn account(&self, bytes: u64) -> bool {
        // A user-supplied Rust budget must never unwind across SQLite's C ABI.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            if self.stopping.load(Ordering::Acquire) {
                return Err(ArchiveError::Closed);
            }
            self.permit.account(bytes)?;
            if self.stopping.load(Ordering::Acquire) {
                return Err(ArchiveError::Closed);
            }
            Ok(())
        }))
        .unwrap_or(Err(ArchiveError::WorkerStopped));
        if let Err(error) = result {
            self.remember(error);
            return false;
        }
        true
    }
}

/// SQLite owns `base` storage; the wrapped native file is allocated by SQLite's
/// own allocator at its guaranteed alignment. No native file layout is assumed.
#[repr(C)]
struct File {
    base: ffi::sqlite3_file,
    real: *mut ffi::sqlite3_file,
    context: *const Context,
}

struct Registered {
    raw: ffi::sqlite3_vfs,
    name: CString,
    context: Box<Context>,
}
pub(super) struct Vfs {
    registered: Option<Box<Registered>>,
}
impl Vfs {
    pub fn new(permit: Arc<dyn CheckpointIoPermit>, stopping: Arc<AtomicBool>) -> Result<Self> {
        // SAFETY: SQLite initialization/registration are serialized internally.
        // The newly registered name is private and is never made the default.
        unsafe {
            let code = ffi::sqlite3_initialize();
            if code != ffi::SQLITE_OK {
                return Err(sql_error(code));
            }
            let parent = ffi::sqlite3_vfs_find(ptr::null());
            if parent.is_null() || (*parent).szOsFile <= 0 || (*parent).xOpen.is_none() {
                return Err(crate::error::invalid("native SQLite VFS is unavailable"));
            }
            let serial = SERIAL
                .try_update(Ordering::AcqRel, Ordering::Acquire, |n| n.checked_add(1))
                .map_err(|_| ArchiveError::WorkerStopped)?;
            let name = CString::new(format!("ori-checkpoint-{}-{serial}", std::process::id()))
                .map_err(|_| ArchiveError::WorkerStopped)?;
            let context = Box::new(Context {
                parent,
                permit,
                stopping,
                failure: Mutex::new(None),
                files: AtomicUsize::new(0),
            });
            let mut registered = Box::new(Registered {
                raw: *parent,
                name,
                context,
            });
            registered.raw.iVersion = (*parent).iVersion.min(3);
            registered.raw.szOsFile = std::mem::size_of::<File>() as c_int;
            registered.raw.pNext = ptr::null_mut();
            registered.raw.zName = registered.name.as_ptr();
            registered.raw.pAppData = (&*registered.context as *const Context).cast_mut().cast();
            registered.raw.xOpen = Some(open);
            registered.raw.xDelete = Some(delete);
            registered.raw.xAccess = Some(access);
            registered.raw.xFullPathname = Some(full_pathname);
            registered.raw.xDlOpen = (*parent).xDlOpen.map(|_| dl_open as _);
            registered.raw.xDlError = (*parent).xDlError.map(|_| dl_error as _);
            registered.raw.xDlSym = (*parent).xDlSym.map(|_| dl_sym as _);
            registered.raw.xDlClose = (*parent).xDlClose.map(|_| dl_close as _);
            registered.raw.xRandomness = Some(randomness);
            registered.raw.xSleep = Some(sleep);
            registered.raw.xCurrentTime = Some(current_time);
            registered.raw.xGetLastError = (*parent).xGetLastError.map(|_| last_error as _);
            registered.raw.xCurrentTimeInt64 =
                (*parent).xCurrentTimeInt64.map(|_| current_time64 as _);
            registered.raw.xSetSystemCall = (*parent).xSetSystemCall.map(|_| set_system_call as _);
            registered.raw.xGetSystemCall = (*parent).xGetSystemCall.map(|_| get_system_call as _);
            registered.raw.xNextSystemCall =
                (*parent).xNextSystemCall.map(|_| next_system_call as _);
            let code = ffi::sqlite3_vfs_register(&mut registered.raw, 0);
            if code != ffi::SQLITE_OK {
                return Err(sql_error(code));
            }
            Ok(Self {
                registered: Some(registered),
            })
        }
    }
    pub fn connect(&self, path: &Path) -> Result<Connection> {
        let registered = self
            .registered
            .as_ref()
            .ok_or(ArchiveError::WorkerStopped)?;
        let connection = Connection::open_with_flags_and_vfs(
            path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
            registered
                .name
                .to_str()
                .map_err(|_| ArchiveError::WorkerStopped)?,
        )?;
        connection.set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true)?;
        // PASSIVE never waits for reader/writer locks. Disable mmap so all data
        // reads pass through the accounted methods, including on native v3 VFS.
        connection.busy_timeout(Duration::ZERO)?;
        connection.execute_batch("PRAGMA mmap_size=0; PRAGMA cache_size=-1024; PRAGMA temp_store=MEMORY; PRAGMA synchronous=FULL; PRAGMA wal_autocheckpoint=0;")?;
        Ok(connection)
    }
    pub fn take_failure(&self) -> Option<ArchiveError> {
        self.registered
            .as_ref()?
            .context
            .failure
            .lock()
            .ok()?
            .take()
    }
    pub fn finish(mut self) -> Result<()> {
        self.unregister()
    }
    fn unregister(&mut self) -> Result<()> {
        let Some(mut registered) = self.registered.take() else {
            return Ok(());
        };
        if registered.context.files.load(Ordering::Acquire) != 0 {
            // A lifetime violation must not free callback data still referenced
            // by SQLite. Fail closed; never turn it into a use-after-free.
            std::mem::forget(registered);
            return Err(ArchiveError::WorkerStopped);
        }
        let code = unsafe { ffi::sqlite3_vfs_unregister(&mut registered.raw) };
        if code != ffi::SQLITE_OK {
            std::mem::forget(registered);
            return Err(sql_error(code));
        }
        Ok(())
    }
    #[cfg(test)]
    pub fn name(&self) -> &std::ffi::CStr {
        &self.registered.as_ref().unwrap().name
    }
    #[cfg(test)]
    pub fn file_storage_words(&self) -> usize {
        std::mem::size_of::<File>().div_ceil(std::mem::size_of::<u64>())
    }
    #[cfg(test)]
    pub unsafe fn open_file(
        &self,
        name: *const c_char,
        file: *mut ffi::sqlite3_file,
        flags: c_int,
        out: *mut c_int,
    ) -> c_int {
        let raw = &self.registered.as_ref().unwrap().raw as *const ffi::sqlite3_vfs;
        unsafe { open(raw.cast_mut(), name, file, flags, out) }
    }
}
impl Drop for Vfs {
    fn drop(&mut self) {
        let _ = self.unregister();
    }
}
fn sql_error(code: c_int) -> ArchiveError {
    rusqlite::Error::SqliteFailure(ffi::Error::new(code), None).into()
}

// Every callback below receives storage owned by SQLite while the Registered
// box lives. The underlying method always receives its ORIGINAL native file or
// VFS pointer, never the forwarding object's layout or pAppData.
unsafe fn context(vfs: *mut ffi::sqlite3_vfs) -> &'static Context {
    unsafe { &*(*vfs).pAppData.cast::<Context>() }
}
unsafe fn file(raw: *mut ffi::sqlite3_file) -> &'static mut File {
    unsafe { &mut *raw.cast::<File>() }
}
unsafe fn methods(file: &File) -> &'static ffi::sqlite3_io_methods {
    unsafe { &*(*file.real).pMethods }
}

unsafe extern "C" fn open(
    vfs: *mut ffi::sqlite3_vfs,
    name: *const c_char,
    raw: *mut ffi::sqlite3_file,
    flags: c_int,
    out: *mut c_int,
) -> c_int {
    unsafe {
        let context = context(vfs);
        raw.cast::<File>().write(File {
            base: ffi::sqlite3_file {
                pMethods: ptr::null(),
            },
            real: ptr::null_mut(),
            context,
        });
        if !context.account(0) {
            return ffi::SQLITE_IOERR;
        }
        let real =
            ffi::sqlite3_malloc64((*context.parent).szOsFile as u64).cast::<ffi::sqlite3_file>();
        if real.is_null() {
            return ffi::SQLITE_NOMEM;
        }
        (*real).pMethods = ptr::null();
        let code = (*context.parent).xOpen.map_or(ffi::SQLITE_IOERR, |f| {
            f(context.parent, name, real, flags, out)
        });
        if code != ffi::SQLITE_OK {
            if !(*real).pMethods.is_null()
                && let Some(close) = (*(*real).pMethods).xClose
            {
                close(real);
            }
            ffi::sqlite3_free(real.cast());
            return code;
        }
        if (*real).pMethods.is_null() {
            ffi::sqlite3_free(real.cast());
            return ffi::SQLITE_IOERR;
        }
        let file = file(raw);
        file.real = real;
        file.base.pMethods = if (*(*real).pMethods).iVersion >= 2 {
            &METHODS
        } else {
            &METHODS_V1
        };
        context.files.fetch_add(1, Ordering::AcqRel);
        ffi::SQLITE_OK
    }
}
unsafe extern "C" fn close(raw: *mut ffi::sqlite3_file) -> c_int {
    unsafe {
        let file = file(raw);
        let code = methods(file)
            .xClose
            .map_or(ffi::SQLITE_IOERR_CLOSE, |f| f(file.real));
        if code != ffi::SQLITE_OK {
            (*file.context).remember(sql_error(code));
        }
        ffi::sqlite3_free(file.real.cast());
        file.real = ptr::null_mut();
        file.base.pMethods = ptr::null();
        (*file.context).files.fetch_sub(1, Ordering::AcqRel);
        code
    }
}
unsafe extern "C" fn read(
    raw: *mut ffi::sqlite3_file,
    buffer: *mut c_void,
    amount: c_int,
    offset: i64,
) -> c_int {
    unsafe {
        let file = file(raw);
        if amount < 0 || offset < 0 {
            return ffi::SQLITE_IOERR_READ;
        }
        let Some(read) = methods(file).xRead else {
            return ffi::SQLITE_IOERR_READ;
        };
        let total = amount as usize;
        let mut done = 0;
        while done < total {
            let chunk = (total - done).min(MAX_IO_BYTES);
            let Some(at) = offset.checked_add(done as i64) else {
                return ffi::SQLITE_IOERR_READ;
            };
            if !(*file.context).account(chunk as u64) {
                return ffi::SQLITE_IOERR_READ;
            }
            let code = read(
                file.real,
                buffer.cast::<u8>().add(done).cast(),
                chunk as c_int,
                at,
            );
            if code != ffi::SQLITE_OK {
                // Native xRead zero-fills its own short chunk. SQLite requires
                // the ENTIRE unfilled suffix, including our later chunks, zero.
                if code == ffi::SQLITE_IOERR_SHORT_READ {
                    ptr::write_bytes(
                        buffer.cast::<u8>().add(done + chunk),
                        0,
                        total - done - chunk,
                    );
                }
                return code;
            }
            done += chunk;
        }
        ffi::SQLITE_OK
    }
}
unsafe extern "C" fn write(
    raw: *mut ffi::sqlite3_file,
    buffer: *const c_void,
    amount: c_int,
    offset: i64,
) -> c_int {
    unsafe {
        let file = file(raw);
        if amount < 0 || offset < 0 {
            return ffi::SQLITE_IOERR_WRITE;
        }
        let Some(write) = methods(file).xWrite else {
            return ffi::SQLITE_IOERR_WRITE;
        };
        let total = amount as usize;
        let mut done = 0;
        while done < total {
            let chunk = (total - done).min(MAX_IO_BYTES);
            let Some(at) = offset.checked_add(done as i64) else {
                return ffi::SQLITE_IOERR_WRITE;
            };
            if !(*file.context).account(chunk as u64) {
                return ffi::SQLITE_IOERR_WRITE;
            }
            let code = write(
                file.real,
                buffer.cast::<u8>().add(done).cast(),
                chunk as c_int,
                at,
            );
            if code != ffi::SQLITE_OK {
                return code;
            }
            done += chunk;
        }
        ffi::SQLITE_OK
    }
}
unsafe extern "C" fn truncate(raw: *mut ffi::sqlite3_file, size: i64) -> c_int {
    unsafe {
        let file = file(raw);
        if !(*file.context).account(0) {
            return ffi::SQLITE_IOERR_TRUNCATE;
        }
        methods(file)
            .xTruncate
            .map_or(ffi::SQLITE_IOERR_TRUNCATE, |f| f(file.real, size))
    }
}
unsafe extern "C" fn sync(raw: *mut ffi::sqlite3_file, flags: c_int) -> c_int {
    unsafe {
        let file = file(raw);
        if !(*file.context).account(0) {
            return ffi::SQLITE_IOERR_FSYNC;
        }
        let code = methods(file)
            .xSync
            .map_or(ffi::SQLITE_IOERR_FSYNC, |f| f(file.real, flags));
        if code == ffi::SQLITE_OK && !(*file.context).account(0) {
            ffi::SQLITE_IOERR_FSYNC
        } else {
            code
        }
    }
}

macro_rules! forward_file {
    ($name:ident, $method:ident, ($($arg:ident:$ty:ty),*) -> $result:ty, $missing:expr) => {
        unsafe extern "C" fn $name(raw:*mut ffi::sqlite3_file, $($arg:$ty),*) -> $result {
            unsafe { let file = file(raw); methods(file).$method.map_or($missing, |f| f(file.real, $($arg),*)) }
        }
    };
}
forward_file!(size, xFileSize, (out:*mut i64) -> c_int, ffi::SQLITE_IOERR_FSTAT);
forward_file!(lock, xLock, (level:c_int) -> c_int, ffi::SQLITE_IOERR_LOCK);
forward_file!(unlock, xUnlock, (level:c_int) -> c_int, ffi::SQLITE_IOERR_UNLOCK);
forward_file!(reserved, xCheckReservedLock, (out:*mut c_int) -> c_int, ffi::SQLITE_IOERR_CHECKRESERVEDLOCK);
forward_file!(file_control, xFileControl, (op:c_int, argument:*mut c_void) -> c_int, ffi::SQLITE_NOTFOUND);
forward_file!(sector_size, xSectorSize, () -> c_int, 0);
forward_file!(device_characteristics, xDeviceCharacteristics, () -> c_int, 0);
forward_file!(shm_map, xShmMap, (page:c_int, page_size:c_int, extend:c_int, out:*mut *mut c_void) -> c_int, ffi::SQLITE_IOERR_SHMMAP);
forward_file!(shm_lock, xShmLock, (offset:c_int, count:c_int, flags:c_int) -> c_int, ffi::SQLITE_IOERR_SHMLOCK);
forward_file!(shm_barrier, xShmBarrier, () -> (), ());
forward_file!(shm_unmap, xShmUnmap, (delete:c_int) -> c_int, ffi::SQLITE_IOERR_SHMOPEN);

const METHODS: ffi::sqlite3_io_methods = ffi::sqlite3_io_methods {
    iVersion: 2,
    xClose: Some(close),
    xRead: Some(read),
    xWrite: Some(write),
    xTruncate: Some(truncate),
    xSync: Some(sync),
    xFileSize: Some(size),
    xLock: Some(lock),
    xUnlock: Some(unlock),
    xCheckReservedLock: Some(reserved),
    xFileControl: Some(file_control),
    xSectorSize: Some(sector_size),
    xDeviceCharacteristics: Some(device_characteristics),
    xShmMap: Some(shm_map),
    xShmLock: Some(shm_lock),
    xShmBarrier: Some(shm_barrier),
    xShmUnmap: Some(shm_unmap),
    xFetch: None,
    xUnfetch: None,
};
const METHODS_V1: ffi::sqlite3_io_methods = ffi::sqlite3_io_methods {
    iVersion: 1,
    xShmMap: None,
    xShmLock: None,
    xShmBarrier: None,
    xShmUnmap: None,
    ..METHODS
};

macro_rules! forward_vfs {
    ($name:ident, $method:ident, ($($arg:ident:$ty:ty),*) -> $result:ty, $missing:expr) => {
        unsafe extern "C" fn $name(vfs:*mut ffi::sqlite3_vfs, $($arg:$ty),*) -> $result {
            unsafe { let parent = context(vfs).parent; (*parent).$method.map_or($missing, |f| f(parent, $($arg),*)) }
        }
    };
}
type DlSymbol = Option<unsafe extern "C" fn(*mut ffi::sqlite3_vfs, *mut c_void, *const c_char)>;
forward_vfs!(delete, xDelete, (name:*const c_char, sync_dir:c_int) -> c_int, ffi::SQLITE_IOERR_DELETE);
forward_vfs!(access, xAccess, (name:*const c_char, flags:c_int, out:*mut c_int) -> c_int, ffi::SQLITE_IOERR_ACCESS);
forward_vfs!(full_pathname, xFullPathname, (name:*const c_char, count:c_int, out:*mut c_char) -> c_int, ffi::SQLITE_CANTOPEN);
forward_vfs!(dl_open, xDlOpen, (name:*const c_char) -> *mut c_void, ptr::null_mut());
forward_vfs!(dl_error, xDlError, (count:c_int, out:*mut c_char) -> (), ());
forward_vfs!(dl_sym, xDlSym, (handle:*mut c_void, name:*const c_char) -> DlSymbol, None);
forward_vfs!(dl_close, xDlClose, (handle:*mut c_void) -> (), ());
forward_vfs!(randomness, xRandomness, (count:c_int, out:*mut c_char) -> c_int, 0);
forward_vfs!(sleep, xSleep, (micros:c_int) -> c_int, 0);
forward_vfs!(current_time, xCurrentTime, (out:*mut f64) -> c_int, ffi::SQLITE_IOERR);
forward_vfs!(last_error, xGetLastError, (count:c_int, out:*mut c_char) -> c_int, 0);
forward_vfs!(current_time64, xCurrentTimeInt64, (out:*mut i64) -> c_int, ffi::SQLITE_IOERR);
forward_vfs!(set_system_call, xSetSystemCall, (name:*const c_char, function:ffi::sqlite3_syscall_ptr) -> c_int, ffi::SQLITE_NOTFOUND);
forward_vfs!(get_system_call, xGetSystemCall, (name:*const c_char) -> ffi::sqlite3_syscall_ptr, None);
forward_vfs!(next_system_call, xNextSystemCall, (name:*const c_char) -> *const c_char, ptr::null());

#[cfg(test)]
#[path = "native_close_tests.rs"]
mod native_close_tests;
