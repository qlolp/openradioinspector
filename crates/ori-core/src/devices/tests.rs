//! Actual owned byte streams, archive commits and coordinated task races.
#[path = "acquisition_tests.rs"]
mod acquisition_health;
use super::*;
use crate::{Core, CoreConfig};
use std::{
    io,
    pin::Pin,
    sync::atomic::AtomicUsize,
    task::{Context, Poll},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, DuplexStream, ReadBuf},
    sync::Semaphore,
};

struct RevocationPoint {
    command: String,
    permission: Arc<AtomicBool>,
}
struct Factory {
    reject_firmware_once: AtomicBool,
    reject_exit_once: AtomicBool,
    wrong_rx_at: AtomicUsize,
    opens: AtomicUsize,
    drops: Arc<AtomicUsize>,
    commands: Arc<Mutex<Vec<String>>>,
    pause_open: AtomicBool,
    open_release: Semaphore,
    pause_poll: Arc<AtomicBool>,
    poll_started: Arc<AtomicUsize>,
    poll_release: Arc<Semaphore>,
    shutdown_failure: bool,
    revoke_after: Arc<Mutex<Option<RevocationPoint>>>,
}
impl Factory {
    fn new(shutdown_failure: bool) -> Arc<Self> {
        Arc::new(Self {
            reject_firmware_once: AtomicBool::new(false),
            reject_exit_once: AtomicBool::new(false),
            wrong_rx_at: AtomicUsize::new(0),
            opens: AtomicUsize::new(0),
            drops: Arc::default(),
            commands: Arc::default(),
            pause_open: AtomicBool::new(false),
            open_release: Semaphore::new(0),
            pause_poll: Arc::default(),
            poll_started: Arc::default(),
            poll_release: Arc::new(Semaphore::new(0)),
            shutdown_failure,
            revoke_after: Arc::default(),
        })
    }
}
struct Tracked {
    inner: DuplexStream,
    drops: Arc<AtomicUsize>,
    shutdown_failure: bool,
}
impl Drop for Tracked {
    fn drop(&mut self) {
        self.drops.fetch_add(1, Ordering::AcqRel);
    }
}
impl AsyncRead for Tracked {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_read(cx, buffer)
    }
}
impl AsyncWrite for Tracked {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.inner).poll_write(cx, bytes)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        if self.shutdown_failure {
            Poll::Ready(Err(io::Error::other("owned synthetic cleanup failure")))
        } else {
            Pin::new(&mut self.inner).poll_shutdown(cx)
        }
    }
}
impl TransportFactory for Factory {
    fn identity(&self, _: &DeviceStream) -> DeviceResult<String> {
        Ok("synthetic-shared-receiver-port".into())
    }
    fn open<'a>(&'a self, stream: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        Box::pin(async move {
            self.opens.fetch_add(1, Ordering::AcqRel);
            if self.pause_open.load(Ordering::Acquire) {
                self.open_release
                    .acquire()
                    .await
                    .map_err(|_| DeviceError::Closed)?
                    .forget();
            }
            let (host, mut device) = tokio::io::duplex(256);
            let commands = self.commands.clone();
            let pause = self.pause_poll.clone();
            let started = self.poll_started.clone();
            let release = self.poll_release.clone();
            let analyzer = stream.role == DeviceRole::SpectrumAnalyzer;
            let wrong_rx_at = self.wrong_rx_at.load(Ordering::Acquire);
            let reject_firmware =
                !analyzer && self.reject_firmware_once.swap(false, Ordering::AcqRel);
            let reject_exit = !analyzer && self.reject_exit_once.swap(false, Ordering::AcqRel);
            let revoke_after = self.revoke_after.clone();
            tokio::spawn(async move {
                let mut command = Vec::new();
                let mut frequency = "RF0145.50000".to_owned();
                let mut mode = "F0".to_owned();
                let mut rx_count = 0;
                while let Ok(byte) = device.read_u8().await {
                    if byte != b'\r' {
                        command.push(byte);
                        continue;
                    }
                    let command = String::from_utf8(std::mem::take(&mut command)).unwrap();
                    commands.lock().unwrap().push(command.clone());
                    if let Some(revoke) = revoke_after.lock().unwrap().as_ref()
                        && revoke.command == command
                    {
                        revoke.permission.store(false, Ordering::Release);
                    }
                    let reply = if analyzer {
                        let body = match command.as_str() {
                            "info" => "tinySA Ultra ZS405\r\n".to_owned(),
                            "version" => format!("{}\r\n", ori_instruments::FIRMWARE),
                            "deviceid" => "SYNTHETIC-ONLY\r\n".into(),
                            "help" => "trace output pause mode caloutput release\r\n".into(),
                            command if command.starts_with("scan ") => {
                                "100000 -0.0\r\n100010 1.0000000000000002\r\n100020 -1e-300\r\n"
                                    .into()
                            }
                            _ => String::new(),
                        };
                        format!("{command}\r\n{body}ch> ")
                    } else {
                        match command.as_str() {
                            "VR" if reject_firmware => "60?\r\n".into(),
                            "EX" if reject_exit => "60?\r\n".into(),
                            "VR" => "20VR2504A\r\n".into(),
                            "RN" => "20RNSYNTHETIC\r\n".into(),
                            "LM" => {
                                if pause.load(Ordering::Acquire) {
                                    started.fetch_add(1, Ordering::AcqRel);
                                    if let Ok(permit) = release.acquire().await {
                                        permit.forget();
                                    } else {
                                        break;
                                    }
                                }
                                "20LM1371\r\n".into()
                            }
                            "RX" => {
                                rx_count += 1;
                                let reported = if rx_count == wrong_rx_at {
                                    "RF0146.00000"
                                } else {
                                    frequency.as_str()
                                };
                                format!("20RX VFA {reported} ST010.00 MD0{mode} LM1371\r\n")
                            }
                            _ => {
                                if command.starts_with("RF") {
                                    frequency.clone_from(&command);
                                }
                                if let Some(selected) = command.strip_prefix("MD0") {
                                    mode = selected.to_owned();
                                }
                                "20\r\n".into()
                            }
                        }
                    };
                    if device.write_all(reply.as_bytes()).await.is_err() {
                        break;
                    }
                }
            });
            Ok(Box::new(Tracked {
                inner: host,
                drops: self.drops.clone(),
                shutdown_failure: self.shutdown_failure,
            }) as BoxTransport)
        })
    }
}
fn session(id: &str) -> Value {
    json!({"id":id,"name":"Synthetic devices","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"contract_version":1,"metadata":{},"streams":[
        {"id":"r","session_id":id,"device":"Synthetic AR-DV1","role":"receiver","state":"disconnected","level_unit":"s_meter_count","port":"fixture-receiver","settings":{"adapter":"ardv1-serial","frequency_hz":145_500_000,"period_s":0.01}},
        {"id":"a","session_id":id,"device":"Synthetic analyzer","role":"spectrum_analyzer","state":"disconnected","level_unit":"dBm","settings":{"adapter":"tinysa-sim","start_hz":100,"stop_hz":200,"points":3,"period_s":0.01}}
    ]})
}
async fn core(directory: &std::path::Path, factory: Arc<Factory>) -> Arc<Core> {
    let mut config = CoreConfig::replay(directory);
    config.devices.transport = Some(factory);
    config.devices.io_limits.receiver_timeout = Duration::from_secs(2);
    config.devices.io_limits.text_timeout = Duration::from_millis(100);
    config.devices.io_limits.write_timeout = Duration::from_millis(100);
    let core = Core::open(config).await.unwrap();
    core.archive()
        .unwrap()
        .create_session(session("devices"))
        .await
        .unwrap();
    core
}
async fn until(check: impl Fn() -> bool) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !check() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn receiver_scope_commits_the_final_point_before_releasing_its_port() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(directory.path(), factory.clone()).await;
    let archive = core.archive().unwrap();
    let mut session = session("scope");
    session["streams"][0]["id"] = json!("scope-r");
    session["streams"][1]["id"] = json!("scope-a");
    session["streams"][0]["settings"] = json!({"adapter":"ardv1-serial",
        "receiver_profile":"ardv1-monitor-v1", "mode":"FM", "period_s":0,
        "fast_scan":{"start_hz":88_000_000,"stop_hz":88_150_000,"step_hz":100_000,"settle_s":0}});
    archive.create_session(session).await.unwrap();
    let manager = core.devices.as_ref().unwrap();
    let target = Target::new("scope", "scope-r");
    let started = manager
        .start_stream("scope", "scope-r", manager.public_gate(target.clone()))
        .await
        .unwrap();
    until(|| factory.drops.load(Ordering::Acquire) == 1).await;
    let receipt = manager
        .retain_started_worker(
            &target,
            started.worker_instance.as_deref().unwrap(),
            manager.public_gate(target.clone()),
        )
        .unwrap();
    assert_eq!(receipt.check_start(true).unwrap().unwrap().measurements, 3);
    assert!(
        receipt.check().is_err(),
        "a completed segment cannot take new control commands"
    );
    let report = manager.stop_stream("scope", "scope-r").await.unwrap();
    assert!(
        receipt.check_start(true).is_err(),
        "an explicit stop supersedes the old start receipt"
    );
    assert_eq!(report.measurements, 3);
    assert!(!report.alive && report.last_error.is_none());
    let mut observations = archive
        .list_observations("scope", Some("scope-r"), 10)
        .await
        .unwrap();
    observations.sort_by_key(|row| row["metadata"]["scan_index"].as_u64().unwrap());
    assert_eq!(observations.len(), 3);
    for (row, frequency) in observations
        .iter()
        .zip([88_000_000, 88_100_000, 88_150_000])
    {
        assert_eq!(row["frequency_hz"], frequency);
        assert_eq!(row["metadata"]["frequency_readback"], true);
        assert_eq!(
            row["metadata"]["scope_source"],
            "sequential_receiver_readback"
        );
        assert!(row["metadata"].get("receiver_tune_context").is_none());
    }
    assert_eq!(factory.commands.lock().unwrap().last().unwrap(), "EX");
    core.close().await.unwrap();
}

#[tokio::test]
async fn wrong_segment_frequency_is_discarded_while_the_analyzer_keeps_recording() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.wrong_rx_at.store(2, Ordering::Release);
    let core = core(directory.path(), factory.clone()).await;
    let archive = core.archive().unwrap();
    let mut recording = session("paired");
    recording["streams"][0]["id"] = json!("paired-r");
    recording["streams"][1]["id"] = json!("paired-a");
    recording["streams"][0]["settings"] = json!({"adapter":"ardv1-serial",
        "receiver_profile":"ardv1-monitor-v1", "mode":"FM", "period_s":0,
        "fast_scan":{"start_hz":88_000_000,"stop_hz":88_150_000,"step_hz":100_000,"settle_s":0}});
    archive.create_session(recording).await.unwrap();
    let manager = core.devices.as_ref().unwrap();
    let gate = |stream| manager.public_gate(Target::new("paired", stream));
    manager
        .start_stream("paired", "paired-a", gate("paired-a"))
        .await
        .unwrap();
    manager
        .start_stream("paired", "paired-r", gate("paired-r"))
        .await
        .unwrap();
    until(|| factory.drops.load(Ordering::Acquire) == 1).await;
    let report = manager.stop_stream("paired", "paired-r").await.unwrap();
    assert_eq!(report.measurements, 1);
    assert!(report.last_error.unwrap().contains("RX did not confirm"));
    assert_eq!(report.current["requested_frequency_hz"], 88_100_000);
    assert!(report.current["observed_frequency_hz"].is_null());
    assert_eq!(report.current["frequency_readback"], false);
    let rows = archive
        .list_observations("paired", Some("paired-r"), 10)
        .await
        .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["frequency_hz"], 88_000_000);
    assert_eq!(rows[0]["level"], 137.0);
    let before = archive
        .list_sweeps("paired", Some("paired-a"), 100)
        .await
        .unwrap()
        .len();
    tokio::time::timeout(Duration::from_secs(3), async {
        while archive
            .list_sweeps("paired", Some("paired-a"), 100)
            .await
            .unwrap()
            .len()
            <= before
        {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(factory.commands.lock().unwrap().last().unwrap(), "RX");
    core.close().await.unwrap();
}

#[tokio::test]
async fn simulated_receiver_refuses_a_native_segment_before_start() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(directory.path(), factory.clone()).await;
    let mut recording = session("sim-segment");
    recording["streams"][0]["id"] = json!("sim-r");
    recording["streams"][1]["id"] = json!("sim-a");
    recording["streams"][0]["settings"] = json!({
        "adapter": "ardv1-sim",
        "fast_scan": {"start_hz":88_000_000,"stop_hz":88_100_000,"step_hz":100_000}
    });
    core.archive()
        .unwrap()
        .create_session(recording)
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap();
    let target = Target::new("sim-segment", "sim-r");
    let error = manager
        .start_stream("sim-segment", "sim-r", manager.public_gate(target))
        .await
        .unwrap_err();
    assert!(matches!(error, DeviceError::Invalid(_)));
    assert_eq!(factory.opens.load(Ordering::Acquire), 0);
    core.close().await.unwrap();
}

#[tokio::test]
async fn graceful_stop_commits_the_running_poll_before_releasing_transport() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.pause_poll.store(true, Ordering::Release);
    let mut config = CoreConfig::replay(directory.path());
    config.devices.transport = Some(factory.clone());
    config.devices.stop_timeout = Duration::from_millis(100);
    let core = Core::open(config).await.unwrap();
    core.archive()
        .unwrap()
        .create_session(session("devices"))
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap().clone();
    let target = Target::new("devices", "r");
    manager
        .start_stream("devices", "r", manager.public_gate(target.clone()))
        .await
        .unwrap();
    until(|| factory.poll_started.load(Ordering::Acquire) == 1).await;
    let pending = manager
        .stop_stream_mode("devices", "r", true)
        .await
        .unwrap();
    assert!(pending.alive);
    assert_eq!(pending.value().unwrap()["stopping"], true);
    assert_eq!(factory.drops.load(Ordering::Acquire), 0);
    factory.poll_release.add_permits(1);
    let stopped = manager
        .stop_stream_mode("devices", "r", true)
        .await
        .unwrap();
    assert_eq!(stopped.measurements, 1);
    assert!(!stopped.alive && stopped.last_error.is_none());
    assert_eq!(factory.commands.lock().unwrap().last().unwrap(), "EX");
    assert_eq!(
        core.archive()
            .unwrap()
            .list_observations("devices", Some("r"), 10)
            .await
            .unwrap()
            .len(),
        1
    );
    core.close().await.unwrap();
}

#[tokio::test]
async fn rejected_receiver_start_leaves_spectrum_recording_until_explicit_reconnect() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.reject_firmware_once.store(true, Ordering::Release);
    let core = core(directory.path(), factory.clone()).await;
    let manager = core.devices.as_ref().unwrap();
    let archive = core.archive().unwrap();
    let gate = |stream| manager.public_gate(Target::new("devices", stream));
    manager
        .start_stream("devices", "a", gate("a"))
        .await
        .unwrap();
    let error = manager
        .start_stream("devices", "r", gate("r"))
        .await
        .unwrap_err();
    assert!(error.to_string().contains("AR-DV1 prepare VR: 60?"));
    let workers = manager.list(Some("devices")).await.unwrap();
    let receiver = workers.iter().find(|w| w.stream_id == "r").unwrap();
    assert!(!receiver.alive);
    assert!(
        receiver
            .last_error
            .as_deref()
            .unwrap()
            .contains("AR-DV1 prepare VR: 60?")
    );
    let before = workers
        .iter()
        .find(|w| w.stream_id == "a")
        .unwrap()
        .measurements;
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let workers = manager.list(Some("devices")).await.unwrap();
            if workers
                .iter()
                .any(|w| w.stream_id == "a" && w.alive && w.measurements > before)
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(
        !archive
            .list_sweeps("devices", Some("a"), 1)
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        archive
            .list_observations("devices", Some("r"), 1)
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(factory.opens.load(Ordering::Acquire), 1);
    manager
        .start_stream("devices", "r", gate("r"))
        .await
        .unwrap();
    assert_eq!(factory.opens.load(Ordering::Acquire), 2);
    core.close().await.unwrap();
}

#[tokio::test]
async fn stalled_receiver_does_not_block_analyzer_commits_or_other_status_and_stop_drops_its_transport()
 {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.pause_poll.store(true, Ordering::Release);
    let core = core(directory.path(), factory.clone()).await;
    let manager = core.devices.as_ref().unwrap();
    let receiver = Target::new("devices", "r");
    let receiver_status = manager
        .start_stream("devices", "r", manager.public_gate(receiver))
        .await
        .unwrap();
    until(|| factory.poll_started.load(Ordering::Acquire) > 0).await;
    manager
        .start_stream(
            "devices",
            "a",
            manager.public_gate(Target::new("devices", "a")),
        )
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            let workers = manager.list(Some("devices")).await.unwrap();
            if workers
                .iter()
                .any(|worker| worker.stream_id == "a" && worker.measurements >= 2)
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let status = manager.stop_stream("devices", "r").await.unwrap();
    assert!(!status.alive);
    assert_eq!(
        factory.drops.load(Ordering::Acquire),
        1,
        "stop ACK must follow actual owned transport drop"
    );
    assert!(
        manager
            .list(Some("devices"))
            .await
            .unwrap()
            .iter()
            .any(|worker| worker.stream_id == "a" && worker.alive)
    );
    factory.pause_poll.store(false, Ordering::Release);
    factory.poll_release.add_permits(1);
    core.close().await.unwrap();
    assert_eq!(receiver_status.current["receiver_firmware"], "2504A");
}

#[tokio::test]
async fn queued_tune_rechecks_original_policy_before_io_while_trusted_reads_continue() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.pause_poll.store(true, Ordering::Release);
    let core = core(directory.path(), factory.clone()).await;
    let manager = core.devices.as_ref().unwrap();
    let allowed = Arc::new(AtomicBool::new(true));
    let permission = allowed.clone();
    let execution = ExecutionGate::new(
        move || {
            if permission.load(Ordering::Acquire) {
                Ok(())
            } else {
                Err(DeviceError::Denied)
            }
        },
        None,
    );
    let target = Target::new("devices", "r");
    manager
        .start_stream("devices", "r", manager.public_gate(target.clone()))
        .await
        .unwrap();
    until(|| factory.poll_started.load(Ordering::Acquire) > 0).await;
    let request_manager = manager.clone();
    let request_target = target.clone();
    let request = tokio::spawn(async move {
        request_manager
            .tune(
                request_target.clone(),
                145_600_000,
                Some("AM".into()),
                execution,
            )
            .await
    });
    let worker = manager
        .registry
        .lock()
        .unwrap()
        .workers
        .get(&target)
        .unwrap()
        .clone();
    until(|| worker.commands.capacity() < manager.options.queue_capacity).await;
    allowed.store(false, Ordering::Release);
    factory.pause_poll.store(false, Ordering::Release);
    factory.poll_release.add_permits(1);
    assert!(matches!(request.await.unwrap(), Err(DeviceError::Denied)));
    until(|| worker.snapshot().measurements >= 2).await;
    assert_eq!(
        factory
            .commands
            .lock()
            .unwrap()
            .iter()
            .filter(|command| command.starts_with("RF"))
            .count(),
        1,
        "only the initial explicitly authorized tune reached the wire"
    );
    core.close().await.unwrap();
}

#[tokio::test]
async fn original_prepare_receipt_is_rechecked_after_each_native_exchange_without_sending_later_commands()
 {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let allowed = Arc::new(AtomicBool::new(true));
    *factory.revoke_after.lock().unwrap() = Some(RevocationPoint {
        command: "VR".into(),
        permission: allowed.clone(),
    });
    let core = core(directory.path(), factory.clone()).await;
    let manager = core.devices.as_ref().unwrap();
    let gate = ExecutionGate::new(
        move || {
            if allowed.load(Ordering::Acquire) {
                Ok(())
            } else {
                Err(DeviceError::Denied)
            }
        },
        None,
    );
    assert!(
        manager.start_stream("devices", "r", gate).await.is_err(),
        "revoked initial receipt allowed a complete native prepare"
    );
    assert_eq!(
        *factory.commands.lock().unwrap(),
        vec!["RE1", "VR"],
        "later identity/tune commands executed after the original permission was revoked"
    );
    assert_eq!(
        factory.drops.load(Ordering::Acquire),
        1,
        "failed preparation did not join/drop its owned transport"
    );
    core.close().await.unwrap();
}

#[tokio::test]
async fn stop_invalidates_delayed_connect_before_any_preparation_and_clears_pending_admission() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.pause_open.store(true, Ordering::Release);
    let core = core(directory.path(), factory.clone()).await;
    let manager = core.devices.as_ref().unwrap();
    let starter = manager.clone();
    let start = tokio::spawn(async move {
        starter
            .start_stream(
                "devices",
                "r",
                starter.public_gate(Target::new("devices", "r")),
            )
            .await
    });
    until(|| factory.opens.load(Ordering::Acquire) == 1).await;
    let stopper = core.clone();
    let stop = tokio::spawn(async move { stopper.transition("devices", false).await });
    until(|| {
        manager
            .registry
            .lock()
            .unwrap()
            .stopping
            .contains_key("devices")
    })
    .await;
    factory.open_release.add_permits(1);
    assert!(matches!(start.await.unwrap(), Err(DeviceError::Cancelled)));
    assert_eq!(stop.await.unwrap().unwrap()["state"], "stopped");
    assert_eq!(factory.drops.load(Ordering::Acquire), 1);
    assert!(factory.commands.lock().unwrap().is_empty());
    assert!(manager.registry.lock().unwrap().pending.is_empty());
    assert!(manager.registry.lock().unwrap().ports.is_empty());
    core.close().await.unwrap();
}

#[tokio::test]
async fn same_port_alias_never_opens_second_owner_and_stale_tune_receipts_cannot_survive_reconnect()
{
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(directory.path(), factory.clone()).await;
    let mut other_session = session("other");
    other_session["streams"][0]["id"] = json!("other-r");
    other_session["streams"][1]["id"] = json!("other-a");
    core.archive()
        .unwrap()
        .create_session(other_session)
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap();
    let target = Target::new("devices", "r");
    let original = manager
        .start_stream("devices", "r", manager.public_gate(target.clone()))
        .await
        .unwrap();
    assert_eq!(
        original.current["tune_sequence"], 1,
        "the wire status must report acknowledged native tunes, including initial tuning"
    );
    let other = Target::new("other", "other-r");
    assert!(
        manager
            .start_stream("other", "other-r", manager.public_gate(other))
            .await
            .is_err()
    );
    assert_eq!(factory.opens.load(Ordering::Acquire), 1);
    let first = manager
        .tune(
            target.clone(),
            145_600_000,
            Some("AM".into()),
            manager.public_gate(target.clone()),
        )
        .await
        .unwrap();
    first.revalidate().unwrap();
    assert_eq!(first.worker.current["tune_sequence"], 2);
    assert_eq!(
        first.applied_tune_revision, 2,
        "one tune execution must bind one revision, not one per I/O poll"
    );
    manager
        .validate_tune(
            &target,
            first.worker.worker_instance.as_deref().unwrap(),
            first.applied_tune_revision,
            &first.tuned,
        )
        .unwrap();
    let second = manager
        .tune(
            target.clone(),
            145_700_000,
            Some("FM".into()),
            manager.public_gate(target.clone()),
        )
        .await
        .unwrap();
    assert!(first.revalidate().is_err());
    second.revalidate().unwrap();
    manager.stop_stream("devices", "r").await.unwrap();
    assert_eq!(factory.drops.load(Ordering::Acquire), 1);
    let replacement = manager
        .start_stream("devices", "r", manager.public_gate(target.clone()))
        .await
        .unwrap();
    assert_ne!(original.worker_instance, replacement.worker_instance);
    assert!(second.revalidate().is_err());
    let repeated = manager
        .tune(
            target.clone(),
            145_600_000,
            Some("AM".into()),
            manager.public_gate(target.clone()),
        )
        .await
        .unwrap();
    assert_eq!(first.applied_tune_revision, repeated.applied_tune_revision);
    assert!(
        manager
            .validate_tune(
                &target,
                first.worker.worker_instance.as_deref().unwrap(),
                first.applied_tune_revision,
                &first.tuned
            )
            .is_err(),
        "matching replacement frequency/revision must not renew an old worker receipt"
    );
    let stale = ExecutionGate::new(|| Ok(()), original.worker_instance);
    assert!(matches!(
        manager
            .tune(target, 145_800_000, Some("FM".into()), stale)
            .await,
        Err(DeviceError::Denied)
    ));
    core.close().await.unwrap();
}

#[tokio::test]
async fn cleanup_failure_latches_core_and_retains_writer_ownership_even_after_transport_drops() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(true);
    let core = core(directory.path(), factory.clone()).await;
    let manager = core.devices.as_ref().unwrap();
    manager
        .start_stream(
            "devices",
            "r",
            manager.public_gate(Target::new("devices", "r")),
        )
        .await
        .unwrap();
    assert!(core.close().await.is_err());
    assert!(core.close().await.is_err());
    assert_eq!(factory.drops.load(Ordering::Acquire), 1);
    assert!(
        ori_runtime::lease::WriterLease::acquire(directory.path()).is_err(),
        "failed cleanup must not release the process's writer lease"
    );
}

#[tokio::test]
async fn rejected_segment_exit_keeps_ownership_and_persists_the_terminal_error() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.reject_exit_once.store(true, Ordering::Release);
    let core = core(directory.path(), factory.clone()).await;
    let archive = core.archive().unwrap();
    let manager = core.devices.as_ref().unwrap();
    let mut segment = session("exit-test");
    segment["streams"][0]["id"] = json!("exit-r");
    segment["streams"][1]["id"] = json!("exit-a");
    segment["streams"][0]["settings"] = json!({"adapter":"ardv1-serial",
        "receiver_profile":"ardv1-monitor-v1", "mode":"FM", "period_s":0,
        "fast_scan":{"start_hz":88_000_000,"stop_hz":88_100_000,"step_hz":100_000,"settle_s":0}});
    archive.create_session(segment).await.unwrap();
    let gate = manager.public_gate(Target::new("exit-test", "exit-r"));
    manager
        .start_stream("exit-test", "exit-r", gate)
        .await
        .unwrap();
    until(|| factory.drops.load(Ordering::Acquire) == 1).await;
    assert!(manager.stop_stream("exit-test", "exit-r").await.is_err());
    let stored = archive.get_session("exit-test").await.unwrap().unwrap();
    let receiver = stored["streams"]
        .as_array()
        .unwrap()
        .iter()
        .find(|stream| stream["id"] == "exit-r")
        .unwrap();
    assert_eq!(receiver["state"], "error");
    assert_eq!(
        archive
            .list_observations("exit-test", Some("exit-r"), 5)
            .await
            .unwrap()
            .len(),
        2
    );
    let other = manager.public_gate(Target::new("devices", "r"));
    assert!(manager.start_stream("devices", "r", other).await.is_err());
    assert_eq!(factory.opens.load(Ordering::Acquire), 1);
    assert!(core.close().await.is_err());
}

#[tokio::test]
async fn native_rx_poll_commits_raw_readback_bound_to_the_applied_tune_receipt() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(directory.path(), factory.clone()).await;
    let mut verified = session("verified");
    verified["streams"][0]["id"] = json!("verified-r");
    verified["streams"][1]["id"] = json!("verified-a");
    verified["streams"][0]["settings"]["receiver_profile"] = json!("ardv1-monitor-v1");
    verified["streams"][0]["settings"]["monitoring_rx_readback"] = json!(true);
    core.archive()
        .unwrap()
        .create_session(verified)
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap();
    let target = Target::new("verified", "verified-r");
    manager
        .start_stream(
            "verified",
            "verified-r",
            manager.public_gate(target.clone()),
        )
        .await
        .unwrap();
    let report = manager
        .tune(
            target.clone(),
            145_600_000,
            Some("AM".into()),
            manager.public_gate(target.clone()),
        )
        .await
        .unwrap();
    let observation = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let samples = core
                .archive()
                .unwrap()
                .list_observations("verified", Some("verified-r"), 20)
                .await
                .unwrap();
            if let Some(sample) = samples
                .into_iter()
                .find(|s| s["frequency_hz"] == 145_600_000)
            {
                break sample;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    report.revalidate().unwrap();
    let metadata = &observation["metadata"];
    let expected = ori_guard::receiver_tune_context(
        report.worker.worker_instance.as_deref().unwrap(),
        report.applied_tune_revision,
        observation["id"].as_str().unwrap(),
    );
    assert_eq!(metadata["receiver_tune_context"], expected);
    assert_eq!(metadata["frequency_readback"], true);
    assert_eq!(
        metadata["raw_receiver_status"],
        "RX VFA RF0145.60000 ST010.00 MD0F1 LM1371"
    );
    assert!(
        observation["interval"]["started_at"].as_str().unwrap() >= report.acknowledged_at.as_str()
    );
    assert_eq!(observation["mode"], "AM");
    assert_eq!(observation["level"], 137.0);
    assert_eq!(observation["level_unit"], "s_meter_count");
    assert_eq!(factory.opens.load(Ordering::Acquire), 1);
    assert!(
        !factory
            .commands
            .lock()
            .unwrap()
            .iter()
            .any(|command| command == "LM")
    );
    core.close().await.unwrap();
}

#[tokio::test]
async fn shutdown_cancels_an_owned_poll_and_drains_already_admitted_tunes_before_writer_release() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.pause_poll.store(true, Ordering::Release);
    let core = core(directory.path(), factory.clone()).await;
    let manager = core.devices.as_ref().unwrap();
    let target = Target::new("devices", "r");
    manager
        .start_stream("devices", "r", manager.public_gate(target.clone()))
        .await
        .unwrap();
    until(|| factory.poll_started.load(Ordering::Acquire) > 0).await;
    let owned = manager.clone();
    let pending_target = target.clone();
    let pending = tokio::spawn(async move {
        owned
            .tune(
                pending_target.clone(),
                145_600_000,
                None,
                owned.public_gate(pending_target),
            )
            .await
    });
    let worker = manager
        .registry
        .lock()
        .unwrap()
        .workers
        .get(&target)
        .unwrap()
        .clone();
    until(|| worker.commands.capacity() < manager.options.queue_capacity).await;
    core.request_shutdown();
    tokio::time::timeout(Duration::from_secs(1), core.close())
        .await
        .unwrap()
        .unwrap();
    assert!(pending.await.unwrap().is_err());
    assert_eq!(factory.drops.load(Ordering::Acquire), 1);
    assert_eq!(
        factory
            .commands
            .lock()
            .unwrap()
            .iter()
            .filter(|command| command.starts_with("RF"))
            .count(),
        1
    );
    ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap();
    factory.pause_poll.store(false, Ordering::Release);
    factory.poll_release.add_permits(1);
}

#[tokio::test]
async fn actual_analyzer_duplex_source_commits_original_level_bits_and_explicit_profile_provenance()
{
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(directory.path(), factory.clone()).await;
    let mut fixture = session("devices");
    fixture["streams"][1]["port"] = json!("fixture-analyzer");
    fixture["streams"][1]["settings"] = json!({"adapter":"tinysa-serial","start_hz":100_000,"stop_hz":100_020,"points":3,"rbw_hz":10_000,"period_s":0.01});
    core.archive()
        .unwrap()
        .save_session(fixture, None)
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap();
    let prepared = manager
        .start_stream(
            "devices",
            "a",
            manager.public_gate(Target::new("devices", "a")),
        )
        .await
        .unwrap();
    assert_eq!(prepared.current["generator_output"], "off");
    assert_eq!(
        prepared.current["command_preparation"]["generator_off_acknowledged"],
        true
    );
    assert_eq!(
        prepared.current["command_preparation"]["input_mode_acknowledged"],
        true
    );
    assert_eq!(prepared.current["generator_output_readback"], false);
    let report = core.device_preparation("devices", "a").unwrap().unwrap();
    assert!(report.input_mode_acknowledged && report.generator_off_acknowledged);
    assert!(!report.device_mode_readback && !report.generator_output_readback);
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let sweeps = core
                .archive()
                .unwrap()
                .list_sweeps("devices", Some("a"), 1)
                .await
                .unwrap();
            if let Some(sweep) = sweeps.first() {
                assert_eq!(sweep["frequencies_hz"], json!([100000, 100010, 100020]));
                for (actual, expected) in sweep["levels"].as_array().unwrap().iter().zip([
                    -0.0f64,
                    1.0000000000000002,
                    -1e-300,
                ]) {
                    assert_eq!(actual.as_f64().unwrap().to_bits(), expected.to_bits());
                }
                assert_eq!(sweep["settings"]["device_identity"]["firmware"],ori_instruments::FIRMWARE);
                assert!(sweep["settings"]["device_identity"].get("deviceid").is_none(),"canonical receive provenance matches existing Python, identity validation remains separate");
                assert_eq!(sweep["settings"]["hardware_validation"], "not_run");
                assert!(sweep["settings"].get("device_mode").is_none());
                assert_eq!(
                    sweep["settings"]["generator_output_source"],
                    "command_acknowledged"
                );
                assert_eq!(sweep["settings"]["generator_output"],"off");
                assert_eq!(sweep["settings"]["generator_output_readback"], false);
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    core.close().await.unwrap();
    assert!(
        core.device_preparation("devices", "a").unwrap().is_none(),
        "closed transport must not retain a usable preparation receipt"
    );
    assert_eq!(factory.drops.load(Ordering::Acquire), 1);
}

#[tokio::test]
async fn processed_or_zero_span_device_output_cannot_be_relabelled_as_a_raw_monitoring_sweep() {
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(directory.path(), factory).await;
    let mut fixture = session("devices");
    fixture["streams"][1]["port"] = json!("fixture-analyzer");
    fixture["streams"][1]["settings"] = json!({"adapter":"tinysa-serial","start_hz":100_000,"stop_hz":100_020,"points":3,"rbw_hz":10_000,"period_s":3600});
    core.archive()
        .unwrap()
        .save_session(fixture, None)
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap();
    let target = Target::new("devices", "a");
    manager
        .start_stream("devices", "a", manager.public_gate(target.clone()))
        .await
        .unwrap();
    let worker = manager
        .registry
        .lock()
        .unwrap()
        .workers
        .get(&target)
        .unwrap()
        .clone();
    until(|| worker.snapshot().measurements == 1).await;
    let Native::Analyzer(actor) = worker.native.clone().unwrap() else {
        panic!("synthetic analyzer");
    };
    actor
        .enqueue(TinySaRequest::Control(
            ori_instruments::TinySaControl::Trace(ori_instruments::TraceCommand::Copy {
                trace: 1,
                source_trace: 2,
            }),
        ))
        .unwrap()
        .result()
        .await
        .unwrap();
    let result = worker
        .poll(&mut Adapter::Native(Native::Analyzer(actor)), None)
        .await;
    assert!(
        result.is_err(),
        "a processed artifact must never enter raw sweep/detector ingestion"
    );
    core.close().await.unwrap();
}

#[tokio::test]
async fn cyclic_scan_plan_commits_each_band_with_immutable_context_and_keeps_receiver_independent()
{
    let directory = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    factory.pause_poll.store(true, Ordering::Release);
    let mut config = CoreConfig::replay(directory.path());
    config.devices.transport = Some(factory.clone());
    let core = Core::open(config).await.unwrap();
    let mut source = session("plan");
    source["streams"][1]["settings"] = json!({"adapter":"tinysa-sim","start_hz":100000,"stop_hz":100020,"points":3,"period_s":0.01,
    "scan_plan":{"id":"immutable-plan-1","revision":1,"config":{"name":"Cycle","bands":[
        {"id":"low","name":"Low","start_hz":100000,"stop_hz":100020,"points":3,"rbw_hz":10000,"max_revisit_seconds":1.0},
        {"id":"high","name":"High","start_hz":200000,"stop_hz":200020,"points":3,"rbw_hz":30000,"max_revisit_seconds":1.0}
    ]}}});
    core.archive()
        .unwrap()
        .create_session(source)
        .await
        .unwrap();
    let manager = core.devices.as_ref().unwrap();
    manager
        .start_stream("plan", "r", manager.public_gate(Target::new("plan", "r")))
        .await
        .unwrap();
    until(|| factory.poll_started.load(Ordering::Acquire) > 0).await;
    manager
        .start_stream("plan", "a", manager.public_gate(Target::new("plan", "a")))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if manager
                .list(Some("plan"))
                .await
                .unwrap()
                .iter()
                .any(|s| s.stream_id == "a" && s.measurements >= 6)
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    manager.stop_stream("plan", "a").await.unwrap();
    let mut sources = core
        .archive()
        .unwrap()
        .list_sweeps("plan", Some("a"), 64)
        .await
        .unwrap();
    sources.sort_by_key(|s| s["settings"]["sequence"].as_u64().unwrap());
    factory.pause_poll.store(false, Ordering::Release);
    factory.poll_release.add_permits(1);
    core.close().await.unwrap();
    assert_eq!(
        factory.opens.load(Ordering::Acquire),
        1,
        "only the explicitly selected receiver transport may open"
    );
    assert!(sources.len() >= 6);
    for (index, sweep) in sources.iter().enumerate() {
        let high = index % 2 == 1;
        assert_eq!(
            sweep["frequencies_hz"][0],
            if high { 200000 } else { 100000 }
        );
        assert_eq!(
            sweep["frequencies_hz"][2],
            if high { 200020 } else { 100020 }
        );
        assert_eq!(
            sweep["settings"]["rbw_hz"],
            if high { 30000 } else { 10000 }
        );
        assert_eq!(
            sweep["settings"]["scan_band_id"],
            if high { "high" } else { "low" }
        );
        assert_eq!(sweep["settings"]["scan_plan_id"], "immutable-plan-1");
        assert_eq!(sweep["settings"]["scan_plan_revision"], 1);
        assert_eq!(sweep["settings"]["simulated"], true);
    }
}
