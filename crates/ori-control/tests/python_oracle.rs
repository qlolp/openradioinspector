//! Independent Python Controller traces through real Rust owned adapters + SQLite.
use base64::{Engine, engine::general_purpose::STANDARD};
use ori_control::*;
use ori_instruments::{
    IoLimits, OperationInterval, ReceiveConfig, TinySa, TinySaActor, TinySaRequest,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    task::{Context, Poll},
};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader, ReadBuf};

fn fixture() -> Value {
    ori_contracts::parse_json_value(include_bytes!("fixtures/python_control.json"), 1024 * 1024)
        .unwrap()
}

struct Rig {
    _directory: tempfile::TempDir,
    archive: ori_archive::Archive,
    target: Target,
    backend: Arc<Backend>,
    store: Arc<Store>,
    control: Coordinator,
}

struct ImmediateStore(AtomicU64);
impl ControlCommitter for ImmediateStore {
    fn commit<'a>(&'a self, b: ControlCommit) -> ControlFuture<'a, CommitOutcome> {
        Box::pin(async move {
            Ok(CommitOutcome {
                event_sequence: self.0.fetch_add(1, Ordering::SeqCst) + 1,
                artifact_summary: b.artifact.as_ref().map(ClosedArtifact::summary),
            })
        })
    }
}

#[tokio::test]
async fn preview_reuse_is_bounded_to_its_actual_control_revision() {
    let r = rig().await;
    let first = r
        .control
        .submit(
            &r.target,
            br#"{"operation":"capture","parameters":{"save":false}}"#,
            None,
        )
        .await
        .unwrap();
    let id = first["operation_id"].as_str().unwrap();
    assert_eq!(done(&r.control, &r.target, id).await["state"], "succeeded");
    let same = r
        .control
        .submit(
            &r.target,
            br#"{"operation":"capture","parameters":{"save":false}}"#,
            None,
        )
        .await
        .unwrap();
    assert_eq!(same["operation_id"], id);
    let changed = r
        .control
        .submit(
            &r.target,
            br#"{"operation":"setting","parameters":{"name":"repeat","value":3}}"#,
            None,
        )
        .await
        .unwrap();
    assert_eq!(
        done(
            &r.control,
            &r.target,
            changed["operation_id"].as_str().unwrap()
        )
        .await["state"],
        "succeeded"
    );
    let fresh = r
        .control
        .submit(
            &r.target,
            br#"{"operation":"capture","parameters":{"save":false}}"#,
            None,
        )
        .await
        .unwrap();
    assert_ne!(fresh["operation_id"], id);
    assert_eq!(
        done(
            &r.control,
            &r.target,
            fresh["operation_id"].as_str().unwrap()
        )
        .await["state"],
        "succeeded"
    );
    r.control.close().await.unwrap();
    r.backend.shutdown().await;
    r.archive.close().await.unwrap();
}
#[tokio::test(start_paused = true)]
async fn lease_expiry_wakes_a_silent_owned_binary_read_before_its_90_second_io_deadline() {
    let target = Target {
        session_id: "owned-session".into(),
        stream_id: "owned-stream".into(),
    };
    let backend = Backend::new(target.clone()).await;
    let control = Coordinator::new(
        backend.clone(),
        Arc::new(ImmediateStore(AtomicU64::new(0))),
        ControlConfig::default(),
    )
    .unwrap();
    let acquire = control
        .submit(
            &target,
            br#"{"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}"#,
            Some("owned-client-0123456789"),
        )
        .await
        .unwrap();
    assert_eq!(
        done(&control, &target, acquire["operation_id"].as_str().unwrap()).await["state"],
        "succeeded"
    );
    *backend.block_command.lock().unwrap() = Some("sd_read TRACE.CSV".into());
    let read = control
        .submit(&target, br#"{"operation":"sd.read"}"#, None)
        .await
        .unwrap();
    backend.entered.acquire().await.unwrap().forget();
    tokio::time::advance(std::time::Duration::from_secs(31)).await;
    tokio::time::timeout(std::time::Duration::from_secs(1), async {
        while !backend.dropped.load(Ordering::SeqCst) {
            tokio::time::sleep(std::time::Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    let view = done(&control, &target, read["operation_id"].as_str().unwrap()).await;
    assert_eq!(view["state"], "unknown");
    assert!(backend.actor.state().restart_required);
    control.close().await.unwrap();
    backend.shutdown().await;
}
async fn rig() -> Rig {
    let directory = tempfile::tempdir().unwrap();
    let archive = ori_archive::Archive::open(ori_archive::ArchiveConfig::new(
        directory.path().join("archive.sqlite"),
    ))
    .unwrap();
    let mut session = ori_contracts::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        65536,
    )
    .unwrap()["session"]
        .clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    let target = Target {
        session_id: session["id"].as_str().unwrap().into(),
        stream_id: session["streams"][0]["id"].as_str().unwrap().into(),
    };
    archive.create_session(session).await.unwrap();
    let backend = Backend::new(target.clone()).await;
    let store = Arc::new(Store {
        archive: archive.clone(),
        summaries: Mutex::new(vec![]),
        fail_terminal_fk: AtomicBool::new(false),
    });
    let control =
        Coordinator::new(backend.clone(), store.clone(), ControlConfig::default()).unwrap();
    Rig {
        _directory: directory,
        archive,
        target,
        backend,
        store,
        control,
    }
}
async fn acquire(r: &Rig) -> String {
    let job = r
        .control
        .submit(
            &r.target,
            br#"{"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}"#,
            Some("owned-client-0123456789"),
        )
        .await
        .unwrap();
    let view = done(&r.control, &r.target, job["operation_id"].as_str().unwrap()).await;
    assert_eq!(view["state"], "succeeded");
    view["result"]["lease_id"].as_str().unwrap().into()
}
fn setting(value: u32, secret: &str) -> Vec<u8> {
    ori_contracts::to_json_vec(&json!({"operation":"setting","parameters":{"name":"repeat","value":value},"lease_id":secret})).unwrap()
}
struct HttpAuthority {
    original: OwnerReceipt,
    credential: Arc<AtomicBool>,
}
impl OwnerAuthority for HttpAuthority {
    fn target(&self) -> &Target {
        self.original.target()
    }
    fn worker_instance(&self) -> &str {
        self.original.worker_instance()
    }
    fn connection_id(&self) -> &str {
        self.original.connection_id()
    }
    fn revalidate(&self) -> ori_control::Result<()> {
        self.original.revalidate()?;
        if self.credential.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(ControlError::Denied)
        }
    }
}

#[tokio::test]
async fn queued_omitted_revisions_and_original_credential_revocation_match_python_worker_oracle() {
    let r = rig().await;
    let secret = acquire(&r).await;
    let oracle: Value = serde_json::from_str(include_str!("fixtures/python_queue.json")).unwrap();
    *r.backend.block_command.lock().unwrap() = Some("repeat 2".into());
    r.backend.commands.lock().unwrap().clear();
    let first = r
        .control
        .submit(&r.target, &setting(2, &secret), None)
        .await
        .unwrap();
    r.backend.entered.acquire().await.unwrap().forget();
    let second = r
        .control
        .submit(&r.target, &setting(3, &secret), None)
        .await
        .unwrap();
    let stale=r.control.submit(&r.target,&ori_contracts::to_json_vec(&json!({"operation":"setting","parameters":{"name":"repeat","value":4},"lease_id":secret,"expected_revision":oracle["omitted_then_stale"]["explicit_revision"]})).unwrap(),None).await.unwrap();
    let mut keys = first
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    keys.sort();
    assert_eq!(json!(keys), oracle["omitted_then_stale"]["admission_keys"]);
    r.backend.release.add_permits(1);
    let mut views = Vec::new();
    for job in [first, second, stale] {
        views.push(done(&r.control, &r.target, job["operation_id"].as_str().unwrap()).await);
    }
    assert_eq!(
        json!(views.iter().map(|v| v["state"].clone()).collect::<Vec<_>>()),
        oracle["omitted_then_stale"]["states"]
    );
    assert_eq!(
        json!(
            views
                .iter()
                .map(|v| v["result"]["revision"].clone())
                .collect::<Vec<_>>()
        ),
        oracle["omitted_then_stale"]["revisions"]
    );
    assert_eq!(
        json!(*r.backend.commands.lock().unwrap()),
        oracle["omitted_then_stale"]["commands"]
    );
    r.backend.commands.lock().unwrap().clear();
    let first = r
        .control
        .submit(&r.target, &setting(2, &secret), None)
        .await
        .unwrap();
    r.backend.entered.acquire().await.unwrap().forget();
    let credential = Arc::new(AtomicBool::new(true));
    let mut snapshot = r.backend.snapshot(&r.target).unwrap();
    snapshot.owner = Some(
        OwnerReceipt::from_backend(Arc::new(HttpAuthority {
            original: snapshot.owner.take().unwrap(),
            credential: credential.clone(),
        }))
        .unwrap(),
    );
    let second = r
        .control
        .submit_admitted(snapshot, &setting(3, &secret), None)
        .await
        .unwrap();
    credential.store(false, Ordering::SeqCst);
    r.backend.release.add_permits(1);
    let mut views = Vec::new();
    for job in [first, second] {
        views.push(done(&r.control, &r.target, job["operation_id"].as_str().unwrap()).await);
    }
    assert_eq!(
        json!(views.iter().map(|v| v["state"].clone()).collect::<Vec<_>>()),
        oracle["revoke_queued"]["states"]
    );
    assert_eq!(
        json!(*r.backend.commands.lock().unwrap()),
        oracle["revoke_queued"]["commands"]
    );
    r.control.close().await.unwrap();
    assert!(r.backend.dropped.load(Ordering::SeqCst));
    r.backend.shutdown().await;
    r.archive.close().await.unwrap();
}
#[tokio::test]
async fn cancelling_pending_real_io_joins_and_drops_original_transport_before_close_returns() {
    let r = rig().await;
    let secret = acquire(&r).await;
    *r.backend.block_command.lock().unwrap() = Some("repeat 2".into());
    let job = r
        .control
        .submit(&r.target, &setting(2, &secret), None)
        .await
        .unwrap();
    r.backend.entered.acquire().await.unwrap().forget();
    let id = job["operation_id"].as_str().unwrap();
    r.control.cancel(&r.target, id).await.unwrap();
    assert!(r.backend.dropped.load(Ordering::SeqCst));
    let view = done(&r.control, &r.target, id).await;
    assert_eq!(view["state"], "unknown");
    assert!(r.backend.actor.state().restart_required);
    r.control.close().await.unwrap();
    r.control.close().await.unwrap();
    r.backend.shutdown().await;
    r.archive.close().await.unwrap();
}
#[tokio::test]
async fn foreign_artifact_scope_rolls_back_terminal_event_and_never_publishes_reference() {
    let r = rig().await;
    r.store.fail_terminal_fk.store(true, Ordering::SeqCst);
    let job = r
        .control
        .submit(
            &r.target,
            br#"{"operation":"capture","parameters":{"save":true}}"#,
            None,
        )
        .await
        .unwrap();
    let id = job["operation_id"].as_str().unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            if r.control
                .status(&r.target)
                .unwrap()
                .get("control_error")
                .is_some()
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(1), async {
        while !r.backend.dropped.load(Ordering::SeqCst) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        r.control.get(&r.target, id, None).unwrap()["state"],
        "running"
    );
    assert_eq!(
        r.control.get(&r.target, id, None).unwrap()["result"],
        Value::Null
    );
    assert!(
        r.archive
            .list_artifacts(&r.target.session_id, &r.target.stream_id, 10, 0)
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(r.archive.events(0, 1000).await.unwrap().len(), 3);
    assert!(r.store.summaries.lock().unwrap().is_empty());
    assert!(r.control.close().await.is_err());
    assert!(r.backend.dropped.load(Ordering::SeqCst));
    assert!(r.control.close().await.is_err());
    r.backend.shutdown().await;
    r.archive.close().await.unwrap();
}
fn bytes(v: &Value) -> Vec<u8> {
    STANDARD.decode(v.as_str().unwrap()).unwrap()
}
struct DropIo {
    io: tokio::io::DuplexStream,
    dropped: Arc<AtomicBool>,
}
impl Drop for DropIo {
    fn drop(&mut self) {
        self.dropped.store(true, Ordering::SeqCst);
    }
}
impl AsyncRead for DropIo {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_read(cx, buf)
    }
}
impl AsyncWrite for DropIo {
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
struct Authority {
    target: Target,
    live: Arc<AtomicBool>,
}
impl OwnerAuthority for Authority {
    fn target(&self) -> &Target {
        &self.target
    }
    fn worker_instance(&self) -> &str {
        "actual-owned-Rust-loop"
    }
    fn connection_id(&self) -> &str {
        "original-owned-connection"
    }
    fn revalidate(&self) -> ori_control::Result<()> {
        if self.live.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(ControlError::Denied)
        }
    }
}
struct Backend {
    target: Target,
    actor: TinySaActor,
    serial: tokio::sync::Mutex<()>,
    revision: AtomicU64,
    live: Arc<AtomicBool>,
    dropped: Arc<AtomicBool>,
    commands: Arc<Mutex<Vec<String>>>,
    peer: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
    measurements: AtomicU64,
    audio: AtomicU64,
    block_command: Arc<Mutex<Option<String>>>,
    entered: Arc<tokio::sync::Semaphore>,
    release: Arc<tokio::sync::Semaphore>,
    active: AtomicBool,
}
struct Active<'a>(&'a AtomicBool);
impl Drop for Active<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}
impl Backend {
    async fn new(target: Target) -> Arc<Self> {
        let oracle = fixture();
        let mut responses = BTreeMap::<String, Vec<u8>>::new();
        responses.insert("info".into(), b"tinySA Ultra ZS405\r\n".to_vec());
        responses.insert(
            "version".into(),
            b"tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n".to_vec(),
        );
        responses.insert("deviceid".into(), b"synthetic\r\n".to_vec());
        let help = oracle["cases"][0]["result"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s.as_str().unwrap())
            .collect::<Vec<_>>()
            .join(" ")
            + "\r\n";
        responses.insert("help".into(), help.into_bytes());
        for case in oracle["cases"].as_array().unwrap() {
            for command in case["commands"].as_array().unwrap() {
                responses
                    .entry(command.as_str().unwrap().into())
                    .or_default();
            }
        }
        for command in [
            "output off",
            "mode input",
            "lna off",
            "attenuate auto",
            "rbw 10",
            "calc off",
            "repeat 2",
            "repeat 4",
        ] {
            responses.entry(command.into()).or_default();
        }
        responses.insert("capture".into(), b"\xf8\0".repeat(480 * 320));
        responses.insert("zero".into(), b"zero {level}\r\n174dBm\r\n".to_vec());
        responses.insert("rbw".into(), b"rbw {auto|...}\r\n10000Hz\r\n".to_vec());
        for case in oracle["cases"].as_array().unwrap() {
            let op = case["operation"].as_str().unwrap();
            match op {
                "sd.list" => {
                    responses.insert(
                        "sd_list".into(),
                        format!("{}\r\n", case["result"]["directory_text"].as_str().unwrap())
                            .into_bytes(),
                    );
                }
                "sd.read" => {
                    let payload = bytes(&case["result"]["artifact"]["data_base64"]);
                    let mut body = (payload.len() as u32).to_le_bytes().to_vec();
                    body.extend(payload);
                    responses.insert("sd_read TRACE.CSV".into(), body);
                }
                "sd.delete" => {
                    responses.insert(
                        "sd_delete TRACE.CSV".into(),
                        b"delete: TRACE.CSV OK\r\n".to_vec(),
                    );
                }
                "acquire.raw" | "acquire.zero" => {
                    let payload = bytes(&case["result"]["artifact"]["data_base64"]);
                    let doc = ori_contracts::parse_json_value(&payload, 65536).unwrap();
                    let command = case["commands"]
                        .as_array()
                        .unwrap()
                        .last()
                        .unwrap()
                        .as_str()
                        .unwrap();
                    responses.insert(command.into(), bytes(&doc["wire_base64"]));
                }
                "trace" if case["parameters"].get("action").is_none() => {
                    let payload = bytes(&case["result"]["artifact"]["data_base64"]);
                    let doc = ori_contracts::parse_json_value(&payload, 65536).unwrap();
                    responses.insert(
                        "trace 1 value".into(),
                        format!("{}\r\n", doc["device_response"].as_str().unwrap()).into_bytes(),
                    );
                }
                "marker" if case["parameters"].get("action").is_none() => {
                    responses.insert(
                        "marker 1".into(),
                        format!(
                            "{}\r\n",
                            case["result"]["device_response"].as_str().unwrap()
                        )
                        .into_bytes(),
                    );
                }
                _ => {}
            }
        }
        let (client, server) = tokio::io::duplex(4096);
        let dropped = Arc::new(AtomicBool::new(false));
        let commands = Arc::new(Mutex::new(vec![]));
        let log = commands.clone();
        let block_command = Arc::new(Mutex::new(None::<String>));
        let peer_block = block_command.clone();
        let entered = Arc::new(tokio::sync::Semaphore::new(0));
        let peer_entered = entered.clone();
        let release = Arc::new(tokio::sync::Semaphore::new(0));
        let peer_release = release.clone();
        let peer = tokio::spawn(async move {
            let mut io = BufReader::new(server);
            loop {
                let mut command = Vec::new();
                if io.read_until(b'\r', &mut command).await.unwrap() == 0 {
                    break;
                }
                let command = String::from_utf8(command[..command.len() - 1].to_vec()).unwrap();
                log.lock().unwrap().push(command.clone());
                let blocked = peer_block.lock().unwrap().as_deref() == Some(command.as_str());
                if blocked {
                    peer_entered.add_permits(1);
                    tokio::select! {
                        permit=peer_release.acquire()=>permit.unwrap().forget(),
                        eof=io.fill_buf()=>{assert!(eof.unwrap().is_empty(),"overlapping device exchanges");break;}
                    }
                }
                let body = responses
                    .get(&command)
                    .unwrap_or_else(|| panic!("command absent from Python transcript: {command}"));
                io.get_mut()
                    .write_all(format!("{command}\r\n").as_bytes())
                    .await
                    .unwrap();
                io.get_mut().write_all(body).await.unwrap();
                io.get_mut().write_all(b"ch> ").await.unwrap();
            }
        });
        let mut adapter = TinySa::new(
            DropIo {
                io: client,
                dropped: dropped.clone(),
            },
            ReceiveConfig {
                start_hz: 100000000,
                stop_hz: 101000000,
                points: 3,
                rbw_hz: 10000,
            },
            IoLimits::default(),
        )
        .unwrap();
        adapter.prepare().await.unwrap();
        adapter.inspect().await.unwrap();
        commands.lock().unwrap().clear();
        Arc::new(Self {
            target,
            actor: TinySaActor::spawn(adapter, 8).unwrap(),
            serial: tokio::sync::Mutex::new(()),
            revision: AtomicU64::new(0),
            live: Arc::new(AtomicBool::new(true)),
            dropped,
            commands,
            peer: tokio::sync::Mutex::new(Some(peer)),
            measurements: AtomicU64::new(0),
            audio: AtomicU64::new(0),
            block_command,
            entered,
            release,
            active: AtomicBool::new(false),
        })
    }
    async fn shutdown(&self) {
        let _ = match self.actor.enqueue(TinySaRequest::Close) {
            Ok(p) => p.result().await,
            Err(e) => Err(e),
        };
        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            while !self.dropped.load(Ordering::SeqCst) {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        if let Some(peer) = self.peer.lock().await.take() {
            peer.await.unwrap();
        }
        self.live.store(false, Ordering::SeqCst);
    }
}
impl ControlExecutor for Backend {
    fn snapshot(&self, t: &Target) -> ori_control::Result<CachedInstrument> {
        if t != &self.target {
            return Err(ControlError::NotFound);
        }
        let revision = self.revision.load(Ordering::SeqCst);
        Ok(CachedInstrument {
            target: t.clone(),
            family: InstrumentFamily::TinySa,
            status: status_from_tinysa(&self.actor.state(), "original-owned-connection", revision),
            control_revision: revision,
            owner: Some(OwnerReceipt::from_backend(Arc::new(Authority {
                target: t.clone(),
                live: self.live.clone(),
            }))?),
            supported_operations: OperationKind::ALL.iter().copied().collect(),
            guard_claimed: false,
            coverage_bridge_available: false,
            science_available: true,
            audio_available: true,
        })
    }
    fn execute<'a>(&'a self, r: ExecutionRequest) -> ControlFuture<'a, ExecutionOutcome> {
        Box::pin(async move {
            let _serial = self.serial.lock().await;
            let execution = BoundExecution::bind(
                r.owner,
                r.expected_revision,
                self.revision.load(Ordering::SeqCst),
            )?;
            let plan = plan_operation(&r.operation, &self.actor.state())?;
            self.active.store(true, Ordering::SeqCst);
            let _active = Active(&self.active);
            let applied_revision =
                if plan.revision_effect == RevisionEffect::AdvanceAfterAcknowledgement {
                    execution
                        .control_revision()
                        .checked_add(1)
                        .ok_or(ControlError::Conflict("revision overflow"))?
                } else {
                    execution.control_revision()
                };
            let started_at = chrono::Utc::now();
            let mut steps = Vec::new();
            for command in plan.commands {
                let AdapterCommand::TinySa(command) = command else {
                    panic!("no receiver control invented")
                };
                let pending = self
                    .actor
                    .enqueue_checked(command, r.check.clone())
                    .map_err(|_| ControlError::Denied)?;
                let reply = pending.result().await.map_err(|error| {
                    classify_adapter_error(error, self.actor.state().restart_required)
                })?;
                steps.push(ExecutedStep {
                    reply: AdapterReply::TinySa(reply),
                    interval: OperationInterval {
                        started_at,
                        ended_at: chrono::Utc::now(),
                    },
                });
            }
            self.revision.store(applied_revision, Ordering::SeqCst);
            Ok(ExecutionOutcome {
                execution,
                applied_revision,
                status: status_from_tinysa(
                    &self.actor.state(),
                    "original-owned-connection",
                    applied_revision,
                ),
                steps,
                interval: OperationInterval {
                    started_at,
                    ended_at: chrono::Utc::now(),
                },
            })
        })
    }
    fn cancel_and_join<'a>(
        &'a self,
        r: CancellationRequest,
    ) -> ControlFuture<'a, CancellationOutcome> {
        Box::pin(async move {
            assert_eq!(r.owner.worker_instance(), "actual-owned-Rust-loop");
            if self.active.load(Ordering::SeqCst) {
                self.actor.cancel();
            }
            let _ = match self.actor.enqueue(TinySaRequest::Close) {
                Ok(p) => p.result().await,
                Err(e) => Err(e),
            };
            let _serial = self.serial.lock().await;
            while !self.dropped.load(Ordering::SeqCst) {
                tokio::task::yield_now().await;
            }
            Ok(CancellationOutcome {
                original_worker_joined: true,
                original_transport_dropped: true,
                tainted: self.actor.state().restart_required,
            })
        })
    }
    fn analyze<'a>(&'a self, r: MeasurementRequest) -> ControlFuture<'a, MeasurementOutcome> {
        Box::pin(async move {
            self.measurements.fetch_add(1, Ordering::SeqCst);

            assert_eq!(r.rbw_applied_hz, 10000.);
            let oracle = fixture();
            let case = oracle["cases"]
                .as_array()
                .unwrap()
                .iter()
                .find(|v| {
                    v["operation"] == "measurement"
                        && v["parameters"]["method"] == r.method.as_str()
                })
                .unwrap();
            let artifact = ori_contracts::parse_json_value(
                &bytes(&case["result"]["artifact"]["data_base64"]),
                65536,
            )
            .unwrap();
            let actual = raw_document(&r.source)?;
            for key in [
                "frequencies_hz",
                "levels",
                "wire_base64",
                "zero_db",
                "requested_stop_hz",
            ] {
                assert_eq!(actual[key], artifact["source"][key]);
            }
            Ok(MeasurementOutcome {
                result: case["result"]["measurement"].clone(),
            })
        })
    }
    fn audio_inputs(&self) -> ControlFuture<'_, AudioInputs> {
        Box::pin(async {
            Ok(AudioInputs {
                inputs: vec![AudioInput {
                    id: 0,
                    label: "owned-memory-input".into(),
                    channels: 1,
                    default_sample_rate_hz: 8000,
                }],
            })
        })
    }
    fn capture_audio<'a>(&'a self, r: AudioRequest) -> ControlFuture<'a, AudioOutcome> {
        Box::pin(async move {
            (r.check)().map_err(|_| ControlError::Denied)?;
            self.audio.fetch_add(1, Ordering::SeqCst);
            let at = chrono::Utc::now();
            Ok(AudioOutcome {
                wav: bytes(&fixture()["wav_base64"]),
                interval: OperationInterval {
                    started_at: at,
                    ended_at: at,
                },
            })
        })
    }
}
struct Store {
    archive: ori_archive::Archive,
    summaries: Mutex<Vec<Value>>,
    fail_terminal_fk: AtomicBool,
}
impl ControlCommitter for Store {
    fn commit<'a>(&'a self, b: ControlCommit) -> ControlFuture<'a, CommitOutcome> {
        Box::pin(async move {
            let mut mutations = Vec::new();
            let summary = b.artifact.as_ref().map(ClosedArtifact::summary);
            if let Some(a) = b.artifact {
                mutations.push(ori_archive::Mutation::Artifact(ori_archive::Artifact {
                    id: a.id().into(),
                    session_id: a.target().session_id.clone(),
                    stream_id: if self.fail_terminal_fk.load(Ordering::SeqCst) {
                        "missing-owned-stream".into()
                    } else {
                        a.target().stream_id.clone()
                    },
                    schema_version: 1,
                    kind: serde_json::to_value(a.kind())
                        .unwrap()
                        .as_str()
                        .unwrap()
                        .into(),
                    mime_type: a.mime_type().into(),
                    filename: a.filename().into(),
                    sha256: a.sha256().into(),
                    created_at: a.summary()["created_at"].as_str().unwrap().into(),
                    metadata: a.metadata().clone(),
                    payload: a.into_payload(),
                }));
            }
            mutations.push(ori_archive::Mutation::Event(ori_archive::EventDraft{event_type:b.event.event_type.into(),occurred_at:b.event.occurred_at.to_rfc3339(),session_id:Some(b.event.target.session_id),payload:json!({"operation_id":b.event.operation_id,"stream_id":b.event.target.stream_id,"operation":b.event.operation,"fields":b.event.fields})}));
            if b.event.state.terminal() && self.fail_terminal_fk.load(Ordering::SeqCst) {
                mutations.reverse();
            }
            let committed = self
                .archive
                .apply(mutations)
                .await
                .map_err(|_| ControlError::Persistence("owned SQLite commit failed".into()))?;
            if let Some(summary) = &summary {
                self.summaries.lock().unwrap().push(summary.clone());
            }
            Ok(CommitOutcome {
                event_sequence: committed.last_sequence.unwrap(),
                artifact_summary: summary,
            })
        })
    }
}
fn normalize(v: &mut Value) {
    match v {
        Value::Object(obj) => {
            for key in [
                "connection_id",
                "segment_id",
                "started_at",
                "ended_at",
                "occurred_at",
                "created_at",
                "lease_seconds",
                "restart_required",
                "source_sha256",
                "source_format",
                "width",
                "height",
            ] {
                obj.remove(key);
            }
            if obj.contains_key("lease_id") {
                obj.insert("lease_id".into(), json!("<private synthetic lease>"));
            }
            for value in obj.values_mut() {
                normalize(value);
            }
        }
        Value::Array(a) => {
            for v in a {
                normalize(v);
            }
        }
        Value::Number(n) => {
            if let Some(f) = n.as_f64()
                && f.fract() == 0.
                && f >= i64::MIN as f64
                && f <= i64::MAX as f64
            {
                *v = json!(f as i64);
            }
        }
        _ => {}
    }
}
async fn done(c: &Coordinator, t: &Target, id: &str) -> Value {
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        loop {
            let v = c.get(t, id, Some("owned-client-0123456789")).unwrap();
            if matches!(
                v["state"].as_str(),
                Some("succeeded" | "failed" | "unknown" | "cancelled")
            ) {
                return v;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap()
}

#[tokio::test]
async fn all_29_operations_match_actual_python_transcripts_and_artifacts_commit_to_reopenable_sqlite()
 {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    let archive = ori_archive::Archive::open(ori_archive::ArchiveConfig::new(&path)).unwrap();
    let mut session = ori_contracts::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        65536,
    )
    .unwrap()["session"]
        .clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    let target = Target {
        session_id: session["id"].as_str().unwrap().into(),
        stream_id: session["streams"][0]["id"].as_str().unwrap().into(),
    };
    archive.create_session(session).await.unwrap();
    let backend = Backend::new(target.clone()).await;
    let store = Arc::new(Store {
        archive: archive.clone(),
        summaries: Mutex::new(vec![]),
        fail_terminal_fk: AtomicBool::new(false),
    });
    let c = Coordinator::new(backend.clone(), store.clone(), ControlConfig::default()).unwrap();
    let oracle = fixture();
    let mut secret = None;
    let mut kinds = std::collections::BTreeSet::new();
    for case in oracle["cases"].as_array().unwrap() {
        let op = case["operation"].as_str().unwrap();
        kinds.insert(op);
        let mut request = json!({"operation":op,"parameters":case["parameters"]});
        if let Some(secret) = &secret {
            request["lease_id"] = json!(secret);
        }
        let from = backend.commands.lock().unwrap().len();
        let submitted = c
            .submit(
                &target,
                &ori_contracts::to_json_vec(&request).unwrap(),
                Some("owned-client-0123456789"),
            )
            .await
            .unwrap();
        let view = done(&c, &target, submitted["operation_id"].as_str().unwrap()).await;
        assert_eq!(view["state"], "succeeded", "{op}: {view}");
        let mut actual = view["result"].clone();
        if let Some(token) = actual["lease_id"].as_str() {
            secret = Some(token.to_owned());
        }
        if matches!(op, "lease.release" | "monitor.resume") {
            secret = None;
        }
        assert_eq!(
            json!(backend.commands.lock().unwrap()[from..]),
            case["commands"],
            "wire order {op}"
        );
        let mut expected = case["result"].clone();
        if let Some(summary) = actual.get("artifact").cloned() {
            let item = archive
                .get_artifact(
                    &target.session_id,
                    &target.stream_id,
                    summary["id"].as_str().unwrap(),
                )
                .await
                .unwrap()
                .unwrap();
            let expected_bytes = bytes(&expected["artifact"]["data_base64"]);
            if matches!(op, "sd.read" | "audio.capture") {
                assert_eq!(item.payload, expected_bytes, "source bytes {op}");
            } else {
                let mut a =
                    ori_contracts::parse_json_value(&item.payload, 8 * 1024 * 1024).unwrap();
                let mut b =
                    ori_contracts::parse_json_value(&expected_bytes, 8 * 1024 * 1024).unwrap();
                normalize(&mut a);
                normalize(&mut b);
                assert_eq!(a, b, "artifact document {op}");
            }
            for key in ["kind", "mime_type", "filename"] {
                assert_eq!(
                    summary[key], expected["artifact"][key],
                    "artifact {key} {op}"
                );
            }
            actual.as_object_mut().unwrap().remove("artifact");
            expected.as_object_mut().unwrap().remove("artifact");
        }
        if op == "capture" {
            let preview = actual.as_object_mut().unwrap().remove("preview").unwrap();
            assert_eq!(preview["kind"], "screen");
            assert_eq!(preview["metadata"]["source_format"], "rgb565be");
            assert!(bytes(&preview["data_base64"]).starts_with(b"\x89PNG"));
            expected.as_object_mut().unwrap().remove("artifact");
        }
        normalize(&mut actual);
        normalize(&mut expected);
        assert_eq!(actual, expected, "result envelope {op}");
    }
    assert_eq!(kinds.len(), 29);
    assert_eq!(backend.measurements.load(Ordering::SeqCst), 5);
    assert_eq!(backend.audio.load(Ordering::SeqCst), 1);
    let saved = c
        .submit(
            &target,
            br#"{"operation":"capture","parameters":{"save":true}}"#,
            Some("owned-client-0123456789"),
        )
        .await
        .unwrap();
    let saved = done(&c, &target, saved["operation_id"].as_str().unwrap()).await;
    assert_eq!(saved["state"], "succeeded");
    let png_id = saved["result"]["artifact"]["id"]
        .as_str()
        .unwrap()
        .to_owned();
    let png = archive
        .get_artifact(&target.session_id, &target.stream_id, &png_id)
        .await
        .unwrap()
        .unwrap();
    assert!(png.payload.starts_with(b"\x89PNG"));
    let events = archive.events(0, 1000).await.unwrap();
    assert_eq!(
        events.len(),
        1 + (oracle["cases"].as_array().unwrap().len() + 1) * 3
    );
    assert!(events.iter().all(|e| {
        !String::from_utf8(ori_contracts::to_json_vec(e).unwrap())
            .unwrap()
            .contains("lease_id")
    }));
    c.close().await.unwrap();
    backend.shutdown().await;
    archive.close().await.unwrap();
    let reopened = ori_archive::Archive::open(ori_archive::ArchiveConfig::new(path)).unwrap();
    assert_eq!(
        reopened
            .get_artifact(&target.session_id, &target.stream_id, &png_id)
            .await
            .unwrap()
            .unwrap()
            .payload,
        png.payload
    );
    assert_eq!(
        reopened
            .list_sweeps(&target.session_id, None, 100)
            .await
            .unwrap()
            .len(),
        0
    );
    assert_eq!(reopened.events(0, 1000).await.unwrap().len(), events.len());
    reopened.close().await.unwrap();
}
