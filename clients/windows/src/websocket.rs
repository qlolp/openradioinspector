use super::{Bridge, StreamMessage};
use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use std::{future::Future, time::Duration};
use tokio_tungstenite::{
    WebSocketStream,
    tungstenite::{
        Error, Message,
        handshake::{client::generate_key, derive_accept_key},
        protocol::{Role, WebSocketConfig},
    },
};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Copy, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BinaryStreamConfig {
    pub max_frame_bytes: usize,
}
impl BinaryStreamConfig {
    pub fn validate(self) -> Result<(), String> {
        if self.max_frame_bytes == 0 || self.max_frame_bytes > ori_stream::MAX_FRAME_BYTES {
            return Err("Invalid binary stream limit".into());
        }
        Ok(())
    }
}
enum Outcome {
    Retry,
    Fallback,
    Stopped,
}
impl Bridge {
    /// Use only an advertised capability. Every frame awaits the existing local
    /// WebView acknowledgement before its exact decimal sequence is sent to core.
    pub async fn events_negotiated<F, A>(
        &self,
        mut after: u64,
        binary: Option<BinaryStreamConfig>,
        cancel: CancellationToken,
        mut deliver: F,
    ) where
        F: FnMut(StreamMessage) -> A + Send,
        A: Future<Output = bool> + Send,
    {
        let mut delivery_id = 0;
        if let Some(config) = binary {
            if config.validate().is_err() {
                return;
            }
            loop {
                match self
                    .websocket_once(&mut after, &mut delivery_id, config, &cancel, &mut deliver)
                    .await
                {
                    Outcome::Stopped => return,
                    Outcome::Fallback => {
                        if !tokio::select! {_=cancel.cancelled()=>return,result=deliver(StreamMessage::state("fallback"))=>result}
                        {
                            return;
                        }
                        break;
                    }
                    Outcome::Retry => {
                        if !tokio::select! {_=cancel.cancelled()=>return,result=deliver(StreamMessage::state("reconnecting"))=>result}
                        {
                            return;
                        }
                        tokio::select! {_=cancel.cancelled()=>return,_=tokio::time::sleep(Duration::from_secs(1))=>{}}
                    }
                }
            }
        }
        self.events_sse(&mut after, &mut delivery_id, cancel, &mut deliver)
            .await;
    }
    async fn websocket_once<F, A>(
        &self,
        after: &mut u64,
        delivery_id: &mut u64,
        config: BinaryStreamConfig,
        cancel: &CancellationToken,
        deliver: &mut F,
    ) -> Outcome
    where
        F: FnMut(StreamMessage) -> A + Send,
        A: Future<Output = bool> + Send,
    {
        let mut endpoint = match self.endpoint(ori_stream::WS_PATH) {
            Ok(value) => value,
            Err(_) => return Outcome::Stopped,
        };
        endpoint
            .query_pairs_mut()
            .append_pair("after", &after.to_string());
        let key = generate_key();
        // Reuse reqwest's platform-verified TLS/Bearer client and its no-redirect
        // policy. Never introduce a separate trust store or URL credentials.
        let response = tokio::select! {_=cancel.cancelled()=>return Outcome::Stopped,result=self.client.get(endpoint)
        .header("Connection","Upgrade").header("Upgrade","websocket")
        .header("Sec-WebSocket-Version","13").header("Sec-WebSocket-Key",&key)
        .header("Sec-WebSocket-Protocol",ori_stream::SUBPROTOCOL).send()=>result};
        let response = match response {
            Ok(value) => value,
            Err(_) => return Outcome::Retry,
        };
        if matches!(response.status().as_u16(), 404 | 405 | 426 | 501) {
            return Outcome::Fallback;
        }
        if response.status() != reqwest::StatusCode::SWITCHING_PROTOCOLS {
            return Outcome::Retry;
        }
        let header = |name: &str| {
            response
                .headers()
                .get(name)
                .and_then(|value| value.to_str().ok())
                .unwrap_or_default()
        };
        if !header("Upgrade").eq_ignore_ascii_case("websocket")
            || !header("Connection")
                .split(',')
                .any(|value| value.trim().eq_ignore_ascii_case("upgrade"))
            || header("Sec-WebSocket-Accept") != derive_accept_key(key.as_bytes())
            || header("Sec-WebSocket-Protocol") != ori_stream::SUBPROTOCOL
            || response
                .headers()
                .get_all("Sec-WebSocket-Accept")
                .iter()
                .count()
                != 1
            || response
                .headers()
                .get_all("Sec-WebSocket-Protocol")
                .iter()
                .count()
                != 1
            || !header("Sec-WebSocket-Extensions").is_empty()
        {
            return Outcome::Fallback;
        }
        // Upgraded retains bytes hyper read beyond 101, including an immediately
        // coalesced first frame. Do not detach/recreate the underlying TCP socket.
        let io = tokio::select! {_=cancel.cancelled()=>return Outcome::Stopped,result=response.upgrade()=>result};
        let io = match io {
            Ok(value) => value,
            Err(_) => return Outcome::Retry,
        };
        let limits = WebSocketConfig::default()
            .max_message_size(Some(config.max_frame_bytes))
            .max_frame_size(Some(config.max_frame_bytes));
        let mut socket = WebSocketStream::from_raw_socket(io, Role::Client, Some(limits)).await;
        if !tokio::select! {_=cancel.cancelled()=>return Outcome::Stopped,result=deliver(StreamMessage::state("open"))=>result}
        {
            return Outcome::Stopped;
        }
        loop {
            let message = tokio::select! {biased;_=cancel.cancelled()=>return Outcome::Stopped,result=socket.next()=>result};
            match message {
                Some(Ok(Message::Binary(bytes)))=>{
                    let header=match ori_stream::validate_frame_with_limits(&bytes,ori_stream::Limits{max_frame_bytes:config.max_frame_bytes,..Default::default()}){Ok(value)=>value,Err(_)=>return Outcome::Fallback};
                    *delivery_id=match delivery_id.checked_add(1){Some(value)=>value,None=>return Outcome::Stopped};
                    let message=StreamMessage{kind:"binary".into(),data:Some(base64::engine::general_purpose::STANDARD.encode(&bytes)),delivery_id:Some(delivery_id.to_string())};
                    if !tokio::select!{biased;_=cancel.cancelled()=>return Outcome::Stopped,result=deliver(message)=>result}{return Outcome::Stopped;}
                    *after=(*after).max(header.sequence);
                    let sent=tokio::select!{_=cancel.cancelled()=>return Outcome::Stopped,result=socket.send(Message::Text(header.sequence.to_string().into()))=>result};
                    if sent.is_err(){return Outcome::Retry;}
                }
                Some(Ok(Message::Ping(_)))=>{
                    if tokio::select!{_=cancel.cancelled()=>return Outcome::Stopped,result=socket.flush()=>result}.is_err(){return Outcome::Retry;}
                }
                Some(Ok(Message::Pong(_)))=>{}
                Some(Ok(Message::Text(_)))=>return Outcome::Fallback,
                Some(Err(Error::Capacity(_)|Error::Protocol(_)|Error::Utf8(_)|Error::AttackAttempt))=>return Outcome::Fallback,
                Some(Ok(Message::Close(_)))|Some(Err(_))|None=>return Outcome::Retry,
                Some(Ok(Message::Frame(_)))=>return Outcome::Fallback,
            }
        }
    }
}
