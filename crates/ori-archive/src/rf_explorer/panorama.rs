use super::validate::{POINTS, Plan, STEP};
use crate::{ArchiveError, Result};
use rusqlite::{Connection, Statement, params};
use serde_json::{Value, json};
use std::sync::Arc;

pub(super) struct Window {
    pub pass_key: i64,
    pub tile: usize,
    pub start: String,
    pub end: String,
}

impl Window {
    fn first(&self, plan: &Plan, bytes: &[u8]) -> Result<usize> {
        if bytes.len() != POINTS || !(1..=plan.tiles()).contains(&self.tile) {
            return Err(ArchiveError::Corrupt("RF Explorer tile index".into()));
        }
        Ok(plan.first(self.tile))
    }

    fn update_latest(&self, latest: &mut Option<String>) {
        if latest.as_ref().is_none_or(|last| last < &self.end) {
            *latest = Some(self.end.clone());
        }
    }
}

struct Peak {
    source: Arc<Window>,
    point: usize,
    index: usize,
    byte: u8,
}

const ORIGINAL: &str = "SELECT sweep_id FROM ori_rfe_tiles_v2 WHERE pass_key=? AND tile=?";

impl Peak {
    fn consider(
        bucket: &mut Option<Self>,
        source: &Arc<Window>,
        index: usize,
        point: usize,
        byte: u8,
    ) {
        // A lower device byte is a stronger dBm level. Ties retain the earliest
        // acquisition, physical tile and original point index.
        let wins = bucket.as_ref().is_none_or(|peak| {
            byte < peak.byte
                || (byte == peak.byte
                    && (&source.start, source.tile, point, source.pass_key)
                        < (
                            &peak.source.start,
                            peak.source.tile,
                            peak.point,
                            peak.source.pass_key,
                        ))
        });
        if wins {
            *bucket = Some(Self {
                source: source.clone(),
                point,
                index,
                byte,
            });
        }
    }

    fn value(
        self,
        originals: &mut Statement<'_>,
        plan: &Plan,
        latest: Option<&String>,
        budget: &mut usize,
    ) -> Result<Value> {
        // Resolve only winning IDs. Keeping a potentially large user-defined
        // ID per candidate window would defeat the bounded projection state.
        let source = self.source;
        let mut rows = originals.query(params![source.pass_key, source.tile as i64])?;
        let row = rows
            .next()?
            .ok_or_else(|| ArchiveError::Corrupt("missing RF Explorer source".into()))?;
        let id = row
            .get_ref(0)?
            .as_str()
            .map_err(|_| ArchiveError::Corrupt("RF Explorer source ID".into()))?;
        *budget = budget
            .checked_sub(id.len())
            .ok_or(ArchiveError::QueryBudget)?;
        Ok(json!({
            "frequency_hz": plan.start_hz + self.index as u64 * STEP,
            "level_dbm": f64::from(plan.offset_db) - f64::from(self.byte) / 2.0,
            "sweep_id": id, "point_index": self.point, "tile": source.tile,
            "started_at": source.start, "ended_at": source.end, "last_measured_at": latest
        }))
    }
}

pub(super) struct Marker {
    index: usize,
    peak: Option<Peak>,
    latest: Option<String>,
}
impl Marker {
    pub fn new(index: usize) -> Self {
        Self {
            index,
            peak: None,
            latest: None,
        }
    }

    pub fn add(&mut self, plan: &Plan, source: &Arc<Window>, bytes: &[u8]) -> Result<()> {
        let first = source.first(plan, bytes)?;
        let point = self
            .index
            .checked_sub(first)
            .filter(|i| *i < POINTS)
            .ok_or_else(|| {
                ArchiveError::Corrupt("RF Explorer marker outside its source tile".into())
            })?;
        source.update_latest(&mut self.latest);
        Peak::consider(&mut self.peak, source, self.index, point, bytes[point]);
        Ok(())
    }

    pub fn render(self, db: &Connection, plan: &Plan, budget: &mut usize) -> Result<Value> {
        match self.peak {
            Some(peak) => peak.value(
                &mut db.prepare(ORIGINAL)?,
                plan,
                self.latest.as_ref(),
                budget,
            ),
            None => Ok(Value::Null),
        }
    }
}
pub(super) struct Panorama {
    columns: Vec<Option<Peak>>,
    // A frequency belongs to one full tile, or to both physical tail tiles.
    // This suffices to compute the latest measurement at the winning frequency
    // even when the strongest sample came from a much earlier pass.
    latest: Vec<Option<String>>,
}

impl Panorama {
    pub fn new(plan: &Plan, columns: usize) -> Self {
        Self {
            columns: (0..columns.min(plan.count())).map(|_| None).collect(),
            latest: vec![None; plan.tiles()],
        }
    }
    pub fn add(&mut self, plan: &Plan, source: &Arc<Window>, bytes: &[u8]) -> Result<()> {
        let first = source.first(plan, bytes)?;
        source.update_latest(&mut self.latest[source.tile - 1]);
        let count = plan.count();
        let width = self.columns.len();
        for (point, &byte) in bytes.iter().enumerate() {
            let index = first + point;
            let bucket = &mut self.columns[index * width / count];
            Peak::consider(bucket, source, index, point, byte);
        }
        Ok(())
    }
    pub fn render(
        self,
        db: &Connection,
        plan: &Plan,
        budget: &mut usize,
    ) -> Result<(Vec<Value>, Vec<Value>)> {
        let mut ranges: Vec<(usize, usize)> = Vec::new();
        for (index, time) in self.latest.iter().enumerate() {
            if time.is_none() {
                continue;
            }
            let first = plan.first(index + 1);
            let last = first + POINTS - 1;
            if let Some((_, previous)) = ranges.last_mut()
                && first <= *previous + 1
            {
                *previous = (*previous).max(last);
            } else {
                ranges.push((first, last));
            }
        }
        let frequency = |index: usize| plan.start_hz + index as u64 * STEP;
        let coverage = ranges
            .into_iter()
            .map(|(a, b)| json!({"start_hz":frequency(a),"stop_hz":frequency(b)}))
            .collect();
        let mut columns = Vec::with_capacity(self.columns.len());
        let mut originals = db.prepare(ORIGINAL)?;
        for peak in self.columns {
            let Some(peak) = peak else {
                columns.push(Value::Null);
                continue;
            };
            let [primary, tail] = plan.source_tiles(peak.index);
            let latest = self.latest[primary - 1]
                .as_ref()
                .max(self.latest[tail - 1].as_ref());
            columns.push(peak.value(&mut originals, plan, latest, budget)?);
        }
        Ok((columns, coverage))
    }
}
