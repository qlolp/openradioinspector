//! Leased, bounded reads of fixed derivative tables; no public paths or SQL.
use crate::{
    HistoryError, Result,
    storage::{Roots, SealedSnapshot, checked},
};
use fs2::FileExt;
use ori_runtime::{
    qos::BackgroundQos,
    worker::{Cancellation, ResearchPool},
};
use serde::Serialize;
use std::{
    fs::{self, File, OpenOptions},
    io::Read,
    sync::{Arc, Mutex},
};

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportTable {
    Streams,
    Sweeps,
    SweepPoints,
    Observations,
}
impl ExportTable {
    fn name(self) -> &'static str {
        match self {
            Self::Streams => "streams",
            Self::Sweeps => "sweeps",
            Self::SweepPoints => "sweep_points",
            Self::Observations => "observations",
        }
    }
}
#[derive(Clone, Serialize)]
pub struct ExportInfo {
    pub file: String,
    pub bytes: u64,
    pub sha256: String,
}
struct ReadState {
    file: Option<File>,
    lock: Option<File>,
    roots: Option<Arc<Roots>>,
    source: Option<Arc<SealedSnapshot>>,
    offset: u64,
}
pub(crate) struct ReaderInner {
    state: Mutex<ReadState>,
    cancel: Cancellation,
}
impl ReaderInner {
    pub(crate) fn is_open(&self) -> bool {
        self.state.lock().is_ok_and(|s| s.file.is_some())
    }
    pub(crate) fn close(&self) {
        self.cancel.cancel();
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        drop(state.file.take());
        drop(state.lock.take());
        drop(state.source.take());
        drop(state.roots.take());
    }
}
impl Drop for ReaderInner {
    fn drop(&mut self) {
        self.close();
    }
}
pub struct ExportReader {
    inner: Arc<ReaderInner>,
    qos: BackgroundQos,
    info: ExportInfo,
    pool: ResearchPool,
}
impl Drop for ExportReader {
    fn drop(&mut self) {
        self.inner.close();
    }
}
impl ExportReader {
    pub fn info(&self) -> &ExportInfo {
        &self.info
    }
    pub async fn read_chunk(&mut self) -> Result<Vec<u8>> {
        let (remaining, cancel) = {
            let state = self
                .inner
                .state
                .lock()
                .map_err(|_| HistoryError::Internal)?;
            if state.file.is_none() {
                return Err(HistoryError::Closed);
            }
            (
                self.info.bytes.saturating_sub(state.offset),
                self.inner.cancel.clone(),
            )
        };
        if remaining == 0 {
            return Ok(Vec::new());
        }
        let count = remaining.min(65536) as usize;
        let permit = self.pool.try_reserve()?;
        let guard = self.qos.register(cancel.clone())?;
        self.qos.account(count as u64, &cancel).await?;
        let inner = self.inner.clone();
        let (bytes, _joined_guard, _joined_permit) = tokio::task::spawn_blocking(move || {
            let mut state = inner.state.lock().map_err(|_| HistoryError::Internal)?;
            if inner.cancel.is_cancelled() {
                return Err(HistoryError::Cancelled);
            }
            let file = state.file.as_mut().ok_or(HistoryError::Closed)?;
            let mut bytes = vec![0; count];
            let read = file.read(&mut bytes).map_err(|_| HistoryError::Internal)?;
            if read == 0 {
                return Err(HistoryError::Internal);
            }
            state.offset = state.offset.saturating_add(read as u64);
            bytes.truncate(read);
            Ok((bytes, guard, permit))
        })
        .await
        .map_err(|_| HistoryError::Internal)??;
        Ok(bytes)
    }
}
pub(crate) fn open(
    roots: Arc<Roots>,
    source: Arc<SealedSnapshot>,
    output: &crate::JobOutput,
    table: ExportTable,
    qos: BackgroundQos,
    pool: ResearchPool,
) -> Result<ExportReader> {
    let mut readers = roots.readers.lock().map_err(|_| HistoryError::Internal)?;
    readers.retain(|r| r.upgrade().is_some_and(|r| r.is_open()));
    if readers.len() >= 2 {
        return Err(HistoryError::Busy);
    }
    let key = output.data["cache_key"]
        .as_str()
        .filter(|s| crate::viewport::hex_key(s))
        .ok_or(HistoryError::Internal)?;
    let directory = roots.worker.join(format!("cache-{key}"));
    checked(&directory, true)?;
    let entry = &output.data["manifest"]["files"][table.name()];
    let file = format!("{}.parquet", table.name());
    if entry["file"] != file {
        return Err(HistoryError::Internal);
    }
    let bytes = entry["bytes"]
        .as_u64()
        .filter(|n| *n <= roots.maximum)
        .ok_or(HistoryError::Internal)?;
    let sha256 = entry["sha256"]
        .as_str()
        .filter(|s| crate::viewport::hex_key(s))
        .ok_or(HistoryError::Internal)?
        .into();
    let lock_path = roots.worker.join(".worker.lock");
    checked(&lock_path, false)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .open(lock_path)
        .map_err(|_| HistoryError::UnsafeStorage)?;
    FileExt::try_lock_shared(&lock).map_err(|_| HistoryError::Busy)?;
    let path = directory.join(&file);
    checked(&path, false)?;
    if fs::metadata(&path)
        .map_err(|_| HistoryError::UnsafeStorage)?
        .len()
        != bytes
    {
        return Err(HistoryError::Internal);
    }
    let handle = File::open(path).map_err(|_| HistoryError::UnsafeStorage)?;
    let inner = Arc::new(ReaderInner {
        state: Mutex::new(ReadState {
            file: Some(handle),
            lock: Some(lock),
            roots: Some(roots.clone()),
            source: Some(source),
            offset: 0,
        }),
        cancel: Cancellation::default(),
    });
    readers.push(Arc::downgrade(&inner));
    Ok(ExportReader {
        inner,
        qos,
        info: ExportInfo {
            file,
            bytes,
            sha256,
        },
        pool,
    })
}
