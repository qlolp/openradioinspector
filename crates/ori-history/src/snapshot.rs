//! A bounded derived source, never a full recovery backup or a mutable DB.
use crate::{
    Filters, HistoryConfig, HistoryError, Result,
    snapshot_owner::SnapshotOwner,
    storage::{Roots, SealedSnapshot},
    types::MAX_RECORD_BYTES,
};
use ori_archive::{
    Archive, ScopedSnapshotExport, ScopedSnapshotKind, ScopedSnapshotQuery, ScopedSnapshotRecord,
    SnapshotExportLimits,
};
use ori_runtime::worker::Cancellation;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{fs::File, io::Write, sync::Arc, time::Duration};

struct Writer {
    owned: Option<SnapshotOwner>,
    roots: Arc<Roots>,
    file: Option<File>,
    hash: Sha256,
    bytes: u64,
    source_limit: u64,
    available: u64,
    counts: [u64; 3],
}
fn io<T>(value: std::io::Result<T>) -> Result<T> {
    value.map_err(|_| HistoryError::ResourceLimit)
}
impl Writer {
    fn digest(&self) -> String {
        self.hash
            .clone()
            .finalize()
            .iter()
            .map(|n| format!("{n:02x}"))
            .collect()
    }
    fn new(roots: Arc<Roots>, source_limit: u64) -> Result<Self> {
        let owned = SnapshotOwner::new(roots.clone())?;
        let mut value = Self {
            owned: Some(owned),
            roots,
            file: None,
            hash: Sha256::new(),
            bytes: 0,
            source_limit,
            available: 0,
            counts: [0; 3],
        };
        value.available = value.roots.maximum.saturating_sub(value.roots.total()?);
        let owned = value.owned.as_mut().ok_or(HistoryError::Internal)?;
        owned.directory("staging")?;
        value.file = Some(owned.file("staging/records.ndjson")?);
        Ok(value)
    }
    fn admit(&self, bytes: usize) -> Result<()> {
        let next = self
            .bytes
            .checked_add(bytes as u64)
            .ok_or(HistoryError::ResourceLimit)?;
        if next > self.source_limit {
            return Err(HistoryError::SourceLimit);
        }
        if next > self.available {
            return Err(HistoryError::ResourceLimit);
        }
        Ok(())
    }
    fn append(&mut self, kind: ScopedSnapshotKind, bytes: &[u8]) -> Result<()> {
        self.admit(bytes.len())?;
        io(self
            .file
            .as_mut()
            .ok_or(HistoryError::Internal)?
            .write_all(bytes))?;
        self.hash.update(bytes);
        self.bytes += bytes.len() as u64;
        let index = match kind {
            ScopedSnapshotKind::Stream => 0,
            ScopedSnapshotKind::Sweep => 1,
            ScopedSnapshotKind::Observation => 2,
        };
        self.counts[index] = self.counts[index]
            .checked_add(1)
            .ok_or(HistoryError::ResourceLimit)?;
        Ok(())
    }
    fn manifest(&self, boundary: u64, filters: &Filters) -> Result<Vec<u8>> {
        let value = json!({"derived_source_version":1,"purpose":"derived-history",
            "records":{"file":"records.ndjson","bytes":self.bytes,"sha256":self.digest()},
            "event_watermark":boundary,"filters":filters,
            "counts":{"streams":self.counts[0],"sweeps":self.counts[1],"observations":self.counts[2]},"audio_payloads_copied":false});
        let mut bytes = ori_contracts::to_json_vec(&value).map_err(|_| HistoryError::Internal)?;
        bytes.push(b'\n');
        self.admit(bytes.len())?;
        if bytes.len() > 65536 {
            return Err(HistoryError::ResourceLimit);
        }
        Ok(bytes)
    }
    fn finish(&mut self, boundary: u64, manifest: &[u8]) -> Result<Arc<SealedSnapshot>> {
        self.admit(manifest.len())?;
        let file = self.file.take().ok_or(HistoryError::Internal)?;
        io(file.sync_all())?;
        let mut permissions = io(file.metadata())?.permissions();
        permissions.set_readonly(true);
        io(file.set_permissions(permissions))?;
        drop(file);
        let owned = self.owned.as_mut().ok_or(HistoryError::Internal)?;
        let manifest_file = owned.file("staging/manifest.json")?;
        let mut output = manifest_file;
        io(output.write_all(manifest))?;
        io(output.sync_all())?;
        let mut permissions = io(output.metadata())?.permissions();
        permissions.set_readonly(true);
        io(output.set_permissions(permissions))?;
        drop(output);
        self.roots.total()?;
        owned.rename("staging", "sealed")?;
        let directory = owned.path()?.join("sealed");
        #[cfg(unix)]
        {
            io(File::open(owned.path()?).and_then(|f| f.sync_all()))?;
        }
        Ok(Arc::new(SealedSnapshot {
            directory,
            boundary,
            sha256: self.digest(),
            _owned: self.owned.take().ok_or(HistoryError::Internal)?,
            _roots: self.roots.clone(),
        }))
    }
    fn cleanup(&mut self) {
        drop(self.file.take());
        drop(self.owned.take());
    }
}
impl Drop for Writer {
    fn drop(&mut self) {
        self.cleanup();
    }
}

async fn cancelled(cancel: &Cancellation) {
    while !cancel.is_cancelled() {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}
fn selection_error(error: ori_archive::ArchiveError) -> HistoryError {
    match error {
        ori_archive::ArchiveError::NotFound | ori_archive::ArchiveError::Invalid(_) => {
            HistoryError::Worker {
                code: "invalid_filter".into(),
                retryable: false,
            }
        }
        other => other.into(),
    }
}
async fn charge(config: &HistoryConfig, cancel: &Cancellation, bytes: usize) -> Result<()> {
    let mut remaining = bytes;
    while remaining > 0 {
        let next = remaining.min(65536);
        config.qos.account(next as u64, cancel).await?;
        remaining -= next;
    }
    Ok(())
}
async fn record_bytes(record: ScopedSnapshotRecord) -> Result<(ScopedSnapshotKind, Vec<u8>)> {
    tokio::task::spawn_blocking(move || {
        let mut bytes =
            ori_contracts::to_json_vec(&json!({"kind":record.kind.as_str(),"value":record.value}))
                .map_err(|_| HistoryError::Internal)?;
        bytes.push(b'\n');
        if bytes.len() > MAX_RECORD_BYTES {
            return Err(HistoryError::ResourceLimit);
        }
        Ok((record.kind, bytes))
    })
    .await
    .map_err(|_| HistoryError::Internal)?
}
async fn collect(
    reader: &ScopedSnapshotExport,
    writer: Writer,
    config: &HistoryConfig,
    filters: &Filters,
    cancel: &Cancellation,
) -> (Option<Writer>, Result<(u64, Vec<u8>)>) {
    let mut writer = Some(writer);
    let result = async {
        let info = tokio::select! {
            value = reader.ready() => value.map_err(selection_error)?,
            _ = cancelled(cancel) => return Err(HistoryError::Cancelled),
        };
        loop {
            let record = tokio::select! {
                value = reader.next_record() => value?,
                _ = cancelled(cancel) => return Err(HistoryError::Cancelled),
            };
            let Some(record) = record else { break };
            let (kind, bytes) = record_bytes(record).await?;
            // Account the bounded canonical payload read even when the selected
            // source will exceed quota; repeated rejected jobs are still paced.
            charge(config, cancel, bytes.len()).await?;
            writer
                .as_ref()
                .ok_or(HistoryError::Internal)?
                .admit(bytes.len())?;
            charge(config, cancel, bytes.len()).await?;
            let mut owned = writer.take().ok_or(HistoryError::Internal)?;
            let (returned, result) = tokio::task::spawn_blocking(move || {
                let result = owned.append(kind, &bytes);
                (owned, result)
            })
            .await
            .map_err(|_| HistoryError::Internal)?;
            writer = Some(returned);
            result?;
        }
        let manifest = writer
            .as_ref()
            .ok_or(HistoryError::Internal)?
            .manifest(info.boundary, filters)?;
        charge(config, cancel, manifest.len()).await?;
        Ok((info.boundary, manifest))
    }
    .await;
    (writer, result)
}

pub(crate) async fn build(
    archive: Archive,
    roots: Arc<Roots>,
    config: &HistoryConfig,
    filters: &Filters,
    cancel: &Cancellation,
) -> Result<Arc<SealedSnapshot>> {
    if cancel.is_cancelled() {
        return Err(HistoryError::Cancelled);
    }
    let query = ScopedSnapshotQuery {
        session_id: filters.session_id.clone(),
        stream_id: filters.stream_id.clone(),
        started_at_us: filters.started_at_us,
        ended_at_us: filters.ended_at_us,
        min_frequency_hz: filters.min_frequency_hz,
        max_frequency_hz: filters.max_frequency_hz,
    };
    let reader = ScopedSnapshotExport::open_live(
        archive,
        query,
        SnapshotExportLimits {
            max_record_bytes: MAX_RECORD_BYTES,
            ..Default::default()
        },
    )
    .map_err(selection_error)?;
    let root = roots.clone();
    let limit = config.max_source_bytes;
    let writer = match tokio::task::spawn_blocking(move || Writer::new(root, limit)).await {
        Ok(writer) => writer,
        Err(_) => {
            reader.close().await?;
            return Err(HistoryError::Internal);
        }
    };
    let (mut writer, result) = match writer {
        Ok(writer) => collect(&reader, writer, config, filters, cancel).await,
        Err(error) => {
            reader.close().await?;
            return Err(error);
        }
    };
    // Always JOIN the original read transaction, including failed/cancelled
    // selection, before sealing or releasing the same research reservation.
    let close = reader.close().await.map_err(HistoryError::from);
    let result = match (result, close) {
        (_, Err(error)) => Err(error),
        (result, Ok(())) => result,
    };
    let result = if cancel.is_cancelled() {
        Err(HistoryError::Cancelled)
    } else {
        result
    };
    let output = tokio::task::spawn_blocking(move || {
        let output = result.and_then(|(boundary, manifest)| {
            writer
                .as_mut()
                .ok_or(HistoryError::Internal)?
                .finish(boundary, &manifest)
        });
        drop(writer);
        output
    })
    .await
    .map_err(|_| HistoryError::Internal)?;
    if roots.cleanup_failed() {
        return Err(HistoryError::Internal);
    }
    output
}
