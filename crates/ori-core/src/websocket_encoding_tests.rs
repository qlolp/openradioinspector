//! Actual WebSocket capacity contention, with controlled semaphore ownership.
use super::*;
use crate::{CoreConfig, security::ListenerRole};
use axum::http::{HeaderMap, Method};
use futures_util::{SinkExt, StreamExt, poll};
use ori_archive::{Archive, ArchiveConfig, EventDraft, Mutation};
use serde_json::json;
use std::task::Poll;
use tokio::{net::TcpListener, task::JoinHandle};
use tokio_tungstenite::{
    MaybeTlsStream, WebSocketStream,
    tungstenite::{Message as ClientMessage, client::IntoClientRequest},
};

type ClientSocket = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;
struct Server {
    core: Arc<Core>,
    url: String,
    task: Option<JoinHandle<crate::Result<()>>>,
    directory: tempfile::TempDir,
}
impl Server {
    async fn start() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let archive = Archive::open(ArchiveConfig::new(
            directory.path().join("sessions.sqlite3"),
        ))
        .unwrap();
        let levels = (0..4096)
            .map(|index| -100.0 + (index % 17) as f64)
            .collect::<Vec<_>>();
        archive
            .apply(
                (0..2)
                    .map(|index| {
                        Mutation::Event(EventDraft::now(
                            "fixture",
                            None,
                            json!({"index":index,"levels":levels}),
                        ))
                    })
                    .collect(),
            )
            .await
            .unwrap();
        archive.close().await.unwrap();
        let core = Core::open(CoreConfig::replay(directory.path()))
            .await
            .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("ws://{}", listener.local_addr().unwrap());
        let own = core.clone();
        let task = tokio::spawn(async move { own.serve(listener).await });
        Self {
            core,
            url,
            task: Some(task),
            directory,
        }
    }
    async fn connect(&self) -> ClientSocket {
        let mut request = format!("{}{path}?after=0", self.url, path = ori_stream::WS_PATH)
            .into_client_request()
            .unwrap();
        request.headers_mut().insert(
            "Sec-WebSocket-Protocol",
            ori_stream::SUBPROTOCOL.parse().unwrap(),
        );
        let (socket, response) = tokio_tungstenite::connect_async_with_config(request, None, true)
            .await
            .unwrap();
        assert_eq!(response.status(), 101);
        socket
    }
    async fn stop(mut self) {
        self.core.request_shutdown();
        tokio::time::timeout(Duration::from_secs(4), self.task.take().unwrap())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        let _released = ori_runtime::lease::WriterLease::acquire(self.directory.path()).unwrap();
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.core.request_shutdown();
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}
async fn client_event(socket: &mut ClientSocket) -> Result<ori_stream::DecodedFrame, String> {
    let message = tokio::time::timeout(Duration::from_secs(4), socket.next())
        .await
        .map_err(|_| "committed frame did not arrive".to_owned())?
        .ok_or_else(|| "socket ended".to_owned())?
        .map_err(|error| format!("socket error: {error}"))?;
    match message {
        ClientMessage::Binary(bytes) => {
            ori_stream::decode_frame(&bytes).map_err(|_| "invalid frame".into())
        }
        ClientMessage::Close(close) => Err(format!("unexpected close: {close:?}")),
        _ => Err("expected a binary committed frame".into()),
    }
}

#[tokio::test]
async fn three_clients_wait_for_held_encoders_then_ack_and_continue_without_1013() {
    let server = Server::start().await;
    let held = server
        .core
        .stream_encoding
        .clone()
        .acquire_many_owned(2)
        .await
        .unwrap();
    let mut sockets = Vec::new();
    for _ in 0..3 {
        sockets.push(server.connect().await);
    }
    let before_release = async {
        for (index, socket) in sockets.iter_mut().enumerate() {
            match tokio::time::timeout(Duration::from_millis(100), socket.next()).await {
                Err(_) => {}
                Ok(Some(Ok(ClientMessage::Close(close)))) => {
                    return Err(format!(
                        "client {index} closed with code {:?} while the two encoders were held",
                        close.as_ref().map(|frame| u16::from(frame.code))
                    ));
                }
                Ok(_) => return Err(format!("client {index} advanced without an encoder permit")),
            }
        }
        Ok(())
    }
    .await;
    drop(held);
    let result = if before_release.is_ok() {
        async {
            for expected in 1..=2 {
                for socket in &mut sockets {
                    let frame = client_event(socket).await?;
                    if frame.sequence != expected
                        || frame.envelope["data"]["payload"]["index"] != expected - 1
                    {
                        return Err("committed cursor or source changed".to_owned());
                    }
                    socket
                        .send(ClientMessage::Text(expected.to_string().into()))
                        .await
                        .map_err(|_| "applied ACK failed".to_owned())?;
                }
            }
            Ok(())
        }
        .await
    } else {
        before_release
    };
    drop(sockets);
    server.stop().await;
    assert!(result.is_ok(), "{}", result.unwrap_err());
}

async fn context(core: Arc<Core>, authentication: Option<AuthContext>) -> Context {
    let authentication = match authentication {
        Some(value) => value,
        None => core
            .security
            .check(
                &Method::GET,
                ori_stream::WS_PATH,
                &HeaderMap::new(),
                ListenerRole::Plain,
            )
            .await
            .unwrap(),
    };
    let mut value = Context {
        shutdown: core.shutdown.subscribe(),
        revocations: core.security.revocations.subscribe(),
        validated_revision: None,
        security: core.security.clone(),
        authentication,
        _subscriber: core.subscribers.clone().try_acquire_owned().unwrap(),
        _admitted: core.admission.enter().unwrap(),
        core,
    };
    assert!(value.validate().await.is_ok());
    value
}

#[tokio::test]
async fn queued_capacity_waiters_are_fifo_without_increasing_the_two_encoder_limit() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::new(directory.path())).await.unwrap();
    let first = core.stream_encoding.clone().acquire_owned().await.unwrap();
    let second = core.stream_encoding.clone().acquire_owned().await.unwrap();
    let mut one = context(core.clone(), None).await;
    let mut two = context(core.clone(), None).await;
    let mut three = context(core.clone(), None).await;
    {
        let deadline = Instant::now() + ACK_TIMEOUT;
        let a = one.encoder_permit(deadline);
        let b = two.encoder_permit(deadline);
        let c = three.encoder_permit(deadline);
        tokio::pin!(a, b, c);
        assert!(poll!(a.as_mut()).is_pending());
        assert!(poll!(b.as_mut()).is_pending());
        assert!(poll!(c.as_mut()).is_pending());
        drop(first);
        let a = match poll!(a.as_mut()) {
            Poll::Ready(Ok(value)) => value,
            _ => panic!("first waiter lost its position"),
        };
        assert!(poll!(b.as_mut()).is_pending());
        assert!(poll!(c.as_mut()).is_pending());
        drop(a);
        let b = match poll!(b.as_mut()) {
            Poll::Ready(Ok(value)) => value,
            _ => panic!("second waiter lost its position"),
        };
        assert!(poll!(c.as_mut()).is_pending());
        drop(b);
        let c = match poll!(c.as_mut()) {
            Poll::Ready(Ok(value)) => value,
            _ => panic!("third waiter lost its position"),
        };
        drop(c);
    }
    drop(second);
    assert_eq!(core.stream_encoding.available_permits(), 2);
    drop((one, two, three));
    core.close().await.unwrap();
}

#[tokio::test]
async fn shutdown_interrupts_an_encoder_wait_without_releasing_capacity_first() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::new(directory.path())).await.unwrap();
    let held = core
        .stream_encoding
        .clone()
        .acquire_many_owned(2)
        .await
        .unwrap();
    let mut ctx = context(core.clone(), None).await;
    let result = {
        let queued = ctx.encoder_permit(Instant::now() + ACK_TIMEOUT);
        tokio::pin!(queued);
        assert!(poll!(queued.as_mut()).is_pending());
        core.request_shutdown();
        queued.await
    };
    assert!(matches!(result, Err(End::Shutdown)));
    assert_eq!(core.stream_encoding.available_permits(), 0);
    drop(ctx);
    core.close().await.unwrap();
    drop(held);
}

#[tokio::test]
async fn capacity_deadline_closes_1013_while_the_two_permits_remain_held() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::new(directory.path())).await.unwrap();
    let held = core
        .stream_encoding
        .clone()
        .acquire_many_owned(2)
        .await
        .unwrap();
    let mut ctx = context(core.clone(), None).await;
    let result = ctx.encoder_permit(Instant::now()).await;
    let code = match result {
        Err(end) => end.close().0,
        Ok(_) => panic!("capacity wait bypassed the held permits"),
    };
    assert_eq!(code, 1013);
    assert_eq!(core.stream_encoding.available_permits(), 0);
    drop(ctx);
    core.close().await.unwrap();
    drop(held);
}

#[tokio::test]
async fn byte_budget_exhaustion_still_closes_1013_instead_of_growing_capacity() {
    let server = Server::start().await;
    let held = server
        .core
        .stream_bytes
        .clone()
        .acquire_many_owned(32 * 1024 * 1024)
        .await
        .unwrap();
    let mut socket = server.connect().await;
    let result = tokio::time::timeout(Duration::from_secs(4), socket.next()).await;
    let busy = matches!(result, Ok(Some(Ok(ClientMessage::Close(Some(frame))))) if u16::from(frame.code) == 1013);
    drop(socket);
    server.stop().await;
    drop(held);
    assert!(
        busy,
        "the existing byte budget was bypassed or silently enlarged"
    );
}

#[tokio::test]
#[cfg(unix)]
async fn revoking_the_original_credential_interrupts_a_queued_encoder_wait() {
    let directory = tempfile::tempdir().unwrap();
    let identity = directory.path().join("identity");
    ori_runtime::security::TlsIdentity::create(&identity, vec!["localhost".into()]).unwrap();
    let mut config = CoreConfig::new(directory.path().join("data"));
    config.lan_security = Some(crate::LanSecurityConfig {
        directory: identity,
        hosts: vec!["localhost:8770".into()],
        origins: vec!["https://localhost:8770".into()],
    });
    let core = Core::open(config).await.unwrap();
    let code = core.security.begin(30, 2).unwrap();
    let paired = core
        .security
        .pair(
            json!({"pairing_code":code,"label":"encoder fixture","client":"native"}),
            &HeaderMap::new(),
        )
        .await
        .unwrap();
    let raw = axum::body::to_bytes(paired.into_body(), 65536)
        .await
        .unwrap();
    let value = ori_contracts::parse_json_value(&raw, 65536).unwrap();
    let mut headers = HeaderMap::new();
    headers.insert(header::HOST, "localhost:8770".parse().unwrap());
    headers.insert(
        header::AUTHORIZATION,
        format!("Bearer {}", value["data"]["token"].as_str().unwrap())
            .parse()
            .unwrap(),
    );
    let authority = core
        .security
        .check(
            &Method::GET,
            ori_stream::WS_PATH,
            &headers,
            ListenerRole::Tls,
        )
        .await
        .unwrap();
    let mut ctx = context(core.clone(), Some(authority.clone())).await;
    let held = core
        .stream_encoding
        .clone()
        .acquire_many_owned(2)
        .await
        .unwrap();
    let result = {
        let queued = ctx.encoder_permit(Instant::now() + ACK_TIMEOUT);
        tokio::pin!(queued);
        assert!(poll!(queued.as_mut()).is_pending());
        core.security.revoke(&authority, &json!({})).await.unwrap();
        queued.await
    };
    assert!(matches!(result, Err(End::Revoked)));
    assert_eq!(core.stream_encoding.available_permits(), 0);
    drop(ctx);
    core.close().await.unwrap();
    drop(held);
}
