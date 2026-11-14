//! Synthetic native error boundary: the actual default-VFS file closes before
//! the private test layer reports IOERR_CLOSE. Never change the default VFS.
use super::*;

struct Allow;
impl CheckpointIoPermit for Allow {
    fn account(&self, _: u64) -> Result<()> {
        Ok(())
    }
    fn cancel(&self) {}
}
#[derive(Default)]
struct Pressure {
    cancelled: AtomicBool,
    denied: AtomicUsize,
}
impl CheckpointIoPermit for Pressure {
    fn account(&self, _: u64) -> Result<()> {
        if self.cancelled.load(Ordering::Acquire) {
            self.denied.fetch_add(1, Ordering::AcqRel);
            Err(ArchiveError::Closed)
        } else {
            Ok(())
        }
    }
    fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
    }
}

unsafe extern "C" fn close_then_report_error(raw: *mut ffi::sqlite3_file) -> c_int {
    // Close the real native file through the exact forwarding implementation,
    // then inject the platform-close failure code. The fixture leaks no handle.
    let native = unsafe { close(raw) };
    if native == ffi::SQLITE_OK {
        ffi::SQLITE_IOERR_CLOSE
    } else {
        native
    }
}
const FAULT_METHODS: ffi::sqlite3_io_methods = ffi::sqlite3_io_methods {
    xClose: Some(close_then_report_error),
    ..METHODS
};
const FAULT_METHODS_V1: ffi::sqlite3_io_methods = ffi::sqlite3_io_methods {
    xClose: Some(close_then_report_error),
    ..METHODS_V1
};
unsafe extern "C" fn open_with_close_fault(
    vfs: *mut ffi::sqlite3_vfs,
    name: *const c_char,
    raw: *mut ffi::sqlite3_file,
    flags: c_int,
    out: *mut c_int,
) -> c_int {
    let code = unsafe { open(vfs, name, raw, flags, out) };
    if code == ffi::SQLITE_OK {
        unsafe {
            let wrapped = file(raw);
            wrapped.base.pMethods = if (*wrapped.base.pMethods).iVersion >= 2 {
                &FAULT_METHODS
            } else {
                &FAULT_METHODS_V1
            };
        }
    }
    code
}

fn database() -> (tempfile::TempDir, std::path::PathBuf) {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("native-close.sqlite");
    let writer = Connection::open(&path).unwrap();
    writer
        .execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE t(payload BLOB)",
        )
        .unwrap();
    writer
        .execute("INSERT INTO t VALUES(?)", [vec![0x3b_u8; 2 * 1024 * 1024]])
        .unwrap();
    drop(writer);
    (root, path)
}

fn layers(pressure: Arc<Pressure>) -> (Vfs, Vfs) {
    let mut native = Vfs::new(Arc::new(Allow), Arc::new(AtomicBool::new(false))).unwrap();
    let mut outer = Vfs::new(pressure, Arc::new(AtomicBool::new(false))).unwrap();
    let parent = native.registered.as_mut().unwrap();
    parent.raw.xOpen = Some(open_with_close_fault);
    // These uniquely named test instances are private; both continue forwarding
    // to the same actual default VFS and its original native locks/files.
    outer.registered.as_mut().unwrap().context.parent = &mut parent.raw;
    (native, outer)
}
fn close_connection(connection: Connection) {
    if let Err((connection, _)) = connection.close() {
        drop(connection);
    }
}
fn finish(native: Vfs, outer: Vfs) -> Option<ArchiveError> {
    let failure = outer.take_failure();
    let native_files = native
        .registered
        .as_ref()
        .unwrap()
        .context
        .files
        .load(Ordering::Acquire);
    let outer_files = outer
        .registered
        .as_ref()
        .unwrap()
        .context
        .files
        .load(Ordering::Acquire);
    let outer_closed = outer.finish();
    let native_closed = native.finish();
    assert_eq!(
        native_files, 0,
        "all real native files must already be closed"
    );
    assert_eq!(outer_files, 0);
    assert!(outer_closed.is_ok() && native_closed.is_ok());
    failure
}

#[test]
fn native_close_error_after_pressure_cancellation_supersedes_the_retryable_error() {
    let (_root, path) = database();
    let default = unsafe { ffi::sqlite3_vfs_find(ptr::null()) };
    let pressure = Arc::new(Pressure::default());
    let (native, outer) = layers(pressure.clone());
    let connection = outer.connect(&path).unwrap();
    pressure.cancel();
    let read = connection.query_row("SELECT payload FROM t", [], |r| r.get::<_, Vec<u8>>(0));
    close_connection(connection);
    let failure = finish(native, outer);
    assert!(pressure.denied.load(Ordering::Acquire) != 0);
    assert!(
        read.is_err(),
        "must exercise a cancelled real SQLite data read"
    );
    assert!(
        matches!(&failure, Some(ArchiveError::Sql(rusqlite::Error::SqliteFailure(error, _))) if error.extended_code == ffi::SQLITE_IOERR_CLOSE),
        "native close failure was masked by cancellation: {failure:?}"
    );
    assert_eq!(unsafe { ffi::sqlite3_vfs_find(ptr::null()) }, default);
}

#[test]
fn later_pressure_cancellation_cannot_replace_an_earlier_native_close_error() {
    let (_root, path) = database();
    let pressure = Arc::new(Pressure::default());
    let (native, outer) = layers(pressure.clone());
    let first = outer.connect(&path).unwrap();
    let second = outer.connect(&path).unwrap();
    close_connection(first);
    pressure.cancel();
    let read = second.query_row("SELECT payload FROM t", [], |r| r.get::<_, Vec<u8>>(0));
    close_connection(second);
    let failure = finish(native, outer);
    assert!(pressure.denied.load(Ordering::Acquire) != 0);
    assert!(read.is_err());
    assert!(
        matches!(&failure, Some(ArchiveError::Sql(rusqlite::Error::SqliteFailure(error, _))) if error.extended_code == ffi::SQLITE_IOERR_CLOSE)
    );
}
