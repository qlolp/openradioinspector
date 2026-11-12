//! Committed ledger transport with one application-acknowledged frame in flight.
use crate::{
    Core,
    admission::MutationGuard,
    error::{ApiError, ApiResult},
    security::{AuthContext, SecurityState},
};
use axum::{
    extract::{
        FromRequestParts, Request,
        ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade},
    },
    http::header,
    response::Response,
};
use base64::Engine;
use ori_contracts::{Envelope, to_json_vec};
use std::{collections::BTreeMap, future::Future, sync::Arc, time::Duration};
use tokio::{
    sync::{OwnedSemaphorePermit, watch},
    time::Instant,
};

const ACK_TIMEOUT: Duration = Duration::from_secs(30);
const PING_INTERVAL: Duration = Duration::from_secs(15);
const MAX_ACK_BYTES: usize = 20;

pub(crate) async fn upgrade(
    core: Arc<Core>,
    request: Request,
    query: BTreeMap<String, String>,
    authentication: AuthContext,
) -> ApiResult<Response> {
    core.archive()?;
    if query.keys().any(|key| key != "after") {
        return Err(ApiError::invalid(
            "WebSocket accepts only the applied after cursor",
        ));
    }
    let cursor = match query.get("after") {
        None => 0,
        Some(value) => decimal(value, true)
            .ok_or_else(|| ApiError::invalid("after must be a canonical unsigned64 decimal"))?,
    };
    if request.headers().contains_key(header::TRANSFER_ENCODING)
        || request
            .headers()
            .get_all(header::CONTENT_LENGTH)
            .iter()
            .count()
            > 1
        || request
            .headers()
            .get(header::CONTENT_LENGTH)
            .is_some_and(|value| value != "0")
    {
        return Err(ApiError::invalid(
            "WebSocket upgrade accepts no request body",
        ));
    }
    for name in [
        header::SEC_WEBSOCKET_KEY,
        header::SEC_WEBSOCKET_VERSION,
        header::UPGRADE,
    ] {
        if request.headers().get_all(name).iter().count() != 1 {
            return Err(ApiError::invalid("Ambiguous WebSocket handshake"));
        }
    }
    if !request
        .headers()
        .get(header::SEC_WEBSOCKET_KEY)
        .and_then(|value| {
            base64::engine::general_purpose::STANDARD
                .decode(value.as_bytes())
                .ok()
        })
        .is_some_and(|value| value.len() == 16)
    {
        return Err(ApiError::invalid("Invalid WebSocket key"));
    }
    let (mut parts, _) = request.into_parts();
    let upgrade = WebSocketUpgrade::from_request_parts(&mut parts, &())
        .await
        .map_err(|_| ApiError::invalid("Invalid WebSocket upgrade"))?;
    if !upgrade
        .requested_protocols()
        .any(|value| value == ori_stream::SUBPROTOCOL)
    {
        return Err(ApiError::invalid(
            "Required measurement subprotocol was not offered",
        ));
    }
    let subscriber = core
        .subscribers
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::unavailable("subscriber_limit"))?;
    let admitted = core.admission.enter()?;
    let context = Context {
        shutdown: core.shutdown.subscribe(),
        revocations: core.security.revocations.subscribe(),
        validated_revision: None,
        security: core.security.clone(),
        authentication,
        core,
        _subscriber: subscriber,
        _admitted: admitted,
    };
    Ok(upgrade
        .protocols([ori_stream::SUBPROTOCOL])
        .read_buffer_size(4096)
        .write_buffer_size(0)
        .max_write_buffer_size(ori_stream::MAX_FRAME_BYTES + 4096)
        .max_message_size(4096)
        .max_frame_size(4096)
        .on_upgrade(move |socket| serve(context, socket, cursor)))
}
fn decimal(value: &str, zero: bool) -> Option<u64> {
    if value.is_empty()
        || value.len() > MAX_ACK_BYTES
        || !value.bytes().all(|byte| byte.is_ascii_digit())
        || value.starts_with('0') && value != "0"
    {
        return None;
    }
    value.parse().ok().filter(|value| zero || *value > 0)
}
struct Context {
    core: Arc<Core>,
    security: Arc<SecurityState>,
    authentication: AuthContext,
    shutdown: watch::Receiver<bool>,
    revocations: watch::Receiver<u64>,
    validated_revision: Option<u64>,
    _subscriber: OwnedSemaphorePermit,
    _admitted: MutationGuard,
}
#[derive(Clone, Copy)]
enum End {
    Shutdown,
    Revoked,
    Policy,
    Size,
    Timeout,
    Busy,
    Gap,
    Client,
}
impl End {
    fn close(self) -> (u16, &'static str) {
        match self {
            Self::Shutdown => (1001, "Core shutdown"),
            Self::Revoked => (1008, "Credential revoked"),
            Self::Policy => (1008, "Invalid applied acknowledgement"),
            Self::Size => (1009, "Acknowledgement exceeds bounds"),
            Self::Timeout => (1008, "Applied acknowledgement deadline"),
            Self::Busy => (1013, "Stream capacity unavailable"),
            Self::Gap => (1011, "Committed replay unavailable"),
            Self::Client => (1000, "Client closed"),
        }
    }
}
impl Context {
    async fn validate(&mut self) -> Result<(), End> {
        let revision = *self.revocations.borrow_and_update();
        if self.validated_revision != Some(revision) {
            self.security
                .validate(&self.authentication)
                .await
                .map_err(|_| End::Revoked)?;
            // Only this captured revision was validated. A change during the
            // awaited hash check remains unseen and must be checked again.
            self.validated_revision = Some(revision);
        }
        Ok(())
    }
    async fn wait<F: Future>(
        &mut self,
        future: F,
        deadline: Option<Instant>,
    ) -> Result<F::Output, End> {
        if *self.shutdown.borrow() {
            return Err(End::Shutdown);
        }
        self.validate().await?;
        tokio::pin!(future);
        let timeout = async {
            if let Some(deadline) = deadline {
                tokio::time::sleep_until(deadline).await;
            } else {
                std::future::pending::<()>().await;
            }
        };
        tokio::pin!(timeout);
        loop {
            tokio::select! {
                result=&mut future=>return Ok(result),
                _=&mut timeout=>return Err(End::Timeout),
                result=self.shutdown.changed()=>{if result.is_err()||*self.shutdown.borrow(){return Err(End::Shutdown);}},
                result=self.revocations.changed()=>{if result.is_err(){return Err(End::Revoked);}self.validate().await?;},
            }
        }
    }
    async fn send(&mut self, socket: &mut WebSocket, message: Message) -> Result<(), End> {
        self.wait(socket.send(message), Some(Instant::now() + ACK_TIMEOUT))
            .await?
            .map_err(|_| End::Client)
    }
}
fn incoming(error: axum::Error) -> End {
    match error
        .into_inner()
        .downcast::<tokio_tungstenite::tungstenite::Error>()
    {
        Ok(error) if matches!(*error, tokio_tungstenite::tungstenite::Error::Capacity(_)) => {
            End::Size
        }
        _ => End::Policy,
    }
}
async fn acknowledge(
    context: &mut Context,
    socket: &mut WebSocket,
    expected: u64,
) -> Result<(), End> {
    let deadline = Instant::now() + ACK_TIMEOUT;
    loop {
        let received = context.wait(socket.recv(), Some(deadline)).await?;
        match received {
            Some(Ok(Message::Text(value))) => {
                if value.len() > MAX_ACK_BYTES {
                    return Err(End::Size);
                }
                return if decimal(value.as_str(), false) == Some(expected) {
                    Ok(())
                } else {
                    Err(End::Policy)
                };
            }
            Some(Ok(Message::Ping(_) | Message::Pong(_))) => {}
            Some(Ok(Message::Close(_))) | None => return Err(End::Client),
            Some(Ok(Message::Binary(_))) => return Err(End::Policy),
            Some(Err(error)) => return Err(incoming(error)),
        }
    }
}
async fn run(context: &mut Context, socket: &mut WebSocket, mut cursor: u64) -> Result<(), End> {
    let archive = context.core.archive.as_ref().ok_or(End::Gap)?.clone();
    // Subscribe before the first durable read: notices are wakeups only, never
    // authority to skip a cursor or a bounded/truncated commit acknowledgement.
    let mut notices = archive.subscribe_commits();
    let mut ping = tokio::time::interval_at(Instant::now() + PING_INTERVAL, PING_INTERVAL);
    ping.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        // SQLite's ledger sequence is signed64. A larger exact client cursor is
        // truthful idle state; never cast/wrap it or replace it with the HWM.
        let events = if cursor > i64::MAX as u64 {
            Vec::new()
        } else {
            context
                .wait(archive.events(cursor, 1), None)
                .await?
                .map_err(|_| End::Gap)?
        };
        if let Some(event) = events.into_iter().next() {
            let sequence = event["sequence"]
                .as_u64()
                .filter(|value| *value > cursor)
                .ok_or(End::Gap)?;
            let encoding = context
                .core
                .stream_encoding
                .clone()
                .try_acquire_owned()
                .map_err(|_| End::Busy)?;
            let budget = context.core.stream_bytes.clone();
            // The connection retains its admission guard through this actual
            // blocking join, even if shutdown/revocation arrives mid-encoding.
            let encoded = tokio::task::spawn_blocking(move || {
                let _encoding = encoding;
                let json = to_json_vec(&Envelope::new(event)).map_err(|_| End::Gap)?;
                let bytes = ori_stream::encode_frame(&json).map_err(|_| End::Size)?;
                let held = budget
                    .try_acquire_many_owned(bytes.len() as u32)
                    .map_err(|_| End::Busy)?;
                Ok::<_, End>((bytes, held))
            })
            .await
            .map_err(|_| End::Gap)??;
            let (bytes, _in_flight) = encoded;
            context.send(socket, Message::Binary(bytes.into())).await?;
            acknowledge(context, socket, sequence).await?;
            cursor = sequence;
            continue;
        }
        enum Wake {
            Notice,
            Input(Option<Result<Message, axum::Error>>),
            Ping,
        }
        let wake = context
            .wait(
                async {
                    tokio::select! {
                        _=notices.recv()=>Wake::Notice,
                        incoming=socket.recv()=>Wake::Input(incoming),
                        _=ping.tick()=>Wake::Ping,
                    }
                },
                None,
            )
            .await?;
        match wake {
            Wake::Notice => {}
            Wake::Ping => {
                context
                    .send(socket, Message::Ping(Vec::new().into()))
                    .await?
            }
            Wake::Input(Some(Ok(Message::Ping(_) | Message::Pong(_)))) => {}
            Wake::Input(Some(Ok(Message::Close(_))) | None) => return Err(End::Client),
            Wake::Input(Some(Ok(Message::Text(value)))) if value.len() > MAX_ACK_BYTES => {
                return Err(End::Size);
            }
            Wake::Input(Some(Err(error))) => return Err(incoming(error)),
            Wake::Input(_) => return Err(End::Policy),
        }
    }
}
async fn serve(mut context: Context, mut socket: WebSocket, cursor: u64) {
    let end = run(&mut context, &mut socket, cursor)
        .await
        .err()
        .unwrap_or(End::Client);
    let (code, reason) = end.close();
    // Best effort bounded close, then actual owned socket drop before the
    // connection's admission guard allows Archive/WriterLease closure.
    let _ = tokio::time::timeout(
        Duration::from_millis(100),
        socket.send(Message::Close(Some(CloseFrame {
            code,
            reason: reason.into(),
        }))),
    )
    .await;
    drop(socket);
    drop(context);
}
