use super::{
    CheckpointIoPermit,
    vfs::{MAX_IO_BYTES, Vfs},
};
use crate::{ArchiveError, Result};
use rusqlite::{Connection, OpenFlags, ffi};
use std::{
    ffi::CString,
    io::Write,
    mem::MaybeUninit,
    ptr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};

// Native xOpen receives a SQLite filename, which may carry URI metadata past
// its first NUL. A plain CString is not that API's full lifetime contract.
struct Filename(ffi::sqlite3_filename);
impl Filename {
    fn new(path: &std::path::Path) -> Self {
        let database = CString::new(path.to_str().unwrap()).unwrap();
        let journal = CString::new(format!("{}-journal", path.display())).unwrap();
        let wal = CString::new(format!("{}-wal", path.display())).unwrap();
        let name = unsafe {
            ffi::sqlite3_create_filename(
                database.as_ptr(),
                journal.as_ptr(),
                wal.as_ptr(),
                0,
                ptr::null_mut(),
            )
        };
        assert!(!name.is_null());
        Self(name)
    }
    fn as_ptr(&self) -> ffi::sqlite3_filename {
        self.0
    }
}
impl Drop for Filename {
    fn drop(&mut self) {
        unsafe {
            ffi::sqlite3_free_filename(self.0);
        }
    }
}

#[derive(Default)]
struct Recorder {
    charges: Mutex<Vec<u64>>,
    fail: AtomicBool,
}
impl CheckpointIoPermit for Recorder {
    fn account(&self, bytes: u64) -> Result<()> {
        self.charges.lock().unwrap().push(bytes);
        if self.fail.load(Ordering::Acquire) {
            Err(ArchiveError::Closed)
        } else {
            Ok(())
        }
    }
    fn cancel(&self) {
        self.fail.store(true, Ordering::Release);
    }
}

#[test]
fn short_reads_zero_every_remaining_byte_and_all_chunks_stay_bounded() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("short-input");
    let input = vec![0x7b; MAX_IO_BYTES + 17];
    std::fs::File::create(&path)
        .unwrap()
        .write_all(&input)
        .unwrap();
    let permit = Arc::new(Recorder::default());
    let stop = Arc::new(AtomicBool::new(false));
    let vfs = Vfs::new(permit.clone(), stop).unwrap();
    let name = Filename::new(&path);
    // SQLite provides this same szOsFile-sized, suitably aligned storage to xOpen.
    let mut storage = vec![MaybeUninit::<u64>::uninit(); vfs.file_storage_words()];
    let file = storage.as_mut_ptr().cast::<ffi::sqlite3_file>();
    let flags = ffi::SQLITE_OPEN_READONLY | ffi::SQLITE_OPEN_MAIN_DB;
    let mut actual = 0;
    let mut output = vec![0xa5; MAX_IO_BYTES * 3];
    // SAFETY: xOpen initialized the file before methods are used; the VFS and
    // aligned file allocation remain owned until the matching xClose below.
    unsafe {
        assert_eq!(
            vfs.open_file(name.as_ptr(), file, flags, &mut actual),
            ffi::SQLITE_OK
        );
        let methods = &*(*file).pMethods;
        let code = methods.xRead.unwrap()(file, output.as_mut_ptr().cast(), output.len() as i32, 0);
        assert_eq!(methods.xClose.unwrap()(file), ffi::SQLITE_OK);
        assert_eq!(code, ffi::SQLITE_IOERR_SHORT_READ);
    }
    assert_eq!(&output[..input.len()], &input);
    assert!(output[input.len()..].iter().all(|byte| *byte == 0));
    assert!(
        permit
            .charges
            .lock()
            .unwrap()
            .iter()
            .all(|bytes| *bytes <= MAX_IO_BYTES as u64)
    );
    vfs.finish().unwrap();
}

#[test]
fn cancelled_write_is_denied_before_touching_the_underlying_file() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("cancelled-output");
    std::fs::write(&path, vec![0x33; 4096]).unwrap();
    let permit = Arc::new(Recorder::default());
    let vfs = Vfs::new(permit.clone(), Arc::new(AtomicBool::new(false))).unwrap();
    let name = Filename::new(&path);
    let mut storage = vec![MaybeUninit::<u64>::uninit(); vfs.file_storage_words()];
    let file = storage.as_mut_ptr().cast::<ffi::sqlite3_file>();
    let mut actual = 0;
    let write_code;
    let close_code;
    unsafe {
        assert_eq!(
            vfs.open_file(
                name.as_ptr(),
                file,
                ffi::SQLITE_OPEN_READWRITE | ffi::SQLITE_OPEN_MAIN_DB,
                &mut actual
            ),
            ffi::SQLITE_OK
        );
        permit.cancel();
        let bytes = [0xf1; 4096];
        let methods = &*(*file).pMethods;
        write_code = methods.xWrite.unwrap()(file, bytes.as_ptr().cast(), 4096, 0);
        close_code = methods.xClose.unwrap()(file);
    }
    assert_eq!(write_code, ffi::SQLITE_IOERR_WRITE);
    assert_eq!(close_code, ffi::SQLITE_OK);
    assert_eq!(std::fs::read(path).unwrap(), vec![0x33; 4096]);
    assert!(matches!(vfs.take_failure(), Some(ArchiveError::Closed)));
    vfs.finish().unwrap();
}

#[test]
fn private_vfs_preserves_wal_locks_and_never_replaces_the_default_vfs() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("locks.sqlite");
    let writer = Connection::open(&path).unwrap();
    writer.execute_batch("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE t(x); INSERT INTO t VALUES(17);").unwrap();
    let default = unsafe { ffi::sqlite3_vfs_find(ptr::null()) };
    let vfs = Vfs::new(
        Arc::new(Recorder::default()),
        Arc::new(AtomicBool::new(false)),
    )
    .unwrap();
    let name = vfs.name().to_owned();
    let checkpoint = vfs.connect(&path).unwrap();
    let reader = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    reader.execute_batch("BEGIN").unwrap();
    assert_eq!(
        reader
            .query_row("SELECT x FROM t", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        17
    );
    writer
        .execute_batch("BEGIN IMMEDIATE; INSERT INTO t VALUES(23); COMMIT")
        .unwrap();
    let (_, frames, copied): (i64, i64, i64) = checkpoint
        .query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })
        .unwrap();
    assert!(
        copied < frames,
        "the same actual reader lock must prevent checkpointing its pinned pages"
    );
    assert_eq!(
        reader
            .query_row("SELECT COUNT(*) FROM t", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
    reader.execute_batch("ROLLBACK").unwrap();
    drop(reader);
    checkpoint
        .query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |r| r.get::<_, i64>(0))
        .unwrap();
    assert_eq!(
        writer
            .query_row("SELECT SUM(x) FROM t", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        40
    );
    assert_eq!(unsafe { ffi::sqlite3_vfs_find(ptr::null()) }, default);
    drop(checkpoint);
    vfs.finish().unwrap();
    assert!(unsafe { ffi::sqlite3_vfs_find(name.as_ptr()) }.is_null());
    assert_eq!(unsafe { ffi::sqlite3_vfs_find(ptr::null()) }, default);
}

struct Panicking;
impl CheckpointIoPermit for Panicking {
    fn account(&self, bytes: u64) -> Result<()> {
        assert_eq!(
            bytes, 0,
            "synthetic panic inside a native data-I/O callback"
        );
        Ok(())
    }
    fn cancel(&self) {}
}

#[test]
fn rust_budget_panic_cannot_unwind_across_sqlite_and_native_files_still_close() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("callback.sqlite");
    let writer = Connection::open(&path).unwrap();
    writer
        .execute_batch("CREATE TABLE source(id); INSERT INTO source VALUES(31)")
        .unwrap();
    drop(writer);
    let vfs = Vfs::new(Arc::new(Panicking), Arc::new(AtomicBool::new(false))).unwrap();
    let result = vfs.connect(&path);
    assert!(result.is_err());
    drop(result);
    assert!(matches!(
        vfs.take_failure(),
        Some(ArchiveError::WorkerStopped)
    ));
    vfs.finish().unwrap();
    let reopened = Connection::open(path).unwrap();
    assert_eq!(
        reopened
            .query_row("SELECT id FROM source", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        31
    );
}
