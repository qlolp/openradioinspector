//! Native writer for python -m benchmarks.ori_bench.panorama_storage --native PATH.
use ori_archive::{Archive, ArchiveConfig, parse_json_value};
use rusqlite::Connection;
use serde_json::json;
use std::{
    fs,
    io::{self, BufRead},
    path::PathBuf,
    time::Instant,
};

fn checkpoint(db: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    let (busy, _, _): (i64, i64, i64) =
        db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })?;
    if busy != 0 {
        return Err("checkpoint busy".into());
    }
    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args_os().skip(1);
    let path = PathBuf::from(args.next().ok_or("expected fresh archive path")?);
    let schema = args.next().ok_or("expected schema 6 or 7")?;
    if path.exists() || (schema != "6" && schema != "7") || args.next().is_some() {
        return Err("expected fresh archive path and schema 6 or 7".into());
    }
    let mut config = ArchiveConfig::new(&path);
    config.compact_sweep_events = schema == "7";
    let archive = Archive::open(config)?;
    let mut input = io::stdin().lock().lines();
    let session = input.next().ok_or("expected session")??;
    archive
        .create_session(parse_json_value(session.as_bytes(), 1_000_000)?)
        .await?;
    let db = Connection::open(&path)?;
    checkpoint(&db)?;
    let size = || {
        db.query_row(
            "SELECT page_size * page_count FROM pragma_page_size,pragma_page_count",
            [],
            |r| r.get::<_, i64>(0),
        )
    };
    let mut wal = path.as_os_str().to_owned();
    wal.push("-wal");
    let files = || -> io::Result<_> {
        Ok(json!({"main_bytes":fs::metadata(&path)?.len(),"wal_bytes":fs::metadata(&wal)?.len()}))
    };
    let baseline = size()?;
    // Hold a single WAL snapshot during each run so the native maintenance worker
    // cannot recycle the log before the observation. Ingestion continues normally.
    let snapshot = Connection::open(&path)?;
    snapshot.execute_batch("BEGIN; SELECT sequence FROM events LIMIT 1")?;
    let started = Instant::now();
    let mut count = 0;
    for line in input {
        let mut batch = parse_json_value(line?.as_bytes(), 16 * 1024 * 1024)?;
        let items = std::mem::take(batch.as_array_mut().ok_or("expected a batch of sweeps")?);
        count += items.len();
        archive.append_sweeps(items).await?;
    }
    let elapsed = started.elapsed().as_secs_f64();
    let before = files()?;
    let growth = size()? - baseline;
    let ledger: i64 = db.query_row("SELECT sum(length(CAST(payload_json AS BLOB))) FROM events WHERE event_type='sweep.received'",
        [], |r| r.get(0))?;
    snapshot.execute_batch("ROLLBACK")?;
    checkpoint(&db)?;
    println!(
        "{}",
        json!({"schema":db.query_row("PRAGMA user_version", [],|r|r.get::<_,i64>(0))?,
        "logical_growth_bytes":growth,"logical_growth_bytes_per_sweep":growth as f64/count as f64,
        "event_payload_bytes":ledger,"before_checkpoint":before,"after_checkpoint":files()?,
        "ingest_seconds":elapsed})
    );
    archive.close().await?;
    Ok(())
}
