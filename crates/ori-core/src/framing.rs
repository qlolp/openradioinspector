//! Bounded HTTP/1 head gate before Hyper's u64 Content-Length parser.
//! One request per connection deliberately matches the legacy HTTP/1.0 service.
use axum_server::accept::Accept;
use std::{
    future::Future,
    io,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf},
    sync::{OwnedSemaphorePermit, Semaphore},
};

#[derive(Clone)]
pub(crate) struct HeadGate<A> {
    inner: A,
    slots: Arc<Semaphore>,
}
impl<A> HeadGate<A> {
    pub fn new(inner: A) -> Self {
        Self {
            inner,
            slots: Arc::new(Semaphore::new(32)),
        }
    }
}
impl<I, S, A> Accept<I, S> for HeadGate<A>
where
    I: Send + 'static,
    S: Send + 'static,
    A: Accept<I, S> + Clone + Send + Sync + 'static,
    A::Future: Send,
    A::Stream: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    A::Service: Send + 'static,
{
    type Stream = Prefixed<crate::audio_http::DisconnectIo<A::Stream>>;
    type Service = A::Service;
    type Future = Pin<Box<dyn Future<Output = io::Result<(Self::Stream, Self::Service)>> + Send>>;
    fn accept(&self, stream: I, service: S) -> Self::Future {
        let inner = self.inner.clone();
        let slot = self.slots.clone().try_acquire_owned();
        Box::pin(async move {
            let slot = slot.map_err(|_| io::Error::other("connection capacity reached"))?;
            let (mut stream, service) = inner.accept(stream, service).await?;
            let result =
                tokio::time::timeout(Duration::from_secs(10), read_head(&mut stream)).await;
            let bytes = match result {
                Ok(Ok(bytes)) => bytes,
                other => {
                    let status = match other {
                        Err(_) => 408,
                        Ok(Err(HeadError::TooLarge)) => 413,
                        _ => 400,
                    };
                    let (code, message) = match status {
                        413 => ("request_too_large", "Тело запроса превышает предел"),
                        408 => ("request_timeout", "Истёк срок чтения заголовка запроса"),
                        _ => (
                            "invalid_request",
                            "Нужен один корректный Content-Length без Transfer-Encoding",
                        ),
                    };
                    let body=ori_contracts::to_json_vec(&serde_json::json!({"contract":ori_contracts::CONTRACT_NAME,"contract_version":1,"error":{"code":code,"message":message}})).expect("finite static error");
                    let reason = match status {
                        413 => "Payload Too Large",
                        408 => "Request Timeout",
                        _ => "Bad Request",
                    };
                    let head = format!(
                        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n",
                        body.len()
                    );
                    let _ = tokio::time::timeout(Duration::from_secs(1), async {
                        stream.write_all(head.as_bytes()).await?;
                        stream.write_all(&body).await?;
                        stream.shutdown().await
                    })
                    .await;
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "request framing rejected",
                    ));
                }
            };
            Ok((
                Prefixed {
                    inner: crate::audio_http::DisconnectIo::new(stream, &bytes),
                    bytes,
                    offset: 0,
                    _slot: slot,
                },
                service,
            ))
        })
    }
}
enum HeadError {
    Invalid,
    TooLarge,
}
async fn read_head(stream: &mut (impl AsyncRead + Unpin)) -> Result<Vec<u8>, HeadError> {
    let mut bytes = Vec::with_capacity(4096);
    let end = loop {
        if let Some(index) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
            break index + 4;
        }
        if bytes.len() >= 65536 {
            return Err(HeadError::Invalid);
        }
        let mut block = [0u8; 4096];
        let count = stream
            .read(&mut block[..4096.min(65536 - bytes.len())])
            .await
            .map_err(|_| HeadError::Invalid)?;
        if count == 0 {
            return Err(HeadError::Invalid);
        }
        bytes.extend_from_slice(&block[..count]);
    };
    let mut headers = [httparse::EMPTY_HEADER; 100];
    let mut request = httparse::Request::new(&mut headers);
    if !matches!(
        request.parse(&bytes[..end]),
        Ok(httparse::Status::Complete(_))
    ) {
        return Err(HeadError::Invalid);
    }
    if !matches!(request.version, Some(0 | 1)) {
        return Err(HeadError::Invalid);
    }
    let mut length = None;
    for header in request.headers.iter() {
        if header.name.eq_ignore_ascii_case("Transfer-Encoding") {
            return Err(HeadError::Invalid);
        }
        if header.name.eq_ignore_ascii_case("Content-Length") {
            if length.is_some() {
                return Err(HeadError::Invalid);
            }
            let raw = std::str::from_utf8(header.value)
                .map_err(|_| HeadError::Invalid)?
                .trim_matches([' ', '\t']);
            if raw.is_empty() || !raw.bytes().all(|byte| byte.is_ascii_digit()) {
                return Err(HeadError::Invalid);
            }
            let normalized = raw.trim_start_matches('0');
            length = Some(if normalized.is_empty() {
                "0"
            } else {
                normalized
            });
        }
    }
    let path = request
        .path
        .ok_or(HeadError::Invalid)?
        .split('?')
        .next()
        .ok_or(HeadError::Invalid)?;
    let maximum = crate::http::request_limit(path);
    if let Some(digits) = length {
        let bound = maximum.to_string();
        if digits.len() > bound.len() || (digits.len() == bound.len() && digits > bound.as_str()) {
            return Err(HeadError::TooLarge);
        }
    }
    // Normalize leading zeros before Hyper, including lengths with thousands of zero digits.
    let first_line = bytes
        .windows(2)
        .position(|window| window == b"\r\n")
        .ok_or(HeadError::Invalid)?;
    let mut normalized = Vec::with_capacity(bytes.len());
    normalized.extend_from_slice(&bytes[..first_line + 2]);
    for header in request.headers.iter() {
        normalized.extend_from_slice(header.name.as_bytes());
        normalized.extend_from_slice(b": ");
        normalized.extend_from_slice(if header.name.eq_ignore_ascii_case("Content-Length") {
            length.expect("parsed length").as_bytes()
        } else {
            header.value
        });
        normalized.extend_from_slice(b"\r\n");
    }
    normalized.extend_from_slice(b"\r\n");
    normalized.extend_from_slice(&bytes[end..]);
    Ok(normalized)
}
pub(crate) struct Prefixed<I> {
    inner: I,
    bytes: Vec<u8>,
    offset: usize,
    _slot: OwnedSemaphorePermit,
}
impl<I: AsyncRead + Unpin> AsyncRead for Prefixed<I> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if this.offset < this.bytes.len() {
            let count = buf.remaining().min(this.bytes.len() - this.offset);
            buf.put_slice(&this.bytes[this.offset..this.offset + count]);
            this.offset += count;
            Poll::Ready(Ok(()))
        } else {
            Pin::new(&mut this.inner).poll_read(cx, buf)
        }
    }
}
impl<I: AsyncWrite + Unpin> AsyncWrite for Prefixed<I> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.get_mut().inner).poll_write(cx, buf)
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_flush(cx)
    }
    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_shutdown(cx)
    }
}
