//! Bounded measurements for the storage forecast. No numeric arrays cross
//! the SQLite reader boundary. WAL size is sampled independently of page_count.
use crate::{Archive, Result};
use std::{ffi::OsString, fs, io, path::Path};

#[derive(serde::Serialize)]
pub struct SweepStorageSample {
    pub count: i64,
    pub array_bytes: Option<i64>,
    pub column_bytes: Option<i64>,
    pub first_started_at: Option<String>,
    pub last_ended_at: Option<String>,
    pub wal_bytes: Option<u64>,
    pub database_allocated_bytes: Option<u64>,
    pub wal_allocated_bytes: Option<u64>,
}

// A second POSIX file descriptor for the database must not be closed while SQLite
// holds process-wide advisory locks. stat(2) obtains allocated blocks without opening it.
#[cfg(unix)]
fn sizes(path: &Path) -> io::Result<(u64, u64)> {
    use std::os::unix::fs::MetadataExt;
    let info = fs::metadata(path)?;
    let allocated = info
        .blocks()
        .checked_mul(512)
        .ok_or_else(|| io::Error::other("allocation overflow"))?;
    Ok((info.len(), allocated))
}
#[cfg(windows)]
fn sizes(path: &Path) -> io::Result<(u64, u64)> {
    use fs2::FileExt;
    let file = fs::File::open(path)?;
    Ok((file.metadata()?.len(), file.allocated_size()?))
}
#[cfg(not(any(unix, windows)))]
fn sizes(_path: &Path) -> io::Result<(u64, u64)> {
    Err(io::ErrorKind::Unsupported.into())
}

impl Archive {
    pub async fn sweep_storage_sample(
        &self,
        session: &str,
        stream: &str,
    ) -> Result<SweepStorageSample> {
        let path = self.inner.config.path.clone();
        let mut wal = OsString::from(&path);
        wal.push("-wal");
        let session = session.to_owned();
        let stream = stream.to_owned();
        self.inner
            .read(move |db| {
                let database_allocated_bytes = sizes(&path).ok().map(|(_, allocated)| allocated);
                let wal_sizes = match sizes(Path::new(&wal)) {
                    Ok(pair) => Some(pair),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => Some((0, 0)),
                    Err(_) => None,
                };
                Ok(db.query_row(
                    include_str!("../../../radioinspector/monitoring/storage_window.sql"),
                    [session, stream],
                    |row| {
                        Ok(SweepStorageSample {
                            count: row.get(0)?,
                            array_bytes: row.get(1)?,
                            column_bytes: row.get(2)?,
                            first_started_at: row.get(3)?,
                            last_ended_at: row.get(4)?,
                            wal_bytes: wal_sizes.map(|(length, _)| length),
                            wal_allocated_bytes: wal_sizes.map(|(_, allocated)| allocated),
                            database_allocated_bytes,
                        })
                    },
                )?)
            })
            .await
    }
}
