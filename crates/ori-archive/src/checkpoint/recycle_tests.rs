use super::*;
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
};

fn salt(file: &mut File) -> [u8; 8] {
    file.seek(SeekFrom::Start(16)).unwrap();
    let mut salt = [0; 8];
    file.read_exact(&mut salt).unwrap();
    salt
}

fn writer(path: &Path) -> Connection {
    let writer = Connection::open(path).unwrap();
    writer.execute_batch("PRAGMA page_size=4096; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE originals(id INTEGER PRIMARY KEY, payload BLOB)").unwrap();
    writer
}

#[test]
fn catch_up_rechecks_progress_and_bounds_single_commit_overlaps() {
    let first = CatchUp::START.next(1_024, 1_056, 4).unwrap();
    assert!(first.next(1_024, 1_040, 1).is_none()); // no backfill advancement
    assert!(first.next(1_056, 1_089, 2).is_none()); // backlog grew over two commits
    assert!(first.next(1_056, 1_056, 1).is_none()); // already current
    assert!(first.next(1_056, 3_000, 1).is_none()); // tail is too large to chase
    let mut next = first;
    for backfilled in [1_056, 1_089, 1_122, 1_155, 1_188] {
        next = next.next(backfilled, backfilled as i32 + 33, 1).unwrap();
    }
    assert!(next.next(1_221, 1_254, 1).is_none());
}

struct Gate {
    snapshot: mpsc::Sender<()>,
    resume: mpsc::Receiver<()>,
}

#[derive(Clone)]
struct SteppedBudget {
    first_bulk: Arc<Mutex<Option<Gate>>>,
    finished: mpsc::Sender<()>,
}

struct SteppedPermit {
    budget: SteppedBudget,
    bytes: AtomicU64,
}

impl CheckpointBudget for SteppedBudget {
    fn try_begin(&self) -> Result<Option<Arc<dyn CheckpointIoPermit>>> {
        Ok(Some(Arc::new(SteppedPermit {
            budget: self.clone(),
            bytes: AtomicU64::new(0),
        })))
    }
}

const WATCHDOG: Duration = Duration::from_secs(30);

impl CheckpointIoPermit for SteppedPermit {
    fn account(&self, bytes: u64) -> Result<()> {
        // Opening this one-table DB takes only a few pages. Crossing 64 KiB
        // places the rendezvous inside the native bulk copy, after its snapshot.
        const BULK: u64 = 64 * 1024;
        let previous = self.bytes.fetch_add(bytes, Ordering::Relaxed);
        if previous < BULK
            && previous + bytes >= BULK
            && let Some(gate) = self.budget.first_bulk.lock().unwrap().take()
        {
            gate.snapshot.send(()).map_err(|_| ArchiveError::Closed)?;
            gate.resume
                .recv_timeout(WATCHDOG)
                .map_err(|_| ArchiveError::Closed)?;
        }
        Ok(())
    }
    fn cancel(&self) {}
}
impl Drop for SteppedPermit {
    fn drop(&mut self) {
        let _ = self.budget.finished.send(());
    }
}

#[test]
fn commit_during_bulk_backfill_gets_an_automatic_tail_pass_before_the_regular_retry() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("stepped.sqlite");
    let mut writer = writer(&path);
    let (control, requests) = Control::new();
    control.install(&mut writer).unwrap();
    let (snapshot, inside_bulk) = mpsc::channel();
    let (resume, written) = mpsc::channel();
    let (finished, passes) = mpsc::channel();
    let budget = Arc::new(SteppedBudget {
        first_bulk: Arc::new(Mutex::new(Some(Gate {
            snapshot,
            resume: written,
        }))),
        finished,
    });
    let owned = control.clone();
    let db = path.clone();
    let worker = thread::spawn(move || {
        let _ = owned.thread.set(thread::current());
        // Only a catch-up decision can schedule the second pass in this test.
        run(&owned, &requests, &db, budget, Duration::from_secs(3600));
    });
    let owner = Owner {
        control: control.clone(),
        handle: Some(worker),
    };
    let bulk = vec![0x19_u8; 5 * 1024 * 1024];
    writer
        .execute("INSERT INTO originals VALUES(1,?)", [&bulk])
        .unwrap();
    inside_bulk.recv_timeout(WATCHDOG).unwrap();
    let mut wal = File::open(path.with_extension("sqlite-wal")).unwrap();
    let initial_salt = salt(&mut wal);
    writer
        .execute("INSERT INTO originals VALUES(2,X'0041ff')", [])
        .unwrap();
    let after_tail = control.frames.load(Ordering::Acquire);
    resume.send(()).unwrap();
    passes
        .recv_timeout(WATCHDOG)
        .expect("bulk checkpoint finished");
    passes
        .recv_timeout(WATCHDOG)
        .expect("automatic follow-up finished before the regular retry");
    writer
        .execute("INSERT INTO originals VALUES(3,X'9a00')", [])
        .unwrap();
    assert_ne!(salt(&mut wal), initial_salt);
    assert!(control.frames.load(Ordering::Acquire) < after_tail);
    owner.join();
    drop(wal);
    drop(writer);

    let reopened = Connection::open(&path).unwrap();
    let mut query = reopened
        .prepare("SELECT payload FROM originals ORDER BY id")
        .unwrap();
    let originals: Vec<Vec<u8>> = query
        .query_map([], |row| row.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert_eq!(originals, [bulk, vec![0, 0x41, 0xff], vec![0x9a, 0]]);
}

// Run explicitly with --ignored --nocapture. Each report covers 120 commits.
#[test]
#[ignore = "paced WAL size and reuse measurement"]
fn measure_wal_size_during_steady_commits() {
    const ROWS: u32 = 120;
    const BYTES: usize = 120 * 1024;
    let batches: u32 =
        std::env::var("ORI_WAL_MEASUREMENT_BATCHES").map_or(1, |s| s.parse().unwrap());
    assert!((1..=32).contains(&batches));
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("steady.sqlite");
    let mut writer = writer(&path);
    let (control, requests) = Control::new();
    let owner = Owner::start(
        control.clone(),
        requests,
        path.clone(),
        Arc::new(LocalBudget::default()),
    )
    .unwrap();
    control.install(&mut writer).unwrap();
    let mut wal = File::open(path.with_extension("sqlite-wal")).unwrap();
    let mut previous = None;
    let mut rewinds = 0;
    let mut peak_bytes = 0;
    for id in 1..=ROWS * batches {
        writer
            .execute(
                "INSERT INTO originals VALUES(?,?)",
                rusqlite::params![id, vec![id as u8; BYTES]],
            )
            .unwrap();
        let salt = salt(&mut wal);
        let frames = control.frames.load(Ordering::Acquire);
        if previous.is_some_and(|(s, n)| salt != s && frames < n) {
            rewinds += 1;
        }
        previous = Some((salt, frames));
        peak_bytes = peak_bytes.max(wal.metadata().unwrap().len());
        if id % ROWS == 0 {
            eprintln!("steady WAL: rows={id}, peak_bytes={peak_bytes}, live_rewinds={rewinds}");
        }
        thread::sleep(Duration::from_millis(50));
    }
    owner.join();
    drop(wal);
    drop(writer);
    let reopened = Connection::open(&path).unwrap();
    let mut originals = reopened
        .prepare("SELECT id, payload FROM originals ORDER BY id")
        .unwrap();
    let mut rows = originals.query([]).unwrap();
    for expected in 1..=ROWS * batches {
        let row = rows.next().unwrap().unwrap();
        assert_eq!(row.get::<_, u32>(0).unwrap(), expected);
        assert_eq!(
            row.get::<_, Vec<u8>>(1).unwrap(),
            vec![expected as u8; BYTES]
        );
    }
    assert!(rows.next().unwrap().is_none());
}
