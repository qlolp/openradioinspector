//! Native-only HTTP transport. No serial ports, hardware adapters, or archive access.
#[cfg(windows)]
pub mod credentials;
mod json;
mod websocket;
use base64::Engine;
use reqwest::header::{AUTHORIZATION, HeaderMap, HeaderValue};
use std::time::Duration;
use std::{
    future::Future,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};
use tokio_util::sync::CancellationToken;
use url::Url;
pub use websocket::BinaryStreamConfig;

const MAX_JSON: usize = 8_388_608;
const MAX_AUDIO: usize = 33_554_432;

#[derive(Clone)]
pub struct Bridge {
    node: Url,
    client: reqwest::Client,
}
impl Bridge {
    pub fn legacy_url(&self) -> &str {
        self.node.as_str()
    }
    pub fn legacy_browser_url(&self) -> String {
        let mut url = self.node.clone();
        if url.scheme() == "https" {
            url.set_path("/modern/");
            url.set_query(Some("open=legacy"));
        }
        url.to_string()
    }

    pub fn new(node: &str, token: &str) -> Result<Self, String> {
        if node.len() > 8192 {
            return Err("Node URL exceeds client limit".into());
        }
        let node = Url::parse(node).map_err(|_| "Invalid node URL")?;
        if !matches!(node.scheme(), "http" | "https")
            || node.host_str().is_none()
            || !node.username().is_empty()
            || node.password().is_some()
            || node.path() != "/"
            || node.query().is_some()
            || node.fragment().is_some()
        {
            return Err(
                "Node must be an HTTP(S) origin without credentials, path, query or fragment"
                    .into(),
            );
        }
        let loopback = match node.host() {
            Some(url::Host::Domain(name)) => name.eq_ignore_ascii_case("localhost"),
            Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
            Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
            None => false,
        };
        if node.scheme() == "http" && !loopback {
            return Err("Remote nodes require trusted HTTPS; legacy HTTP is loopback only".into());
        }
        if token.len() > 4096 {
            return Err("Credential exceeds client limit".into());
        }
        let mut headers = HeaderMap::new();
        if !token.is_empty() {
            let mut authorization = HeaderValue::from_str(&format!("Bearer {token}"))
                .map_err(|_| "Invalid credential")?;
            authorization.set_sensitive(true);
            headers.insert(AUTHORIZATION, authorization);
        }
        let client = reqwest::Client::builder()
            .default_headers(headers)
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .connect_timeout(Duration::from_secs(10))
            .read_timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| "Native HTTP initialization failed")?;
        Ok(Self { node, client })
    }
    pub fn endpoint(&self, path: &str) -> Result<Url, String> {
        if !path.starts_with("/api/v1/")
            || path.len() > 8192
            || path.bytes().any(|c| c.is_ascii_control())
            || path.contains(['\\', '#'])
        {
            return Err("Only relative /api/v1 requests are allowed".into());
        }
        let route = path
            .split('?')
            .next()
            .unwrap_or_default()
            .to_ascii_lowercase()
            .replace("%2e", ".");
        if route
            .split('/')
            .any(|segment| matches!(segment, "." | ".."))
        {
            return Err("Traversal is not allowed".into());
        }
        let endpoint = self.node.join(path).map_err(|_| "Invalid API path")?;
        if endpoint.origin() != self.node.origin() || !endpoint.path().starts_with("/api/v1/") {
            return Err("Request origin changed".into());
        }
        Ok(endpoint)
    }
    pub async fn request(
        &self,
        path: &str,
        body: Option<String>,
        audio: bool,
    ) -> Result<String, String> {
        self.request_impl(path, body, audio, false).await
    }
    pub fn credential_origin(&self) -> String {
        self.node.origin().ascii_serialization()
    }
    pub async fn pair_native(&self, code: &str, label: &str) -> Result<PairedNode, String> {
        if self.node.scheme() != "https" {
            return Err("Pairing requires trusted HTTPS".into());
        }
        if code.is_empty() || code.len() > 256 || label.trim().is_empty() || label.len() > 200 {
            return Err("Invalid pairing code or client label".into());
        }
        #[derive(serde::Serialize)]
        struct PairRequest<'a> {
            pairing_code: &'a str,
            label: &'a str,
            client: &'static str,
        }
        let body = serde_json::to_string(&PairRequest {
            pairing_code: code,
            label,
            client: "native",
        })
        .map_err(|_| "Could not encode pairing request")?;
        let raw = self
            .request_impl("/api/v1/security/pair", Some(body), false, true)
            .await?;
        let token = pair_token(&raw)?;
        let bridge = Self::new(self.node.as_str(), &token)?;
        Ok(PairedNode { bridge, token })
    }
    async fn request_impl(
        &self,
        path: &str,
        body: Option<String>,
        audio: bool,
        allow_pair: bool,
    ) -> Result<String, String> {
        let endpoint = self.endpoint(path)?;
        let category = endpoint
            .path()
            .strip_prefix("/api/v1/")
            .unwrap_or_default()
            .split('/')
            .next()
            .unwrap_or_default();
        if category.contains('%')
            || (!allow_pair
                && category.eq_ignore_ascii_case("security")
                && !matches!(
                    endpoint.path(),
                    "/api/v1/security/status" | "/api/v1/security/credentials/current/revoke"
                ))
        {
            return Err("Use the native pairing command for credential issuance".into());
        }
        if audio
            && (body.is_some()
                || !endpoint.path().starts_with("/api/v1/monitoring-projects/")
                || !endpoint.path().ends_with("/audio"))
        {
            return Err("Only project event audio is allowed".into());
        }
        let request = match body {
            Some(raw) => {
                json::object(&raw, MAX_JSON)?;
                self.client
                    .post(endpoint)
                    .header(reqwest::header::CONTENT_TYPE, "application/json")
                    .body(raw)
            }
            None => self.client.get(endpoint),
        };
        let mut response = request
            .timeout(Duration::from_secs(45))
            .send()
            .await
            .map_err(|_| "Core request failed or timed out")?;
        if response.status().is_redirection() {
            return Err("Core redirects are not allowed".into());
        }
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|x| x.to_str().ok())
            .unwrap_or_default();
        if audio {
            if !response.status().is_success() || !content_type.starts_with("audio/wav") {
                return Err("Audio evidence is unavailable".into());
            }
        } else if !content_type.starts_with("application/json") {
            return Err("Core returned an unsupported response type".into());
        }
        let maximum = if audio { MAX_AUDIO } else { MAX_JSON };
        if response
            .content_length()
            .is_some_and(|length| length > maximum as u64)
        {
            return Err("Core response exceeds client limit".into());
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| "Core response was interrupted")?
        {
            if bytes.len() + chunk.len() > maximum {
                return Err("Core response exceeds client limit".into());
            }
            bytes.extend_from_slice(&chunk);
        }
        if audio {
            Ok(base64::engine::general_purpose::STANDARD.encode(bytes))
        } else {
            String::from_utf8(bytes).map_err(|_| "Core JSON is not UTF-8".into())
        }
    }

    /// Compatibility callback for synchronous consumers. The desktop uses events_acked.
    pub async fn events<F>(&self, after: u64, cancel: CancellationToken, mut deliver: F)
    where
        F: FnMut(StreamMessage) -> bool + Send,
    {
        self.events_acked(after, cancel, move |message| {
            std::future::ready(deliver(message))
        })
        .await;
    }

    /// At most one frame crosses the native/WebView boundary before application ACK.
    /// A reconnect cursor advances only after the callback confirms application.
    pub async fn events_acked<F, A>(
        &self,
        mut after: u64,
        cancel: CancellationToken,
        mut deliver: F,
    ) where
        F: FnMut(StreamMessage) -> A + Send,
        A: Future<Output = bool> + Send,
    {
        let mut delivery_id = 0;
        self.events_sse(&mut after, &mut delivery_id, cancel, &mut deliver)
            .await;
    }
    async fn events_sse<F, A>(
        &self,
        after: &mut u64,
        delivery_id: &mut u64,
        cancel: CancellationToken,
        deliver: &mut F,
    ) where
        F: FnMut(StreamMessage) -> A + Send,
        A: Future<Output = bool> + Send,
    {
        while !cancel.is_cancelled() {
            let mut endpoint = match self.endpoint("/api/v1/events") {
                Ok(value) => value,
                Err(_) => return,
            };
            endpoint
                .query_pairs_mut()
                .append_pair("after", &after.to_string());
            let response = tokio::select! {
                _ = cancel.cancelled() => return,
                result = self.client.get(endpoint).header("Accept", "text/event-stream").send() => result,
            };
            if let Ok(mut response) = response
                && response.status().is_success()
                && response
                    .headers()
                    .get(reqwest::header::CONTENT_TYPE)
                    .and_then(|x| x.to_str().ok())
                    .is_some_and(|value| value.starts_with("text/event-stream"))
            {
                if !tokio::select! { _ = cancel.cancelled() => return, ack = deliver(StreamMessage::state("open")) => ack }
                {
                    return;
                }
                let mut decoder = SseDecoder::default();
                loop {
                    let chunk = tokio::select! { _ = cancel.cancelled() => return, result = response.chunk() => result };
                    match chunk {
                        Ok(Some(data)) => match decoder.push(&data) {
                            Ok(events) => {
                                for data in events {
                                    let sequence = serde_json::from_str::<SequenceEnvelope>(&data)
                                        .ok()
                                        .map(|value| value.data.sequence);
                                    *delivery_id = match delivery_id.checked_add(1) {
                                        Some(id) => id,
                                        None => return,
                                    };
                                    if !tokio::select! { _ = cancel.cancelled() => return, ack = deliver(StreamMessage {
                                        kind: "event".into(),
                                        data: Some(data),
                                        delivery_id: Some(delivery_id.to_string()),
                                    }) => ack }
                                    {
                                        return;
                                    }
                                    if let Some(sequence) = sequence {
                                        *after = (*after).max(sequence);
                                    }
                                }
                            }
                            Err(_) => break,
                        },
                        _ => break,
                    }
                }
            }
            if !tokio::select! { _ = cancel.cancelled() => return, ack = deliver(StreamMessage::state("reconnecting")) => ack }
            {
                return;
            }
            tokio::select! { _ = cancel.cancelled() => return, _ = tokio::time::sleep(Duration::from_secs(1)) => {} }
        }
    }
}

#[derive(serde::Deserialize)]
struct SequenceEnvelope {
    data: Sequence,
}
#[derive(serde::Deserialize)]
struct Sequence {
    sequence: u64,
}

#[derive(Clone, serde::Serialize)]
pub struct StreamMessage {
    pub kind: String,
    pub data: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub delivery_id: Option<String>,
}
impl StreamMessage {
    fn state(kind: &str) -> Self {
        Self {
            kind: kind.into(),
            data: None,
            delivery_id: None,
        }
    }
}

/// One outstanding delivery with a bounded acknowledgement queue. Stream identity
/// is checked by the desktop runtime before this handle can be acknowledged.
pub struct ApplicationAck {
    expected: AtomicU64,
    sender: tokio::sync::mpsc::Sender<u64>,
}
impl ApplicationAck {
    pub fn channel() -> (Arc<Self>, tokio::sync::mpsc::Receiver<u64>) {
        let (sender, receiver) = tokio::sync::mpsc::channel(1);
        (
            Arc::new(Self {
                expected: AtomicU64::new(0),
                sender,
            }),
            receiver,
        )
    }
    pub fn arm(&self, delivery_id: &str) -> Result<u64, String> {
        let id = Self::parse(delivery_id)?;
        self.expected
            .compare_exchange(0, id, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "Native delivery already awaiting acknowledgement")?;
        Ok(id)
    }
    pub fn acknowledge(&self, delivery_id: &str) -> Result<(), String> {
        let id = Self::parse(delivery_id)?;
        self.expected
            .compare_exchange(id, 0, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "Stale native acknowledgement")?;
        self.sender
            .try_send(id)
            .map_err(|_| "Native acknowledgement unavailable".to_owned())
    }
    fn parse(raw: &str) -> Result<u64, String> {
        if raw.is_empty()
            || raw.len() > 20
            || raw.starts_with('0')
            || !raw.bytes().all(|c| c.is_ascii_digit())
        {
            return Err("Invalid native acknowledgement".into());
        }
        raw.parse()
            .map_err(|_| "Invalid native acknowledgement".into())
    }
}

#[derive(Default)]
pub struct SseDecoder {
    pending: Vec<u8>,
    data: Vec<String>,
    event_bytes: usize,
}
impl SseDecoder {
    pub fn push(&mut self, data: &[u8]) -> Result<Vec<String>, String> {
        if self.pending.len() + self.event_bytes + data.len() > MAX_JSON {
            return Err("SSE frame exceeds client limit".into());
        }
        self.pending.extend_from_slice(data);
        let mut events = Vec::new();
        while let Some(end) = self.pending.iter().position(|&c| c == b'\n') {
            let bytes: Vec<u8> = self.pending.drain(..=end).collect();
            let line = std::str::from_utf8(&bytes[..bytes.len() - 1])
                .map_err(|_| "SSE frame is not UTF-8")?
                .trim_end_matches('\r');
            if line.is_empty() {
                if !self.data.is_empty() {
                    events.push(self.data.join("\n"));
                    self.data.clear();
                }
                self.event_bytes = 0;
            } else if let Some(value) = line.strip_prefix("data:") {
                let value = value.strip_prefix(' ').unwrap_or(value);
                self.event_bytes += value.len();
                self.data.push(value.into());
            }
        }
        Ok(events)
    }
}

/// Native-only result; never serialize this credential to a webview or a log.
pub struct PairedNode {
    pub bridge: Bridge,
    pub token: String,
}
fn pair_token(raw: &str) -> Result<String, String> {
    #[derive(serde::Deserialize)]
    struct Envelope {
        contract: String,
        contract_version: u64,
        data: Data,
    }
    #[derive(serde::Deserialize)]
    struct Data {
        paired: bool,
        credential: String,
        token: String,
    }
    let envelope: Envelope = serde_json::from_str(raw)
        .map_err(|_| "Pairing rejected. Check the operator code window.")?;
    if envelope.contract != "openradioinspector-platform"
        || envelope.contract_version != 1
        || !envelope.data.paired
        || envelope.data.credential != "bearer"
        || envelope.data.token.is_empty()
        || envelope.data.token.len() > 4096
    {
        return Err("Invalid native pairing response".into());
    }
    Ok(envelope.data.token)
}
#[cfg(test)]
mod pairing_tests {
    use super::*;
    #[test]
    fn pairing_response_requires_an_explicit_native_bearer_credential() {
        assert_eq!(pair_token(r#"{"contract":"openradioinspector-platform","contract_version":1,"data":{"paired":true,"credential":"bearer","token":"synthetic-only"}}"#).unwrap(),"synthetic-only");
        for raw in [
            r#"{"data":{"paired":true,"credential":"browser_cookie"}}"#,
            r#"{"contract":"openradioinspector-platform","contract_version":1,"error":{"message":"rejected"}}"#,
        ] {
            assert!(pair_token(raw).is_err());
        }
    }
}
