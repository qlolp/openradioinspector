//! A real SQL worker must finish its job's budget ownership before replying.
use super::*;
use std::sync::atomic::{AtomicBool, Ordering};

struct Budget {
    deny: bool,
    entered: mpsc::Sender<()>,
    release: Mutex<mpsc::Receiver<()>>,
    finished: Arc<AtomicBool>,
}
impl LegacyIoBudget for Budget {
    fn account(&self, _: u64, _: &LegacyCancellation) -> Result<()> {
        if self.deny {
            Err(LegacyError::ResourceLimit("owned denial".into()))
        } else {
            Ok(())
        }
    }
}
impl Drop for Budget {
    fn drop(&mut self) {
        let _ = self.entered.send(());
        let _ = self
            .release
            .get_mut()
            .unwrap()
            .recv_timeout(Duration::from_secs(3));
        self.finished.store(true, Ordering::SeqCst);
    }
}

#[tokio::test]
async fn threshold_reply_follows_actual_budget_cleanup_on_commit_and_refusal() {
    for deny in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("source.pan");
        let db = Connection::open(&path).unwrap();
        db.execute_batch("CREATE TABLE panorama(id INTEGER PRIMARY KEY,recorded_at TEXT,latitude REAL,longitude REAL,altitude REAL,speed REAL,heading REAL,levels BLOB,start_hz REAL,stop_hz REAL,step_hz REAL,unit TEXT); CREATE TABLE panorama_meta(key TEXT,value TEXT); CREATE TABLE threshold_line(line_index INTEGER PRIMARY KEY,frequencies BLOB,levels BLOB); CREATE TABLE signal_event(id INTEGER,panorama_id INTEGER,recorded_at TEXT,frequency_hz REAL,bandwidth_hz REAL,power_over_w REAL,level_db REAL);").unwrap();
        let expected = data_version(&db).unwrap();
        db.close().unwrap();
        let before = fs::read(&path).unwrap();
        let owner = PanOwner::open(path.clone(), 128 * 1024 * 1024)
            .await
            .unwrap();
        let (entered, ready) = mpsc::channel();
        let (release, gate) = mpsc::channel();
        let finished = Arc::new(AtomicBool::new(false));
        let (reply, mut response) = oneshot::channel();
        owner
            .send(Command::Threshold {
                expected,
                frequencies: 100.0_f64.to_le_bytes().to_vec(),
                levels: (-80.0_f64).to_le_bytes().to_vec(),
                cancel: LegacyCancellation::default(),
                io_budget: Some(Arc::new(Budget {
                    deny,
                    entered,
                    release: Mutex::new(gate),
                    finished: finished.clone(),
                })),
                timeout: Duration::from_secs(2),
                reply,
            })
            .unwrap();
        tokio::task::spawn_blocking(move || ready.recv_timeout(Duration::from_secs(3)).unwrap())
            .await
            .unwrap();
        let premature = response.try_recv();
        let before_cleanup = finished.load(Ordering::SeqCst);
        release.send(()).unwrap();
        let (replied_early, result) = match premature {
            Ok(result) => (true, result),
            Err(oneshot::error::TryRecvError::Empty) => (
                false,
                tokio::time::timeout(Duration::from_secs(3), response)
                    .await
                    .unwrap()
                    .unwrap(),
            ),
            Err(error) => panic!("SQL response disappeared: {error}"),
        };
        owner.close().await.unwrap();
        let db = Connection::open(&path).unwrap();
        let rows: i64 = db
            .query_row("SELECT COUNT(*) FROM threshold_line", [], |row| row.get(0))
            .unwrap();
        db.close().unwrap();
        assert!(!before_cleanup);
        assert!(finished.load(Ordering::SeqCst));
        assert_eq!(rows, i64::from(!deny));
        if deny {
            assert!(matches!(result, Err(LegacyError::ResourceLimit(_))));
            assert_eq!(fs::read(&path).unwrap(), before);
        } else {
            assert!(result.is_ok());
        }
        assert!(
            !replied_early,
            "SQL reply escaped while its budget destructor was blocked (deny={deny})"
        );
    }
}
