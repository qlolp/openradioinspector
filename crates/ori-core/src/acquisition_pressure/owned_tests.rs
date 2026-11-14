//! Real actor byte streams, raw append acknowledgements and retained work grants.
use super::*;
use crate::devices::{
    BoxTransport, DeviceFuture, DeviceManager, DeviceOptions, DeviceResult, Target,
    TransportFactory,
};
use ori_archive::{Archive, ArchiveConfig};
use ori_contracts::{DeviceRole, DeviceStream};
use ori_runtime::{
    Error as RuntimeError,
    qos::{BackgroundWorkGuard, QosError},
    worker::{Cancellation, ResearchPool},
};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    sync::Semaphore,
};

#[derive(Clone, Debug)]
struct ObservedEpoch {
    epoch: u64,
    cycles: u64,
    samples: u32,
    pressured: bool,
}
#[derive(Default)]
pub(super) struct ObserverProbe {
    hold: Latch,
    epochs: Mutex<BTreeMap<String, ObservedEpoch>>,
    origin: OnceLock<Instant>,
    decisions: Mutex<VecDeque<Value>>,
}
impl ObserverProbe {
    fn relative_us(&self, at: Instant) -> i64 {
        let origin = *self.origin.get_or_init(Instant::now);
        if at >= origin {
            elapsed(at, origin).as_micros().min(i64::MAX as u128) as i64
        } else {
            -(elapsed(origin, at).as_micros().min(i64::MAX as u128) as i64)
        }
    }
    pub(super) fn record_decision(
        &self,
        rows: &[AcquisitionHealth],
        policy: &Policy,
        decision: &Decision,
        now: Instant,
    ) {
        self.origin.get_or_init(|| now);
        let rows_total = rows.len();
        let rows = rows
            .iter()
            .take(8)
            .filter_map(|row| {
                policy.trackers.get(&row.worker_instance).map(|tracker| {
                    let mut record = if row.active && !row.intentionally_paused {
                        tracker.last_decision.clone().unwrap_or_else(|| json!({}))
                    } else {
                        json!({"inactive": true})
                    };
                    let object = record.as_object_mut().unwrap();
                    object.extend(
                        json!({"worker": row.worker_instance, "epoch": tracker.epoch,
                            "cycles": row.completed_cycles, "active": row.active,
                            "paused": row.intentionally_paused,
                            "configuration_us": self.relative_us(row.configuration_changed_at)
                        })
                        .as_object()
                        .unwrap()
                        .clone(),
                    );
                    record
                })
            })
            .collect::<Vec<_>>();
        let mut entries = self.decisions.lock().unwrap();
        entries.push_back(
            json!({"at_us": self.relative_us(now), "pressure": decision.pressure,
            "active": decision.active, "calibrating": decision.calibrating,
            "delayed": decision.delayed, "rows_total": rows_total, "rows": rows}),
        );
        while entries.len() > 8 {
            entries.pop_front();
        }
    }
    fn command_evidence(&self, times: &CommandTimes) -> Value {
        json!(
            times
                .lock()
                .unwrap()
                .iter()
                .map(|(at, phase)| { json!({"at_us": self.relative_us(*at), "phase": phase}) })
                .collect::<Vec<_>>()
        )
    }
    pub(super) async fn pause(&self) {
        self.hold.pause().await;
    }
    pub(super) fn record(&self, rows: &[AcquisitionHealth], policy: &Policy) {
        *self.epochs.lock().unwrap() = rows
            .iter()
            .filter_map(|row| {
                policy.trackers.get(&row.worker_instance).map(|tracker| {
                    (
                        row.worker_instance.clone(),
                        ObservedEpoch {
                            epoch: tracker.epoch,
                            cycles: tracker.last_cycles,
                            samples: tracker.samples,
                            pressured: tracker.pressured,
                        },
                    )
                })
            })
            .collect();
    }
}

struct Latch {
    held: AtomicBool,
    entered: AtomicU64,
    release: Semaphore,
    delay_ms: AtomicU64,
}
impl Default for Latch {
    fn default() -> Self {
        Self {
            held: AtomicBool::new(false),
            entered: AtomicU64::new(0),
            release: Semaphore::new(0),
            delay_ms: AtomicU64::new(0),
        }
    }
}
impl Latch {
    async fn pause(&self) -> bool {
        if self.held.load(Ordering::Acquire) {
            self.entered.fetch_add(1, Ordering::AcqRel);
            match self.release.acquire().await {
                Ok(permit) => permit.forget(),
                Err(_) => return false,
            }
        }
        let delay = self.delay_ms.load(Ordering::Acquire);
        if delay != 0 {
            tokio::time::sleep(Duration::from_millis(delay)).await;
        }
        true
    }
    fn resume(&self) {
        self.held.store(false, Ordering::Release);
        self.release.add_permits(1);
    }
}
#[derive(Default)]
struct Factory {
    analyzer: Arc<Latch>,
    receiver: Arc<Latch>,
    peers: Mutex<Vec<tokio::task::JoinHandle<()>>>,
    rbw_delay_ms: Arc<AtomicU64>,
    rbw_times: CommandTimes,
}
type CommandTimes = Arc<Mutex<VecDeque<(Instant, &'static str)>>>;
fn command_time(times: &CommandTimes, phase: &'static str) {
    let mut events = times.lock().unwrap();
    events.push_back((Instant::now(), phase));
    while events.len() > 8 {
        events.pop_front();
    }
}
impl TransportFactory for Factory {
    fn identity(&self, stream: &DeviceStream) -> DeviceResult<String> {
        Ok(format!("owned-pressure-fixture-{}", stream.id))
    }
    fn open<'a>(&'a self, stream: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        Box::pin(async move {
            let analyzer = stream.role == DeviceRole::SpectrumAnalyzer;
            let latch = if analyzer {
                &self.analyzer
            } else {
                &self.receiver
            }
            .clone();
            let rbw_delay = self.rbw_delay_ms.clone();
            let rbw_times = self.rbw_times.clone();
            let (host, mut peer) = tokio::io::duplex(4096);
            let task = tokio::spawn(async move {
                let mut command = Vec::new();
                while let Ok(byte) = peer.read_u8().await {
                    if byte != b'\r' {
                        command.push(byte);
                        continue;
                    }
                    let command = String::from_utf8(std::mem::take(&mut command)).unwrap();
                    if analyzer && command == "rbw 30" {
                        command_time(&rbw_times, "rbw_command_received");
                        // The synthetic instrument's natural scan duration
                        // changes with this actual acknowledged RBW command.
                        latch
                            .delay_ms
                            .store(rbw_delay.load(Ordering::Acquire), Ordering::Release);
                    }
                    let reply = if analyzer {
                        let body = match command.as_str() {
                            "info" => "tinySA Ultra ZS405\r\n".to_owned(),
                            "version" => format!("{}\r\n", ori_instruments::FIRMWARE),
                            "deviceid" => "SYNTHETIC-PRESSURE-ONLY\r\n".into(),
                            "help" => "trace output pause mode caloutput release rbw sweep info version deviceid help\r\n".into(),
                            command if command.starts_with("scan ") => {
                                if !latch.pause().await {
                                    break;
                                }
                                "100000 -0.0\r\n100010 1.0000000000000002\r\n100020 -1e-300\r\n".into()
                            }
                            _ => String::new(),
                        };
                        format!("{command}\r\n{body}ch> ")
                    } else {
                        match command.as_str() {
                            "VR" => "20VR2504A\r\n",
                            "RN" => "20RNSYNTHETIC\r\n",
                            "LM" => {
                                if !latch.pause().await {
                                    break;
                                }
                                "20LM1371\r\n"
                            }
                            _ => "20\r\n",
                        }
                        .to_owned()
                    };
                    if peer.write_all(reply.as_bytes()).await.is_err() {
                        break;
                    }
                    if analyzer && command == "rbw 30" {
                        command_time(&rbw_times, "rbw_prompt_written");
                    }
                }
            });
            self.peers.lock().unwrap().push(task);
            Ok(Box::new(host) as BoxTransport)
        })
    }
}
impl Factory {
    async fn join(&self) {
        self.analyzer.release.close();
        self.receiver.release.close();
        let tasks = std::mem::take(&mut *self.peers.lock().unwrap());
        for task in tasks {
            tokio::time::timeout(Duration::from_secs(3), task)
                .await
                .expect("owned instrument peer did not JOIN")
                .unwrap();
        }
    }
}
impl Drop for Factory {
    fn drop(&mut self) {
        self.analyzer.release.close();
        self.receiver.release.close();
    }
}
fn fixture() -> Value {
    json!({"id":"pressure-fixture","name":"Synthetic acquisition pressure","state":"running",
        "created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z",
        "ended_at":null,"contract_version":1,"metadata":{},"streams":[
        {"id":"a","session_id":"pressure-fixture","device":"Synthetic analyzer","role":"spectrum_analyzer",
         "state":"disconnected","level_unit":"dBm","port":"synthetic-analyzer",
         "settings":{"adapter":"tinysa-serial","start_hz":100000,"stop_hz":100020,"points":3,"rbw_hz":10000,"period_s":0.005}},
        {"id":"r","session_id":"pressure-fixture","device":"Synthetic receiver","role":"receiver",
         "state":"disconnected","level_unit":"s_meter_count","port":"synthetic-receiver",
         "settings":{"adapter":"ardv1-serial","frequency_hz":145500000,"period_s":0.005}}
    ]})
}
fn fast_options() -> AcquisitionPressureOptions {
    AcquisitionPressureOptions {
        sample_interval: Duration::from_millis(10),
        initial_grace: Duration::from_secs(1),
        lag_floor: Duration::from_millis(150),
        lag_slack: Duration::from_millis(25),
        recovery_dwell: Duration::from_millis(100),
    }
}
async fn manager(root: &std::path::Path) -> (Archive, Arc<DeviceManager>, Arc<Factory>) {
    let archive = Archive::open(ArchiveConfig::new(root.join("source.sqlite"))).unwrap();
    archive.create_session(fixture()).await.unwrap();
    let factory = Arc::new(Factory::default());
    let mut options = DeviceOptions {
        transport: Some(factory.clone()),
        ..DeviceOptions::default()
    };
    options.io_limits.scan_timeout = Duration::from_secs(5);
    options.io_limits.receiver_timeout = Duration::from_secs(5);
    let manager = DeviceManager::new(archive.clone(), options);
    (archive, manager, factory)
}
async fn start(manager: &Arc<DeviceManager>, id: &str) {
    manager
        .start_stream(
            "pressure-fixture",
            id,
            manager.public_gate(Target::new("pressure-fixture", id)),
        )
        .await
        .unwrap();
}
// Only fixture admission may wait for an actually quiet observer. Once a
// grant exists it is retained unchanged through the complete RBW scenario.
async fn quiet_fixture_grant(
    qos: &BackgroundQos,
    cancellation: Cancellation,
) -> std::result::Result<BackgroundWorkGuard, QosError> {
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            match qos.register(cancellation.clone()) {
                Err(QosError::Pressure) => tokio::time::sleep(Duration::from_millis(2)).await,
                result => return result,
            }
        }
    })
    .await
    .unwrap_or(Err(QosError::Pressure))
}

#[tokio::test]
async fn quiet_fixture_waits_for_admission_without_replacing_an_original_grant() {
    let qos = BackgroundQos::default();
    qos.pressure(true).await.unwrap();
    let original = Cancellation::default();
    let owned_qos = qos.clone();
    let owned_cancel = original.clone();
    let mut waiter =
        tokio::spawn(async move { quiet_fixture_grant(&owned_qos, owned_cancel).await });
    let early = tokio::time::timeout(Duration::from_millis(20), &mut waiter).await;
    let waited = early.is_err();
    qos.pressure(false).await.unwrap();
    let admitted = match early {
        Ok(joined) => joined.unwrap(),
        Err(_) => waiter.await.unwrap(),
    };
    let kept = admitted.is_ok() && qos.status().registered_work == 1 && !original.is_cancelled();
    drop(admitted);
    assert!(
        waited && kept,
        "fixture must await its initial quiet grant, not fail on pre-existing pressure"
    );
    assert_eq!(qos.status().registered_work, 0);
}

async fn until(mut condition: impl FnMut() -> bool) -> bool {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !condition() {
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .is_ok()
}
fn consumed(observer: &AcquisitionPressure, row: &AcquisitionHealth, cycles: u64) -> bool {
    observer
        .probe
        .epochs
        .lock()
        .unwrap()
        .get(&row.worker_instance)
        .is_some_and(|seen| {
            seen.epoch == row.configuration_epoch && seen.cycles >= cycles && seen.samples >= 3
        })
}
async fn calibrated(manager: &DeviceManager, observer: &AcquisitionPressure) {
    assert!(
        until(|| manager
            .acquisition_health()
            .unwrap()
            .iter()
            .any(|health| health.completed_cycles >= 4 && consumed(observer, health, 4)))
        .await,
        "actual analyzer calibration captures were not consumed by its observer"
    );
}

fn pressure_evidence(
    manager: &DeviceManager,
    observer: &AcquisitionPressure,
    qos: &BackgroundQos,
    cancellation: &Cancellation,
) -> Value {
    let rows = manager.acquisition_health().unwrap().into_iter().map(|row| json!({
        "worker":row.worker_instance,"epoch":row.configuration_epoch,"cycles":row.completed_cycles,
        "active":row.active,"paused":row.intentionally_paused,
        "poll_us":row.last_poll_duration.map(|d|d.as_micros()),
        "commit_us":row.last_commit_duration.map(|d|d.as_micros()),
        "poll_in_flight_us":row.poll_started_at.map(|at|at.elapsed().as_micros()),
        "commit_in_flight_us":row.commit_started_at.map(|at|at.elapsed().as_micros())
    })).collect::<Vec<_>>();
    let seen = observer
        .probe
        .epochs
        .lock()
        .unwrap()
        .iter()
        .map(|(worker, seen)| {
            json!({
                "worker":worker,"epoch":seen.epoch,"cycles":seen.cycles,
                "samples":seen.samples,"pressured":seen.pressured
            })
        })
        .collect::<Vec<_>>();
    let state = observer.status();
    let budget = qos.status();
    let decisions = observer.probe.decisions.lock().unwrap().clone();
    json!({"rows":rows,"observed":seen,"decisions_before_drain":decisions,
        "observer_active":state.active_analyzers,
        "calibrating":state.calibrating_analyzers,"delayed":state.delayed_analyzers,
        "observer_pressure":state.acquisition_pressure,"qos_pressure":budget.acquisition_pressure,
        "registered_work":budget.registered_work,"cancelled":cancellation.is_cancelled()})
}

#[tokio::test]
async fn committed_cycles_do_not_prove_that_the_observer_consumed_calibration() {
    let root = tempfile::tempdir().unwrap();
    let (archive, manager, factory) = manager(root.path()).await;
    let observer =
        AcquisitionPressure::start(manager.clone(), BackgroundQos::default(), fast_options())
            .unwrap();
    observer.probe.hold.held.store(true, Ordering::Release);
    start(&manager, "a").await;
    let held = until(|| observer.probe.hold.entered.load(Ordering::Acquire) > 0).await;
    let committed = until(|| manager.acquisition_health().unwrap()[0].completed_cycles >= 4).await;
    let row = manager.acquisition_health().unwrap().remove(0);
    let before = observer
        .probe
        .epochs
        .lock()
        .unwrap()
        .get(&row.worker_instance)
        .cloned();
    let mut calibration = Box::pin(calibrated(&manager, &observer));
    // Poll exactly once: no sleep can turn a counter into an observer receipt.
    let returned_without_observation = if committed {
        std::future::poll_fn(|cx| {
            std::task::Poll::Ready(std::future::Future::poll(calibration.as_mut(), cx).is_ready())
        })
        .await
    } else {
        false
    };
    drop(calibration);
    observer.probe.hold.resume();
    let consumed = until(|| {
        observer
            .probe
            .epochs
            .lock()
            .unwrap()
            .get(&row.worker_instance)
            .is_some_and(|value| value.samples >= 3)
    })
    .await;
    let after = observer
        .probe
        .epochs
        .lock()
        .unwrap()
        .get(&row.worker_instance)
        .cloned();
    observer.close().await.unwrap();
    manager.close().await.unwrap();
    factory.join().await;
    archive.close().await.unwrap();
    assert!(
        held && committed && before.is_none() && consumed,
        "controlled fixture must hold observation while real raw commits progress: held={held}, committed={committed}, before={before:?}, after={after:?}"
    );
    assert!(
        !returned_without_observation,
        "calibrated() accepted committed cycles without an observer epoch receipt: row={row:?}, before={before:?}, after={after:?}"
    );
}

async fn instrument_call(
    core: &Arc<crate::Core>,
    method: axum::http::Method,
    tail: &str,
    payload: Option<Value>,
) -> Value {
    let path = format!("/api/v1/sessions/pressure-fixture/streams/a/{tail}");
    let parts = path
        .trim_start_matches('/')
        .split('/')
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let mut headers = axum::http::HeaderMap::new();
    headers.insert(
        "X-Instrument-Client",
        "pressure-epoch-client-0001".parse().unwrap(),
    );
    let auth = core
        .security
        .check(
            &method,
            &path,
            &headers,
            crate::security::ListenerRole::Plain,
        )
        .await
        .unwrap()
        .receipt();
    let response = crate::instrument_control::dispatch(
        core,
        &method,
        &parts,
        &BTreeMap::new(),
        &headers,
        auth,
        payload,
    )
    .await
    .unwrap()
    .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 2 * 1024 * 1024)
        .await
        .unwrap();
    let value: Value = ori_contracts::parse_json_value(&bytes, 2 * 1024 * 1024).unwrap();
    assert!(
        status.is_success(),
        "actual instrument dispatch failed: {status} {value}"
    );
    value["data"].clone()
}
async fn completed_setting(core: &Arc<crate::Core>, id: &str) -> Value {
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let row = instrument_call(
                core,
                axum::http::Method::GET,
                &format!("instrument/operations/{id}"),
                None,
            )
            .await;
            if matches!(
                row["state"].as_str(),
                Some("succeeded" | "failed" | "cancelled")
            ) {
                return row;
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .unwrap()
}

#[tokio::test]
async fn latched_actual_analyzer_cancels_original_work_grant_and_fresh_retry_follows_recovery() {
    let root = tempfile::tempdir().unwrap();
    let (archive, manager, factory) = manager(root.path()).await;
    let qos = BackgroundQos::default();
    let observer =
        AcquisitionPressure::start(manager.clone(), qos.clone(), fast_options()).unwrap();
    start(&manager, "a").await;
    calibrated(&manager, &observer).await;
    let cancellation = Cancellation::default();
    let pool = ResearchPool::default();
    let permit = pool.try_reserve().unwrap();
    let grant = qos.register(cancellation.clone()).unwrap();
    let (release, held) = std::sync::mpsc::channel();
    let (entered, ready) = tokio::sync::oneshot::channel();
    let token = cancellation.clone();
    let work = tokio::spawn(async move {
        let observed = tokio::task::spawn_blocking(move || {
            entered.send(()).unwrap();
            held.recv().unwrap();
            token.is_cancelled()
        })
        .await
        .unwrap();
        drop(grant); // Actual preparation thread joined before either grant is released.
        drop(permit);
        observed
    });
    ready.await.unwrap();
    factory.analyzer.held.store(true, Ordering::Release);
    let entered_poll = until(|| factory.analyzer.entered.load(Ordering::Acquire) > 0).await;
    let cancelled = until(|| cancellation.is_cancelled()).await;
    let retained = !work.is_finished()
        && qos.status().registered_work == 1
        && matches!(pool.try_reserve(), Err(RuntimeError::Busy));
    let denied = matches!(
        qos.register(Cancellation::default()),
        Err(QosError::Pressure)
    );
    release.send(()).unwrap();
    let joined_cancelled = work.await.unwrap();
    let drained_but_paused = pool.try_reserve().is_ok()
        && matches!(
            qos.register(Cancellation::default()),
            Err(QosError::Pressure)
        );
    factory.analyzer.resume();
    let recovered = until(|| !qos.status().acquisition_pressure).await;
    let retried = qos.register(Cancellation::default()).is_ok() && pool.try_reserve().is_ok();
    observer.close().await.unwrap();
    manager.close().await.unwrap();
    factory.join().await;
    let sweeps = archive
        .list_sweeps("pressure-fixture", Some("a"), 1000)
        .await
        .unwrap();
    for sweep in &sweeps {
        assert_eq!(sweep["frequencies_hz"], json!([100000, 100010, 100020]));
        assert_eq!(sweep["level_unit"], "dBm");
        for (actual, expected) in
            sweep["levels"]
                .as_array()
                .unwrap()
                .iter()
                .zip([-0.0_f64, 1.0000000000000002, -1e-300])
        {
            assert_eq!(actual.as_f64().unwrap().to_bits(), expected.to_bits());
        }
    }
    archive.close().await.unwrap();
    assert!(
        entered_poll && cancelled,
        "actual analyzer lag did not cancel original research grant"
    );
    assert!(
        retained && denied,
        "pressure must retain owned preparation through JOIN and deny new admission"
    );
    assert!(
        joined_cancelled && drained_but_paused && recovered && retried,
        "after real JOIN the heavy slot is free, but only a fresh retry after recovery may run"
    );
    assert!(!sweeps.is_empty());
}

#[tokio::test]
async fn independently_latched_receiver_does_not_pause_healthy_analyzer_or_research() {
    let root = tempfile::tempdir().unwrap();
    let (archive, manager, factory) = manager(root.path()).await;
    let qos = BackgroundQos::default();
    let observer =
        AcquisitionPressure::start(manager.clone(), qos.clone(), fast_options()).unwrap();
    start(&manager, "a").await;
    calibrated(&manager, &observer).await;
    factory.receiver.held.store(true, Ordering::Release);
    start(&manager, "r").await;
    let receiver_waiting = until(|| factory.receiver.entered.load(Ordering::Acquire) > 0).await;
    let before = manager.acquisition_health().unwrap()[0].completed_cycles;
    let analyzer_progressed = until(|| {
        let row = manager.acquisition_health().unwrap().remove(0);
        row.completed_cycles >= before + 12 && consumed(&observer, &row, before + 12)
    })
    .await;
    let cancellation = Cancellation::default();
    let allowed = qos.register(cancellation.clone()).is_ok();
    let isolated = manager.acquisition_health().unwrap().len() == 1
        && !qos.status().acquisition_pressure
        && !cancellation.is_cancelled();
    let evidence = pressure_evidence(&manager, &observer, &qos, &cancellation);
    factory.receiver.resume();
    observer.close().await.unwrap();
    manager.close().await.unwrap();
    factory.join().await;
    archive.close().await.unwrap();
    assert!(
        receiver_waiting && analyzer_progressed && isolated && allowed,
        "receiver_waiting={receiver_waiting}, analyzer_progressed={analyzer_progressed}, isolated={isolated}, allowed={allowed}; captured_before_cleanup={evidence}"
    );
}

#[tokio::test]
async fn no_capture_preserves_independent_pressure_and_join_prevents_a_late_clear() {
    let root = tempfile::tempdir().unwrap();
    let (archive, manager, factory) = manager(root.path()).await;
    let qos = BackgroundQos::default();
    let observer =
        AcquisitionPressure::start(manager.clone(), qos.clone(), fast_options()).unwrap();
    let cancellation = Cancellation::default();
    let grant = qos.register(cancellation.clone()).unwrap();
    tokio::time::sleep(Duration::from_millis(50)).await;
    let idle = manager.acquisition_health().unwrap().is_empty()
        && !qos.status().acquisition_pressure
        && !cancellation.is_cancelled();
    drop(grant);
    qos.pressure(true).await.unwrap();
    tokio::time::sleep(Duration::from_millis(40)).await;
    let external_pressure = qos.status().acquisition_pressure
        && matches!(
            qos.register(Cancellation::default()),
            Err(QosError::Pressure)
        );
    qos.pressure(false).await.unwrap();
    tokio::time::sleep(Duration::from_millis(30)).await;
    let external_clear = !qos.status().acquisition_pressure;
    observer.close().await.unwrap();
    qos.pressure(true).await.unwrap();
    tokio::time::sleep(Duration::from_millis(30)).await;
    let stayed_closed = qos.status().acquisition_pressure && !observer.status().observer_running;
    manager.close().await.unwrap();
    factory.join().await;
    archive.close().await.unwrap();
    assert!(idle && external_pressure && external_clear && stayed_closed);
}

#[tokio::test]
async fn dropping_close_waiter_keeps_actual_observer_join_owned_through_pressure_drain() {
    let root = tempfile::tempdir().unwrap();
    let (archive, manager, factory) = manager(root.path()).await;
    let qos = BackgroundQos::default();
    let observer =
        AcquisitionPressure::start(manager.clone(), qos.clone(), fast_options()).unwrap();
    start(&manager, "a").await;
    calibrated(&manager, &observer).await;
    let cancellation = Cancellation::default();
    let grant = qos.register(cancellation.clone()).unwrap();
    factory.analyzer.held.store(true, Ordering::Release);
    let cancelled = until(|| cancellation.is_cancelled()).await;
    let completion = observer.begin_shutdown();
    let held = observer.clone();
    let waiter = tokio::spawn(async move { held.close().await });
    tokio::task::yield_now().await;
    let pending = !waiter.is_finished() && completion.borrow().is_none();
    waiter.abort();
    let _ = waiter.await;
    let owned = observer.status().observer_running && qos.status().registered_work == 1;
    drop(grant);
    observer.close().await.unwrap();
    let stopped = !observer.status().observer_running && qos.status().acquisition_pressure;
    factory.analyzer.resume();
    manager.close().await.unwrap();
    factory.join().await;
    archive.close().await.unwrap();
    assert!(cancelled && pending && owned && stopped);
}

#[tokio::test]
async fn pending_pressure_decision_is_retained_before_original_grant_drain() {
    let root = tempfile::tempdir().unwrap();
    let (archive, manager, factory) = manager(root.path()).await;
    let qos = BackgroundQos::default();
    let observer =
        AcquisitionPressure::start(manager.clone(), qos.clone(), fast_options()).unwrap();
    start(&manager, "a").await;
    calibrated(&manager, &observer).await;
    let cancellation = Cancellation::default();
    let grant = qos.register(cancellation.clone()).unwrap();
    factory.analyzer.held.store(true, Ordering::Release);
    let cancelled = until(|| cancellation.is_cancelled()).await;
    let decisions = observer.probe.decisions.lock().unwrap().clone();
    let completed = observer
        .probe
        .epochs
        .lock()
        .unwrap()
        .values()
        .cloned()
        .collect::<Vec<_>>();
    let draining = observer.status().observer_running && qos.status().registered_work == 1;
    drop(grant);
    factory.analyzer.resume();
    observer.close().await.unwrap();
    manager.close().await.unwrap();
    factory.join().await;
    archive.close().await.unwrap();
    assert!(cancelled && draining);
    assert!(decisions.len() <= 8);
    let last = decisions.back().expect("pre-drain decision missing");
    assert!(last["pressure"].as_bool().unwrap());
    assert_eq!(last["rows"][0]["bad_samples"], 2);
    assert!(
        last["rows"][0]["poll_age_us"].as_u64().unwrap()
            > last["rows"][0]["budget_us"].as_u64().unwrap()
    );
    assert!(
        completed.iter().all(|row| !row.pressured),
        "completed receipts must not falsely claim the blocked decision completed"
    );
}

#[tokio::test]
async fn actual_same_worker_rbw_ack_starts_new_cadence_calibration_without_learning_overload() {
    let root = tempfile::tempdir().unwrap();
    let factory = Arc::new(Factory::default());
    let mut config = crate::CoreConfig::replay(root.path());
    config.acquisition_pressure = fast_options();
    config.devices.transport = Some(factory.clone());
    let core = crate::Core::open(config).await.unwrap();
    let mut source = fixture();
    source["streams"][0]["settings"]["monitoring_instrument_context"] = json!("inspect");
    core.archive()
        .unwrap()
        .create_session(source)
        .await
        .unwrap();
    instrument_call(&core, axum::http::Method::POST, "start", Some(json!({}))).await;
    let manager = core.devices.as_ref().unwrap();
    let observer = core.acquisition_pressure.get().unwrap();
    calibrated(manager, observer).await;
    let before = manager.acquisition_health().unwrap().remove(0);
    assert!(until(|| before.started_at.elapsed() > Duration::from_millis(1100)).await);
    let qos = &core.background.as_ref().unwrap().qos;
    let cancellation = Cancellation::default();
    let grant = match quiet_fixture_grant(qos, cancellation.clone()).await {
        Ok(grant) => grant,
        Err(error) => {
            let evidence = pressure_evidence(manager, observer, qos, &cancellation);
            core.close().await.unwrap();
            factory.join().await;
            panic!("RBW fixture never reached quiet initial admission: {error}; {evidence}");
        }
    };
    factory.rbw_delay_ms.store(300, Ordering::Release);
    let submitted = instrument_call(
        &core,
        axum::http::Method::POST,
        "instrument/operations",
        Some(json!({"operation":"setting","parameters":{"name":"rbw_hz","value":30000}})),
    )
    .await;
    let terminal = completed_setting(&core, submitted["operation_id"].as_str().unwrap()).await;
    let changed = manager.acquisition_health().unwrap().remove(0);
    let progressed = until(|| {
        let row = manager.acquisition_health().unwrap().remove(0);
        row.completed_cycles >= changed.completed_cycles + 4
            && consumed(observer, &changed, changed.completed_cycles + 4)
    })
    .await;
    let status = observer.status();
    let kept_original_grant = !cancellation.is_cancelled() && !qos.status().acquisition_pressure;
    let mut evidence = pressure_evidence(manager, observer, qos, &cancellation);
    evidence["rbw_times"] = observer.probe.command_evidence(&factory.rbw_times);
    drop(grant);
    core.close().await.unwrap();
    factory.join().await;
    assert_eq!(
        terminal["state"], "succeeded",
        "actual acknowledged settings operation: {terminal}"
    );
    assert_eq!(
        changed.worker_instance, before.worker_instance,
        "this must be a same-worker change"
    );
    assert!(changed.configuration_epoch > before.configuration_epoch);
    assert!(changed.configuration_changed_at > before.configuration_changed_at);
    assert!(
        progressed && kept_original_grant && status.calibrating_analyzers == 0,
        "confirmed new cadence must calibrate, while overload alone must not create an epoch: progressed={progressed}, kept_original_grant={kept_original_grant}, calibrating={}; captured_before_cleanup={evidence}",
        status.calibrating_analyzers
    );
}

#[tokio::test]
async fn invalid_startup_timing_is_rejected_before_creating_a_canonical_archive() {
    let root = tempfile::tempdir().unwrap();
    let mut config = crate::CoreConfig::replay(root.path());
    config.acquisition_pressure.sample_interval = Duration::ZERO;
    let database = config.database.clone();
    let result = crate::Core::open(config).await;
    let rejected = matches!(&result, Err(crate::CoreError::Invalid(_)));
    let created = database.exists();
    if let Ok(core) = result {
        core.close().await.unwrap();
    }
    assert!(
        rejected && !created,
        "invalid observer timing opened a canonical writer"
    );
}
