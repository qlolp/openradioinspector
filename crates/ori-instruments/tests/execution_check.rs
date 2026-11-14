//! Owned in-memory transports only. No port enumeration or hardware.
use ori_instruments::*;
use std::{
    io,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    task::{Context, Poll},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, DuplexStream, ReadBuf},
    sync::Notify,
};

#[derive(Default)]
struct Audit {
    written: Mutex<Vec<u8>>,
    read_pending: Notify,
    write_pending: Notify,
    dropped: Notify,
    calls: AtomicUsize,
}
struct OwnedWire {
    io: DuplexStream,
    audit: Arc<Audit>,
    allowed: Arc<AtomicBool>,
    revoke_after_read: Arc<AtomicBool>,
}
impl Drop for OwnedWire {
    fn drop(&mut self) {
        self.audit.dropped.notify_one();
    }
}
impl AsyncRead for OwnedWire {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        out: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let before = out.filled().len();
        let result = Pin::new(&mut this.io).poll_read(cx, out);
        if result.is_pending() && !this.audit.written.lock().unwrap().is_empty() {
            this.audit.read_pending.notify_one();
        }
        if out.filled().len() > before && this.revoke_after_read.load(Ordering::SeqCst) {
            this.allowed.store(false, Ordering::SeqCst);
        }
        result
    }
}
impl AsyncWrite for OwnedWire {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        let result = Pin::new(&mut this.io).poll_write(cx, bytes);
        match &result {
            Poll::Ready(Ok(count)) => this
                .audit
                .written
                .lock()
                .unwrap()
                .extend_from_slice(&bytes[..*count]),
            Poll::Pending => this.audit.write_pending.notify_one(),
            _ => {}
        }
        result
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_flush(cx)
    }
    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_shutdown(cx)
    }
}
struct Fixture {
    wire: OwnedWire,
    peer: DuplexStream,
    audit: Arc<Audit>,
    allowed: Arc<AtomicBool>,
    revoke_after_read: Arc<AtomicBool>,
}
impl Fixture {
    fn new(capacity: usize) -> Self {
        let (io, peer) = tokio::io::duplex(capacity);
        let audit = Arc::new(Audit::default());
        let allowed = Arc::new(AtomicBool::new(true));
        let revoke_after_read = Arc::new(AtomicBool::new(false));
        Self {
            wire: OwnedWire {
                io,
                audit: audit.clone(),
                allowed: allowed.clone(),
                revoke_after_read: revoke_after_read.clone(),
            },
            peer,
            audit,
            allowed,
            revoke_after_read,
        }
    }
    fn check(&self) -> ExecutionCheck {
        let allowed = self.allowed.clone();
        let audit = self.audit.clone();
        Arc::new(move || {
            audit.calls.fetch_add(1, Ordering::SeqCst);
            if allowed.load(Ordering::SeqCst) {
                Ok(())
            } else {
                Err(InstrumentError::UnsafeState(
                    "private-owner-token-must-never-leak".into(),
                ))
            }
        })
    }
}
fn receiver(wire: OwnedWire) -> ArDv1Actor {
    ArDv1Actor::spawn(
        ArDv1::new(
            wire,
            ReceiverConfig {
                frequency_hz: 145_500_000,
                mode: ReceiverMode::Fm,
                profile: ReceiverProfile::LegacyFmAm,
            },
            IoLimits::default(),
        )
        .unwrap(),
        4,
    )
    .unwrap()
}
fn analyzer(wire: OwnedWire) -> TinySaActor {
    TinySaActor::spawn(
        TinySa::new(
            wire,
            ReceiveConfig {
                start_hz: 100_000_000,
                stop_hz: 101_000_000,
                points: 3,
                rbw_hz: 10_000,
            },
            IoLimits::default(),
        )
        .unwrap(),
        4,
    )
    .unwrap()
}
async fn command(peer: &mut DuplexStream) -> String {
    let mut bytes = Vec::new();
    loop {
        let byte = peer.read_u8().await.unwrap();
        if byte == b'\r' {
            return String::from_utf8(bytes).unwrap();
        }
        bytes.push(byte);
        assert!(bytes.len() <= 200);
    }
}
async fn closed_receiver(actor: &ArDv1Actor, audit: &Audit) {
    assert!(matches!(
        actor
            .enqueue(ArDv1Request::Close)
            .unwrap()
            .result()
            .await
            .unwrap(),
        ArDv1Reply::Closed
    ));
    tokio::time::timeout(Duration::from_secs(1), audit.dropped.notified())
        .await
        .unwrap();
}
async fn closed_analyzer(actor: &TinySaActor, audit: &Audit) {
    assert!(matches!(
        actor
            .enqueue(TinySaRequest::Close)
            .unwrap()
            .result()
            .await
            .unwrap(),
        TinySaReply::Closed
    ));
    tokio::time::timeout(Duration::from_secs(1), audit.dropped.notified())
        .await
        .unwrap();
}

#[tokio::test]
async fn denied_original_check_sends_nothing_preserves_state_and_redacts_policy_details() {
    let fixture = Fixture::new(4096);
    let check = fixture.check();
    fixture.allowed.store(false, Ordering::SeqCst);
    let audit = fixture.audit.clone();
    let actor = analyzer(fixture.wire);
    let before = actor.state();
    let error = actor
        .enqueue_checked(TinySaRequest::Inspect, check)
        .unwrap()
        .result()
        .await
        .unwrap_err();
    assert!(matches!(error, InstrumentError::PolicyDenied));
    assert!(!error.to_string().contains("private-owner-token"));
    assert!(audit.written.lock().unwrap().is_empty());
    assert_eq!(actor.state().settings_source, before.settings_source);
    assert!(!actor.state().restart_required);
    closed_analyzer(&actor, &audit).await;
}

#[tokio::test]
async fn revocation_while_read_is_pending_prevents_reply_consumption_and_taints_connection() {
    let mut fixture = Fixture::new(4096);
    let check = fixture.check();
    let audit = fixture.audit.clone();
    let actor = receiver(fixture.wire);
    let pending = actor.enqueue_checked(ArDv1Request::Prepare, check).unwrap();
    assert_eq!(command(&mut fixture.peer).await, "RE1");
    audit.read_pending.notified().await;
    fixture.allowed.store(false, Ordering::SeqCst);
    fixture.peer.write_all(b"20\r\n").await.unwrap();
    let error = pending.result().await.unwrap_err();
    assert!(matches!(error, InstrumentError::PolicyDenied));
    assert_eq!(*audit.written.lock().unwrap(), b"RE1\r");
    assert!(actor.state().restart_required);
    assert!(actor.state().frequency_hz.is_none());
    assert!(audit.calls.load(Ordering::SeqCst) > 1);
    closed_receiver(&actor, &audit).await;
}

#[tokio::test]
async fn revocation_while_partial_write_is_pending_prevents_remaining_bytes() {
    let mut fixture = Fixture::new(2);
    let check = fixture.check();
    let audit = fixture.audit.clone();
    let actor = analyzer(fixture.wire);
    let pending = actor
        .enqueue_checked(TinySaRequest::Inspect, check)
        .unwrap();
    audit.write_pending.notified().await;
    fixture.allowed.store(false, Ordering::SeqCst);
    let mut first = [0; 2];
    fixture.peer.read_exact(&mut first).await.unwrap();
    assert_eq!(&first, b"in");
    let error = pending.result().await.unwrap_err();
    assert!(matches!(error, InstrumentError::PolicyDenied));
    assert_eq!(*audit.written.lock().unwrap(), b"in");
    assert!(actor.state().restart_required);
    assert_eq!(actor.state().device_mode, DeviceMode::Unknown);
    closed_analyzer(&actor, &audit).await;
    assert_eq!(
        fixture.peer.read_u8().await.unwrap_err().kind(),
        io::ErrorKind::UnexpectedEof
    );
}

#[tokio::test]
async fn revocation_during_the_final_ready_read_cannot_publish_a_successful_operation() {
    let mut fixture = Fixture::new(4096);
    let check = fixture.check();
    let audit = fixture.audit.clone();
    let actor = receiver(fixture.wire);
    let pending = actor.enqueue_checked(ArDv1Request::Prepare, check).unwrap();
    for (expected, reply) in [("RE1", "20\r\n"), ("VR", "20VR2504A\r\n")] {
        assert_eq!(command(&mut fixture.peer).await, expected);
        fixture.peer.write_all(reply.as_bytes()).await.unwrap();
    }
    assert_eq!(command(&mut fixture.peer).await, "RN");
    fixture.revoke_after_read.store(true, Ordering::SeqCst);
    fixture.peer.write_all(b"20RNsynthetic\r\n").await.unwrap();
    assert!(matches!(
        pending.result().await.unwrap_err(),
        InstrumentError::PolicyDenied
    ));
    assert!(actor.state().restart_required);
    closed_receiver(&actor, &audit).await;
}

#[tokio::test]
async fn scoped_check_is_removed_before_trusted_close_cleanup() {
    let mut fixture = Fixture::new(4096);
    let check = fixture.check();
    let audit = fixture.audit.clone();
    let actor = receiver(fixture.wire);
    let pending = actor.enqueue_checked(ArDv1Request::Prepare, check).unwrap();
    for (expected, reply) in [
        ("RE1", "20\r\n"),
        ("VR", "20VR2504A\r\n"),
        ("RN", "20RNsynthetic\r\n"),
    ] {
        assert_eq!(command(&mut fixture.peer).await, expected);
        fixture.peer.write_all(reply.as_bytes()).await.unwrap();
    }
    assert!(matches!(pending.result().await.unwrap(), ArDv1Reply::Ready));
    let calls = audit.calls.load(Ordering::SeqCst);
    fixture.allowed.store(false, Ordering::SeqCst);
    let close = actor.enqueue(ArDv1Request::Close).unwrap();
    assert_eq!(command(&mut fixture.peer).await, "EX");
    fixture.peer.write_all(b"20\r\n").await.unwrap();
    assert!(matches!(close.result().await.unwrap(), ArDv1Reply::Closed));
    tokio::time::timeout(Duration::from_secs(1), audit.dropped.notified())
        .await
        .unwrap();
    assert_eq!(audit.calls.load(Ordering::SeqCst), calls);
}

#[tokio::test]
async fn queued_original_epoch_is_checked_after_earlier_work_without_tainting_completed_io() {
    let mut fixture = Fixture::new(4096);
    let audit = fixture.audit.clone();
    let actor = receiver(fixture.wire);
    let first = actor.enqueue(ArDv1Request::Prepare).unwrap();
    assert_eq!(command(&mut fixture.peer).await, "RE1");
    let epoch = Arc::new(AtomicU64::new(1));
    let original = epoch.clone();
    let second = actor
        .enqueue_checked(
            ArDv1Request::Tune {
                frequency_hz: 146_000_000,
                mode: ReceiverMode::Fm,
            },
            Arc::new(move || {
                if original.load(Ordering::SeqCst) == 1 {
                    Ok(())
                } else {
                    Err(InstrumentError::PolicyDenied)
                }
            }),
        )
        .unwrap();
    epoch.store(2, Ordering::SeqCst);
    fixture.peer.write_all(b"20\r\n").await.unwrap();
    for (expected, reply) in [("VR", "20VR2504A\r\n"), ("RN", "20RNsynthetic\r\n")] {
        assert_eq!(command(&mut fixture.peer).await, expected);
        fixture.peer.write_all(reply.as_bytes()).await.unwrap();
    }
    assert!(matches!(first.result().await.unwrap(), ArDv1Reply::Ready));
    assert!(matches!(
        second.result().await.unwrap_err(),
        InstrumentError::PolicyDenied
    ));
    assert!(!actor.state().restart_required);
    assert_eq!(*audit.written.lock().unwrap(), b"RE1\rVR\rRN\r");
    let close = actor.enqueue(ArDv1Request::Close).unwrap();
    assert_eq!(command(&mut fixture.peer).await, "EX");
    fixture.peer.write_all(b"20\r\n").await.unwrap();
    assert!(matches!(close.result().await.unwrap(), ArDv1Reply::Closed));
    audit.dropped.notified().await;
}

#[tokio::test]
async fn a_pending_checked_receiver_does_not_block_an_independent_checked_analyzer() {
    let mut blocked = Fixture::new(4096);
    let blocked_check = blocked.check();
    let first_audit = blocked.audit.clone();
    let first = receiver(blocked.wire);
    let pending = first
        .enqueue_checked(ArDv1Request::Prepare, blocked_check)
        .unwrap();
    assert_eq!(command(&mut blocked.peer).await, "RE1");
    first_audit.read_pending.notified().await;
    let mut healthy = Fixture::new(4096);
    let check = healthy.check();
    let audit = healthy.audit.clone();
    let second = analyzer(healthy.wire);
    let ready = second
        .enqueue_checked(TinySaRequest::Inspect, check)
        .unwrap();
    for (expected, response) in [
        ("info", "tinySA Ultra ZS405\r\n"),
        (
            "version",
            "tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n",
        ),
        ("deviceid", "synthetic\r\n"),
        ("help", "commands: output mode capture\r\n"),
    ] {
        assert_eq!(command(&mut healthy.peer).await, expected);
        healthy
            .peer
            .write_all(format!("{expected}\r\n{response}ch> ").as_bytes())
            .await
            .unwrap();
    }
    assert!(matches!(
        ready.result().await.unwrap(),
        TinySaReply::Inspection(_)
    ));
    assert_eq!(*first_audit.written.lock().unwrap(), b"RE1\r");
    blocked.allowed.store(false, Ordering::SeqCst);
    blocked.peer.write_all(b"20\r\n").await.unwrap();
    assert!(matches!(
        pending.result().await.unwrap_err(),
        InstrumentError::PolicyDenied
    ));
    assert!(!second.state().restart_required);
    closed_receiver(&first, &first_audit).await;
    closed_analyzer(&second, &audit).await;
}
