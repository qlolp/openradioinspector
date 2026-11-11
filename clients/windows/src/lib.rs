//! Native-only HTTP transport. No serial ports, hardware adapters, or archive access.
use base64::Engine;
use reqwest::header::{AUTHORIZATION, HeaderMap, HeaderValue};
use std::time::Duration;
use tokio_util::sync::CancellationToken;
use url::Url;

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
    pub fn new(node: &str, token: &str) -> Result<Self, String> {
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
        body: Option<serde_json::Value>,
        audio: bool,
    ) -> Result<String, String> {
        let endpoint = self.endpoint(path)?;
        if audio
            && (body.is_some()
                || !endpoint.path().starts_with("/api/v1/monitoring-projects/")
                || !endpoint.path().ends_with("/audio"))
        {
            return Err("Only project event audio is allowed".into());
        }
        let request = match body {
            Some(value) if value.is_object() => self.client.post(endpoint).json(&value),
            Some(_) => return Err("Core controls require a JSON object".into()),
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

    /// Reconnect with the last delivered sequence. The UI separately detects gaps and resyncs snapshots.
    pub async fn events<F>(&self, mut after: u64, cancel: CancellationToken, mut deliver: F)
    where
        F: FnMut(StreamMessage) -> bool + Send,
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
                if !deliver(StreamMessage::state("open")) {
                    return;
                }
                let mut decoder = SseDecoder::default();
                loop {
                    let chunk = tokio::select! { _ = cancel.cancelled() => return, result = response.chunk() => result };
                    match chunk {
                        Ok(Some(data)) => match decoder.push(&data) {
                            Ok(events) => {
                                for data in events {
                                    if let Ok(value) =
                                        serde_json::from_str::<serde_json::Value>(&data)
                                        && let Some(sequence) =
                                            value.pointer("/data/sequence").and_then(|x| x.as_u64())
                                    {
                                        after = after.max(sequence);
                                    }
                                    if !deliver(StreamMessage {
                                        kind: "event".into(),
                                        data: Some(data),
                                    }) {
                                        return;
                                    }
                                }
                            }
                            Err(_) => break,
                        },
                        _ => break,
                    }
                }
            }
            if !deliver(StreamMessage::state("reconnecting")) {
                return;
            }
            tokio::select! { _ = cancel.cancelled() => return, _ = tokio::time::sleep(Duration::from_secs(1)) => {} }
        }
    }
}

#[derive(Clone, serde::Serialize)]
pub struct StreamMessage {
    pub kind: String,
    pub data: Option<String>,
}
impl StreamMessage {
    fn state(kind: &str) -> Self {
        Self {
            kind: kind.into(),
            data: None,
        }
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
