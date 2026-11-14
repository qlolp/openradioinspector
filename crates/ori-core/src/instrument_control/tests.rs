//! Owned duplex + real canonical writer, with only delivery of a committed
//! operation's terminal transaction held to force both completion orders.
use super::*;
use crate::devices::{BoxTransport, DeviceFuture, DeviceResult, TransportFactory};
use ori_contracts::{DeviceStream, parse_json_value, to_json_vec};
use ori_control::{
    CommitOutcome, ControlCommit, ControlCommitter, ControlFuture, OperationKind, OperationState,
};
use serde_json::{Value, json};
use std::{
    pin::Pin,
    sync::atomic::AtomicU8,
    task::{Context, Poll},
    time::Duration,
};
use tokio::io::{
    AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader, ReadBuf,
};

struct Transport {
    io: tokio::io::DuplexStream,
    dropped: Arc<AtomicBool>,
}
impl Drop for Transport {
    fn drop(&mut self) {
        self.dropped.store(true, Ordering::Release);
    }
}
impl AsyncRead for Transport {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_read(cx, buf)
    }
}
impl AsyncWrite for Transport {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.get_mut().io).poll_write(cx, buf)
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_flush(cx)
    }
    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_shutdown(cx)
    }
}
struct Factory {
    dropped: Arc<AtomicBool>,
    wire: Arc<Mutex<Vec<String>>>,
    peer: Mutex<Vec<tokio::task::JoinHandle<()>>>,
}
impl TransportFactory for Factory {
    fn identity(&self, _: &DeviceStream) -> DeviceResult<String> {
        Ok("owned-held-commit-memory".into())
    }
    fn open<'a>(&'a self, stream: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        Box::pin(async move {
            let receiver = stream.role == ori_contracts::DeviceRole::Receiver;
            let (client, server) = tokio::io::duplex(4096);
            let wire = self.wire.clone();
            let peer = tokio::spawn(async move {
                let oracle = parse_json_value(
                    include_bytes!("../../../ori-control/tests/fixtures/python_control.json"),
                    8 * 1024 * 1024,
                )
                .unwrap();
                let mut responses = BTreeMap::<String, Vec<u8>>::new();
                for command in [
                    "output off",
                    "mode input",
                    "lna off",
                    "attenuate auto",
                    "rbw 10",
                    "calc off",
                    "trigger auto",
                    "trace dBm",
                    "refresh off",
                ] {
                    responses.insert(command.into(), vec![]);
                }
                for case in oracle["cases"].as_array().unwrap() {
                    for command in case["commands"].as_array().unwrap() {
                        responses
                            .entry(command.as_str().unwrap().into())
                            .or_default();
                    }
                }
                responses.insert(
                    "scan 100M 101M 3 3".into(),
                    b"100000000 -90\r\n100500000 -80\r\n101000000 -70\r\n".to_vec(),
                );
                responses.insert("info".into(), b"tinySA Ultra ZS405\r\n".to_vec());
                responses.insert(
                    "version".into(),
                    b"tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n".to_vec(),
                );
                responses.insert("deviceid".into(), b"owned synthetic\r\n".to_vec());
                responses.insert(
                    "help".into(),
                    (oracle["cases"][0]["result"]["commands"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|s| s.as_str().unwrap())
                        .collect::<Vec<_>>()
                        .join(" ")
                        + "\r\n")
                        .into_bytes(),
                );
                let mut io = BufReader::new(server);
                loop {
                    let mut command = vec![];
                    if io.read_until(b'\r', &mut command).await.unwrap() == 0 {
                        break;
                    }
                    let command = String::from_utf8(command[..command.len() - 1].to_vec()).unwrap();
                    wire.lock().unwrap().push(command.clone());
                    if receiver {
                        if command == "RF0146.50000" {
                            let mut byte = [0];
                            assert_eq!(io.get_mut().read(&mut byte).await.unwrap(), 0);
                            break;
                        }
                        let response = match command.as_str() {
                            "VR" => "20VR2504A\r\n",
                            "RN" => "20RNOWNED\r\n",
                            "LM" => "20LM1371\r\n",
                            "RE1" | "VFA" | "MD0F0" | "EX" => "20\r\n",
                            c if c.starts_with("RF") => "20\r\n",
                            _ => panic!("outside verified owned receiver oracle: {command}"),
                        };
                        if io.get_mut().write_all(response.as_bytes()).await.is_err() {
                            break;
                        }
                        continue;
                    }
                    let body = responses.get(&command).unwrap_or_else(|| {
                        panic!("command outside owned Python oracle: {command}")
                    });
                    let mut packet = format!("{command}\r\n").into_bytes();
                    packet.extend_from_slice(body);
                    packet.extend_from_slice(b"ch> ");
                    if io.get_mut().write_all(&packet).await.is_err() {
                        break;
                    }
                }
            });
            self.peer.lock().unwrap().push(peer);
            Ok(Box::new(Transport {
                io: client,
                dropped: self.dropped.clone(),
            }) as BoxTransport)
        })
    }
}
struct HeldCommit {
    inner: backend::Committer,
    kind: AtomicU8,
    entered: tokio::sync::Semaphore,
    release: tokio::sync::Semaphore,
}
impl ControlCommitter for HeldCommit {
    fn commit<'a>(&'a self, batch: ControlCommit) -> ControlFuture<'a, CommitOutcome> {
        Box::pin(async move {
            let kind = match batch.event.operation {
                OperationKind::GeneratorSweep => 1,
                OperationKind::OutputOff => 2,
                _ => 0,
            };
            if kind != 0
                && batch.event.state == OperationState::Succeeded
                && self
                    .kind
                    .compare_exchange(kind, 0, Ordering::AcqRel, Ordering::Acquire)
                    .is_ok()
            {
                self.entered.add_permits(1);
                self.release.acquire().await.unwrap().forget();
            }
            self.inner.commit(batch).await
        })
    }
}
async fn done(c: &Coordinator, target: &Target, id: &str) -> Value {
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let view = c.get(target, id, Some("owned-client-0123456789")).unwrap();
            if terminal(&view) {
                return view;
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .unwrap()
}
async fn submit(c: &Coordinator, target: &Target, payload: Value) -> Value {
    c.submit(
        target,
        &to_json_vec(&payload).unwrap(),
        Some("owned-client-0123456789"),
    )
    .await
    .unwrap()
}

#[tokio::test]
async fn held_terminal_commits_keep_reserved_off_and_both_actual_published_revisions_monotonic() {
    let dir = tempfile::tempdir().unwrap();
    let factory = Arc::new(Factory {
        dropped: Arc::new(AtomicBool::new(false)),
        wire: Arc::new(Mutex::new(vec![])),
        peer: Mutex::new(vec![]),
    });
    let mut config = crate::CoreConfig::replay(dir.path());
    config.devices.transport = Some(factory.clone());
    let core = Core::open(config).await.unwrap();
    let target = Target {
        session_id: "held-control".into(),
        stream_id: "source".into(),
    };
    core.archive().unwrap().create_session(json!({"id":target.session_id,"name":"Owned held-commit controller","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"contract_version":1,"metadata":{},"streams":[{"id":target.stream_id,"session_id":target.session_id,"device":"Owned memory","role":"spectrum_analyzer","state":"disconnected","level_unit":"dBm","settings":{"adapter":"tinysa-serial","start_hz":100000000,"stop_hz":101000000,"points":3,"rbw_hz":10000,"monitoring_instrument_context":"inspect"}}]})).await.unwrap();
    let auth = core
        .security
        .check(
            &axum::http::Method::POST,
            "/api/v1/sessions",
            &Default::default(),
            crate::security::ListenerRole::Plain,
        )
        .await
        .unwrap()
        .receipt();
    let guard = core.domains.get().unwrap().guard.clone();
    let grant = guard
        .capture_instrument_control(
            &ori_guard::ControlTarget {
                session_id: target.session_id.clone(),
                stream_id: target.stream_id.clone(),
            },
            None,
        )
        .unwrap();
    core.devices
        .as_ref()
        .unwrap()
        .start_instrument(
            &devices::Target::new(&target.session_id, &target.stream_id),
            gate(auth.clone(), grant, None),
            true,
        )
        .await
        .unwrap();
    let service = core.instrument_control.get().unwrap();
    let scope = service.capture(&target, auth, None).unwrap();
    let _admitted = service.remember(target.clone(), scope).unwrap();
    let held = Arc::new(HeldCommit {
        inner: backend::Committer {
            core: Arc::downgrade(&core),
        },
        kind: AtomicU8::new(0),
        entered: tokio::sync::Semaphore::new(0),
        release: tokio::sync::Semaphore::new(0),
    });
    let c = Coordinator::new(
        Arc::new(backend::Executor {
            core: Arc::downgrade(&core),
        }),
        held.clone(),
        Default::default(),
    )
    .unwrap();
    let acquire = submit(
        &c,
        &target,
        json!({"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}),
    )
    .await;
    let acquire = done(&c, &target, acquire["operation_id"].as_str().unwrap()).await;
    assert_eq!(acquire["state"], "succeeded");
    let secret = acquire["result"]["lease_id"].as_str().unwrap().to_owned();
    // The normal generator ACK is pending canonical publication. The reserved
    // off lane must still really disable the same owned transport and publish.
    held.kind.store(1, Ordering::Release);
    let normal = submit(
        &c,
        &target,
        json!({"operation":"generator.sweep","parameters":{"confirm":true},"lease_id":secret}),
    )
    .await;
    tokio::time::timeout(Duration::from_secs(3), held.entered.acquire())
        .await
        .unwrap()
        .unwrap()
        .forget();
    let off = submit(&c, &target, json!({"operation":"output.off"})).await;
    let off = done(&c, &target, off["operation_id"].as_str().unwrap()).await;
    let before = service.snapshot(&target).unwrap().status;
    held.release.add_permits(1);
    let normal = done(&c, &target, normal["operation_id"].as_str().unwrap()).await;
    let after = service.snapshot(&target).unwrap().status;
    let first = (off, normal, before, after);
    // Invert completion: a terminal off ACK is held, then the explicit new
    // generator operation executes. A later old off cannot publish RF=false.
    held.kind.store(2, Ordering::Release);
    let off = submit(&c, &target, json!({"operation":"output.off"})).await;
    tokio::time::timeout(Duration::from_secs(3), held.entered.acquire())
        .await
        .unwrap()
        .unwrap()
        .forget();
    let normal = submit(
        &c,
        &target,
        json!({"operation":"generator.sweep","parameters":{"confirm":true},"lease_id":secret}),
    )
    .await;
    let normal = done(&c, &target, normal["operation_id"].as_str().unwrap()).await;
    let before = service.snapshot(&target).unwrap().status;
    held.release.add_permits(1);
    let off = done(&c, &target, off["operation_id"].as_str().unwrap()).await;
    let after = service.snapshot(&target).unwrap().status;
    let renewed = submit(
        &c,
        &target,
        json!({"operation":"lease.renew","lease_id":secret}),
    )
    .await;
    let renewed = done(&c, &target, renewed["operation_id"].as_str().unwrap()).await;
    c.close().await.unwrap();
    core.close().await.unwrap();
    assert!(factory.dropped.load(Ordering::Acquire));
    let peers = std::mem::take(&mut *factory.peer.lock().unwrap());
    for peer in peers {
        peer.await.unwrap();
    }
    assert_eq!(
        first.0["state"], "succeeded",
        "reserved actual off: {}",
        first.0
    );
    assert_eq!(first.1["state"], "succeeded");
    assert_eq!(first.2["settings"]["output_enabled"], false);
    assert_eq!(first.3["settings"]["output_enabled"], false);
    assert_eq!(first.2["revision"], first.3["revision"]);
    assert_eq!(normal["state"], "succeeded", "new explicit on: {normal}");
    assert_eq!(off["state"], "succeeded");
    assert_eq!(before["settings"]["output_enabled"], true);
    assert_eq!(after["settings"]["output_enabled"], true);
    assert_eq!(before["revision"], after["revision"]);
    assert_eq!(
        renewed["result"]["revision"], after["revision"],
        "lease heartbeat must use latest committed revision"
    );
    assert_eq!(renewed["result"]["settings"]["output_enabled"], true);
}

#[tokio::test]
async fn actual_ready_to_bind_window_is_owned_until_stop_invalidates_the_original_setup() {
    let dir = tempfile::tempdir().unwrap();
    let factory = Arc::new(Factory {
        dropped: Arc::new(AtomicBool::new(false)),
        wire: Arc::new(Mutex::new(vec![])),
        peer: Mutex::new(vec![]),
    });
    let mut config = crate::CoreConfig::replay(dir.path());
    config.devices.transport = Some(factory.clone());
    let core = Core::open(config).await.unwrap();
    let target = Target {
        session_id: "bind-window".into(),
        stream_id: "source".into(),
    };
    core.archive().unwrap().create_session(json!({"id":target.session_id,"name":"Owned ready window","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"contract_version":1,"metadata":{},"streams":[{"id":target.stream_id,"session_id":target.session_id,"device":"Owned memory","role":"spectrum_analyzer","state":"disconnected","level_unit":"dBm","settings":{"adapter":"tinysa-serial","start_hz":100000000,"stop_hz":101000000,"points":3,"rbw_hz":10000,"period_s":1.0,"monitoring_instrument_context":"inspect"}}]})).await.unwrap();
    let auth = core
        .security
        .check(
            &axum::http::Method::POST,
            "/api/v1/sessions",
            &Default::default(),
            crate::security::ListenerRole::Plain,
        )
        .await
        .unwrap()
        .receipt();
    let service = core.instrument_control.get().unwrap();
    let barrier = Arc::new(BindBarrier {
        used: AtomicBool::new(false),
        entered: tokio::sync::Semaphore::new(0),
        release: tokio::sync::Semaphore::new(0),
        stop_requested: tokio::sync::Semaphore::new(0),
    });
    *service.bind_barrier.lock().unwrap() = Some(barrier.clone());
    let make_parts = |action: &str| {
        vec![
            "api".into(),
            "v1".into(),
            "sessions".into(),
            target.session_id.clone(),
            "streams".into(),
            target.stream_id.clone(),
            action.into(),
        ]
    };
    let source = core.clone();
    let parts = make_parts("start");
    let original = auth.clone();
    let first = tokio::spawn(async move {
        http::dispatch(
            &source,
            &axum::http::Method::POST,
            &parts,
            &Default::default(),
            &Default::default(),
            original,
            Some(json!({})),
        )
        .await
    });
    tokio::time::timeout(Duration::from_secs(3), barrier.entered.acquire())
        .await
        .unwrap()
        .unwrap()
        .forget();
    let cell = service.target_cell(&target).unwrap();
    let ready_window_locked = cell.lock.try_lock().is_err();
    let source = core.clone();
    let parts = make_parts("stop");
    let original = auth.clone();
    let stop = tokio::spawn(async move {
        http::dispatch(
            &source,
            &axum::http::Method::POST,
            &parts,
            &Default::default(),
            &Default::default(),
            original,
            Some(json!({})),
        )
        .await
    });
    tokio::time::timeout(Duration::from_secs(3), barrier.stop_requested.acquire())
        .await
        .unwrap()
        .unwrap()
        .forget();
    barrier.release.add_permits(1);
    let first = first.await.unwrap();
    let stopped = stop.await.unwrap();
    core.close().await.unwrap();
    let peers = std::mem::take(&mut *factory.peer.lock().unwrap());
    for peer in peers {
        peer.await.unwrap();
    }
    assert!(
        ready_window_locked,
        "actual safe-ready to bind must stay inside its original target lifecycle"
    );
    if let Ok(Some(response)) = first {
        assert_eq!(response.status(), axum::http::StatusCode::OK);
    }
    assert_eq!(
        stopped.unwrap().unwrap().status(),
        axum::http::StatusCode::OK
    );
}

#[tokio::test]
async fn disconnected_setup_waiter_keeps_owned_task_and_core_close_joins_before_writer_release() {
    let dir = tempfile::tempdir().unwrap();
    let factory = Arc::new(Factory {
        dropped: Arc::new(AtomicBool::new(false)),
        wire: Arc::new(Mutex::new(vec![])),
        peer: Mutex::new(vec![]),
    });
    let mut config = crate::CoreConfig::replay(dir.path());
    config.devices.transport = Some(factory.clone());
    let core = Core::open(config).await.unwrap();
    core.archive().unwrap().create_session(json!({"id":"owned-setup-close","name":"Owned disconnected setup","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"contract_version":1,"metadata":{},"streams":[{"id":"source","session_id":"owned-setup-close","device":"Owned memory","role":"spectrum_analyzer","state":"disconnected","level_unit":"dBm","settings":{"adapter":"tinysa-serial","start_hz":100000000,"stop_hz":101000000,"points":3,"rbw_hz":10000,"period_s":1.0,"monitoring_instrument_context":"inspect"}}]})).await.unwrap();
    let auth = core
        .security
        .check(
            &axum::http::Method::POST,
            "/api/v1/sessions",
            &Default::default(),
            crate::security::ListenerRole::Plain,
        )
        .await
        .unwrap()
        .receipt();
    let service = core.instrument_control.get().unwrap().clone();
    let barrier = Arc::new(BindBarrier {
        used: AtomicBool::new(false),
        entered: tokio::sync::Semaphore::new(0),
        release: tokio::sync::Semaphore::new(0),
        stop_requested: tokio::sync::Semaphore::new(0),
    });
    *service.bind_barrier.lock().unwrap() = Some(barrier.clone());
    let source = core.clone();
    let waiter = tokio::spawn(async move {
        let parts = [
            "api",
            "v1",
            "sessions",
            "owned-setup-close",
            "streams",
            "source",
            "start",
        ]
        .map(str::to_owned);
        http::dispatch(
            &source,
            &axum::http::Method::POST,
            &parts,
            &Default::default(),
            &Default::default(),
            auth,
            Some(json!({})),
        )
        .await
    });
    tokio::time::timeout(Duration::from_secs(3), barrier.entered.acquire())
        .await
        .unwrap()
        .unwrap()
        .forget();
    waiter.abort();
    assert!(waiter.await.unwrap_err().is_cancelled());
    let retained_owned = service.setups.lock().unwrap().len();
    core.request_shutdown();
    let source = core.clone();
    let close = tokio::spawn(async move { source.close().await });
    assert!(ori_runtime::lease::WriterLease::acquire(dir.path()).is_err());
    barrier.release.add_permits(1);
    close.await.unwrap().unwrap();
    assert_eq!(
        retained_owned, 1,
        "HTTP disconnect must not detach an untracked setup task"
    );
    assert!(factory.dropped.load(Ordering::Acquire));
    let _lease = ori_runtime::lease::WriterLease::acquire(dir.path()).unwrap();
    let peers = std::mem::take(&mut *factory.peer.lock().unwrap());
    for peer in peers {
        peer.await.unwrap();
    }
}

#[tokio::test]
async fn held_canonical_ready_write_cannot_hide_a_detached_inner_start_from_owned_close() {
    let dir = tempfile::tempdir().unwrap();
    let factory = Arc::new(Factory {
        dropped: Arc::new(AtomicBool::new(false)),
        wire: Arc::new(Mutex::new(vec![])),
        peer: Mutex::new(vec![]),
    });
    let mut config = crate::CoreConfig::replay(dir.path());
    config.devices.transport = Some(factory.clone());
    config.devices.connect_timeout = Duration::from_millis(100);
    config.devices.stop_timeout = Duration::from_millis(100);
    let core = Core::open(config).await.unwrap();
    core.archive().unwrap().create_session(json!({"id":"held-ready-sql","name":"Owned blocked ready write","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"contract_version":1,"metadata":{},"streams":[{"id":"source","session_id":"held-ready-sql","device":"Owned memory","role":"spectrum_analyzer","state":"disconnected","level_unit":"dBm","settings":{"adapter":"tinysa-serial","start_hz":100000000,"stop_hz":101000000,"points":3,"rbw_hz":10000,"period_s":1.0,"monitoring_instrument_context":"inspect"}}]})).await.unwrap();
    let auth = core
        .security
        .check(
            &axum::http::Method::POST,
            "/api/v1/sessions",
            &Default::default(),
            crate::security::ListenerRole::Plain,
        )
        .await
        .unwrap()
        .receipt();
    let held = rusqlite::Connection::open(dir.path().join("sessions.sqlite3")).unwrap();
    held.execute_batch("BEGIN IMMEDIATE").unwrap();
    let source = core.clone();
    let waiter = tokio::spawn(async move {
        let parts = [
            "api",
            "v1",
            "sessions",
            "held-ready-sql",
            "streams",
            "source",
            "start",
        ]
        .map(str::to_owned);
        http::dispatch(
            &source,
            &axum::http::Method::POST,
            &parts,
            &Default::default(),
            &Default::default(),
            auth,
            Some(json!({})),
        )
        .await
    });
    tokio::time::timeout(Duration::from_secs(2), async {
        while !factory.wire.lock().unwrap().iter().any(|c| c == "help") {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    core.security.begin_shutdown();
    let service = core.instrument_control.get().unwrap();
    let closed = tokio::time::timeout(Duration::from_secs(2), service.begin_shutdown().completed())
        .await
        .unwrap();
    let actually_retained = closed.is_err();
    let writer_retained = ori_runtime::lease::WriterLease::acquire(dir.path()).is_err();
    held.execute_batch("ROLLBACK").unwrap();
    assert!(waiter.await.unwrap().is_err());
    let core_closed = core.close().await;
    let peers = std::mem::take(&mut *factory.peer.lock().unwrap());
    for peer in peers {
        peer.await.unwrap();
    }
    assert!(factory.dropped.load(Ordering::Acquire));
    assert!(
        actually_retained,
        "owned close reported success with the original start still blocked in real SQL"
    );
    assert!(writer_retained);
    assert!(
        core_closed.is_err(),
        "a failed close must remain sticky after SQL is released"
    );
    assert!(ori_runtime::lease::WriterLease::acquire(dir.path()).is_err());
}

#[tokio::test]
async fn disconnected_tune_waiter_retains_original_revocation_wakeup_and_actual_join() {
    let dir = tempfile::tempdir().unwrap();
    let factory = Arc::new(Factory {
        dropped: Arc::new(AtomicBool::new(false)),
        wire: Arc::new(Mutex::new(vec![])),
        peer: Mutex::new(vec![]),
    });
    let mut config = crate::CoreConfig::replay(dir.path());
    config.devices.transport = Some(factory.clone());
    // Cancellation must wake the original exchange, not wait for this timeout.
    config.devices.io_limits.receiver_timeout = Duration::from_secs(10);
    let core = Core::open(config).await.unwrap();
    core.archive().unwrap().create_session(json!({"id":"owned-tune-close","name":"Owned disconnected tune","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"contract_version":1,"metadata":{},"streams":[{"id":"r","session_id":"owned-tune-close","device":"Owned receiver","role":"receiver","state":"disconnected","level_unit":"s_meter_count","settings":{"adapter":"ardv1-serial","frequency_hz":145500000,"mode":"FM","period_s":0.05}}]})).await.unwrap();
    let auth = core
        .security
        .check(
            &axum::http::Method::POST,
            "/api/v1/sessions",
            &Default::default(),
            crate::security::ListenerRole::Plain,
        )
        .await
        .unwrap()
        .receipt();
    let manager = core.devices.as_ref().unwrap();
    manager
        .start_stream(
            "owned-tune-close",
            "r",
            manager.public_gate(devices::Target::new("owned-tune-close", "r")),
        )
        .await
        .unwrap();
    let source = core.clone();
    let waiter = tokio::spawn(async move {
        let parts = [
            "api",
            "v1",
            "sessions",
            "owned-tune-close",
            "streams",
            "r",
            "tune",
        ]
        .map(str::to_owned);
        http::dispatch(
            &source,
            &axum::http::Method::POST,
            &parts,
            &Default::default(),
            &Default::default(),
            auth,
            Some(json!({"frequency_hz":146500000,"mode":"FM"})),
        )
        .await
    });
    tokio::time::timeout(Duration::from_secs(2), async {
        while !factory
            .wire
            .lock()
            .unwrap()
            .iter()
            .any(|c| c == "RF0146.50000")
        {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    waiter.abort();
    assert!(waiter.await.unwrap_err().is_cancelled());
    core.security.begin_shutdown();
    let woke = tokio::time::timeout(Duration::from_secs(1), async {
        while !factory.dropped.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .is_ok();
    core.close().await.unwrap();
    let peers = std::mem::take(&mut *factory.peer.lock().unwrap());
    for peer in peers {
        peer.await.unwrap();
    }
    assert!(
        woke,
        "dropping HTTP waiter removed the original revocation cleanup owner"
    );
    let _lease = ori_runtime::lease::WriterLease::acquire(dir.path()).unwrap();
}
