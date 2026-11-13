//! Real private source handles and SQLite; no hardware or shared user files.
use super::*;

fn database(path: &Path) {
    let db = Connection::open(path).unwrap();
    db.execute_batch("CREATE TABLE proof(id INTEGER PRIMARY KEY, value BLOB NOT NULL); INSERT INTO proof VALUES(7,x'0080ff');")
        .unwrap();
    db.close().unwrap();
}

fn sqlite_codes(db: Option<&Connection>, error: &rusqlite::Error) -> String {
    let extended = error.sqlite_error().map(|error| error.extended_code);
    // The borrowed connection remains live for this read-only error query.
    let system = db.map(|db| unsafe { rusqlite::ffi::sqlite3_system_errno(db.handle()) });
    format!("sqlite_extended={extended:?} sqlite_system={system:?}")
}

#[test]
fn private_windows_byte_cannot_cover_admissible_data_or_sqlite_locks() {
    assert!(WINDOWS_SOURCE_LOCK_BYTE > LegacyLimits::default().max_database_bytes);
    assert!(!(0x40000000..0x40000200).contains(&WINDOWS_SOURCE_LOCK_BYTE));
}

#[test]
fn actual_source_lease_allows_sqlite_and_excludes_aliases_until_last_owner() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("source-é-e\u{301}-😀.pan");
    let alias = directory.path().join("same-inode.pan");
    database(&path);
    fs::hard_link(&path, &alias).unwrap();
    let before = fs::read(&path).unwrap();
    let length = fs::metadata(&path).unwrap().len();
    let unlocked = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .unwrap();
    assert_eq!(
        unlocked
            .query_row("SELECT hex(value) FROM proof WHERE id=7", [], |row| row
                .get::<_, String>(
                0
            ))
            .unwrap(),
        "0080FF"
    );
    unlocked.close().unwrap();

    let (file, read_only) = source_lease(&path).unwrap();
    assert!(!read_only);
    let owner = Arc::new(file);
    let retained = owner.clone();
    assert!(matches!(source_lease(&path), Err(LegacyError::Busy)));
    assert!(matches!(source_lease(&alias), Err(LegacyError::Busy)));
    assert!(matches!(
        File::open(&alias).unwrap().try_lock(),
        Err(std::fs::TryLockError::WouldBlock)
    ));
    let db = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .unwrap_or_else(|error| panic!("leased SQLite open: {}", sqlite_codes(None, &error)));
    let actual = db
        .query_row("SELECT hex(value) FROM proof WHERE id=7", [], |row| {
            row.get::<_, String>(0)
        })
        .unwrap_or_else(|error| panic!("leased SQLite read: {}", sqlite_codes(Some(&db), &error)));
    assert_eq!(actual, "0080FF");
    db.close().unwrap();
    assert!(matches!(source_lease(&alias), Err(LegacyError::Busy)));
    drop(owner);
    assert!(matches!(source_lease(&path), Err(LegacyError::Busy)));
    assert_eq!(fs::metadata(&path).unwrap().len(), length);
    assert_eq!(fs::read(&path).unwrap(), before);
    drop(retained);
    let (released, _) = source_lease(&alias).unwrap();
    drop(released);
    assert_eq!(fs::metadata(&path).unwrap().len(), length);
    assert_eq!(fs::read(&path).unwrap(), before);
}

#[cfg(windows)]
#[test]
fn old_whole_file_lock_reports_the_actual_independent_read_failure() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("whole-file-control.pan");
    database(&path);
    let before = fs::read(&path).unwrap();
    let file = File::open(&path).unwrap();
    file.try_lock().unwrap();
    assert!(matches!(source_lease(&path), Err(LegacyError::Busy)));
    let header = File::open(&path).unwrap().read_exact(&mut [0_u8; 16]);
    let os_error = header.as_ref().err().and_then(std::io::Error::raw_os_error);
    writeln!(
        std::io::stderr().lock(),
        "legacy-old-whole-lock-control header_read_ok={} raw_os_error={os_error:?}",
        header.is_ok()
    )
    .unwrap();
    let sqlite_failed = match Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        Err(error) => {
            writeln!(
                std::io::stderr().lock(),
                "legacy-old-whole-lock-control open {}",
                sqlite_codes(None, &error)
            )
            .unwrap();
            true
        }
        Ok(db) => {
            let result = db.query_row("SELECT count(*) FROM proof", [], |row| row.get::<_, i64>(0));
            if let Err(error) = &result {
                writeln!(
                    std::io::stderr().lock(),
                    "legacy-old-whole-lock-control read {}",
                    sqlite_codes(Some(&db), error)
                )
                .unwrap();
            }
            let failed = result.is_err();
            db.close().unwrap();
            failed
        }
    };
    drop(file);
    let (released, _) = source_lease(&path).unwrap();
    drop(released);
    assert!(
        header.is_err(),
        "whole-file control unexpectedly allowed an independent header read"
    );
    assert!(
        sqlite_failed,
        "whole-file control unexpectedly allowed independent SQLite access"
    );
    assert_eq!(fs::read(&path).unwrap(), before);
}
