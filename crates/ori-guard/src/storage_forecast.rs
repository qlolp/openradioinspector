//! Working space on the selected archive volume, including optional recovery artifacts.
use crate::{GuardError, GuardService, Result, StorageProbe, util::*};
use serde_json::{Value, json};
use std::{collections::VecDeque, time::Duration};

pub(crate) const RECHECK: Duration = Duration::from_secs(2);
const SAMPLE_WINDOW: Duration = Duration::from_secs(60);
const MIN_SPAN: Duration = Duration::from_secs(5);

#[derive(Clone, Copy)]
struct SizeSample {
    at: Duration,
    logical: u64,
    allocated: Option<u64>,
    database: Option<u64>,
    wal: Option<u64>,
}
#[derive(Default)]
pub(crate) struct GrowthWindow(VecDeque<SizeSample>);

#[derive(Default)]
struct Growth {
    logical: Option<f64>,
    physical: Option<f64>,
    database: Option<f64>,
    wal: Option<f64>,
}

impl GrowthWindow {
    fn observe(&mut self, sample: SizeSample) -> Growth {
        // Calls from clients and supervisor ticks share one bounded window per archive.
        // Discard stale data and ignore out-of-order / repeated reads of the same second.
        while self
            .0
            .front()
            .is_some_and(|old| sample.at.saturating_sub(old.at) > SAMPLE_WINDOW)
        {
            self.0.pop_front();
        }
        if self
            .0
            .back()
            .is_none_or(|last| sample.at.saturating_sub(last.at) >= Duration::from_secs(1))
        {
            self.0.push_back(sample);
        }
        let latest = self.0.back().expect("first sample was inserted");
        let mut growth = Growth::default();
        for previous in &self.0 {
            let span = latest.at.saturating_sub(previous.at);
            if span < MIN_SPAN {
                continue;
            }
            // Use the greatest recent positive slope to follow a newly pinned reader.
            // Recycled WAL blocks and shrinking files consume no additional free space.
            let slope =
                |before: u64, after: u64| after.saturating_sub(before) as f64 / span.as_secs_f64();
            let logical = slope(previous.logical, latest.logical);
            growth.logical = Some(growth.logical.unwrap_or(0.).max(logical));
            let update = |rate: &mut Option<f64>, before: Option<u64>, after: Option<u64>| {
                if let Some((before, after)) = before.zip(after) {
                    *rate = Some(rate.unwrap_or(0.).max(slope(before, after)));
                }
            };
            update(&mut growth.physical, previous.allocated, latest.allocated);
            update(&mut growth.database, previous.database, latest.database);
            update(&mut growth.wal, previous.wal, latest.wal);
        }
        growth
    }
}

fn projected_bytes(rate: Option<f64>, seconds: u64) -> Option<u64> {
    let bytes = (rate? * seconds as f64).ceil();
    (bytes.is_finite() && bytes >= 0. && bytes < u64::MAX as f64).then_some(bytes as u64)
}

impl GuardService {
    pub async fn storage_report(&self, id: &str, probe: bool) -> Result<Value> {
        let project = self.project(id).await?;
        let config = project.get("archive").cloned().unwrap_or_else(
            || json!({"minimum_free_bytes":268435456,"minimum_retention_seconds":3600}),
        );
        let minimum = integer(&config, "minimum_free_bytes")?;
        let retention_required = integer(&config, "minimum_retention_seconds")?;
        let selected = |key: &str| {
            config
                .get(key)
                .map(|v| v.as_bool().ok_or_else(|| invalid(format!("archive {key}"))))
                .transpose()
        };
        let reserve_backup = selected("reserve_backup")?.unwrap_or(false);
        let reserve_restore = selected("reserve_restore")?.unwrap_or(false);
        let reserve_copies = reserve_backup || reserve_restore;
        let duty_remaining = self.remaining_duty_seconds(&project)?;
        let horizon = duty_remaining.unwrap_or(retention_required);
        let require_source_rate = reserve_copies || duty_remaining.is_some();
        let sample_at = self.clock.monotonic();
        let capacity = self.archive.capacity().await?;
        let inspected = self.control.storage_probe().await.unwrap_or(StorageProbe {
            filesystem_free_bytes: None,
            writable: None,
        });
        let receiver_primary = project["primary_source_kind"] == "ardv1";
        let sid = s(&project, "active_session_id")?;
        let stream = s(
            &project,
            if receiver_primary {
                "receiver_stream_id"
            } else {
                "analyzer_stream_id"
            },
        )?;
        let mut sample = self.archive.sweep_storage_sample(sid, stream).await?;
        if receiver_primary {
            let observations = self
                .archive
                .list_observations(sid, Some(stream), 64)
                .await?;
            sample.count = observations.len() as i64;
            sample.first_started_at = observations
                .last()
                .and_then(|v| v["interval"]["started_at"].as_str())
                .map(str::to_owned);
            sample.last_ended_at = observations
                .first()
                .and_then(|v| v["interval"]["ended_at"].as_str())
                .map(str::to_owned);
            let mut bytes = 0;
            for v in &observations {
                bytes += spaced_json(v, true)?.len() as i64;
            }
            sample.column_bytes = Some(bytes);
            sample.array_bytes = None;
        }
        let mean_stored_sweep_bytes = sample
            .array_bytes
            .map(|bytes| bytes as f64 / sample.count as f64);
        let mut rate = None;
        if sample.count >= 2
            && let (Some(first), Some(last), Some(bytes)) = (
                &sample.first_started_at,
                &sample.last_ended_at,
                sample.column_bytes,
            )
        {
            let span = elapsed(first, last)?;
            if span > 0. {
                // Forecast the original plus ledger, with room for record envelopes.
                // Measured whole-archive page growth below may raise this estimate.
                rate = Some(2. * (bytes as f64 + 256. * sample.count as f64) / span);
            }
        }
        let source_rate_measured = rate.is_some_and(|rate| rate.is_finite() && rate > 0.);
        let now = self.clock.monotonic();
        let size = capacity["database_bytes"]
            .as_u64()
            .ok_or_else(|| invalid("archive database_bytes"))?;
        let allocated = sample
            .database_allocated_bytes
            .zip(sample.wal_allocated_bytes)
            .and_then(|(db, wal)| db.checked_add(wal));
        let growth = self
            .capacity_samples
            .lock()
            .map_err(|_| GuardError::Conflict("capacity samples lock failed".into()))?
            .observe(SizeSample {
                at: sample_at,
                logical: size,
                allocated,
                database: sample.database_allocated_bytes,
                wal: sample.wal_allocated_bytes,
            });
        if let Some(logical) = growth.logical.filter(|r| *r > 0.) {
            rate = Some(rate.unwrap_or(0.).max(logical));
        }
        let mut startup_basis_used = false;
        if require_source_rate
            && !source_rate_measured
            && let Some(runtime) = self.runtime_snapshot(id)?
            && !runtime.stopped
            && runtime.sid == s(&project, "active_session_id")?
            && let Some(basis) = runtime.startup_storage_basis
            && project["baseline_id"] == basis.baseline_id
            && now < basis.expires
        {
            rate = Some(rate.unwrap_or(0.).max(basis.bytes_per_second));
            startup_basis_used = true;
        }
        if require_source_rate && !source_rate_measured && !startup_basis_used {
            // Metadata/page growth alone does not establish the new run's
            // measurement rate once the validated startup window ends.
            rate = None;
        }
        // The backup API copies a consistent logical SQLite image. The live volume
        // can grow faster than that image when its WAL needs additional blocks.
        let recording_rate = rate.map(|r| r.max(growth.physical.unwrap_or(0.)));
        let future_logical = projected_bytes(rate, horizon);
        let future_recording = projected_bytes(recording_rate, horizon);
        let snapshot = future_logical.and_then(|n| size.checked_add(n));
        let backup = if reserve_backup { snapshot } else { Some(0) };
        let restore = if reserve_restore { snapshot } else { Some(0) };
        let reserve = backup
            .zip(restore)
            .and_then(|(b, r)| minimum.checked_add(b)?.checked_add(r));
        let required = reserve
            .zip(future_recording)
            .and_then(|(r, n)| r.checked_add(n));
        let free = inspected.filesystem_free_bytes;
        // Even without a source rate we know the cost of the current image and floor.
        let copies = u64::from(reserve_backup) + u64::from(reserve_restore);
        let minimum_required = size
            .checked_mul(copies)
            .and_then(|n| minimum.checked_add(n));
        let minimum_shortfall = minimum_required
            .zip(free)
            .map(|(needed, left)| needed.saturating_sub(left));
        // Available filesystem space already excludes all currently allocated files.
        let after_reserve = free.zip(reserve).map(|(f, r)| f.saturating_sub(r));
        let quota = capacity["remaining_bytes"].as_u64();
        let free_seconds = after_reserve.zip(recording_rate).map(|(f, r)| f as f64 / r);
        let quota_seconds = quota.zip(rate).map(|(q, r)| q as f64 / r);
        let retention = free_seconds.map(|f| quota_seconds.map_or(f, |q| f.min(q)));
        let available = after_reserve.map_or(0, |f| quota.map_or(f, |q| f.min(q)));
        let shortfall = required
            .zip(free)
            .map(|(needed, left)| needed.saturating_sub(left));
        let quota_shortfall = future_logical
            .zip(quota)
            .map(|(needed, left)| needed.saturating_sub(left));
        let reason = if inspected.writable != Some(true) || free.is_none() || allocated.is_none() {
            "volume_unavailable"
        } else if capacity["state"] == "full" || quota_shortfall.is_some_and(|n| n > 0) {
            "archive_quota"
        } else if minimum_required.is_none() {
            "forecast_overflow"
        } else if minimum_shortfall.is_some_and(|n| n > 0) {
            "insufficient_space"
        } else if require_source_rate && rate.is_none() {
            "source_rate_unavailable"
        } else if reserve_restore && growth.physical.is_none() {
            "measuring_allocation"
        } else if reserve.is_none() || (rate.is_some() && required.is_none()) {
            "forecast_overflow"
        } else if available == 0 || shortfall.is_some_and(|n| n > 0) {
            "insufficient_space"
        } else {
            "ready"
        };
        let mut report = json!({
            "storage_id":"node-archive", "capacity":capacity, "filesystem_free_bytes":free,
            "minimum_free_bytes":minimum, "available_bytes":available,
            "bytes_per_second":rate, "recording_bytes_per_second":recording_rate,
            "physical_growth_bytes_per_second":growth.physical,
            "database_growth_bytes_per_second":growth.database, "wal_growth_bytes_per_second":growth.wal,
            "database_allocated_bytes":sample.database_allocated_bytes, "wal_allocated_bytes":sample.wal_allocated_bytes,
            "estimated_retention_seconds":retention, "required_retention_seconds":retention_required,
            "forecast_seconds":horizon, "forecast_basis":if duty_remaining.is_some() { "duty_window" } else { "minimum_retention" },
            "projected_recording_bytes":future_recording, "required_free_bytes":required,
            "backup_reserve_requested":reserve_backup, "backup_reserve_bytes":backup,
            "restore_reserve_requested":reserve_restore, "restore_reserve_bytes":restore,
            "effective_minimum_free_bytes":reserve, "shortfall_bytes":shortfall,
            "minimum_required_free_bytes":minimum_required, "minimum_shortfall_bytes":minimum_shortfall,
            "quota_shortfall_bytes":quota_shortfall, "next_recheck_seconds":RECHECK.as_secs(),
            "ready":reason=="ready","forecast_status":reason,
            "write_probe":if probe{inspected.writable}else{None},
            "rate_source":"measured_archive_or_conservative_payload_window","automatic_deletion":false
        });
        if receiver_primary {
            report["sampled_observations"] = json!(sample.count);
        } else {
            report["sampled_sweeps"] = json!(sample.count);
            report["mean_stored_sweep_bytes"] = json!(mean_stored_sweep_bytes);
        }
        report["projected_bytes_per_day"] = json!(rate.filter(|r| *r > 0.).map(|r| r * 86_400.));
        report["wal_bytes"] = json!(sample.wal_bytes);
        report["writer_queue"] = json!(self.archive.queue_status());
        if startup_basis_used {
            report["rate_source"] = json!("validated_preflight_startup");
        }
        Ok(report)
    }
}
