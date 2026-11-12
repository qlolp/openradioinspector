//! Actual dispatch futures, private synthetic credentials and owned duplex I/O.
use super::*;
use crate::{
    CoreConfig,
    devices::{BoxTransport, DeviceFuture, DeviceResult, Target, TransportFactory},
};
use ori_contracts::DeviceStream;
use ori_runtime::security::{Credentials, TlsIdentity};
use std::{
    io,
    pin::Pin,
    sync::{
        Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    task::{Context, Poll, Waker},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf},
    sync::Notify,
};

struct Fixture {
    _root: tempfile::TempDir,
    core: Arc<Core>,
    token: String,
}
impl Fixture {
    async fn new(factory: Option<Arc<Factory>>) -> Self {
        let root = tempfile::tempdir().unwrap();
        let security = root.path().join("security");
        TlsIdentity::create(&security, vec!["localhost".into()]).unwrap();
        let token = Credentials::open(&security)
            .unwrap()
            .issue("synthetic manual request", 1_794_355_200)
            .unwrap();
        let mut config = CoreConfig::replay(root.path());
        config.lan_security = Some(crate::LanSecurityConfig {
            directory: security,
            hosts: vec!["localhost:8770".into()],
            origins: vec!["https://localhost:8770".into()],
        });
        if let Some(factory) = factory {
            config.devices.transport = Some(Arc::new(factory));
        }
        config.devices.io_limits.write_timeout = Duration::from_secs(2);
        let core = Core::open(config).await.unwrap();
        core.archive().unwrap().create_session(json!({"id":"manual","name":"Synthetic manual scope","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"streams":[
            {"id":"spectrum","device":"Synthetic source","role":"spectrum_analyzer","level_unit":"dBm","settings":{"adapter":"tinysa-sim","start_hz":145_500_000,"stop_hz":145_700_000,"points":3}},
            {"id":"receiver","device":"Synthetic duplex receiver","role":"receiver","level_unit":"s_meter_count","port":"owned-test-only","settings":{"adapter":"ardv1-serial","frequency_hz":145_500_000,"period_s":0.1}}
        ]})).await.unwrap();
        let session = &core.domain().unwrap().session;
        session.create_candidate("manual",json!({"id":"candidate","source_stream_id":"spectrum","interval":{"started_at":"2026-11-11T00:00:00.000000Z","ended_at":"2026-11-11T00:00:01.000000Z"},"center_frequency_hz":145_600_000,"level_unit":"dBm","peak_level":-0.0,"score":1.0000000000000002})).await.unwrap();
        session
            .enqueue_investigation(
                "manual",
                "candidate",
                json!({"id":"job","target_stream_id":"receiver","mode":"FM"}),
            )
            .await
            .unwrap();
        Self {
            _root: root,
            core,
            token,
        }
    }
    fn request(&self, path: &str, value: Value) -> Request {
        let bytes = to_json_vec(&value).unwrap();
        let mut request = Request::builder()
            .method(Method::POST)
            .uri(path)
            .header(header::HOST, "localhost:8770")
            .header(header::AUTHORIZATION, format!("Bearer {}", self.token))
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::CONTENT_LENGTH, bytes.len())
            .body(Body::from(bytes))
            .unwrap();
        request
            .extensions_mut()
            .insert(crate::security::ListenerRole::Tls);
        request
    }
    async fn revoke(&self) {
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, "localhost:8770".parse().unwrap());
        headers.insert(
            header::AUTHORIZATION,
            format!("Bearer {}", self.token).parse().unwrap(),
        );
        let context = self
            .core
            .security
            .check(
                &Method::GET,
                "/api/v1/health",
                &headers,
                crate::security::ListenerRole::Tls,
            )
            .await
            .unwrap();
        self.core
            .security
            .revoke(&context, &json!({}))
            .await
            .unwrap();
    }
    async fn job(&self) -> Value {
        self.core
            .domain()
            .unwrap()
            .session
            .get_investigation("manual", "job")
            .await
            .unwrap()
    }
}
async fn admitted(core: &Core) {
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let drain = core.admission.wait();
            tokio::pin!(drain);
            if futures_util::poll!(drain).is_pending() {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}
#[tokio::test]
async fn revoked_manual_request_waiting_for_coordinator_leaves_queue_and_ledger_unchanged() {
    for (path, payload) in [
        (
            "/api/v1/sessions/manual/investigations/job/start",
            json!({}),
        ),
        (
            "/api/v1/sessions/manual/investigations/start-next",
            json!({"confirm":true,"investigation_id":"job"}),
        ),
    ] {
        let fixture = Fixture::new(None).await;
        let before = fixture.job().await;
        let counts = fixture
            .core
            .archive()
            .unwrap()
            .registry_counts()
            .await
            .unwrap();
        let coordinator = fixture.core.archive().unwrap().domain_coordinator();
        let held = coordinator.lock().await;
        let request = fixture.request(path, payload);
        let core = fixture.core.clone();
        let result = tokio::spawn(async move {
            dispatch(core, "127.0.0.1:12000".parse().unwrap(), request, false).await
        });
        admitted(&fixture.core).await;
        fixture.revoke().await;
        drop(held);
        let _ = result.await.unwrap();
        let after = fixture.job().await;
        let final_counts = fixture
            .core
            .archive()
            .unwrap()
            .registry_counts()
            .await
            .unwrap();
        fixture.core.close().await.unwrap();
        assert_eq!(
            after, before,
            "revoked {path} changed the queued investigation"
        );
        assert_eq!(
            final_counts, counts,
            "revoked {path} wrote an event or raw source"
        );
    }
}

#[derive(Default)]
struct Factory {
    blocked: AtomicBool,
    entered: AtomicBool,
    wake: Mutex<Option<Waker>>,
    changed: Notify,
    drops: Arc<AtomicUsize>,
    target_written: Arc<AtomicBool>,
    peers: Mutex<Vec<tokio::task::JoinHandle<()>>>,
}
impl Factory {
    async fn entered(&self) {
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let changed = self.changed.notified();
                tokio::pin!(changed);
                changed.as_mut().enable();
                if self.entered.load(Ordering::Acquire) {
                    break;
                }
                changed.await;
            }
        })
        .await
        .unwrap();
    }
    fn release(&self) {
        self.blocked.store(false, Ordering::Release);
        if let Some(wake) = self.wake.lock().unwrap().take() {
            wake.wake();
        }
    }
    async fn join(&self) {
        let peers = std::mem::take(&mut *self.peers.lock().unwrap());
        for peer in peers {
            tokio::time::timeout(Duration::from_secs(3), peer)
                .await
                .unwrap()
                .unwrap();
        }
    }
}
struct Transport {
    io: tokio::io::DuplexStream,
    factory: Arc<Factory>,
}
impl Drop for Transport {
    fn drop(&mut self) {
        self.factory.drops.fetch_add(1, Ordering::AcqRel);
    }
}
impl AsyncRead for Transport {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_read(cx, buf)
    }
}
impl AsyncWrite for Transport {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        let own = self.get_mut();
        if own.factory.blocked.load(Ordering::Acquire) && bytes.starts_with(b"RF") {
            *own.factory.wake.lock().unwrap() = Some(cx.waker().clone());
            own.factory.entered.store(true, Ordering::Release);
            own.factory.changed.notify_waiters();
            return Poll::Pending;
        }
        Pin::new(&mut own.io).poll_write(cx, bytes)
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_flush(cx)
    }
    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_shutdown(cx)
    }
}
impl TransportFactory for Arc<Factory> {
    fn identity(&self, _: &DeviceStream) -> DeviceResult<String> {
        Ok("manual-owned-duplex".into())
    }
    fn open<'a>(&'a self, _: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        Box::pin(async move {
            let (host, mut peer) = tokio::io::duplex(256);
            let written = self.target_written.clone();
            let task = tokio::spawn(async move {
                let mut command = Vec::new();
                while let Ok(byte) = peer.read_u8().await {
                    if byte != b'\r' {
                        command.push(byte);
                        continue;
                    }
                    let command = String::from_utf8(std::mem::take(&mut command)).unwrap();
                    if command == "RF0145.60000" {
                        written.store(true, Ordering::Release);
                    }
                    let reply = match command.as_str() {
                        "VR" => "20VR2504A\r\n",
                        "RN" => "20RNSYNTHETIC\r\n",
                        "LM" => "20LM1371\r\n",
                        _ => "20\r\n",
                    };
                    if peer.write_all(reply.as_bytes()).await.is_err() {
                        break;
                    }
                }
            });
            self.peers.lock().unwrap().push(task);
            Ok(Box::new(Transport {
                io: host,
                factory: self.clone(),
            }) as BoxTransport)
        })
    }
}
#[tokio::test]
async fn dropping_actual_manual_dispatch_future_retains_tune_and_terminal_commit() {
    for revoke in [false, true] {
        let factory = Arc::new(Factory::default());
        let fixture = Fixture::new(Some(factory.clone())).await;
        fixture
            .core
            .devices
            .as_ref()
            .unwrap()
            .start_stream(
                "manual",
                "receiver",
                fixture
                    .core
                    .devices
                    .as_ref()
                    .unwrap()
                    .public_gate(Target::new("manual", "receiver")),
            )
            .await
            .unwrap();
        factory.blocked.store(true, Ordering::Release);
        let request = fixture.request(
            "/api/v1/sessions/manual/investigations/job/start",
            json!({}),
        );
        let core = fixture.core.clone();
        let waiter = tokio::spawn(async move {
            dispatch(core, "127.0.0.1:12000".parse().unwrap(), request, false).await
        });
        factory.entered().await;
        let tuning = fixture.job().await;
        // This abort drops the actual dispatch scope, not merely a TCP client.
        waiter.abort();
        assert!(waiter.await.unwrap_err().is_cancelled());
        if revoke {
            fixture.revoke().await;
        }
        factory.release();
        let drained = tokio::time::timeout(Duration::from_secs(3), fixture.core.admission.wait())
            .await
            .is_ok();
        let final_job = fixture.job().await;
        fixture.core.close().await.unwrap();
        factory.join().await;
        assert_eq!(tuning["state"], "tuning");
        assert!(drained);
        assert_eq!(
            final_job["state"],
            if revoke { "failed" } else { "listening" },
            "dropped dispatch abandoned its terminal commit"
        );
        assert_eq!(
            factory.target_written.load(Ordering::Acquire),
            !revoke,
            "revoked queued RF bytes were written"
        );
        assert_eq!(factory.drops.load(Ordering::Acquire), 1);
    }
}
