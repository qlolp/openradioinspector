//! Synthetic firmware transcripts; no port is enumerated or opened.
use ori_instruments::*;
use std::{
    collections::VecDeque,
    future::Future,
    io,
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
    time::Duration,
};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

#[derive(Clone)]
struct Step {
    command: String,
    fragments: Vec<(Duration, Vec<u8>)>,
    eof: bool,
}
impl Step {
    fn text(command: &str, body: &str) -> Self {
        Self::bytes(command, format!("{command}\r\n{body}ch> ").into_bytes())
    }
    fn ack(command: &str) -> Self {
        Self::text(command, "")
    }
    fn line(command: &str, body: &str) -> Self {
        Self::bytes(command, format!("{body}\r\n").into_bytes())
    }
    fn bytes(command: &str, bytes: Vec<u8>) -> Self {
        Self {
            command: command.into(),
            fragments: vec![(Duration::ZERO, bytes)],
            eof: false,
        }
    }
    fn binary(command: &str, payload: &[u8]) -> Self {
        let mut wire = format!("{command}\r\n").into_bytes();
        wire.extend_from_slice(payload);
        wire.extend_from_slice(b"ch> ");
        Self::bytes(command, wire)
    }
}
#[derive(Default)]
struct Audit {
    sent: Vec<Vec<u8>>,
    remaining: usize,
}
struct Scripted {
    steps: VecDeque<Step>,
    pending: VecDeque<(Duration, Vec<u8>)>,
    command: Vec<u8>,
    sleep: Option<Pin<Box<tokio::time::Sleep>>>,
    eof: bool,
    audit: Arc<Mutex<Audit>>,
}
impl Scripted {
    fn new(steps: Vec<Step>) -> (Self, Arc<Mutex<Audit>>) {
        let audit = Arc::new(Mutex::new(Audit {
            remaining: steps.len(),
            ..Audit::default()
        }));
        (
            Self {
                steps: steps.into(),
                pending: VecDeque::new(),
                command: Vec::new(),
                sleep: None,
                eof: false,
                audit: audit.clone(),
            },
            audit,
        )
    }
}
impl AsyncRead for Scripted {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        out: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if let Some((delay, _)) = this.pending.front() {
            if !delay.is_zero() && this.sleep.is_none() {
                this.sleep = Some(Box::pin(tokio::time::sleep(*delay)));
            }
            if let Some(sleep) = &mut this.sleep
                && sleep.as_mut().poll(cx).is_pending()
            {
                return Poll::Pending;
            }
            let (_, mut bytes) = this.pending.pop_front().unwrap();
            let count = bytes.len().min(out.remaining());
            out.put_slice(&bytes[..count]);
            bytes.drain(..count);
            this.sleep = None;
            if !bytes.is_empty() {
                this.pending.push_front((Duration::ZERO, bytes));
            }
            return Poll::Ready(Ok(()));
        }
        if this.eof {
            Poll::Ready(Ok(()))
        } else {
            Poll::Pending
        }
    }
}
impl AsyncWrite for Scripted {
    fn poll_write(
        self: Pin<&mut Self>,
        _: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        // Fragment every write: adapters must handle partial transport writes.
        let count = bytes.len().min(3);
        for &byte in &bytes[..count] {
            this.command.push(byte);
            if byte == b'\r' {
                let step = this
                    .steps
                    .pop_front()
                    .ok_or_else(|| io::Error::other("unexpected command"))?;
                let command = std::mem::take(&mut this.command);
                this.audit.lock().unwrap().sent.push(command.clone());
                if command != format!("{}\r", step.command).as_bytes() {
                    return Poll::Ready(Err(io::Error::other(format!(
                        "expected {:?}, got {:?}",
                        step.command, command
                    ))));
                }
                if !this.pending.is_empty() {
                    return Poll::Ready(Err(io::Error::other("unread scripted response")));
                }
                this.pending = step.fragments.into();
                this.eof = step.eof;
                this.audit.lock().unwrap().remaining = this.steps.len();
            }
        }
        Poll::Ready(Ok(count))
    }
    fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
    fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}
fn receive() -> ReceiveConfig {
    ReceiveConfig {
        start_hz: 100_000_000,
        stop_hz: 101_000_000,
        points: 3,
        rbw_hz: 10_000,
    }
}
fn setup() -> Vec<Step> {
    vec![
        Step::text("info", "tinySA Ultra ZS405\r\n"),
        Step::text(
            "version",
            "tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n",
        ),
        Step::text("deviceid", "synthetic-only\r\n"),
        Step::ack("output off"),
        Step::ack("mode input"),
        Step::ack("lna off"),
        Step::ack("attenuate auto"),
        Step::ack("rbw 10"),
        Step::ack("calc off"),
    ]
}
fn help() -> Step {
    Step::text(
        "help",
        "commands: output mode lna attenuate rbw calc pause resume capture touch release menu text refresh caloutput sweep scan scanraw zero trace marker trigger repeat spur ultra ext_gain level freq modulation levelchange save recall sd_list sd_read sd_delete selftest sweeptime\r\n",
    )
}
fn safe_off() -> Vec<Step> {
    vec![
        Step::ack("output off"),
        Step::ack("caloutput off"),
        Step::ack("release"),
    ]
}
fn resume() -> Vec<Step> {
    let mut steps = safe_off();
    steps.extend([
        Step::ack("mode input"),
        Step::ack("lna off"),
        Step::ack("attenuate auto"),
        Step::ack("calc off"),
        Step::ack("trigger auto"),
        Step::ack("trace dBm"),
        Step::ack("refresh off"),
    ]);
    steps
}
fn receiver_setup(firmware: &str) -> Vec<Step> {
    vec![
        Step::line("RE1", "20"),
        Step::line("VR", &format!("20VR{firmware}")),
        Step::line("RN", "20RNsynthetic"),
    ]
}
fn receiver() -> ReceiverConfig {
    ReceiverConfig {
        frequency_hz: 145_500_000,
        mode: ReceiverMode::Fm,
        profile: ReceiverProfile::LegacyFmAm,
    }
}
fn sent(audit: &Arc<Mutex<Audit>>) -> Vec<String> {
    audit
        .lock()
        .unwrap()
        .sent
        .iter()
        .map(|b| String::from_utf8(b.clone()).unwrap())
        .collect()
}
fn assert_drained(audit: &Arc<Mutex<Audit>>) {
    assert_eq!(audit.lock().unwrap().remaining, 0);
}

fn inspection_oracle() -> serde_json::Value {
    serde_json::from_str(include_str!(
        "../../../contracts/fixtures/instruments-inspection-v1.json"
    ))
    .unwrap()
}

#[tokio::test]
async fn inspect_is_query_only_cold_and_does_not_prepare_or_claim_input() {
    let oracle = inspection_oracle();
    let body = oracle["inspection"][0]["help_body"].as_str().unwrap();
    let mut steps = setup()[..3].to_vec();
    steps.push(Step::text("help", body));
    steps.extend_from_slice(&setup()[3..]);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    let result = adapter.inspect().await.unwrap();
    assert_eq!(result.state.device_mode, DeviceMode::Unknown);
    assert!(!result.state.profile_verified);
    assert!(result.state.control_profile_verified);
    assert_eq!(result.state.settings_source, "unknown");
    assert_eq!(
        result.commands,
        oracle["inspection"][0]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    );
    assert_eq!(
        sent(&audit),
        ["info\r", "version\r", "deviceid\r", "help\r"]
    );
    let count = sent(&audit).len();
    assert!(matches!(
        adapter
            .acquire_raw(RawScanConfig {
                start_hz: 100_000_000,
                stop_hz: 101_000_000,
                points: 3
            })
            .await,
        Err(InstrumentError::UnsafeState(_))
    ));
    adapter.inspect().await.unwrap();
    assert_eq!(sent(&audit).len(), count);
    adapter.prepare().await.unwrap();
    assert_eq!(adapter.state().device_mode, DeviceMode::Input);
    assert!(adapter.state().profile_verified && adapter.state().control_profile_verified);
    assert_eq!(sent(&audit).len(), 10);
    assert_drained(&audit);
}

#[tokio::test]
async fn prepared_inspect_matches_actual_python_help_regex_and_cache() {
    for case in inspection_oracle()["inspection"].as_array().unwrap() {
        let mut steps = setup();
        steps.push(Step::text("help", case["help_body"].as_str().unwrap()));
        let (wire, audit) = Scripted::new(steps);
        let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
        adapter.prepare().await.unwrap();
        assert!(!adapter.state().control_profile_verified);
        let result = adapter.inspect().await.unwrap();
        assert!(result.state.profile_verified && result.state.control_profile_verified);
        assert_eq!(result.state.device_mode, DeviceMode::Input);
        assert_eq!(result.state.settings_source, "command_acknowledged");
        assert_eq!(
            result.commands,
            case["commands"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect::<Vec<_>>(),
            "{}",
            case["name"]
        );
        assert_eq!(&sent(&audit)[9..], ["help\r"]);
        adapter.inspect().await.unwrap();
        assert_eq!(sent(&audit).len(), 10);
        if result.commands.is_empty() {
            assert!(matches!(
                adapter.read_rbw().await,
                Err(InstrumentError::UnsupportedCommand(_))
            ));
        }
        assert_drained(&audit);
    }
}

#[tokio::test]
async fn warm_and_cached_inspect_preserve_unknown_and_output_without_safe_off_surrogates() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    steps.extend([
        Step::ack("output off"),
        Step::ack("mode output"),
        Step::ack("output off"),
        Step::ack("freq 30000000"),
    ]);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    adapter.invalidate_device_knowledge();
    let result = adapter.inspect().await.unwrap();
    assert_eq!(result.state.device_mode, DeviceMode::Unknown);
    assert_eq!(result.state.settings_source, "unknown");
    assert_eq!(sent(&audit).len(), 10);
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    adapter
        .execute(TinySaControl::Set(Setting::OutputHz(30_000_000)))
        .await
        .unwrap();
    assert_eq!(adapter.state().device_mode, DeviceMode::Output);
    let count = sent(&audit).len();
    assert_eq!(
        adapter.inspect().await.unwrap().state.device_mode,
        DeviceMode::Output
    );
    assert_eq!(sent(&audit).len(), count);
    adapter.invalidate_device_knowledge();
    assert_eq!(
        adapter.inspect().await.unwrap().state.device_mode,
        DeviceMode::Unknown
    );
    assert_eq!(sent(&audit).len(), count);
    assert!(matches!(
        adapter
            .acquire_raw(RawScanConfig {
                start_hz: 100_000_000,
                stop_hz: 101_000_000,
                points: 3
            })
            .await,
        Err(InstrumentError::UnsafeState(_))
    ));
    assert_drained(&audit);
}

#[tokio::test]
async fn inspect_malformed_identity_or_help_taints_without_retry_or_setup() {
    for (command, bytes) in [
        ("version", b"version\r\nother-firmware\r\nch> ".to_vec()),
        ("help", b"help\r\n\xff\r\nch> ".to_vec()),
        ("help", b"help\r\nbody\r\nch> wrong\r\nch> ".to_vec()),
        ("help", b"help\r\ntruncated".to_vec()),
    ] {
        let mut steps = setup()[..3].to_vec();
        if command == "version" {
            steps[1] = Step::bytes(command, bytes);
        } else {
            let mut step = Step::bytes(command, bytes);
            step.eof = true;
            steps.push(step);
        }
        let (wire, audit) = Scripted::new(steps);
        let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
        assert!(adapter.inspect().await.is_err());
        assert!(adapter.state().restart_required);
        assert_eq!(adapter.state().device_mode, DeviceMode::Unknown);
        assert!(!adapter.state().profile_verified && !adapter.state().control_profile_verified);
        let count = sent(&audit).len();
        assert!(matches!(
            adapter.inspect().await,
            Err(InstrumentError::Tainted)
        ));
        assert!(matches!(
            adapter.prepare().await,
            Err(InstrumentError::Tainted)
        ));
        assert_eq!(sent(&audit).len(), count);
        assert!(
            sent(&audit)
                .iter()
                .all(|c| ["info\r", "version\r", "deviceid\r", "help\r"].contains(&c.as_str()))
        );
    }
}

#[tokio::test(start_paused = true)]
async fn inspect_actor_rechecks_queued_policy_and_keeps_cached_reads_immediate() {
    use std::sync::atomic::{AtomicBool, Ordering};
    let mut steps = setup();
    steps[0].fragments[0].0 = Duration::from_secs(1);
    let (wire, audit) = Scripted::new(steps);
    let actor = TinySaActor::spawn(
        TinySa::new(wire, receive(), IoLimits::default()).unwrap(),
        2,
    )
    .unwrap();
    let first = actor.enqueue(TinySaRequest::Prepare).unwrap();
    tokio::task::yield_now().await;
    let allowed = Arc::new(AtomicBool::new(true));
    let check = allowed.clone();
    let query = actor
        .enqueue_guarded(TinySaRequest::Inspect, move || {
            if check.load(Ordering::SeqCst) {
                Ok(())
            } else {
                Err(InstrumentError::PolicyDenied)
            }
        })
        .unwrap();
    allowed.store(false, Ordering::SeqCst);
    let before = tokio::time::Instant::now();
    assert_eq!(actor.state().device_mode, DeviceMode::Unknown);
    assert_eq!(before.elapsed(), Duration::ZERO);
    assert!(matches!(first.result().await.unwrap(), TinySaReply::Ready));
    assert!(matches!(
        query.result().await,
        Err(InstrumentError::PolicyDenied)
    ));
    assert_eq!(sent(&audit).len(), 9);
    assert!(!actor.state().control_profile_verified);
    assert!(matches!(
        actor
            .enqueue(TinySaRequest::State)
            .unwrap()
            .result()
            .await
            .unwrap(),
        TinySaReply::State(_)
    ));
    assert_drained(&audit);
}

#[tokio::test(start_paused = true)]
async fn dropped_help_query_taints_and_inspection_reply_is_typed() {
    let mut steps = setup();
    let mut delayed_help = help();
    delayed_help.fragments[0].0 = Duration::from_secs(10);
    steps.push(delayed_help);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(1), adapter.inspect())
            .await
            .is_err()
    );
    assert!(adapter.state().restart_required && !adapter.state().control_profile_verified);
    let count = sent(&audit).len();
    assert!(matches!(
        adapter.inspect().await,
        Err(InstrumentError::Tainted)
    ));
    assert_eq!(sent(&audit).len(), count);
    let mut steps = setup();
    steps.push(help());
    let (wire, _) = Scripted::new(steps);
    let actor = TinySaActor::spawn(
        TinySa::new(wire, receive(), IoLimits::default()).unwrap(),
        1,
    )
    .unwrap();
    actor
        .enqueue(TinySaRequest::Prepare)
        .unwrap()
        .result()
        .await
        .unwrap();
    match actor
        .enqueue(TinySaRequest::Inspect)
        .unwrap()
        .result()
        .await
        .unwrap()
    {
        TinySaReply::Inspection(value) => assert!(value.state.control_profile_verified),
        other => panic!("wrong inspect reply: {other:?}"),
    }
}

#[tokio::test]
async fn fresh_native_monitoring_projection_matches_actual_python_metadata_commands_and_bits() {
    let oracle = inspection_oracle();
    let mut evidence = Vec::new();
    for case in oracle["sweeps"].as_array().unwrap() {
        let mut steps = setup();
        let rbw = case["rbw_hz"].as_u64().unwrap();
        steps[7] = Step::ack(&format!("rbw {}", rbw / 1000));
        let inspected = case["inspected"].as_bool().unwrap();
        if inspected {
            steps.push(Step::text(
                "help",
                oracle["inspection"][0]["help_body"].as_str().unwrap(),
            ));
        }
        if case["name"] == "explicit-trace-dbm" {
            steps.push(Step::ack("trace dBm"));
        }
        steps.push(Step::text(
            "scan 100M 101M 3 3",
            case["scan_body"].as_str().unwrap(),
        ));
        let (wire, audit) = Scripted::new(steps);
        let mut adapter = TinySa::new(
            wire,
            ReceiveConfig {
                rbw_hz: u32::try_from(rbw).unwrap(),
                ..receive()
            },
            IoLimits::default(),
        )
        .unwrap();
        adapter.prepare().await.unwrap();
        if inspected {
            adapter.inspect().await.unwrap();
        }
        if case["name"] == "explicit-trace-dbm" {
            adapter
                .execute(TinySaControl::Set(Setting::TraceUnit(TraceUnit::Dbm)))
                .await
                .unwrap();
        }
        let sample = adapter.poll_spectrum().await.unwrap();
        let state = adapter.state();
        let context = inspected.then(|| TinySaProvenanceContext {
            connection_id: "0123456789abcdef0123456789abcdef".into(),
            revision: case["sweep"]["settings"]["instrument"]["revision"]
                .as_u64()
                .unwrap(),
        });
        let mapped = legacy_monitoring_settings(&sample, &state, context.as_ref()).unwrap();
        assert_eq!(mapped, case["sweep"]["settings"], "{}", case["name"]);
        assert_eq!(
            sample.frequencies_hz,
            case["sweep"]["frequencies_hz"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_u64().unwrap())
                .collect::<Vec<_>>()
        );
        assert_eq!(
            sample
                .levels
                .iter()
                .map(|v| format!("{:016x}", v.to_bits()))
                .collect::<Vec<_>>(),
            case["level_bits"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect::<Vec<_>>()
        );
        let first_peak = (1..sample.levels.len()).fold(0, |best, index| {
            if sample.levels[index] > sample.levels[best] {
                index
            } else {
                best
            }
        });
        assert_eq!(
            first_peak,
            case["first_peak_index"].as_u64().unwrap() as usize
        );
        assert_eq!(
            sample.frequencies_hz[first_peak],
            case["peak_frequency_hz"].as_u64().unwrap()
        );
        assert_eq!(
            format!("{:016x}", sample.levels[first_peak].to_bits()),
            case["peak_level_bits"]
        );
        assert_eq!(sample.level_unit, case["sweep"]["level_unit"]);
        assert_eq!(
            sent(&audit)
                .iter()
                .map(|s| s.trim_end_matches('\r').to_owned())
                .collect::<Vec<_>>(),
            case["commands"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect::<Vec<_>>()
        );
        assert!(sample.interval.started_at <= sample.interval.ended_at);
        assert_drained(&audit);
        evidence.push(serde_json::json!({"name":case["name"],"frequencies_hz":sample.frequencies_hz,"levels":sample.levels,"level_unit":sample.level_unit,"settings":mapped,"operation_interval":sample.interval}));
    }
    if let Some(path) = std::env::var_os("ORI_INSPECTION_EVIDENCE") {
        std::fs::write(path, serde_json::to_vec(&evidence).unwrap()).unwrap();
    }
}

#[tokio::test]
async fn monitoring_projection_refuses_unproved_state_context_and_unmapped_source_metadata() {
    let mut steps = setup();
    steps.push(Step::text(
        "scan 100M 101M 3 3",
        "100000000 -0.0 0\r\n100500000 0.0 0\r\n101000000 -1 0\r\n",
    ));
    let (wire, _) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    let sample = adapter.poll_spectrum().await.unwrap();
    let state = adapter.state();
    assert!(legacy_monitoring_settings(&sample, &state, None).is_ok());
    for mode in [DeviceMode::Unknown, DeviceMode::Output] {
        let mut bad = state.clone();
        bad.device_mode = mode;
        assert!(legacy_monitoring_settings(&sample, &bad, None).is_err());
    }
    let mut bad = state.clone();
    bad.settings_source = "unknown".into();
    assert!(legacy_monitoring_settings(&sample, &bad, None).is_err());
    let mut bad = state.clone();
    bad.restart_required = true;
    assert!(legacy_monitoring_settings(&sample, &bad, None).is_err());
    let mut bad = state.clone();
    bad.control_mode = ControlMode::Lab;
    assert!(legacy_monitoring_settings(&sample, &bad, None).is_err());
    let mut bad = state.clone();
    bad.settings["rbw_hz"] = serde_json::json!(30000);
    assert!(legacy_monitoring_settings(&sample, &bad, None).is_err());
    for kind in [SpectrumKind::ProcessedTrace, SpectrumKind::ZeroSpan] {
        let mut bad = sample.clone();
        bad.kind = kind;
        assert!(legacy_monitoring_settings(&bad, &state, None).is_err());
    }
    let mut bad = sample.clone();
    bad.level_unit = "original-raw".into();
    assert!(legacy_monitoring_settings(&bad, &state, None).is_err());
    let mut bad = sample.clone();
    bad.settings["unknown-nonvolatile-extension"] = serde_json::json!(1);
    assert!(legacy_monitoring_settings(&bad, &state, None).is_err());
    let mut bad = sample.clone();
    bad.levels[0] = f64::NAN;
    assert!(legacy_monitoring_settings(&bad, &state, None).is_err());
    let context = TinySaProvenanceContext {
        connection_id: "live-worker".into(),
        revision: 0,
    };
    assert!(legacy_monitoring_settings(&sample, &state, Some(&context)).is_err());
}

#[tokio::test]
async fn tinysa_golden_receive_keeps_integer_hz_f64_and_ack_provenance() {
    let mut steps = setup();
    steps.push(Step::text(
        "scan 100M 101M 3 3",
        "100000000 -90 0\r\n100500000 -42.50000000000001 0\r\n101000000 -80 0\r\n",
    ));
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    let sample = adapter.poll_spectrum().await.unwrap();
    assert_eq!(
        sample.frequencies_hz,
        [100_000_000, 100_500_000, 101_000_000]
    );
    assert_eq!(sample.levels, [-90.0, -42.50000000000001, -80.0]);
    assert_eq!(sample.level_unit, "dBm");
    assert_eq!(sample.settings["settings_source"], "command_acknowledged");
    assert_eq!(sample.settings["rbw_applied_state"], "unknown");
    assert_eq!(sample.settings["settings_readback"], false);
    assert_eq!(sample.settings["generator_output_readback"], false);
    assert_eq!(sample.settings["hardware_validation"], "not_run");
    assert!(sample.interval.started_at <= sample.interval.ended_at);
    let encoded = serde_json::to_string(&sample).unwrap();
    let restored: SpectrumSample = serde_json::from_str(&encoded).unwrap();
    assert_eq!(sample.levels[1].to_bits(), restored.levels[1].to_bits());
    assert_eq!(adapter.state().device_mode, DeviceMode::Input);
    assert_eq!(
        sent(&audit),
        [
            "info\r",
            "version\r",
            "deviceid\r",
            "output off\r",
            "mode input\r",
            "lna off\r",
            "attenuate auto\r",
            "rbw 10\r",
            "calc off\r",
            "scan 100M 101M 3 3\r"
        ]
    );
    assert_drained(&audit);
}

#[tokio::test]
async fn unsupported_identity_never_sends_setup_and_connection_cannot_retry() {
    let (wire, audit) = Scripted::new(vec![
        Step::text("info", "tinySA Ultra+ ZS407\r\n"),
        Step::text("version", "tinySA4_v1.4-235-gea71dfd\r\n"),
        Step::text("deviceid", "synthetic\r\n"),
    ]);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    assert!(matches!(
        adapter.prepare().await,
        Err(InstrumentError::UnsupportedProfile(_))
    ));
    assert!(matches!(
        adapter.prepare().await,
        Err(InstrumentError::Tainted)
    ));
    assert_eq!(sent(&audit).len(), 3);
    assert_eq!(adapter.state().device_mode, DeviceMode::Unknown);
}

#[test]
fn invalid_receive_and_receiver_requests_are_rejected_before_io() {
    for config in [
        ReceiveConfig {
            rbw_hz: 20_000,
            ..receive()
        },
        ReceiveConfig {
            stop_hz: 800_000_001,
            ..receive()
        },
        ReceiveConfig {
            points: 291,
            ..receive()
        },
        ReceiveConfig {
            start_hz: 101_000_000,
            ..receive()
        },
    ] {
        let (wire, audit) = Scripted::new(vec![]);
        assert!(TinySa::new(wire, config, IoLimits::default()).is_err());
        assert!(sent(&audit).is_empty());
    }
    assert!("TETRA".parse::<ReceiverMode>().is_err());
    let (wire, audit) = Scripted::new(vec![]);
    assert!(
        ArDv1::new(
            wire,
            ReceiverConfig {
                mode: ReceiverMode::Dmr,
                ..receiver()
            },
            IoLimits::default()
        )
        .is_err()
    );
    assert!(sent(&audit).is_empty());
}

#[tokio::test(start_paused = true)]
async fn prompt_fragments_and_silent_measurement_gap_are_not_terminators() {
    let mut steps = setup();
    steps.push(Step {
        command: "scan 100M 101M 3 3".into(),
        fragments: vec![
            (Duration::ZERO, b"scan 100M 101M 3 3\r\n".to_vec()),
            (
                Duration::from_secs(2),
                b"100000000 -90 0\r\n100500000 -80 0\r\n101000000 -70 0\r\nc".to_vec(),
            ),
            (Duration::from_secs(1), b"h> ".to_vec()),
        ],
        eof: false,
    });
    let (wire, _) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    assert_eq!(
        adapter.poll_spectrum().await.unwrap().levels,
        [-90.0, -80.0, -70.0]
    );
}

#[tokio::test]
async fn malformed_text_frames_and_scans_taint_without_followup() {
    for bytes in [
        b"info\r\ntinySA Ultra ZS405\r\nch> ch> ".to_vec(),
        b"info\r\n\xff\r\nch> ".to_vec(),
        vec![b'x'; 1025],
    ] {
        let (wire, audit) = Scripted::new(vec![Step::bytes("info", bytes)]);
        let mut adapter = TinySa::new(
            wire,
            receive(),
            IoLimits {
                max_text_bytes: 1024,
                ..IoLimits::default()
            },
        )
        .unwrap();
        assert!(adapter.prepare().await.is_err());
        assert!(adapter.state().restart_required);
        assert!(matches!(
            adapter.prepare().await,
            Err(InstrumentError::Tainted)
        ));
        assert_eq!(sent(&audit).len(), 1);
    }
    for body in [
        "100000000 -90\r\n101000000 -80\r\n",
        "100000001 -90\r\n100500000 -80\r\n101000000 -70\r\n",
        "100000000 -90\r\n100500000 NaN\r\n101000000 -70\r\n",
        "100000000 -90dBmdBm\r\n100500000 -80\r\n101000000 -70\r\n",
    ] {
        let mut steps = setup();
        steps.push(Step::text("scan 100M 101M 3 3", body));
        let (wire, _) = Scripted::new(steps);
        let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
        assert!(adapter.poll_spectrum().await.is_err());
        assert!(adapter.state().restart_required);
    }
}

#[tokio::test(start_paused = true)]
async fn timed_out_or_dropped_exchange_cannot_consume_late_ack() {
    let slow = Step {
        command: "info".into(),
        fragments: vec![(
            Duration::from_secs(20),
            b"tinySA Ultra ZS405\r\nch> ".to_vec(),
        )],
        eof: false,
    };
    let (wire, audit) = Scripted::new(vec![slow.clone()]);
    let mut adapter = TinySa::new(
        wire,
        receive(),
        IoLimits {
            text_timeout: Duration::from_millis(10),
            ..IoLimits::default()
        },
    )
    .unwrap();
    assert!(matches!(
        adapter.prepare().await,
        Err(InstrumentError::Timeout)
    ));
    assert!(matches!(
        adapter.prepare().await,
        Err(InstrumentError::Tainted)
    ));
    assert_eq!(sent(&audit), ["info\r"]);
    let (wire, audit) = Scripted::new(vec![slow]);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(10), adapter.prepare())
            .await
            .is_err()
    );
    assert!(adapter.state().restart_required);
    assert!(matches!(
        adapter.prepare().await,
        Err(InstrumentError::Tainted)
    ));
    assert_eq!(sent(&audit), ["info\r"]);
}

#[tokio::test]
async fn raw_binary_uses_exact_echo_length_little_endian_and_open_grid() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::text("zero", "zero {level}\r\n174dBm\r\n"));
    steps.push(Step::binary(
        "scanraw 100000000 101000000 3 0",
        b"{x\x80\x0ax\x00\x0bx\x80\x0b}",
    ));
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    let sample = adapter
        .acquire_raw(RawScanConfig {
            start_hz: 100_000_000,
            stop_hz: 101_000_000,
            points: 3,
        })
        .await
        .unwrap();
    assert_eq!(
        sample.frequencies_hz,
        [100_000_000, 100_333_333, 100_666_666]
    );
    assert_eq!(sample.levels, [-90.0, -86.0, -82.0]);
    assert_eq!(sample.wire, b"{x\x80\x0ax\x00\x0bx\x80\x0b}");
    assert_eq!(sample.zero_db, 174);
    assert_eq!(sample.settings["stop_is_inclusive"], false);
    assert_drained(&audit);
}

#[tokio::test]
async fn rf_off_preserves_output_and_unknown_modes_until_explicit_resume() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    steps.extend([
        Step::ack("output off"),
        Step::ack("mode output"),
        Step::ack("output off"),
        Step::ack("freq 30000000"),
    ]);
    steps.extend(safe_off());
    steps.extend(resume());
    steps.push(Step::text("zero", "zero {level}\r\n174dBm\r\n"));
    steps.push(Step::binary(
        "scanraw 100000000 101000000 3 0",
        b"{x\0\x10x\0\x10x\0\x10}",
    ));
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    adapter
        .execute(TinySaControl::Set(Setting::OutputHz(30_000_000)))
        .await
        .unwrap();
    adapter.execute(TinySaControl::OutputOff).await.unwrap();
    assert_eq!(adapter.state().device_mode, DeviceMode::Output);
    let count = sent(&audit).len();
    let raw = RawScanConfig {
        start_hz: 100_000_000,
        stop_hz: 101_000_000,
        points: 3,
    };
    assert!(matches!(
        adapter.acquire_raw(raw).await,
        Err(InstrumentError::UnsafeState(_))
    ));
    assert!(matches!(
        adapter
            .acquire_raw(RawScanConfig {
                stop_hz: 100_000_000,
                ..raw
            })
            .await,
        Err(InstrumentError::UnsafeState(_))
    ));
    assert_eq!(sent(&audit).len(), count);
    adapter
        .execute(TinySaControl::ResumeMonitoring)
        .await
        .unwrap();
    assert_eq!(adapter.state().device_mode, DeviceMode::Input);
    assert!(adapter.acquire_raw(raw).await.is_ok());
    assert_drained(&audit);
}

#[tokio::test]
async fn menu_recall_selftest_invalidate_mode_and_failed_transition_taints() {
    for (control, mut commands) in [
        (
            TinySaControl::Touch { x: 1, y: 2 },
            vec![Step::ack("touch 1 2")],
        ),
        (
            TinySaControl::LoadPreset {
                slot: 0,
                confirmed: true,
            },
            vec![Step::ack("recall 0")],
        ),
        (
            TinySaControl::SelfTest {
                test: 1,
                confirmed: true,
            },
            {
                let mut s = safe_off();
                s.push(Step::ack("selftest 1"));
                s
            },
        ),
    ] {
        let mut steps = setup();
        steps.push(help());
        steps.push(Step::ack("pause"));
        steps.extend(safe_off());
        steps.append(&mut commands);
        steps.extend(safe_off());
        let (wire, audit) = Scripted::new(steps);
        let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
        adapter.prepare().await.unwrap();
        adapter.execute(TinySaControl::EnterLab).await.unwrap();
        adapter.execute(control).await.unwrap();
        adapter.execute(TinySaControl::OutputOff).await.unwrap();
        assert_eq!(adapter.state().device_mode, DeviceMode::Unknown);
        let count = sent(&audit).len();
        assert!(matches!(
            adapter.poll_spectrum().await,
            Err(InstrumentError::UnsafeState(_))
        ));
        assert_eq!(sent(&audit).len(), count);
    }
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    steps.extend(safe_off());
    steps.push(Step::text("mode input", "ERROR invalid\r\n"));
    let (wire, _) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    assert!(
        adapter
            .execute(TinySaControl::ResumeMonitoring)
            .await
            .is_err()
    );
    assert!(adapter.state().restart_required);
    assert_eq!(adapter.state().device_mode, DeviceMode::Unknown);
}

#[tokio::test]
async fn binary_prompt_payload_is_not_text_and_truncation_taints() {
    let mut steps = setup();
    steps.push(help());
    let pixels = vec![b'c'; 480 * 320 * 2];
    let mut expected = pixels.clone();
    expected[100..104].copy_from_slice(b"ch> ");
    let mut step = Step::binary("capture", &expected);
    step.fragments[0].1.shrink_to_fit();
    steps.push(step);
    let (wire, _) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    let image = adapter.capture_screen().await.unwrap();
    assert_eq!(image, expected);
    let mut steps = setup();
    steps.push(help());
    let mut bad = Step::bytes("capture", b"capture\r\n123".to_vec());
    bad.eof = true;
    steps.push(bad);
    let (wire, _) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    assert!(adapter.capture_screen().await.is_err());
    assert!(adapter.state().restart_required);
}

#[test]
fn scanraw_decoder_preserves_firmware_f32_grid_and_rejects_partial_frames() {
    let (hz, levels) =
        decode_scanraw(b"{x\x80\x0ax\x00\x0bx\x80\x0bx\x00\x0c}", 100, 111, 4, 174).unwrap();
    assert_eq!(hz, [100, 102, 104, 106]);
    assert_eq!(levels, [-90.0, -86.0, -82.0, -78.0]);
    assert_eq!(
        decode_scanraw(b"{x\0\x10}", 100, 100, 1, 128).unwrap(),
        (vec![100], vec![0.0])
    );
    assert!(decode_scanraw(b"{x\0\0}", 100, 200, 2, 174).is_err());
    assert!(decode_scanraw(b"{q\0\0x\0\0}", 100, 200, 2, 174).is_err());
}

#[tokio::test]
async fn ardv1_tune_and_lm_preserve_exact_commands_raw_count_and_no_readback() {
    let mut steps = receiver_setup("1805A");
    steps.extend([
        Step::line("VFA", "20"),
        Step::line("MD0F0", "20"),
        Step::line("RF0433.07500", "20"),
        Step::line("LM", "20LM0551"),
        Step::line("EX", "20"),
    ]);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = ArDv1::new(wire, receiver(), IoLimits::default()).unwrap();
    adapter.tune(433_075_000, ReceiverMode::Fm).await.unwrap();
    let sample = adapter.poll_receiver().await.unwrap();
    assert_eq!(sample.frequency_hz, 433_075_000);
    assert_eq!(sample.mode, ReceiverMode::Fm);
    assert_eq!(sample.level, 55);
    assert_eq!(sample.level_unit, "s_meter_count");
    assert_eq!(sample.raw_squelch_status, 1);
    assert!(sample.squelch_open);
    assert!(sample.decoded_mode.is_none());
    assert_eq!(sample.metadata["frequency_readback"], false);
    adapter.close().await.unwrap();
    assert_eq!(
        sent(&audit),
        [
            "RE1\r",
            "VR\r",
            "RN\r",
            "VFA\r",
            "MD0F0\r",
            "RF0433.07500\r",
            "LM\r",
            "EX\r"
        ]
    );
    assert_drained(&audit);
}

#[tokio::test]
async fn ardv1_auto_profile_requires_firmware_and_retains_decoder_evidence() {
    let config = ReceiverConfig {
        profile: ReceiverProfile::MonitorV1,
        mode: ReceiverMode::Auto,
        ..receiver()
    };
    let mut steps = receiver_setup("2504A");
    steps.extend([
        Step::line("VFA", "10"),
        Step::line("MD000", "20"),
        Step::line("RF0145.50000", "20"),
        Step::line("LM", "20LM1043"),
        Step::line("MD", "20MD700"),
    ]);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = ArDv1::new(wire, config.clone(), IoLimits::default()).unwrap();
    let sample = adapter.poll_receiver().await.unwrap();
    assert_eq!(sample.level, 104);
    assert_eq!(sample.raw_squelch_status, 3);
    assert_eq!(sample.decoded_mode.as_deref(), Some("DMR"));
    assert_eq!(sample.raw_mode_status.as_deref(), Some("MD700"));
    assert_eq!(sample.metadata["decoder_source"], "receiver_MD");
    assert_drained(&audit);
    let (wire, audit) = Scripted::new(vec![Step::line("RE1", "20"), Step::line("VR", "20VR1805A")]);
    let mut adapter = ArDv1::new(wire, config, IoLimits::default()).unwrap();
    assert!(matches!(
        adapter.poll_receiver().await,
        Err(InstrumentError::UnsupportedProfile(_))
    ));
    assert_eq!(sent(&audit), ["RE1\r", "VR\r"]);
}

#[tokio::test]
async fn ardv1_rejected_tune_or_lm_invalidates_frequency_and_never_sends_followup() {
    for bad in ["20RF0145.50000", "20LM0551", "21", "50", "?", "", "20MD000"] {
        let mut steps = receiver_setup("1805A");
        steps.push(Step::line("VFA", bad));
        let (wire, audit) = Scripted::new(steps);
        let mut adapter = ArDv1::new(wire, receiver(), IoLimits::default()).unwrap();
        assert!(adapter.poll_receiver().await.is_err());
        assert!(adapter.state().frequency_hz.is_none());
        assert!(matches!(
            adapter.poll_receiver().await,
            Err(InstrumentError::Tainted)
        ));
        assert_eq!(sent(&audit).len(), 4);
    }
    for bad in [
        "20LM+551",
        "20LM-551",
        "20LM5554",
        "21LM0551",
        "50",
        "20",
        "20LM５５1",
    ] {
        let mut steps = receiver_setup("1805A");
        steps.extend([
            Step::line("VFA", "20"),
            Step::line("MD0F0", "20"),
            Step::line("RF0145.50000", "20"),
            Step::line("LM", bad),
        ]);
        let (wire, _) = Scripted::new(steps);
        let mut adapter = ArDv1::new(wire, receiver(), IoLimits::default()).unwrap();
        assert!(adapter.poll_receiver().await.is_err());
        assert!(adapter.state().frequency_hz.is_none());
    }
}

#[tokio::test(start_paused = true)]
async fn independent_bounded_actors_allow_analyzer_progress_while_receiver_is_busy() {
    let mut steps = receiver_setup("1805A");
    steps[0].fragments[0].0 = Duration::from_secs(10);
    let (wire, _) = Scripted::new(steps);
    let receiver = ArDv1Actor::spawn(
        ArDv1::new(wire, receiver(), IoLimits::default()).unwrap(),
        1,
    )
    .unwrap();
    let first = receiver.enqueue(ArDv1Request::Poll).unwrap();
    tokio::task::yield_now().await;
    let second = receiver.enqueue(ArDv1Request::Poll).unwrap();
    assert!(matches!(
        receiver.enqueue(ArDv1Request::Poll),
        Err(InstrumentError::QueueFull)
    ));
    let mut steps = setup();
    steps.push(Step::text(
        "scan 100M 101M 3 3",
        "100000000 -90\r\n100500000 -80\r\n101000000 -70\r\n",
    ));
    let (wire, _) = Scripted::new(steps);
    let analyzer = TinySaActor::spawn(
        TinySa::new(wire, receive(), IoLimits::default()).unwrap(),
        1,
    )
    .unwrap();
    let start = tokio::time::Instant::now();
    let reply = analyzer
        .enqueue(TinySaRequest::Poll)
        .unwrap()
        .result()
        .await
        .unwrap();
    assert!(matches!(reply, TinySaReply::Spectrum(_)));
    assert!(start.elapsed() < Duration::from_secs(1));
    receiver.cancel();
    assert!(matches!(
        first.result().await,
        Err(InstrumentError::Cancelled)
    ));
    assert!(second.result().await.is_err());
}

#[test]
fn setting_allowlist_emits_existing_wire_units_and_rejects_invalid_values() {
    for (setting, expected, lab) in [
        (Setting::StartHz(100_000), "sweep start 100000", false),
        (Setting::Points(450), "sweep 450", false),
        (Setting::RbwHz(Rbw::Hz(200)), "rbw 0.2", false),
        (Setting::RbwHz(Rbw::Hz(30_000)), "rbw 30", false),
        (Setting::RbwHz(Rbw::Auto), "rbw auto", false),
        (
            Setting::AttenuationDb(Attenuation::Auto),
            "attenuate auto",
            false,
        ),
        (Setting::Lna(true), "lna on", false),
        (Setting::TraceUnit(TraceUnit::Dbuv), "trace dBuV", false),
        (Setting::SweepSeconds(0.003), "sweeptime 0.003", false),
        (Setting::OutputHz(30_000_000), "freq 30000000", true),
        (Setting::OutputDbm(-30.0), "level -30", true),
        (Setting::OutputEnabled(true), "output on", true),
        (Setting::Modulation(Modulation::Fm), "modulation fm", true),
        (Setting::CalOutputMhz(None), "caloutput off", true),
    ] {
        let command = setting_command(&setting).unwrap();
        assert_eq!(command.wire, expected);
        assert_eq!(command.requires_lab, lab);
    }
    for setting in [
        Setting::RbwHz(Rbw::Hz(20_000)),
        Setting::OutputDbm(f64::NAN),
        Setting::OutputDbm(-18.0),
        Setting::ModulationHz(5001),
        Setting::Points(451),
        Setting::CalOutputMhz(Some(5)),
    ] {
        assert!(setting_command(&setting).is_err());
    }
}

#[tokio::test]
async fn explicit_rf_enable_requires_lab_and_current_explicit_frequency_and_level() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    steps.extend([
        Step::ack("output off"),
        Step::ack("mode output"),
        Step::ack("output off"),
        Step::ack("freq 30000000"),
        Step::ack("level -30"),
        Step::ack("output on"),
    ]);
    steps.extend(safe_off());
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    let count = sent(&audit).len();
    assert!(
        adapter
            .execute(TinySaControl::Set(Setting::OutputEnabled(true)))
            .await
            .is_err()
    );
    assert_eq!(sent(&audit).len(), count);
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    let count = sent(&audit).len();
    assert!(
        adapter
            .execute(TinySaControl::Set(Setting::OutputEnabled(true)))
            .await
            .is_err()
    );
    assert_eq!(sent(&audit).len(), count);
    adapter
        .execute(TinySaControl::Set(Setting::OutputHz(30_000_000)))
        .await
        .unwrap();
    adapter
        .execute(TinySaControl::Set(Setting::OutputDbm(-30.0)))
        .await
        .unwrap();
    adapter
        .execute(TinySaControl::Set(Setting::OutputEnabled(true)))
        .await
        .unwrap();
    adapter.execute(TinySaControl::OutputOff).await.unwrap();
    let count = sent(&audit).len();
    assert!(
        adapter
            .execute(TinySaControl::Set(Setting::OutputEnabled(true)))
            .await
            .is_err()
    );
    assert_eq!(sent(&audit).len(), count);
    assert_drained(&audit);
}

#[tokio::test]
async fn setting_range_points_and_rbw_follow_existing_receive_commands() {
    let mut steps = setup();
    steps.push(help());
    steps.extend([
        Step::ack("sweep start 99000000"),
        Step::ack("sweep 99000000 101000000 4"),
        Step::ack("rbw 30"),
    ]);
    steps.push(Step::text(
        "scan 99M 101M 4 3",
        "99000000 -90\r\n99500000 -80\r\n100500000 -70\r\n101000000 -60\r\n",
    ));
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    adapter
        .execute(TinySaControl::Set(Setting::StartHz(99_000_000)))
        .await
        .unwrap();
    adapter
        .execute(TinySaControl::Set(Setting::Points(4)))
        .await
        .unwrap();
    adapter
        .execute(TinySaControl::Set(Setting::RbwHz(Rbw::Hz(30_000))))
        .await
        .unwrap();
    let sample = adapter.poll_spectrum().await.unwrap();
    assert_eq!(
        sample.frequencies_hz,
        [99_000_000, 99_500_000, 100_500_000, 101_000_000]
    );
    assert_eq!(sample.settings["rbw_requested_hz"], 30_000);
    assert_drained(&audit);
}

#[tokio::test]
async fn manual_numeric_text_is_validated_before_io_and_invalidation_blocks_raw() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    steps.push(Step::ack("text -12.5M"));
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    let count = sent(&audit).len();
    for text in ["1\routput on", "1e3", "hello", "1234567890123"] {
        assert!(
            adapter
                .execute(TinySaControl::Keypad { text: text.into() })
                .await
                .is_err()
        );
    }
    assert_eq!(sent(&audit).len(), count);
    adapter
        .execute(TinySaControl::Keypad {
            text: "-12.5M".into(),
        })
        .await
        .unwrap();
    assert_eq!(adapter.state().device_mode, DeviceMode::Unknown);
    assert_drained(&audit);
}

#[tokio::test]
async fn sd_read_checks_prefix_size_and_preserves_payload_with_prompt_bytes() {
    let mut steps = setup();
    steps.push(help());
    let mut body = 5u32.to_le_bytes().to_vec();
    body.extend_from_slice(b"a\0ch>");
    steps.push(Step::binary("sd_read A.CSV", &body));
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    assert!(adapter.read_sd("../secret").await.is_err());
    assert!(sent(&audit).is_empty());
    assert_eq!(adapter.read_sd("A.CSV").await.unwrap(), b"a\0ch>");
    assert_drained(&audit);
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::binary("sd_read A.CSV", &9_000_000u32.to_le_bytes()));
    let (wire, _) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    assert!(adapter.read_sd("A.CSV").await.is_err());
    assert!(adapter.state().restart_required);
}

#[tokio::test]
async fn monitoring_processed_trace_retains_unit_and_is_marked_for_artifact_archive() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("trace dBuV"));
    steps.push(Step::text(
        "scan 100M 101M 3 3",
        "100000000 1\r\n100500000 2\r\n101000000 3\r\n",
    ));
    let (wire, _) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    adapter
        .execute(TinySaControl::Set(Setting::TraceUnit(TraceUnit::Dbuv)))
        .await
        .unwrap();
    let sample = adapter.poll_spectrum().await.unwrap();
    assert_eq!(sample.level_unit, "dBuV");
    assert_eq!(sample.settings["artifact_kind"], "processed_trace");
}

#[test]
fn intervals_serialize_as_utc_microseconds_and_serial_validation_is_pure() {
    let interval: OperationInterval = serde_json::from_str(
        r#"{"started_at":"2026-11-11T12:30:00.123456Z","ended_at":"2026-11-11T12:30:01.000000Z"}"#,
    )
    .unwrap();
    assert_eq!(
        serde_json::to_string(&interval).unwrap(),
        r#"{"started_at":"2026-11-11T12:30:00.123456Z","ended_at":"2026-11-11T12:30:01.000000Z"}"#
    );
    #[cfg(feature = "serial")]
    {
        assert_eq!(
            serial::validate_windows_com_port("\\\\.\\com12").unwrap(),
            "COM12"
        );
        for path in ["COM0", "COM01", "tcp://host", "COM3/other"] {
            assert!(serial::validate_windows_com_port(path).is_err());
        }
        for options in [
            serial::SerialOptions {
                path: "".into(),
                baud: 9600,
            },
            serial::SerialOptions {
                path: "tcp://host".into(),
                baud: 9600,
            },
            serial::SerialOptions {
                path: "COM3".into(),
                baud: 123,
            },
        ] {
            assert!(options.validate().is_err());
        }
    }
}

#[tokio::test]
async fn explicit_generator_sweep_emits_existing_sequence_and_blocks_acquisition() {
    let request = GeneratorSweepConfig {
        start_hz: 30_000_000,
        stop_hz: 31_000_000,
        points: 100,
        seconds: 1.0,
        level_dbm: -30.0,
        delta_db: 0.0,
        confirmed: true,
    };
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    steps.extend(safe_off());
    steps.extend([
        Step::ack("mode output"),
        Step::ack("output off"),
        Step::ack("sweep 30000000 31000000 100"),
        Step::ack("level -30"),
        Step::ack("levelchange 0"),
        Step::ack("sweeptime 1"),
        Step::ack("output on"),
        Step::ack("sweep go"),
        Step::ack("resume"),
    ]);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    assert!(
        adapter
            .execute(TinySaControl::GeneratorSweep(request.clone()))
            .await
            .is_err()
    );
    assert!(sent(&audit).is_empty());
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    let count = sent(&audit).len();
    assert!(
        adapter
            .execute(TinySaControl::GeneratorSweep(GeneratorSweepConfig {
                confirmed: false,
                ..request.clone()
            }))
            .await
            .is_err()
    );
    assert_eq!(sent(&audit).len(), count);
    adapter
        .execute(TinySaControl::GeneratorSweep(request))
        .await
        .unwrap();
    assert_eq!(adapter.state().device_mode, DeviceMode::Output);
    assert!(
        adapter
            .acquire_raw(RawScanConfig {
                start_hz: 100_000_000,
                stop_hz: 101_000_000,
                points: 3
            })
            .await
            .is_err()
    );
    assert_drained(&audit);
}

#[tokio::test]
async fn documented_trace_marker_and_rbw_readbacks_preserve_device_evidence() {
    let mut steps = setup();
    steps.push(help());
    steps.extend([
        Step::ack("trace 1 copy 2"),
        Step::text("trace 1 value", "-90\r\n-80\r\n"),
        Step::text("marker 2 peak", "2 100500000 -42.5\r\n"),
        Step::text("rbw", "rbw {auto|3|10|30}\r\nactual\r\n10000Hz\r\n"),
    ]);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    adapter
        .execute(TinySaControl::Trace(TraceCommand::Copy {
            trace: 1,
            source_trace: 2,
        }))
        .await
        .unwrap();
    let trace = adapter.read_trace(1).await.unwrap();
    assert_eq!(trace.level_unit, "internal_dBm");
    assert_eq!(trace.body, "-90\n-80");
    let marker = adapter
        .marker(MarkerCommand::Peak { marker: 2 })
        .await
        .unwrap();
    assert_eq!(marker.body, "2 100500000 -42.5");
    assert_eq!(marker.level_unit, "dBm");
    assert_eq!(adapter.read_rbw().await.unwrap(), 10000.0);
    assert_drained(&audit);
}

#[tokio::test]
async fn unsupported_required_control_never_claims_rf_off_or_sends_partial_sequence() {
    let mut steps = setup();
    steps.push(Step::text("help", "commands: pause mode\r\n"));
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    assert!(matches!(
        adapter.execute(TinySaControl::OutputOff).await,
        Err(InstrumentError::UnsupportedCommand(_))
    ));
    assert!(!adapter.state().restart_required);
    assert_eq!(sent(&audit).last().map(String::as_str), Some("help\r"));
}

#[tokio::test]
async fn unexpected_pending_input_and_partial_binary_terminal_taint_connection() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::bytes(
        "capture",
        [b"capture\r\n".as_slice(), &vec![0; 480 * 320 * 2], b"nope"].concat(),
    ));
    let (wire, _) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    assert!(adapter.capture_screen().await.is_err());
    assert!(adapter.state().restart_required);
    let (mut wire, audit) = Scripted::new(setup());
    wire.pending.push_back((Duration::ZERO, b"20\r\n".to_vec()));
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    assert!(adapter.prepare().await.is_err());
    assert!(sent(&audit).is_empty());
    assert!(adapter.state().restart_required);
}

#[tokio::test]
async fn every_supported_receiver_mode_uses_profile_command_and_preserves_raw_status() {
    for (mode, command, status) in [
        (ReceiverMode::Auto, "MD000", "20MD000"),
        (ReceiverMode::Dstar, "MD010", "20MD110"),
        (ReceiverMode::Ysf, "MD020", "20MD220"),
        (ReceiverMode::Alinco, "MD030", "20MD330"),
        (ReceiverMode::Nxdn, "MD040", "20MD440"),
        (ReceiverMode::P25, "MD050", "20MD550"),
        (ReceiverMode::Dpmr, "MD060", "20MD660"),
        (ReceiverMode::Dmr, "MD070", "20MD770"),
        (ReceiverMode::Fm, "MD0F0", "20MD0F0"),
        (ReceiverMode::Am, "MD0F1", "20MD0F1"),
    ] {
        let mut steps = receiver_setup("2504A");
        steps.extend([
            Step::line("VFA", "20"),
            Step::line(command, "20"),
            Step::line("RF0145.50000", "20"),
            Step::line("LM", "20LM0000"),
            Step::line("MD", status),
        ]);
        let (wire, audit) = Scripted::new(steps);
        let mut adapter = ArDv1::new(
            wire,
            ReceiverConfig {
                mode,
                profile: ReceiverProfile::MonitorV1,
                ..receiver()
            },
            IoLimits::default(),
        )
        .unwrap();
        let sample = adapter.poll_receiver().await.unwrap();
        assert_eq!(sample.mode, mode);
        assert_eq!(sample.level, 0);
        assert_eq!(sample.raw_mode_status.as_deref(), Some(&status[2..]));
        assert_drained(&audit);
    }
}

#[tokio::test(start_paused = true)]
async fn actor_state_reads_and_invalid_large_commands_never_wait_for_busy_io() {
    let mut steps = setup();
    steps[0].fragments[0].0 = Duration::from_secs(20);
    let (wire, _) = Scripted::new(steps);
    let actor = TinySaActor::spawn(
        TinySa::new(wire, receive(), IoLimits::default()).unwrap(),
        1,
    )
    .unwrap();
    let pending = actor.enqueue(TinySaRequest::Prepare).unwrap();
    tokio::task::yield_now().await;
    let start = tokio::time::Instant::now();
    assert_eq!(actor.state().device_mode, DeviceMode::Unknown);
    assert_eq!(start.elapsed(), Duration::ZERO);
    assert!(matches!(
        actor.enqueue(TinySaRequest::Control(TinySaControl::Keypad {
            text: "1".repeat(1_000_000)
        })),
        Err(InstrumentError::InvalidRequest(_))
    ));
    actor.cancel();
    assert!(actor.state().restart_required);
    assert!(matches!(
        pending.result().await,
        Err(InstrumentError::Cancelled)
    ));
}

#[tokio::test]
async fn cancelled_or_failed_mode_readback_cannot_reuse_old_receiver_frequency() {
    let mut steps = receiver_setup("2504A");
    steps.extend([
        Step::line("VFA", "20"),
        Step::line("MD000", "20"),
        Step::line("RF0145.50000", "20"),
        Step::line("LM", "20LM0551"),
        Step::line("MD", "20MD0F0"),
    ]);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = ArDv1::new(
        wire,
        ReceiverConfig {
            mode: ReceiverMode::Auto,
            profile: ReceiverProfile::MonitorV1,
            ..receiver()
        },
        IoLimits::default(),
    )
    .unwrap();
    assert!(adapter.poll_receiver().await.is_err());
    assert!(adapter.state().frequency_hz.is_none());
    assert!(adapter.state().restart_required);
    let count = sent(&audit).len();
    assert!(adapter.tune(433_075_000, ReceiverMode::Fm).await.is_err());
    assert_eq!(sent(&audit).len(), count);
}

#[test]
fn all_shared_setting_fixtures_match_python_allowlist_and_units() {
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/instruments-tinysa-settings-v1.json"
    ))
    .unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let setting: Setting = serde_json::from_value(case["setting"].clone()).unwrap();
        let command = setting_command(&setting).unwrap();
        assert_eq!(command.wire, case["expected_wire"], "{setting:?}");
        assert_eq!(command.requires_lab, case["requires_lab"], "{setting:?}");
    }
}

#[tokio::test]
async fn resumed_manual_or_processed_settings_remain_processed_for_monitoring_archive() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("trace 1 freeze on"));
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    steps.push(Step::ack("touch 1 2"));
    steps.extend(resume());
    steps.push(Step::text(
        "scan 100M 101M 3 3",
        "100000000 -90\r\n100500000 -80\r\n101000000 -70\r\n",
    ));
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    adapter
        .execute(TinySaControl::Trace(TraceCommand::Freeze {
            trace: 1,
            enabled: true,
        }))
        .await
        .unwrap();
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    adapter
        .execute(TinySaControl::Touch { x: 1, y: 2 })
        .await
        .unwrap();
    adapter
        .execute(TinySaControl::ResumeMonitoring)
        .await
        .unwrap();
    let sample = adapter.poll_spectrum().await.unwrap();
    assert_eq!(sample.kind, SpectrumKind::ProcessedTrace);
    assert_eq!(sample.settings["artifact_kind"], "processed_trace");
    assert_drained(&audit);
}

fn fixture_steps(fixture: &serde_json::Value) -> Vec<Step> {
    fixture["transcript"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            Step::bytes(
                row["write_ascii"]
                    .as_str()
                    .unwrap()
                    .strip_suffix('\r')
                    .unwrap(),
                row["read_ascii"].as_str().unwrap().as_bytes().to_vec(),
            )
        })
        .collect()
}
#[tokio::test]
async fn shared_synthetic_transcripts_are_reusable_contracts_for_real_adapters() {
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/instruments-tinysa-receive-v1.json"
    ))
    .unwrap();
    let config: ReceiveConfig = serde_json::from_value(fixture["config"].clone()).unwrap();
    let (wire, audit) = Scripted::new(fixture_steps(&fixture));
    let mut adapter = TinySa::new(wire, config, IoLimits::default()).unwrap();
    let sample = serde_json::to_value(adapter.poll_spectrum().await.unwrap()).unwrap();
    for (key, value) in fixture["expected"].as_object().unwrap() {
        assert_eq!(&sample[key], value, "{key}");
    }
    assert_drained(&audit);
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/instruments-ardv1-monitor-v1.json"
    ))
    .unwrap();
    let config: ReceiverConfig = serde_json::from_value(fixture["config"].clone()).unwrap();
    let (wire, audit) = Scripted::new(fixture_steps(&fixture));
    let mut adapter = ArDv1::new(wire, config, IoLimits::default()).unwrap();
    let sample = serde_json::to_value(adapter.poll_receiver().await.unwrap()).unwrap();
    for (key, value) in fixture["expected"].as_object().unwrap() {
        assert_eq!(
            if key == "frequency_readback" {
                &sample["metadata"][key]
            } else {
                &sample[key]
            },
            value,
            "{key}"
        );
    }
    assert_drained(&audit);
}

#[tokio::test]
async fn lab_mode_pauses_zero_span_monitor_poll_without_sending_raw_commands() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("sweep span 0"));
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    adapter
        .execute(TinySaControl::Set(Setting::SpanHz(0)))
        .await
        .unwrap();
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    let count = sent(&audit).len();
    assert!(matches!(
        adapter.poll_spectrum().await,
        Err(InstrumentError::UnsafeState(_))
    ));
    assert_eq!(sent(&audit).len(), count);
    assert_drained(&audit);
}

#[tokio::test]
async fn orderly_lab_close_sends_rf_off_before_transport_shutdown_and_rejects_reuse() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    steps.extend(safe_off());
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    adapter.close().await.unwrap();
    assert!(adapter.state().closed);
    assert_eq!(adapter.state().device_mode, DeviceMode::Unknown);
    assert!(matches!(
        adapter.poll_spectrum().await,
        Err(InstrumentError::Closed)
    ));
    assert_eq!(
        &sent(&audit)[sent(&audit).len() - 3..],
        ["output off\r", "caloutput off\r", "release\r"]
    );
    assert_drained(&audit);
}

#[tokio::test(start_paused = true)]
async fn late_receiver_level_after_timeout_is_never_retimestamped_or_followed_by_ex() {
    let mut steps = receiver_setup("1805A");
    steps.extend([
        Step::line("VFA", "20"),
        Step::line("MD0F0", "20"),
        Step::line("RF0145.50000", "20"),
    ]);
    let mut level = Step::line("LM", "20LM0551");
    level.fragments[0].0 = Duration::from_secs(2);
    steps.push(level);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = ArDv1::new(wire, receiver(), IoLimits::default()).unwrap();
    assert!(matches!(
        adapter.poll_receiver().await,
        Err(InstrumentError::Timeout)
    ));
    assert!(adapter.state().frequency_hz.is_none());
    let count = sent(&audit).len();
    tokio::time::advance(Duration::from_secs(10)).await;
    assert!(matches!(
        adapter.poll_receiver().await,
        Err(InstrumentError::Tainted)
    ));
    adapter.close().await.unwrap();
    assert_eq!(sent(&audit).len(), count);
}

#[tokio::test]
async fn sd_directory_delete_and_preset_save_require_exact_existing_responses() {
    let mut steps = setup();
    steps.push(help());
    steps.push(Step::text("sd_list", "A.CSV\r\n"));
    steps.push(Step::ack("pause"));
    steps.extend(safe_off());
    steps.extend([
        Step::ack("save 2"),
        Step::text("sd_delete A.CSV", "delete: A.CSV OK\r\n"),
    ]);
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    assert_eq!(adapter.list_sd().await.unwrap(), "A.CSV");
    assert!(
        adapter
            .execute(TinySaControl::DeleteSd {
                filename: "A.CSV".into(),
                confirmed: true
            })
            .await
            .is_err()
    );
    adapter.execute(TinySaControl::EnterLab).await.unwrap();
    adapter
        .execute(TinySaControl::SavePreset {
            slot: 2,
            confirmed: true,
        })
        .await
        .unwrap();
    adapter
        .execute(TinySaControl::DeleteSd {
            filename: "A.CSV".into(),
            confirmed: true,
        })
        .await
        .unwrap();
    assert_drained(&audit);
}

struct BrokenWriter {
    inner: Scripted,
    remaining: usize,
}
impl AsyncRead for BrokenWriter {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_read(cx, buf)
    }
}
impl AsyncWrite for BrokenWriter {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        if this.remaining == 0 {
            return Poll::Ready(Err(io::ErrorKind::BrokenPipe.into()));
        }
        let count = bytes.len().min(this.remaining);
        let result = Pin::new(&mut this.inner).poll_write(cx, &bytes[..count]);
        if let Poll::Ready(Ok(written)) = &result {
            this.remaining -= written;
        }
        result
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_flush(cx)
    }
    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_shutdown(cx)
    }
}
#[tokio::test]
async fn partial_write_failure_taints_connection_before_any_retry() {
    let (wire, audit) = Scripted::new(setup());
    let mut adapter = TinySa::new(
        BrokenWriter {
            inner: wire,
            remaining: 2,
        },
        receive(),
        IoLimits::default(),
    )
    .unwrap();
    assert!(matches!(
        adapter.prepare().await,
        Err(InstrumentError::Io(_))
    ));
    assert!(adapter.state().restart_required);
    assert!(matches!(
        adapter.prepare().await,
        Err(InstrumentError::Tainted)
    ));
    assert!(sent(&audit).is_empty());
}

#[tokio::test(start_paused = true)]
async fn terminal_prompt_space_fragment_must_be_consumed_before_next_command() {
    let mut steps = setup();
    steps[0].fragments = vec![
        (
            Duration::ZERO,
            b"info\r\ntinySA Ultra ZS405\r\nch>".to_vec(),
        ),
        (Duration::from_millis(1), b" ".to_vec()),
    ];
    let (wire, audit) = Scripted::new(steps);
    let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
    adapter.prepare().await.unwrap();
    assert_drained(&audit);
}

#[tokio::test]
async fn malformed_rbw_readbacks_cannot_claim_applied_bandwidth() {
    for value in [".5Hz", "10000.Hz", "1e4Hz", "unknown"] {
        let mut steps = setup();
        steps.push(help());
        steps.push(Step::text(
            "rbw",
            &format!("rbw {{auto|3|10|30}}\r\n{value}\r\n"),
        ));
        let (wire, _) = Scripted::new(steps);
        let mut adapter = TinySa::new(wire, receive(), IoLimits::default()).unwrap();
        assert!(adapter.read_rbw().await.is_err());
        assert!(adapter.state().restart_required);
    }
}

#[tokio::test(start_paused = true)]
async fn core_execution_guard_rechecks_queued_lease_immediately_before_device_io() {
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    let mut steps = setup();
    steps[0].fragments[0].0 = Duration::from_millis(100);
    let (wire, audit) = Scripted::new(steps);
    let actor = TinySaActor::spawn(
        TinySa::new(wire, receive(), IoLimits::default()).unwrap(),
        1,
    )
    .unwrap();
    let first = actor.enqueue(TinySaRequest::Prepare).unwrap();
    tokio::task::yield_now().await;
    let valid = Arc::new(AtomicBool::new(true));
    let checks = Arc::new(AtomicUsize::new(0));
    let lease = valid.clone();
    let guard_checks = checks.clone();
    let second = actor
        .enqueue_guarded(
            TinySaRequest::Control(TinySaControl::OutputOff),
            move || {
                guard_checks.fetch_add(1, Ordering::SeqCst);
                if lease.load(Ordering::SeqCst) {
                    Ok(())
                } else {
                    Err(InstrumentError::UnsafeState(
                        "lease expired before dispatch".into(),
                    ))
                }
            },
        )
        .unwrap();
    assert_eq!(checks.load(Ordering::SeqCst), 0);
    valid.store(false, Ordering::SeqCst);
    first.result().await.unwrap();
    assert!(matches!(
        second.result().await,
        Err(InstrumentError::PolicyDenied)
    ));
    assert_eq!(checks.load(Ordering::SeqCst), 1);
    assert_eq!(sent(&audit).len(), 9);
    assert!(!actor.state().restart_required);
    assert_drained(&audit);
}

#[tokio::test(start_paused = true)]
async fn receiver_guard_denies_stale_generation_without_tune_bytes_or_token_details() {
    let mut steps = receiver_setup("1805A");
    steps[0].fragments[0].0 = Duration::from_millis(100);
    let (wire, audit) = Scripted::new(steps);
    let actor = ArDv1Actor::spawn(
        ArDv1::new(wire, receiver(), IoLimits::default()).unwrap(),
        1,
    )
    .unwrap();
    let first = actor.enqueue(ArDv1Request::Prepare).unwrap();
    tokio::task::yield_now().await;
    let second = actor
        .enqueue_guarded(
            ArDv1Request::Tune {
                frequency_hz: 433_075_000,
                mode: ReceiverMode::Fm,
            },
            || {
                Err(InstrumentError::InvalidRequest(
                    "private-client-token must never appear".into(),
                ))
            },
        )
        .unwrap();
    first.result().await.unwrap();
    let error = second.result().await.unwrap_err();
    assert!(matches!(error, InstrumentError::PolicyDenied));
    assert_eq!(
        error.to_string(),
        "operation denied by current core control policy"
    );
    assert_eq!(sent(&audit), ["RE1\r", "VR\r", "RN\r"]);
    assert!(!actor.state().restart_required);
    assert_drained(&audit);
}
