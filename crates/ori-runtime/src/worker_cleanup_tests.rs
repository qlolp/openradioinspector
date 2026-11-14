use super::*;

#[test]
fn cleanup_records_are_bounded_and_do_not_format_error_data() {
    const SECRET: &str = "DO_NOT_LOG command environment token child-data";
    let errors = [
        Error::Io(std::io::Error::other(SECRET)),
        Error::Unsafe(SECRET.into()),
        Error::Busy,
        Error::Unauthorized,
        Error::PairingUnavailable,
        Error::Cancelled,
        Error::Deadline,
        Error::ResourceLimit,
        Error::OutputLimit,
        Error::Protocol(SECRET.into()),
        Error::Json(serde_json::from_str::<serde_json::Value>("[").unwrap_err()),
    ];
    for error in errors {
        let record = cleanup_error_record(u32::MAX, &error);
        assert!(record.is_ascii() && record.len() <= 512);
        assert!(record.starts_with("ori-research-cleanup-error owned_pid=4294967295 variant="));
        assert!(record.ends_with(" raw_os_errno=none\n"));
        assert!(!record.contains(SECRET));
        assert_eq!(record.lines().count(), 1);
    }
    let record = cleanup_error_record(
        u32::MAX,
        &Error::Io(std::io::Error::from_raw_os_error(i32::MIN)),
    );
    assert_eq!(
        record,
        "ori-research-cleanup-error owned_pid=4294967295 variant=Io raw_os_errno=-2147483648\n"
    );
    assert!(record.len() <= 512);
}

#[test]
fn diagnostic_write_failure_is_attempted_once_and_does_not_escape_cleanup() {
    struct RejectedWrite {
        calls: usize,
    }
    impl Write for RejectedWrite {
        fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
            self.calls += 1;
            Err(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "synthetic diagnostic sink failure",
            ))
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let state = Mutex::new(CleanupLoggerState::New);
    let mut joined = None;
    let sender = cleanup_logger_sender_from(&state, |receiver| {
        joined = Some(thread::spawn(move || {
            let mut output = RejectedWrite { calls: 0 };
            drain_cleanup_records(receiver, &mut output);
            output.calls
        }));
        Ok(())
    })
    .unwrap();
    let mut reported = false;
    dispatch_cleanup_error_once(&mut reported, 17, &Error::Busy, || Some(sender.clone()));
    dispatch_cleanup_error_once(&mut reported, 17, &Error::Cancelled, || {
        Some(sender.clone())
    });
    drop(sender);
    drop(state);
    let calls = joined.unwrap().join().unwrap();
    assert!(reported);
    assert_eq!(calls, 1);
}

#[test]
fn owner_dispatch_does_not_wait_for_a_blocked_sink() {
    struct HeldSink {
        entered: Option<mpsc::SyncSender<()>>,
        release: mpsc::Receiver<()>,
        records: Vec<Vec<u8>>,
    }
    impl Write for HeldSink {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            if let Some(entered) = self.entered.take() {
                entered.send(()).unwrap();
                self.release.recv().unwrap();
            }
            self.records.push(bytes.to_vec());
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let state = Arc::new(Mutex::new(CleanupLoggerState::New));
    let logger_join = Arc::new(Mutex::new(None));
    let (entered, observed) = mpsc::sync_channel(0);
    let (release, held) = mpsc::channel();
    let (proceed, continue_owner) = mpsc::channel();
    let (done, returned) = mpsc::channel();
    let own_state = state.clone();
    let own_join = logger_join.clone();
    let owner = thread::spawn(move || {
        let sender = cleanup_logger_sender_from(&own_state, |receiver| {
            let logger = thread::spawn(move || {
                let mut sink = HeldSink {
                    entered: Some(entered),
                    release: held,
                    records: vec![],
                };
                drain_cleanup_records(receiver, &mut sink);
                sink.records
            });
            *own_join.lock().unwrap() = Some(logger);
            Ok(())
        })
        .unwrap();
        let mut reported = false;
        dispatch_cleanup_error_once(&mut reported, 17, &Error::Busy, || Some(sender.clone()));
        continue_owner.recv().unwrap();
        // One record is in the held sink, one fills the capacity-one queue;
        // a third owner must drop its record rather than wait for either.
        dispatch_cleanup_error_once(&mut false, 18, &Error::Busy, || Some(sender.clone()));
        dispatch_cleanup_error_once(&mut false, 19, &Error::Busy, || Some(sender.clone()));
        let mut duplicate_lookup = 0;
        dispatch_cleanup_error_once(&mut reported, 17, &Error::Cancelled, || {
            duplicate_lookup += 1;
            Some(sender.clone())
        });
        done.send(()).unwrap();
        duplicate_lookup
    });
    let sink_is_held = observed.recv_timeout(Duration::from_secs(5)).is_ok();
    let _ = proceed.send(());
    let returned_before_release =
        sink_is_held && returned.recv_timeout(Duration::from_secs(5)).is_ok();
    // Even a failing regression must release the sink and JOIN both original
    // synthetic threads before asserting, rather than detach the test logger.
    let _ = release.send(());
    let owner_result = owner.join();
    drop(state);
    let logger = logger_join.lock().unwrap().take();
    let logger_result = logger.map(|logger| logger.join());
    assert!(sink_is_held && returned_before_release);
    assert!(owner_result.is_ok_and(|lookups| lookups == 0));
    let records = logger_result.unwrap().unwrap();
    assert_eq!(records.len(), 2);
    assert!(records[0].starts_with(b"ori-research-cleanup-error owned_pid=17 "));
    assert!(records[1].starts_with(b"ori-research-cleanup-error owned_pid=18 "));
    assert!(records.iter().all(|record| record.len() <= 512));
}

#[test]
fn busy_or_failed_initialization_does_not_wait_or_spawn_again() {
    let state = Arc::new(Mutex::new(CleanupLoggerState::New));
    let held = state.lock().unwrap();
    let owner_state = state.clone();
    let (done, returned) = mpsc::channel();
    let owner = thread::spawn(move || {
        let unavailable = cleanup_logger_sender_from(&owner_state, |_| {
            panic!("busy initialization must not attempt a spawn")
        })
        .is_none();
        done.send(unavailable).unwrap();
    });
    let returned_while_locked = returned.recv_timeout(Duration::from_secs(5));
    drop(held);
    let joined = owner.join();
    assert!(joined.is_ok() && returned_while_locked == Ok(true));
    let mut attempts = 0;
    for _ in 0..2 {
        let unavailable = cleanup_logger_sender_from(&state, |_| {
            attempts += 1;
            Err(std::io::Error::other("synthetic spawn failure"))
        });
        assert!(unavailable.is_none());
    }
    assert_eq!(attempts, 1);
}
