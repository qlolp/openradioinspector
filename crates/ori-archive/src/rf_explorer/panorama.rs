use super::validate::{POINTS, Plan, STEP};
use crate::{ArchiveError, Result};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::sync::Arc;

pub(super) struct Window {
    pub pass_key: i64,
    pub tile: usize,
    pub start: String,
    pub end: String,
}

struct Peak {
    source: Arc<Window>,
    point: usize,
    index: usize,
    byte: u8,
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
        if bytes.len() != POINTS || !(1..=plan.tiles()).contains(&source.tile) {
            return Err(ArchiveError::Corrupt("RF Explorer tile index".into()));
        }
        let latest = &mut self.latest[source.tile - 1];
        if latest.as_ref().is_none_or(|last| last < &source.end) {
            *latest = Some(source.end.clone());
        }
        let first = plan.first(source.tile);
        let count = plan.count();
        let width = self.columns.len();
        for (point, &byte) in bytes.iter().enumerate() {
            let index = first + point;
            let bucket = &mut self.columns[index * width / count];
            // A lower device byte is a stronger dBm level. Ties keep the
            // earliest acquisition, physical tile and original point index.
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
                *bucket = Some(Peak {
                    source: source.clone(),
                    point,
                    index,
                    byte,
                });
            }
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
        let tail = self.latest.last().and_then(Option::as_ref);
        let mut columns = Vec::with_capacity(self.columns.len());
        let mut originals =
            db.prepare("SELECT sweep_id FROM ori_rfe_tiles WHERE pass_key=? AND tile=?")?;
        for peak in self.columns {
            let Some(peak) = peak else {
                columns.push(Value::Null);
                continue;
            };
            let primary = &self.latest[peak.index / POINTS];
            let latest = if peak.index >= plan.count() - POINTS {
                primary.as_ref().max(tail)
            } else {
                primary.as_ref()
            };
            let source = peak.source;
            // Resolve only winning IDs. Keeping a potentially large user-defined
            // ID per candidate window would defeat the bounded column state.
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
            columns.push(json!({
                "frequency_hz":frequency(peak.index),"level_dbm":f64::from(plan.offset_db)-f64::from(peak.byte)/2.0,
                "sweep_id":id,"point_index":peak.point,"tile":source.tile,"started_at":source.start,
                "ended_at":source.end,"last_measured_at":latest
            }));
        }
        Ok((columns, coverage))
    }
}
