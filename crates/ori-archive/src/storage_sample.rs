//! Bounded measurements for the storage forecast. No numeric arrays cross
//! the SQLite reader boundary. WAL size is sampled independently of page_count.
use crate::{Archive, Result};
use std::{ffi::OsString, fs, io::ErrorKind};

#[derive(serde::Serialize)]
pub struct SweepStorageSample {
    pub count: i64,
    pub array_bytes: Option<i64>,
    pub column_bytes: Option<i64>,
    pub first_started_at: Option<String>,
    pub last_ended_at: Option<String>,
    pub wal_bytes: Option<u64>,
}

impl Archive {
    pub async fn sweep_storage_sample(
        &self,
        session: &str,
        stream: &str,
    ) -> Result<SweepStorageSample> {
        let mut wal = OsString::from(&self.inner.config.path);
        wal.push("-wal");
        let session = session.to_owned();
        let stream = stream.to_owned();
        self.inner
            .read(move |db| {
                let wal_bytes = match fs::metadata(&wal) {
                    Ok(file) => Some(file.len()),
                    Err(error) if error.kind() == ErrorKind::NotFound => Some(0),
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
                            wal_bytes,
                        })
                    },
                )?)
            })
            .await
    }
}
