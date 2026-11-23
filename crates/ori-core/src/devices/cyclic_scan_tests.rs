use super::*;

fn cyclic_session(id: &str, stop_hz: u64) -> Value {
    let mut recording = session(id);
    recording["streams"] = json!([recording["streams"][0].clone()]);
    recording["streams"][0]["id"] = json!(format!("{id}-r"));
    recording["streams"][0]["settings"] = json!({"adapter":"ardv1-serial",
        "receiver_profile":"ardv1-monitor-v1","mode":"FM","period_s":0,
        "fast_scan":{"start_hz":100_000,"stop_hz":stop_hz,"step_hz":10_000,
        "settle_s":0,"cyclic":true}});
    recording
}

#[tokio::test]
async fn cyclic_receiver_commits_two_passes_and_resumes_after_wrong_readback() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.wrong_rx_at.store(8, Ordering::Release);
    let core = core(directory.path(), factory.clone()).await;
    let archive = core.archive().unwrap();
    archive
        .create_session(cyclic_session("cycle", 115_010))
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap();
    let start = || {
        manager.start_stream(
            "cycle",
            "cycle-r",
            manager.public_gate(Target::new("cycle", "cycle-r")),
        )
    };
    start().await.unwrap();
    until(|| factory.drops.load(Ordering::Acquire) == 1).await;
    let stopped = manager.stop_stream("cycle", "cycle-r").await.unwrap();
    assert_eq!(stopped.state, "error");
    assert_eq!(stopped.current["receiver_scan"]["state"], "interrupted");
    assert_eq!(stopped.current["receiver_scan"]["next_pass"], 3);
    assert_eq!(stopped.current["receiver_scan"]["next_index"], 1);
    assert_eq!(stopped.current["receiver_scan"]["partial"], true);
    assert!(stopped.current["observed_frequency_hz"].is_null());
    assert_eq!(stopped.current["frequency_readback"], false);
    let state_events = archive.events(0, 100).await.unwrap();
    let interrupted = state_events
        .iter()
        .find(|event| event["event_type"] == "device.state" && event["payload"]["state"] == "error")
        .unwrap();
    assert_eq!(
        interrupted["payload"]["receiver_scan"],
        stopped.current["receiver_scan"]
    );
    let rows = archive
        .list_observations("cycle", Some("cycle-r"), 16)
        .await
        .unwrap();
    assert_eq!(rows.len(), 7);
    let mut points = rows.iter().collect::<Vec<_>>();
    points.sort_by_key(|row| {
        (
            row["metadata"]["scan_pass"].as_u64(),
            row["metadata"]["scan_index"].as_u64(),
        )
    });
    for (position, row) in points.into_iter().enumerate() {
        let meta = &row["metadata"];
        assert_eq!(meta["scope_source"], "sequential_receiver_readback");
        assert_eq!(meta["scan_pass"], position / 3 + 1);
        assert_eq!(meta["scan_index"], position % 3);
        assert_eq!(meta["scan_segment_index"], 0);
        assert_eq!(meta["scan_total"], 3);
        assert_eq!(meta["scan_start_hz"], 100_000);
        assert_eq!(meta["scan_stop_hz"], 115_010);
        assert_eq!(meta["scan_step_hz"], 10_000);
        assert_eq!(meta["settle_s"], 0.0);
        assert_eq!(meta["scan_tune_command"], "RF");
        assert_eq!(
            row["frequency_hz"],
            [100_000, 110_000, 115_010][position % 3]
        );
        assert_eq!(meta["requested_frequency_hz"], row["frequency_hz"]);
        assert_eq!(meta["observed_frequency_hz"], row["frequency_hz"]);
        assert_eq!(meta["requested_mode"], "FM");
        assert_eq!(meta["observed_mode"], "FM");
        assert_eq!(meta["frequency_readback"], true);
        assert_eq!(row["level"], 137.0);
        assert_eq!(row["level_unit"], "s_meter_count");
    }
    factory.wrong_rx_at.store(3, Ordering::Release);
    start().await.unwrap();
    until(|| factory.drops.load(Ordering::Acquire) == 2).await;
    let resumed = manager.stop_stream("cycle", "cycle-r").await.unwrap();
    assert_eq!(resumed.measurements, 2);
    assert_eq!(resumed.current["receiver_scan"]["next_pass"], 4);
    assert_eq!(resumed.current["receiver_scan"]["next_index"], 0);
    assert_eq!(resumed.current["receiver_scan"]["partial"], false);
    let rows = archive
        .list_observations("cycle", Some("cycle-r"), 20)
        .await
        .unwrap();
    assert_eq!(rows.len(), 9);
    for index in 0..3 {
        assert_eq!(
            rows.iter()
                .filter(|row| row["metadata"]["scan_pass"] == 3
                    && row["metadata"]["scan_index"] == index)
                .count(),
            1
        );
    }
    assert_eq!(factory.opens.load(Ordering::Acquire), 2);
    assert!(
        !factory
            .commands
            .lock()
            .unwrap()
            .iter()
            .any(|cmd| cmd == "ZK")
    );
    let mut changed = archive.get_session("cycle").await.unwrap().unwrap();
    changed["streams"][0]["settings"]["fast_scan"]["settle_s"] = json!(0.001);
    archive.save_session(changed, None).await.unwrap();
    factory.wrong_rx_at.store(2, Ordering::Release);
    start().await.unwrap();
    until(|| factory.drops.load(Ordering::Acquire) == 3).await;
    let changed = manager.stop_stream("cycle", "cycle-r").await.unwrap();
    assert_eq!(changed.current["receiver_scan"]["next_pass"], 1);
    assert_eq!(changed.current["receiver_scan"]["next_index"], 1);
    let changed_original = archive
        .get_observation(
            changed.current["receiver_scan"]["last_observation_id"]
                .as_str()
                .unwrap(),
        )
        .await
        .unwrap()
        .unwrap();
    assert_eq!(changed_original["metadata"]["settle_s"], 0.001);
    assert_ne!(
        changed_original["metadata"]["scan_plan_key"],
        rows[0]["metadata"]["scan_plan_key"]
    );
    core.close().await.unwrap();
}

#[tokio::test]
async fn cyclic_gateway_holds_and_resumes_the_owned_port_at_a_committed_boundary() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(directory.path(), factory.clone()).await;
    let archive = core.archive().unwrap();
    let mut recording = cyclic_session("held", 125_010);
    recording["streams"][0]["settings"]["period_s"] = json!(0.01);
    archive.create_session(recording).await.unwrap();
    let manager = core.devices.as_ref().unwrap();
    let target = Target::new("held", "held-r");
    let gate = || manager.public_gate(target.clone());
    let started = manager
        .start_stream("held", "held-r", gate())
        .await
        .unwrap();
    assert!(
        manager
            .tune(target.clone(), 145_500_000, Some("AM".into()), gate())
            .await
            .is_err()
    );
    let held = manager
        .set_receiver_scan_hold(target.clone(), true, gate())
        .await
        .unwrap();
    assert_eq!(held.worker_instance, started.worker_instance);
    assert_eq!(held.current["receiver_scan"]["state"], "held");
    let saved = held.current["receiver_scan"].clone();
    let original = manager.registry.lock().unwrap().workers[&target].clone();
    let instance = original.instance.clone();
    // The original permit is revoked while returning control. The driver must
    // wait for an authorized return before admitting another automatic point.
    let revoked = ExecutionGate::new(
        move || {
            if original.cyclic_scan.as_ref().unwrap().held() {
                Ok(())
            } else {
                Err(DeviceError::Denied)
            }
        },
        Some(instance),
    );
    assert!(matches!(
        manager
            .set_receiver_scan_hold(target.clone(), false, revoked)
            .await,
        Err(DeviceError::Denied)
    ));
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(
        manager.list(Some("held")).await.unwrap()[0].current["receiver_scan"],
        saved
    );
    let receipt = manager
        .tune(target.clone(), 145_500_000, Some("AM".into()), gate())
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if let Some(manual) = archive
                .list_observations("held", Some("held-r"), 1)
                .await
                .unwrap()
                .pop()
                && manual["frequency_hz"] == 145_500_000
            {
                assert_eq!(manual["mode"], "AM");
                assert_eq!(manual["metadata"]["frequency_source"], "receiver_RX");
                assert!(manual["metadata"]["receiver_tune_context"].is_string());
                assert!(manual["metadata"]["scan_pass"].is_null());
                break;
            }
            tokio::time::sleep(Duration::from_millis(3)).await;
        }
    })
    .await
    .unwrap();
    receipt.revalidate().unwrap();
    let resumed = manager
        .set_receiver_scan_hold(target.clone(), false, gate())
        .await
        .unwrap();
    assert_eq!(resumed.worker_instance, started.worker_instance);
    assert_eq!(resumed.current["receiver_scan"]["state"], "scanning");
    assert!(receipt.revalidate().is_err());
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let rows = archive
                .list_observations("held", Some("held-r"), 50)
                .await
                .unwrap();
            if rows.iter().any(|r| {
                r["metadata"]["scan_pass"] == saved["next_pass"]
                    && r["metadata"]["scan_index"] == saved["next_index"]
                    && r["mode"] == "FM"
            }) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(3)).await;
        }
    })
    .await
    .unwrap();
    let stopped = manager
        .stop_stream_mode("held", "held-r", true)
        .await
        .unwrap();
    assert!(!stopped.alive);
    assert_eq!(stopped.current["receiver_scan"]["state"], "stopped");
    assert_eq!(factory.opens.load(Ordering::Acquire), 1);
    assert_eq!(factory.drops.load(Ordering::Acquire), 1);
    assert_eq!(factory.commands.lock().unwrap().last().unwrap(), "EX");
    core.close().await.unwrap();
}

#[tokio::test]
async fn cyclic_receiver_resumes_uncommitted_point_after_archive_failure_and_core_restart() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(directory.path(), factory.clone()).await;
    let db = rusqlite::Connection::open(directory.path().join("sessions.sqlite3")).unwrap();
    db.execute_batch(
        "CREATE TRIGGER fail_cyclic_point BEFORE INSERT ON observations
        WHEN NEW.frequency_hz=110000 BEGIN SELECT RAISE(ABORT,'fixture commit failure'); END",
    )
    .unwrap();
    let archive = core.archive().unwrap();
    archive
        .create_session(cyclic_session("commit", 115_010))
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap();
    manager
        .start_stream(
            "commit",
            "commit-r",
            manager.public_gate(Target::new("commit", "commit-r")),
        )
        .await
        .unwrap();
    until(|| factory.drops.load(Ordering::Acquire) == 1).await;
    let report = manager.stop_stream("commit", "commit-r").await.unwrap();
    assert_eq!(report.state, "error");
    assert_eq!(report.current["receiver_scan"]["next_index"], 1);
    assert_eq!(report.current["receiver_scan"]["last_index"], 0);
    assert_eq!(report.current["receiver_scan"]["state"], "interrupted");
    assert!(
        report
            .last_error
            .unwrap()
            .contains("fixture commit failure")
    );
    let old = archive
        .list_observations("commit", Some("commit-r"), 4)
        .await
        .unwrap();
    assert_eq!(old.len(), 1);
    core.close().await.unwrap();
    db.execute_batch("DROP TRIGGER fail_cyclic_point").unwrap();
    drop(db);
    // A fresh manager has no original worker or in-memory scan cursor.
    factory.wrong_rx_at.store(3, Ordering::Release);
    let mut config = CoreConfig::replay(directory.path());
    config.devices.transport = Some(factory.clone());
    let reopened = Core::open(config).await.unwrap();
    let manager = reopened.devices.as_ref().unwrap();
    manager
        .start_stream(
            "commit",
            "commit-r",
            manager.public_gate(Target::new("commit", "commit-r")),
        )
        .await
        .unwrap();
    until(|| factory.drops.load(Ordering::Acquire) == 2).await;
    let report = manager.stop_stream("commit", "commit-r").await.unwrap();
    assert_eq!(report.current["receiver_scan"]["next_pass"], 2);
    assert_eq!(report.current["receiver_scan"]["next_index"], 0);
    let rows = reopened
        .archive()
        .unwrap()
        .list_observations("commit", Some("commit-r"), 5)
        .await
        .unwrap();
    assert_eq!(rows.len(), 3);
    for index in 0..3 {
        assert!(
            rows.iter()
                .any(|r| r["metadata"]["scan_pass"] == 1 && r["metadata"]["scan_index"] == index)
        );
    }
    reopened.close().await.unwrap();
}

#[tokio::test]
async fn cyclic_receiver_segments_a_1001_point_off_raster_grid_and_wraps_on_one_port() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    // Stop at the second pass's second readback, after committing the wrap to start.
    factory.wrong_rx_at.store(1003, Ordering::Release);
    let core = core(directory.path(), factory.clone()).await;
    let archive = core.archive().unwrap();
    let stop = 10_095_010;
    archive
        .create_session(cyclic_session("wide", stop))
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap();
    manager
        .start_stream(
            "wide",
            "wide-r",
            manager.public_gate(Target::new("wide", "wide-r")),
        )
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(60), async {
        while factory.drops.load(Ordering::Acquire) == 0 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let stopped = manager.stop_stream("wide", "wide-r").await.unwrap();
    assert_eq!(stopped.measurements, 1002, "{:?}", stopped.last_error);
    assert_eq!(stopped.current["receiver_scan"]["next_pass"], 2);
    let rows = archive
        .list_observations("wide", Some("wide-r"), 1000)
        .await
        .unwrap();
    for index in [998usize, 999, 1000] {
        let row = rows
            .iter()
            .find(|r| r["metadata"]["scan_pass"] == 1 && r["metadata"]["scan_index"] == index)
            .unwrap();
        assert_eq!(
            row["metadata"]["scan_segment_index"],
            usize::from(index >= 999)
        );
        assert_eq!(
            row["frequency_hz"],
            (100_000 + index as u64 * 10_000).min(stop)
        );
        assert_eq!(row["metadata"]["scan_total"], 1001);
    }
    let commands = factory.commands.lock().unwrap().clone();
    let expected = (0..1001)
        .map(|i| (100_000 + i * 10_000).min(stop))
        .chain([100_000, 110_000])
        .map(|hz| format!("RF{:04}.{:05}", hz / 1_000_000, (hz % 1_000_000) / 10))
        .collect::<Vec<_>>();
    assert_eq!(
        commands
            .iter()
            .filter(|cmd| cmd.starts_with("RF"))
            .collect::<Vec<_>>(),
        expected.iter().collect::<Vec<_>>()
    );
    assert_eq!(
        commands.iter().filter(|cmd| cmd.as_str() == "VFA").count(),
        3
    );
    assert_eq!(
        commands.iter().filter(|cmd| cmd.as_str() == "RX").count(),
        1003
    );
    assert_eq!(factory.opens.load(Ordering::Acquire), 1);
    core.close().await.unwrap();
}

#[tokio::test]
async fn guard_profiles_validate_the_selected_receiver_or_rf_explorer_without_opening_ports() {
    let dir = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let mut config = CoreConfig::live(dir.path());
    config.devices.transport = Some(factory.clone());
    let live = Core::open(config).await.unwrap();
    let manager = live.devices.as_ref().unwrap();
    let receiver = cyclic_session("profiles", 1_300_000_000);
    assert!(
        manager
            .validate_guard_profiles(&parse_session(&receiver).unwrap())
            .is_ok()
    );
    let mut rfe = session("profiles");
    let mut analyzer = rfe["streams"][1].clone();
    analyzer["settings"] =
        json!({"adapter":"rf-explorer-3g-serial","baud":500_000,"survey_step_hz":10000});
    analyzer["port"] = json!("explicit-fixture");
    rfe["streams"] = json!([analyzer]);
    assert!(
        manager
            .validate_guard_profiles(&parse_session(&rfe).unwrap())
            .is_ok()
    );
    rfe["streams"][0]["settings"]["survey_step_hz"] = json!(1001);
    assert!(
        manager
            .validate_guard_profiles(&parse_session(&rfe).unwrap())
            .is_err()
    );
    let mut invalid = receiver.clone();
    invalid["streams"][0]["settings"]["fast_scan"]["cyclic"] = json!(1);
    assert!(
        manager
            .validate_guard_profiles(&parse_session(&invalid).unwrap())
            .is_err()
    );
    invalid["streams"][0]["settings"]["fast_scan"]["cyclic"] = json!(false);
    assert!(
        manager
            .validate_guard_profiles(&parse_session(&invalid).unwrap())
            .is_err()
    );
    assert_eq!(factory.opens.load(Ordering::Acquire), 0);
    live.close().await.unwrap();
}

async fn open_duty_node(path: &std::path::Path, factory: &Arc<Factory>) -> Arc<Core> {
    let mut config = CoreConfig::replay(path);
    config.devices.transport = Some(factory.clone());
    Core::open(config).await.unwrap()
}

async fn receiver_duty_fixture(node: &Core) -> String {
    let mut template = cyclic_session("reference", 115_010);
    template["streams"][0]["port"] = json!("COM22");
    template["streams"][0]["settings"]["period_s"] = json!(10);
    let archive = node.archive().unwrap();
    archive.create_session(template.clone()).await.unwrap();
    let guard = &node.domains.get().unwrap().guard;
    let project = guard
        .create(json!({"name":"Cyclic duty","site":"Fixture","session_id":"reference"}))
        .await
        .unwrap();
    let id = project["id"].as_str().unwrap();
    guard
        .update_settings(
            id,
            json!({"archive":{"minimum_free_bytes":1048576,"minimum_retention_seconds":60}}),
        )
        .await
        .unwrap();
    guard.save_preparation(id, json!({"antenna_notes":"Fixture","receive_path_confirmed":true,"location_confirmed":true})).await.unwrap();
    let archived = archive.get_session("reference").await.unwrap().unwrap();
    #[cfg(feature = "serial")]
    ori_instruments::serial::validate_windows_com_port(
        archived["streams"][0]["port"].as_str().unwrap(),
    )
    .expect("duplex receiver fixture needs a portable native port");
    let key = ori_guard::ardv1_plan_key(&parse_session(&archived).unwrap().streams[0]).unwrap();
    let mut rows = vec![];
    for pass in 1..=20 {
        for (index, hz) in [100_000_u64, 110_000, 115_010].into_iter().enumerate() {
            let time = chrono::DateTime::from_timestamp(1_777_000_000 + 4 * pass + index as i64, 0)
                .unwrap()
                .to_rfc3339();
            rows.push(json!({"id":format!("ref-{pass}-{index}"),"session_id":"reference","stream_id":"reference-r",
                "interval":{"started_at":time,"ended_at":time},"frequency_hz":hz,"mode":"FM","level_unit":"s_meter_count","level":137,
                "metadata":{"adapter":"ardv1-serial","receiver_profile":"ardv1-monitor-v1","receiver_firmware":"2504A",
                    "scan_plan_key":key,"scan_strategy_requested":"auto","scope_source":"sequential_receiver_readback",
                    "scan_pass":pass,"scan_index":index,"scan_total":3,"scan_start_hz":100000,"scan_stop_hz":115010,
                    "scan_step_hz":10000,"scan_segment_index":0,"settle_s":0.0,"frequency_readback":true,"simulated":false,
                    "observed_frequency_hz":hz,"requested_frequency_hz":hz,"observed_mode":"FM","requested_mode":"FM",
                    "frequency_source":"receiver_RX","mode_source":"receiver_RX",
                    "raw_receiver_status":format!("RX VFA RF0000.{:05} ST010.00 MD0F0 LM1371",hz/10)}}));
        }
    }
    assert!(
        ori_archive::verified_receiver_scan_context(&rows[0]).is_some(),
        "{}",
        rows[0]
    );
    archive.append_observations(rows).await.unwrap();
    let progress = archive
        .ardv1_reference_progress("reference", "reference-r")
        .await
        .unwrap();
    let checked = guard.baseline_collection(&project).await.unwrap();
    assert_eq!(checked["ready"], true, "{progress} {checked} {key}");
    guard
        .create_baseline(
            id,
            json!({"name":"Reference","location":"Fixture","confirm":true}),
        )
        .await
        .unwrap();
    id.to_owned()
}

/// Keep the supervisor's monotonic tick frozen and advance civil time at a specific
/// command on the serial fixture. Driver measurement timestamps still come from the driver.
struct CutoffClock(AtomicBool);
impl ori_guard::Clock for CutoffClock {
    fn now(&self) -> ori_contracts::UtcTimestamp {
        if self.0.load(Ordering::Acquire) {
            "2026-11-23T10:59:59.999999Z"
        } else {
            "2026-11-23T11:00:00Z"
        }
        .parse()
        .unwrap()
    }
    fn monotonic(&self) -> Duration {
        Duration::ZERO
    }
}
impl ori_guard::IdSource for CutoffClock {
    fn next_id(&self, _: ori_guard::IdKind) -> String {
        crate::new_id()
    }
}

async fn cutoff_node(
    path: &std::path::Path,
    factory: &Arc<Factory>,
    first: &str,
) -> (Arc<Core>, String, Arc<CutoffClock>) {
    factory.pause_poll.store(true, Ordering::Release);
    *factory.pause_command.lock().unwrap() = first.to_owned();
    let clock = Arc::new(CutoffClock(AtomicBool::new(true)));
    let mut config = CoreConfig::replay(path);
    config.devices.transport = Some(factory.clone());
    config.domain_sources = Some(crate::DomainSources {
        clock: clock.clone(),
        ids: clock.clone(),
    });
    let node = Core::open(config).await.unwrap();
    let id = receiver_duty_fixture(&node).await;
    (node, id, clock)
}

#[tokio::test]
async fn weekly_cutoff_between_tune_and_rx_does_not_start_a_late_measurement() {
    let dir = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let factory = Factory::new(false);
    let (node, id, clock) = cutoff_node(dir.path(), &factory, "RF0000.10000").await;
    let guard = &node.domains.get().unwrap().guard;
    let armed = guard
        .arm_duty(
            &id,
            json!({"schedule":{"mode":"weekly","timezone":"Europe/Moscow","weekdays":[1],
        "start_local":"13:00","end_local":"14:00"}}),
        )
        .await
        .unwrap();
    assert_eq!(armed["status"], "armed");
    until(|| factory.poll_started.load(Ordering::Acquire) == 1).await;
    clock.0.store(false, Ordering::Release);
    factory.poll_release.add_permits(1);
    until(|| {
        factory
            .commands
            .lock()
            .unwrap()
            .iter()
            .any(|c| c == "RX" || c == "EX")
    })
    .await;
    let commands = factory.commands.lock().unwrap().clone();
    assert!(commands.iter().any(|c| c == "RF0000.10000"));
    assert!(
        !commands.iter().any(|c| c == "RX"),
        "RX dispatched after the weekly closing instant: {commands:?}"
    );
    let p = guard.project(&id).await.unwrap();
    let sid = p["active_session_id"].as_str().unwrap();
    let devices = node.devices.as_ref().unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        while devices
            .list(Some(sid))
            .await
            .unwrap()
            .iter()
            .any(|worker| worker.alive)
        {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    // The worker has closed at the deadline, while the periodic duty tick is still frozen.
    guard.tick(&id).await.unwrap();
    assert_eq!(guard.duty(&id).await.unwrap()["status"], "armed");
    guard
        .reconcile_duty(&id, ori_guard::DutyTrigger::Timer)
        .await
        .unwrap();
    let saved = node
        .archive()
        .unwrap()
        .list_observations(sid, None, 2)
        .await
        .unwrap();
    assert!(saved.is_empty());
    assert_eq!(guard.duty(&id).await.unwrap()["status"], "waiting");
    node.close().await.unwrap();
}

#[tokio::test]
async fn until_cutoff_drains_a_dispatched_rx_and_archives_its_original() {
    let dir = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let factory = Factory::new(false);
    let (node, id, clock) = cutoff_node(dir.path(), &factory, "RX").await;
    let guard = node.domains.get().unwrap().guard.clone();
    let armed = guard
        .arm_duty(
            &id,
            json!({"schedule":{"mode":"until","until_at":"2026-11-23T11:00:00Z"}}),
        )
        .await
        .unwrap();
    assert_eq!(armed["status"], "armed");
    until(|| factory.poll_started.load(Ordering::Acquire) == 1).await;
    clock.0.store(false, Ordering::Release);
    let p = guard.project(&id).await.unwrap();
    let sid = p["active_session_id"].as_str().unwrap();
    let stream = p["primary_stream_id"].as_str().unwrap();
    let stop = tokio::spawn({
        let guard = guard.clone();
        let id = id.clone();
        async move {
            guard
                .reconcile_duty(&id, ori_guard::DutyTrigger::Timer)
                .await
        }
    });
    let manager = node.devices.as_ref().unwrap();
    tokio::time::timeout(Duration::from_secs(4), async {
        loop {
            let worker = manager
                .list(Some(sid))
                .await
                .unwrap()
                .into_iter()
                .find(|w| w.stream_id == stream)
                .unwrap();
            if worker.stopping == Some(true) || !worker.alive {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    factory.poll_release.add_permits(1);
    stop.await.unwrap().unwrap();
    assert_eq!(guard.duty(&id).await.unwrap()["status"], "expired");
    let rows = node
        .archive()
        .unwrap()
        .list_observations(sid, Some(stream), 8)
        .await
        .unwrap();
    assert_eq!(
        rows.len(),
        1,
        "An RX sent inside the window must finish and its original must survive the cutoff"
    );
    assert_eq!(rows[0]["metadata"]["scan_index"], 0);
    assert_eq!(
        rows[0]["metadata"]["raw_receiver_status"],
        "RX VFA RF0000.10000 ST010.00 MD0F0 LM1371"
    );
    assert_eq!(
        factory
            .commands
            .lock()
            .unwrap()
            .iter()
            .filter(|c| c.as_str() == "RX")
            .count(),
        1
    );
    node.close().await.unwrap();
}

async fn arm_receiver_duty(node: &Core, id: &str, resume_after_restart: bool) -> Value {
    let guard = &node.domains.get().unwrap().guard;
    let preflight = guard.preflight(id, false).await.unwrap();
    assert_eq!(preflight["ready"], true, "{}", preflight["checks"]);
    let duty = guard
        .arm_duty(
            id,
            json!({"schedule":{"mode":"continuous"},"resume_after_restart":resume_after_restart}),
        )
        .await
        .unwrap();
    assert_eq!(duty["status"], "armed", "{duty}");
    let status = guard.guard_status(id).await.unwrap();
    assert_eq!(status["active"], true, "{status}");
    let project = &status["project"];
    assert_ne!(project["active_session_id"], "reference");
    assert_ne!(project["primary_stream_id"], "reference-r");
    project.clone()
}

async fn next_duty_original(node: &Core, sid: &str, stream: &str, n: usize) -> Vec<Value> {
    tokio::time::timeout(Duration::from_secs(8), async {
        loop {
            let rows = node
                .archive()
                .unwrap()
                .list_observations(sid, Some(stream), 10)
                .await
                .unwrap();
            if rows.len() >= n {
                for original in &rows {
                    assert_eq!(original["session_id"], sid);
                    assert_eq!(original["stream_id"], stream);
                    assert!(ori_archive::verified_receiver_scan_context(original).is_some());
                    assert!(original["metadata"]["receiver_run_unit"].is_string());
                }
                return rows;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("cyclic duty committed its next RX original")
}

#[tokio::test]
async fn opted_in_duty_reopens_its_raw_stream_and_only_fills_the_next_verified_point() {
    let dir = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let factory = Factory::new(false);
    let node = open_duty_node(dir.path(), &factory).await;
    let id = receiver_duty_fixture(&node).await;
    let project = arm_receiver_duty(&node, &id, true).await;
    let sid = project["active_session_id"].as_str().unwrap();
    let stream = project["primary_stream_id"].as_str().unwrap();
    let first = next_duty_original(&node, sid, stream, 1).await.remove(0);
    assert_eq!(first["metadata"]["scan_index"], 0);
    let unit = first["metadata"]["receiver_run_unit"].as_str().unwrap();
    assert_eq!(unit.len(), 64);
    assert!(!first.to_string().contains("SYNTHETIC"));
    node.close().await.unwrap();

    let reopened = open_duty_node(dir.path(), &factory).await;
    let guard = &reopened.domains.get().unwrap().guard;
    let recovered = guard.get_record("project", &id, None).await.unwrap();
    assert_eq!(recovered["active_session_id"], sid, "{recovered}");
    assert_eq!(recovered["primary_stream_id"], stream);
    assert_eq!(recovered["session_ids"], project["session_ids"]);
    let rows = next_duty_original(&reopened, sid, stream, 2).await;
    let after = rows.iter().find(|o| o["id"] != first["id"]).unwrap();
    assert_eq!(after["frequency_hz"], 110_000);
    assert_eq!(after["metadata"]["scan_pass"], 1);
    assert_eq!(after["metadata"]["scan_index"], 1);
    let recovered_pass = reopened
        .archive()
        .unwrap()
        .receiver_panorama(sid, stream, Some(1), 3)
        .await
        .unwrap();
    assert_eq!(recovered_pass["coverage_hex"], "03");
    assert_eq!(recovered_pass["complete"], false);
    assert_eq!(recovered_pass["bins"][0]["observation_id"], first["id"]);

    assert_eq!(after["metadata"]["receiver_run_unit"], unit);
    let journal = reopened.archive().unwrap().events(0, 256).await.unwrap();
    let restart = journal
        .iter()
        .find(|e| e["session_id"] == sid && e["payload"]["recovery"].is_object())
        .unwrap();
    assert_eq!(
        restart["payload"]["recovery"]["last_observation_id"],
        first["id"]
    );
    assert_eq!(
        restart["payload"]["recovery"]["last_observation_ended_at"],
        first["interval"]["ended_at"]
    );

    // Sleep releases the worker, repeats preflight and keeps the same raw pass.
    guard
        .reconcile_duty(&id, ori_guard::DutyTrigger::Wake)
        .await
        .unwrap();
    let woke = next_duty_original(&reopened, sid, stream, 3).await;
    assert!(
        woke.iter()
            .any(|o| o["metadata"]["scan_index"] == 2 && o["frequency_hz"] == 115_010)
    );
    assert_eq!(
        reopened
            .archive()
            .unwrap()
            .receiver_panorama(sid, stream, Some(1), 3)
            .await
            .unwrap()["coverage_hex"],
        "07"
    );

    // An explicit stop followed by a new duty never borrows the previous cursor.
    guard.disarm(&id, json!({})).await.unwrap();
    let fresh = arm_receiver_duty(&reopened, &id, false).await;
    assert_ne!(fresh["active_session_id"], sid);
    let fresh_row = next_duty_original(
        &reopened,
        fresh["active_session_id"].as_str().unwrap(),
        fresh["primary_stream_id"].as_str().unwrap(),
        1,
    )
    .await
    .remove(0);
    assert_eq!(fresh_row["metadata"]["scan_index"], 0);
    assert_ne!(fresh_row["metadata"]["receiver_run_unit"], unit);
    reopened.close().await.unwrap();
    let opens = factory.opens.load(Ordering::Acquire);
    let no_resume = open_duty_node(dir.path(), &factory).await;
    assert_eq!(
        no_resume
            .domains
            .get()
            .unwrap()
            .guard
            .duty(&id)
            .await
            .unwrap()["enabled"],
        false
    );
    assert_eq!(factory.opens.load(Ordering::Acquire), opens);
    no_resume.close().await.unwrap();
}

#[tokio::test]
async fn duty_recovery_rejects_a_different_receiver_on_the_same_port_before_scanning() {
    let dir = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let factory = Factory::new(false);
    let node = open_duty_node(dir.path(), &factory).await;
    let id = receiver_duty_fixture(&node).await;
    let project = arm_receiver_duty(&node, &id, true).await;
    let sid = project["active_session_id"].as_str().unwrap();
    let stream = project["primary_stream_id"].as_str().unwrap();
    let original = next_duty_original(&node, sid, stream, 1).await.remove(0);
    node.close().await.unwrap();
    *factory.receiver_id.lock().unwrap() = "REPLACEMENT".into();
    factory.commands.lock().unwrap().clear();

    let reopened = open_duty_node(dir.path(), &factory).await;
    let guard = &reopened.domains.get().unwrap().guard;
    assert_eq!(guard.duty(&id).await.unwrap()["status"], "preflight_failed");
    assert!(!guard.any_armed());
    let commands = factory.commands.lock().unwrap().clone();
    assert!(commands.iter().any(|command| command == "RN"));
    assert!(
        !commands
            .iter()
            .any(|command| command.starts_with("RF") || command == "RX")
    );
    let rows = reopened
        .archive()
        .unwrap()
        .list_observations(sid, Some(stream), 8)
        .await
        .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["id"], original["id"]);
    reopened.close().await.unwrap();
}

#[tokio::test]
async fn duty_recovery_refuses_an_archive_run_reassigned_to_a_different_project() {
    let dir = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let factory = Factory::new(false);
    let node = open_duty_node(dir.path(), &factory).await;
    let id = receiver_duty_fixture(&node).await;
    let project = arm_receiver_duty(&node, &id, true).await;
    let sid = project["active_session_id"].as_str().unwrap();
    let stream = project["primary_stream_id"].as_str().unwrap();
    next_duty_original(&node, sid, stream, 1).await;
    node.close().await.unwrap();

    let archive = ori_archive::Archive::open(ori_archive::ArchiveConfig::new(
        dir.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    let mut changed = archive.get_session(sid).await.unwrap().unwrap();
    changed["metadata"]["monitoring_project_id"] = json!("different-owner");
    archive.save_session(changed, None).await.unwrap();
    archive.close().await.unwrap();
    let opens = factory.opens.load(Ordering::Acquire);

    let reopened = open_duty_node(dir.path(), &factory).await;
    let guard = &reopened.domains.get().unwrap().guard;
    assert_eq!(guard.duty(&id).await.unwrap()["status"], "preflight_failed");
    assert!(!guard.any_armed());
    assert_eq!(factory.opens.load(Ordering::Acquire), opens);
    reopened.close().await.unwrap();
}
