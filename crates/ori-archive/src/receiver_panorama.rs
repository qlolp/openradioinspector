//! Indexed projections of sequential receiver observations. Only original,
//! RX-confirmed points supply levels, coverage, checkpoints or references.
mod guard;
mod index;
#[cfg(test)]
mod tests;
pub use index::verified_receiver_scan_context;
pub(crate) use index::{insert, install};

use crate::{Archive, ArchiveError, Result, error::invalid, rows};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::fmt::Write;

const MAX_PASS: u64 = 9_007_199_254_740_991;

struct Grid {
    start: i64,
    stop: i64,
    step: i64,
    total: usize,
}

impl Grid {
    fn checked(start: i64, stop: i64, step: i64, total: i64) -> Option<Self> {
        if !(100_000..=1_300_000_000).contains(&start)
            || !(start + 1..=1_300_000_000).contains(&stop)
            || !(10..=1_300_000_000).contains(&step)
            || [start, stop, step].iter().any(|n| n % 10 != 0)
            || !(2..=129_991).contains(&total)
            || total != (stop - start + step - 1) / step + 1
        {
            return None;
        }
        Some(Self {
            start,
            stop,
            step,
            total: total as usize,
        })
    }

    fn from_columns(row: &rusqlite::Row<'_>, first: usize) -> Result<Self> {
        Self::checked(
            row.get(first)?,
            row.get(first + 1)?,
            row.get(first + 2)?,
            row.get(first + 3)?,
        )
        .ok_or_else(|| ArchiveError::Corrupt("receiver grid dimensions".into()))
    }

    fn frequency(&self, index: usize) -> i64 {
        (self.start + index as i64 * self.step).min(self.stop)
    }

    fn exact_index(&self, frequency: i64) -> Option<usize> {
        if frequency == self.stop {
            return Some(self.total - 1);
        }
        let delta = frequency - self.start;
        (frequency >= self.start && frequency < self.stop && delta % self.step == 0)
            .then_some((delta / self.step) as usize)
    }
}

struct Pass {
    context: i64,
    key: String,
    number: i64,
    grid: Grid,
    start_time: String,
    end_time: String,
}

fn pass_number(pass: u64) -> Result<i64> {
    (1..=MAX_PASS)
        .contains(&pass)
        .then_some(pass as i64)
        .ok_or_else(|| invalid("receiver scan pass"))
}

fn select_pass(
    db: &Connection,
    session: &str,
    stream: &str,
    number: Option<i64>,
    key: Option<&str>,
) -> Result<Pass> {
    let current: i64 = if let Some(key) = key {
        db.query_row(
            "SELECT id FROM ori_ardv1_contexts_v1 WHERE session_id=? AND stream_id=? AND context_key=?",
            params![session, stream, key], |r| r.get(0),
        )
    } else {
        db.query_row(
            "SELECT id FROM ori_ardv1_contexts_v1
        WHERE session_id=? AND stream_id=? ORDER BY last_seq DESC LIMIT 1",
            params![session, stream],
            |r| r.get(0),
        )
    }.optional()?.ok_or(ArchiveError::NotFound)?;
    let current_pass = if let Some(number) = number {
        // Prefer the current context when a changed plan restarted numbering.
        let found = db
            .query_row(
                "SELECT 1 FROM ori_ardv1_passes_v1 WHERE context_id=? AND pass=?",
                params![current, number],
                |_| Ok(()),
            )
            .optional()?
            .is_some();
        let context = if found {
            current
        } else if key.is_some() {
            return Err(ArchiveError::NotFound);
        } else {
            db.query_row("SELECT context_id FROM ori_ardv1_passes_v1 WHERE stream_id=? AND pass=? ORDER BY last_seq DESC LIMIT 1",
                params![stream,number],|r|r.get(0)).optional()?.ok_or(ArchiveError::NotFound)?
        };
        (context, number)
    } else {
        let number = db.query_row(
            "SELECT pass FROM ori_ardv1_passes_v1 WHERE context_id=? ORDER BY pass DESC LIMIT 1",
            [current],
            |r| r.get(0),
        )?;
        (current, number)
    };
    let (context, number) = current_pass;
    let mut stmt = db.prepare(
        "SELECT c.start_hz,c.stop_hz,c.step_hz,c.total,p.started_at,p.ended_at,c.context_key
         FROM ori_ardv1_contexts_v1 c JOIN ori_ardv1_passes_v1 p ON p.context_id=c.id
         WHERE c.id=? AND p.pass=?",
    )?;
    let mut cursor = stmt.query(params![context, number])?;
    let row = cursor.next()?.ok_or(ArchiveError::NotFound)?;
    Ok(Pass {
        context,
        key: row.get(6)?,
        number,
        grid: Grid::from_columns(row, 0)?,
        start_time: row.get(4)?,
        end_time: row.get(5)?,
    })
}

fn hex(bytes: &[u8]) -> String {
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(encoded, "{byte:02x}").expect("String write");
    }
    encoded
}

#[derive(Default, Clone)]
struct Bin {
    measured: usize,
    // Raw S-meter count and archive insertion order provide a stable minimum.
    best: Option<(u32, i64, usize)>,
}

fn bin_range(total: usize, columns: usize, column: usize) -> std::ops::Range<usize> {
    (column * total).div_ceil(columns)..((column + 1) * total).div_ceil(columns)
}

fn project_points(
    db: &Connection,
    pass: &Pass,
    bins: &mut [Bin],
    coverage: &mut [u8],
) -> Result<usize> {
    // Each subquery uses the unique (context_id, pass, idx) index. Separate
    // MIN/MAX queries let SQLite seek directly to each end of this pass.
    let (measured, first, last): (u32, Option<u32>, Option<u32>) = db.query_row(
        "SELECT
         (SELECT COUNT(*) FROM ori_ardv1_points_v1 WHERE context_id=?1 AND pass=?2),
         (SELECT MIN(idx) FROM ori_ardv1_points_v1 WHERE context_id=?1 AND pass=?2),
         (SELECT MAX(idx) FROM ori_ardv1_points_v1 WHERE context_id=?1 AND pass=?2)",
        params![pass.context, pass.number],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )?;
    let (Some(first), Some(last)) = (first, last) else {
        return Ok(0);
    };
    let (measured, first, last) = (measured as usize, first as usize, last as usize);
    let total = pass.grid.total;
    if last >= total {
        return Err(ArchiveError::Corrupt("receiver point index".into()));
    }
    let columns = bins.len();
    if measured == last - first + 1 {
        // Unique integer indices and one read snapshot prove every bit in
        // this span came from a measured point, including both endpoints.
        coverage[first / 8..=last / 8].fill(u8::MAX);
        coverage[first / 8] &= u8::MAX << (first % 8);
        coverage[last / 8] &= u8::MAX >> (7 - last % 8);
        let mut strongest = db.prepare(
            "SELECT best_level,best_seq,idx FROM ori_ardv1_points_v1
             WHERE context_id=?1 AND pass=?2 AND idx>=?3 AND idx<?4
             ORDER BY best_level,best_seq,idx LIMIT 1",
        )?;
        for (i, bin) in bins.iter_mut().enumerate() {
            let range = bin_range(total, columns, i);
            let start = range.start.max(first);
            let end = range.end.min(last + 1);
            if start < end {
                bin.measured = end - start;
                bin.best = Some(strongest.query_row(
                    params![pass.context, pass.number, start as i64, end as i64],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get::<_, u32>(2)? as usize)),
                )?);
            }
        }
    } else {
        let mut points = db.prepare(
            "SELECT idx,best_level,best_seq FROM ori_ardv1_points_v1
             WHERE context_id=? AND pass=? ORDER BY idx",
        )?;
        let mut cursor = points.query(params![pass.context, pass.number])?;
        while let Some(row) = cursor.next()? {
            let index = row.get::<_, u32>(0)? as usize;
            coverage[index / 8] |= 1 << (index % 8);
            let bin = &mut bins[index * columns / total];
            let candidate = (row.get(1)?, row.get(2)?, index);
            bin.measured += 1;
            if bin.best.is_none_or(|best| candidate < best) {
                bin.best = Some(candidate);
            }
        }
    }
    Ok(measured)
}

impl Archive {
    pub async fn latest_receiver_scan_point(
        &self,
        session: &str,
        stream: &str,
        plan_key: &str,
    ) -> Result<Option<Value>> {
        let [session, stream, plan] = [session, stream, plan_key].map(str::to_owned);
        self.inner
            .read(move |db| {
                let id = db
                    .query_row(
                        "SELECT last_id FROM ori_ardv1_contexts_v1
                WHERE session_id=? AND stream_id=? AND plan_key=? ORDER BY last_seq DESC LIMIT 1",
                        params![session, stream, plan],
                        |r| r.get::<_, String>(0),
                    )
                    .optional()?;
                match id {
                    Some(id) => rows::get_observation(db, &id),
                    None => Ok(None),
                }
            })
            .await
    }

    pub async fn receiver_panorama(
        &self,
        session: &str,
        stream: &str,
        pass: Option<u64>,
        columns: usize,
    ) -> Result<Value> {
        self.receiver_panorama_in_context(session, stream, pass, columns, None)
            .await
    }

    pub async fn receiver_panorama_in_context(
        &self,
        session: &str,
        stream: &str,
        pass: Option<u64>,
        columns: usize,
        context_id: Option<&str>,
    ) -> Result<Value> {
        if !(2..=1024).contains(&columns) {
            return Err(invalid("receiver panorama columns"));
        }
        let pass = pass.map(pass_number).transpose()?;
        let [session, stream] = [session, stream].map(str::to_owned);
        let context = context_id.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let pass = select_pass(db, &session, &stream, pass, context.as_deref())?;
                let grid = &pass.grid;
                let columns = columns.min(grid.total);
                let mut bins = vec![Bin::default(); columns];
                let mut coverage = vec![0u8; grid.total.div_ceil(8)];
                let measured = project_points(db, &pass, &mut bins, &mut coverage)?;
                let mut winners = db.prepare(
                    "SELECT o.id,o.started_at,o.ended_at FROM ori_ardv1_points_v1 p
                 JOIN observations o ON o.id=COALESCE(p.best_id,p.first_id)
                 WHERE p.context_id=? AND p.pass=? AND p.idx=?",
                )?;
                let mut result = Vec::with_capacity(columns);
                let mut remaining = maximum;
                for (i, bin) in bins.into_iter().enumerate() {
                    let Some((level, _, index)) = bin.best else {
                        continue;
                    };
                    let mut cursor =
                        winners.query(params![pass.context, pass.number, index as i64])?;
                    let row = cursor.next()?.ok_or(ArchiveError::NotFound)?;
                    let id = row
                        .get_ref(0)?
                        .as_str()
                        .map_err(|_| ArchiveError::Corrupt("receiver observation ID".into()))?;
                    remaining = remaining
                        .checked_sub(id.len())
                        .ok_or(ArchiveError::QueryBudget)?;
                    let range = bin_range(grid.total, columns, i);
                    result.push(json!({
                        "start_index": range.start, "end_index": range.end - 1,
                        "measured_points": bin.measured,
                        "frequency_hz": grid.frequency(index),
                        "level": level, "observation_id": id,
                        "started_at": crate::validate::utc(&row.get::<_, String>(1)?)?,
                        "ended_at": crate::validate::utc(&row.get::<_, String>(2)?)?
                    }));
                }
                Ok(json!({
                    "pass": pass.number, "context_id": pass.key, "total_points": grid.total, "measured_points": measured,
                    "complete": measured == grid.total, "start_hz": grid.start,
                    "stop_hz": grid.stop, "step_hz": grid.step,
                    "level_unit": "s_meter_count", "lower_is_stronger": true,
                    "started_at": pass.start_time, "ended_at": pass.end_time,
                    "coverage_hex": hex(&coverage), "bins": result
                }))
            })
            .await
    }

    pub async fn receiver_panorama_point(
        &self,
        session: &str,
        stream: &str,
        pass: u64,
        frequency: u64,
    ) -> Result<Value> {
        self.receiver_panorama_point_in_context(session, stream, pass, frequency, None)
            .await
    }

    pub async fn receiver_panorama_point_in_context(
        &self,
        session: &str,
        stream: &str,
        pass: u64,
        frequency: u64,
        context_id: Option<&str>,
    ) -> Result<Value> {
        let number = pass_number(pass)?;
        let frequency =
            i64::try_from(frequency).map_err(|_| invalid("receiver point frequency"))?;
        let [session, stream] = [session, stream].map(str::to_owned);
        let context = context_id.map(str::to_owned);
        self.inner
            .read(move |db| {
                let pass = select_pass(db, &session, &stream, Some(number), context.as_deref())?;
                let idx = pass
                    .grid
                    .exact_index(frequency)
                    .ok_or(ArchiveError::NotFound)?;
                let id = db
                    .query_row(
                        "SELECT COALESCE(best_id,first_id) FROM ori_ardv1_points_v1
                WHERE context_id=? AND pass=? AND idx=?",
                        params![pass.context, pass.number, idx as i64],
                        |r| r.get::<_, String>(0),
                    )
                    .optional()?
                    .ok_or(ArchiveError::NotFound)?;
                rows::get_observation(db, &id)?.ok_or(ArchiveError::NotFound)
            })
            .await
    }
}
