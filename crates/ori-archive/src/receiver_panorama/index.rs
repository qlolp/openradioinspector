//! Write-side validation and derived indices. The queue is populated by a
//! SQLite trigger so alternate archive writers cannot leave a permanent gap.
use super::{Grid, MAX_PASS, hex};
use crate::{ArchiveError, Result, rows};
use chrono::DateTime;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn claim(row: &str) -> String {
    format!(
        "{row}level_unit='s_meter_count' AND CASE WHEN json_valid({row}metadata_json) THEN
        json_type({row}metadata_json,'$.scan_pass')='integer' AND
        json_extract({row}metadata_json,'$.scope_source')='sequential_receiver_readback' END"
    )
}

pub(crate) fn install(db: &Connection) -> Result<()> {
    let installed: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='ori_ardv1_pending_v1')",
        [],
        |r| r.get(0),
    )?;
    db.execute_batch(include_str!("schema.sql"))?;
    db.execute_batch(&format!(
        "CREATE TRIGGER IF NOT EXISTS ori_ardv1_enqueue_v1
        AFTER INSERT ON observations WHEN {} BEGIN
        INSERT INTO ori_ardv1_pending_v1(observation_id) VALUES(NEW.id); END",
        claim("NEW.")
    ))?;
    if !installed {
        db.execute(
            &format!(
                "INSERT INTO ori_ardv1_pending_v1(observation_id)
            SELECT id FROM observations WHERE {} ORDER BY rowid",
                claim("")
            ),
            [],
        )?;
    }
    // Only claims from historical or rollback writers enter this cursor.
    // Do not load their event history or unrelated measurements into memory.
    let mut pending = db.prepare(
        "SELECT sequence,observation_id FROM ori_ardv1_pending_v1 ORDER BY sequence LIMIT 1",
    )?;
    let mut remove = db.prepare("DELETE FROM ori_ardv1_pending_v1 WHERE sequence=?")?;
    while let Some((seq, id)) = pending
        .query_row([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })
        .optional()?
    {
        match rows::get_observation(db, &id) {
            Ok(Some(raw)) => record(db, seq, &raw)?,
            Ok(None)
            | Err(ArchiveError::Invalid(_) | ArchiveError::Corrupt(_) | ArchiveError::Json(_)) => {}
            Err(error) => return Err(error),
        }
        remove.execute([seq])?;
    }
    Ok(())
}

pub(crate) fn insert(db: &Connection, raw: &Value) -> Result<()> {
    let meta = &raw["metadata"];
    if meta["scan_pass"].as_i64().is_none()
        || meta["scope_source"] != "sequential_receiver_readback"
    {
        return Ok(());
    }
    let sequence = db
        .prepare("DELETE FROM ori_ardv1_pending_v1 WHERE observation_id=? RETURNING sequence")?
        .query_row([raw["id"].as_str()], |r| r.get(0))
        .optional()?;
    if let Some(seq) = sequence {
        record(db, seq, raw)?;
    }
    Ok(())
}

struct Source<'a> {
    grid: Grid,
    first_us: i64,
    last_us: i64,
    index: i64,
    pass: i64,
    level: u32,
    plan: &'a str,
    firmware: &'a str,
    settle: f64,
    context: String,
}

// Check the saved VFO-A RX frame as well as its interpreted fields. An LM-only
// sample or an acknowledged tune is an ordinary observation, never coverage.
fn wire_level(raw: &Value, frequency: i64) -> Option<u32> {
    let meta = &raw["metadata"];
    let mut fields = meta["raw_receiver_status"].as_str()?.split(' ');
    if fields.next()? != "RX"
        || fields.next()? != "VFA"
        || fields.next()?
            != format!(
                "RF{:04}.{:05}",
                frequency / 1_000_000,
                frequency % 1_000_000 / 10
            )
    {
        return None;
    }
    let step = fields.next()?.strip_prefix("ST")?.as_bytes();
    let mode = fields.next()?;
    let md = mode.as_bytes();
    if step.len() != 6
        || step[3] != b'.'
        || !step[..3].iter().chain(&step[4..]).all(u8::is_ascii_digit)
        || md.len() != 5
        || &md[..2] != b"MD"
        || !matches!(md[2], b'0'..=b'7')
        || &md[3..] != b"F0"
        || meta.get("raw_mode_status").is_some_and(|s| s != mode)
    {
        return None;
    }
    let lm = fields.next()?.strip_prefix("LM")?;
    if fields.next().is_some()
        || !(2..=7).contains(&lm.len())
        || !lm.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let (count, squelch) = lm.split_at(lm.len() - 1);
    let squelch = squelch.parse::<u8>().ok()?;
    let count = count.parse::<u32>().ok()?;
    if squelch > 3
        || raw["level"].as_f64()? != f64::from(count)
        || meta
            .get("raw_squelch_status")
            .is_some_and(|s| s.as_u64() != Some(squelch.into()))
        || raw["squelch_open"]
            .as_bool()
            .is_some_and(|open| open != (squelch != 0))
    {
        return None;
    }
    Some(count)
}

/// Identify a canonical original using the same qualification as panorama and guard reads.
pub fn verified_receiver_scan_context(raw: &Value) -> Option<String> {
    source(raw).map(|point| point.context)
}

fn source(raw: &Value) -> Option<Source<'_>> {
    let m = &raw["metadata"];
    if raw["mode"] != "FM"
        || raw["level_unit"] != "s_meter_count"
        || m["scope_source"] != "sequential_receiver_readback"
        || m["adapter"] != "ardv1-serial"
        || m["receiver_profile"] != "ardv1-monitor-v1"
        || m["simulated"] != false
        || m["frequency_readback"] != true
        || m["frequency_source"] != "receiver_RX"
        || m["mode_source"] != "receiver_RX"
        || m["observed_mode"] != "FM"
        || m["requested_mode"] != "FM"
    {
        return None;
    }
    let plan = m["scan_plan_key"]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 512)?;
    let firmware = m["receiver_firmware"]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 128)?;
    let strategy = m["scan_strategy_requested"]
        .as_str()
        .filter(|s| matches!(*s, "auto" | "rf"))?;
    let start = m["scan_start_hz"].as_i64()?;
    let stop = m["scan_stop_hz"].as_i64()?;
    let step = m["scan_step_hz"].as_i64()?;
    let total = m["scan_total"].as_i64()?;
    let index = m["scan_index"].as_i64()?;
    let pass = m["scan_pass"].as_i64()?;
    let settle = m["settle_s"].as_f64().filter(|n| (0.0..=1.0).contains(n))?;
    let grid = Grid::checked(start, stop, step, total)?;
    if !(0..total).contains(&index) || !(1..=MAX_PASS as i64).contains(&pass) {
        return None;
    }
    let segment = index / 1000 + i64::from(total % 1000 == 1 && index == total - 2);
    if m["scan_segment_index"].as_i64()? != segment {
        return None;
    }
    let frequency = grid.frequency(index as usize);
    if [
        raw["frequency_hz"].as_i64(),
        m["observed_frequency_hz"].as_i64(),
        m["requested_frequency_hz"].as_i64(),
    ]
    .iter()
    .any(|v| *v != Some(frequency))
    {
        return None;
    }
    let level = wire_level(raw, frequency)?;
    for key in ["id", "session_id", "stream_id"] {
        raw[key].as_str().filter(|s| !s.trim().is_empty())?;
    }
    let first = DateTime::parse_from_rfc3339(raw["interval"]["started_at"].as_str()?).ok()?;
    let last = DateTime::parse_from_rfc3339(raw["interval"]["ended_at"].as_str()?).ok()?;
    if last < first {
        return None;
    }
    // Stable across restarts, independent of JSON key order or row IDs.
    let signature =
        serde_json::to_vec(&(plan, firmware, strategy, start, stop, step, total, settle)).ok()?;
    let context = hex(&Sha256::digest(signature));
    Some(Source {
        grid,
        first_us: first.timestamp_micros(),
        last_us: last.timestamp_micros(),
        index,
        pass,
        level,
        plan,
        firmware,
        settle,
        context,
    })
}

fn record(db: &Connection, seq: i64, raw: &Value) -> Result<()> {
    let Some(point) = source(raw) else {
        return Ok(());
    };
    let id = raw["id"].as_str();
    let session = raw["session_id"].as_str();
    let stream = raw["stream_id"].as_str();
    let start = raw["interval"]["started_at"].as_str().unwrap_or_default();
    let end = raw["interval"]["ended_at"].as_str().unwrap_or_default();
    let context: i64 = db.prepare("INSERT INTO ori_ardv1_contexts_v1
        (session_id,stream_id,context_key,plan_key,firmware,start_hz,stop_hz,step_hz,total,settle_s,last_seq,last_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(stream_id,context_key) DO UPDATE SET
        last_seq=excluded.last_seq,last_id=excluded.last_id RETURNING id")?
        .query_row(params![session,stream,point.context,point.plan,point.firmware,point.grid.start,point.grid.stop,
            point.grid.step,point.grid.total as i64,point.settle,seq,id],|r|r.get(0))?;
    db.prepare("INSERT INTO ori_ardv1_passes_v1(context_id,stream_id,pass,started_at,ended_at,last_seq)
        VALUES(?,?,?,?,?,?) ON CONFLICT(context_id,pass) DO UPDATE SET started_at=MIN(started_at,excluded.started_at),
        ended_at=MAX(ended_at,excluded.ended_at),last_seq=excluded.last_seq")?
        .execute(params![context,stream,point.pass,start,end,seq])?;
    let added = db.prepare("INSERT INTO ori_ardv1_points_v1
        (sequence,stream_id,context_id,pass,idx,level,first_id,started_at,ended_at,best_seq,best_level)
        VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?1,?6) ON CONFLICT(context_id,pass,idx) DO NOTHING")?
        .execute(params![seq,stream,context,point.pass,point.index,point.level,id,start,end])?;
    if added == 0 {
        db.prepare(
            "UPDATE ori_ardv1_points_v1 SET best_level=?1,best_seq=?2,best_id=?3
            WHERE context_id=?4 AND pass=?5 AND idx=?6 AND best_level>?1",
        )?
        .execute(params![
            point.level,
            seq,
            id,
            context,
            point.pass,
            point.index
        ])?;
        return Ok(());
    }
    let samples: i64 = db.prepare("INSERT INTO ori_ardv1_progress_v1(context_id,idx,samples,first_us,last_us)
        VALUES(?,?,1,?,?) ON CONFLICT(context_id,idx) DO UPDATE SET samples=samples+1,
        first_us=MIN(first_us,excluded.first_us),last_us=MAX(last_us,excluded.last_us) RETURNING samples")?
        .query_row(params![context,point.index,point.first_us,point.last_us],|r|r.get(0))?;
    if samples == 1 {
        db.prepare("UPDATE ori_ardv1_contexts_v1 SET seen=seen+1 WHERE id=?")?
            .execute([context])?;
    } else if samples >= 20
        && db
            .prepare(
                "UPDATE ori_ardv1_progress_v1 SET ready=1
        WHERE context_id=? AND idx=? AND ready=0 AND last_us-first_us>=60000000",
            )?
            .execute(params![context, point.index])?
            == 1
    {
        db.prepare("UPDATE ori_ardv1_contexts_v1 SET ready=ready+1 WHERE id=?")?
            .execute([context])?;
    }
    Ok(())
}
