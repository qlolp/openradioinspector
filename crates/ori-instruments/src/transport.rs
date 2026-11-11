use crate::{InstrumentError, IoLimits, Result};
use std::{
    future::poll_fn,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    task::Poll,
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf},
    sync::Notify,
};

#[derive(Default)]
struct Flags {
    tainted: AtomicBool,
    cancelled: AtomicBool,
    closed: AtomicBool,
    active: AtomicBool,
    notify: Notify,
}
/// Cancels only this connection. Cancellation is permanent; create a new transport.
#[derive(Clone)]
pub struct CancellationHandle {
    flags: Arc<Flags>,
}
impl CancellationHandle {
    pub fn cancel(&self) {
        self.flags.cancelled.store(true, Ordering::SeqCst);
        self.flags.tainted.store(true, Ordering::SeqCst);
        self.flags.notify.notify_waiters();
    }
    pub fn is_tainted(&self) -> bool {
        self.flags.tainted.load(Ordering::SeqCst)
    }
    pub fn is_cancelled(&self) -> bool {
        self.flags.cancelled.load(Ordering::SeqCst)
    }
    async fn cancelled(&self) {
        loop {
            let notified = self.flags.notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.is_cancelled() {
                return;
            }
            notified.await;
        }
    }
}
struct ExchangeGuard {
    flags: Arc<Flags>,
    complete: bool,
}
impl ExchangeGuard {
    fn complete(mut self) {
        self.complete = true;
    }
}
impl Drop for ExchangeGuard {
    fn drop(&mut self) {
        if !self.complete {
            self.flags.tainted.store(true, Ordering::SeqCst);
        }
        self.flags.active.store(false, Ordering::SeqCst);
    }
}

pub(crate) struct Connection<T> {
    io: T,
    stash: Vec<u8>,
    pub limits: IoLimits,
    flags: Arc<Flags>,
}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Connection<T> {
    pub fn new(io: T, limits: IoLimits) -> Result<Self> {
        if !(64..=1024 * 1024).contains(&limits.max_text_bytes)
            || !(4..=8 * 1024 * 1024).contains(&limits.max_binary_bytes)
            || [
                limits.write_timeout,
                limits.text_timeout,
                limits.scan_timeout,
                limits.receiver_timeout,
            ]
            .iter()
            .any(|t| t.is_zero() || *t > Duration::from_secs(300))
        {
            return Err(InstrumentError::InvalidRequest(
                "I/O bounds exceed supported limits".into(),
            ));
        }
        Ok(Self {
            io,
            stash: Vec::new(),
            limits,
            flags: Arc::new(Flags::default()),
        })
    }
    pub fn cancellation_handle(&self) -> CancellationHandle {
        CancellationHandle {
            flags: self.flags.clone(),
        }
    }
    pub fn tainted(&self) -> bool {
        self.flags.tainted.load(Ordering::SeqCst)
    }
    pub fn closed(&self) -> bool {
        self.flags.closed.load(Ordering::SeqCst)
    }
    pub fn ensure(&self) -> Result<()> {
        if self.closed() {
            return Err(InstrumentError::Closed);
        }
        if self.tainted() {
            return Err(InstrumentError::Tainted);
        }
        Ok(())
    }
    pub fn taint(&self) {
        self.flags.tainted.store(true, Ordering::SeqCst);
    }
    fn begin(&self) -> Result<ExchangeGuard> {
        self.ensure()?;
        // Active is separate from taint so a dropped future poisons the connection.
        // The mutable adapter borrow prevents concurrent exchanges on one transport.
        self.flags.active.store(true, Ordering::SeqCst);
        Ok(ExchangeGuard {
            flags: self.flags.clone(),
            complete: false,
        })
    }
    pub async fn close(&mut self) -> Result<()> {
        self.flags.closed.store(true, Ordering::SeqCst);
        self.stash.clear();
        match tokio::time::timeout(self.limits.write_timeout, self.io.shutdown()).await {
            Ok(result) => result.map_err(InstrumentError::Io),
            Err(_) => Err(InstrumentError::Timeout),
        }
    }
    async fn clean_input(&mut self) -> Result<()> {
        if !self.stash.is_empty() {
            return Err(InstrumentError::Protocol(
                "unread bytes before command".into(),
            ));
        }
        poll_fn(|cx| {
            let mut bytes = [0u8; 4096];
            let mut buffer = ReadBuf::new(&mut bytes);
            match Pin::new(&mut self.io).poll_read(cx, &mut buffer) {
                Poll::Pending => Poll::Ready(Ok(())),
                Poll::Ready(Err(error)) => Poll::Ready(Err(InstrumentError::Io(error))),
                Poll::Ready(Ok(())) if buffer.filled().is_empty() => Poll::Ready(Err(
                    InstrumentError::Io(std::io::ErrorKind::UnexpectedEof.into()),
                )),
                Poll::Ready(Ok(())) => Poll::Ready(Err(InstrumentError::Protocol(
                    "unread bytes before command".into(),
                ))),
            }
        })
        .await
    }
    async fn send(&mut self, command: &str) -> Result<()> {
        self.clean_input().await?;
        let data = format!("{command}\r");
        tokio::time::timeout(self.limits.write_timeout, async {
            self.io.write_all(data.as_bytes()).await?;
            self.io.flush().await
        })
        .await
        .map_err(|_| InstrumentError::Timeout)??;
        Ok(())
    }
    async fn chunk(&mut self, maximum: usize) -> Result<()> {
        let mut bytes = [0u8; 4096];
        let count = self.io.read(&mut bytes).await?;
        if count == 0 {
            return Err(InstrumentError::Io(
                std::io::ErrorKind::UnexpectedEof.into(),
            ));
        }
        if self.stash.len().saturating_add(count) > maximum {
            return Err(InstrumentError::Protocol(
                "reply exceeds bounded frame size".into(),
            ));
        }
        self.stash.extend_from_slice(&bytes[..count]);
        Ok(())
    }
    async fn exact(&mut self, count: usize) -> Result<Vec<u8>> {
        if count > self.limits.max_binary_bytes {
            return Err(InstrumentError::Protocol(
                "binary size exceeds limit".into(),
            ));
        }
        let mut output = Vec::with_capacity(count);
        while output.len() < count {
            if self.stash.is_empty() {
                self.chunk(self.limits.max_binary_bytes.saturating_add(4096))
                    .await?;
            }
            let take = (count - output.len()).min(self.stash.len());
            output.extend(self.stash.drain(..take));
        }
        Ok(output)
    }
    pub async fn text(
        &mut self,
        command: &str,
        deadline: Duration,
        leading_prompt: bool,
    ) -> Result<String> {
        validate_command(command)?;
        let guard = self.begin()?;
        let cancel = self.cancellation_handle();
        let result = tokio::select! { biased;
            _=cancel.cancelled()=>Err(InstrumentError::Cancelled),
            result=tokio::time::timeout(deadline,async {
                self.send(command).await?;
                loop {
                    self.chunk(self.limits.max_text_bytes).await?;
                    // Stock ea71dfd emits all four bytes, including the final
                    // space. A split `ch>` must not leave that byte to taint or
                    // prefix the next command's response.
                    if self.stash.ends_with(b"ch> ") {
                        let raw=std::mem::take(&mut self.stash);
                        return parse_text(&raw,command,leading_prompt);
                    }
                }
            })=>result.map_err(|_|InstrumentError::Timeout).and_then(|r|r),
        };
        if result.is_ok() {
            guard.complete();
        }
        result
    }
    pub async fn line(&mut self, command: &str) -> Result<String> {
        validate_command(command)?;
        let guard = self.begin()?;
        let cancel = self.cancellation_handle();
        let result = tokio::select! { biased;
            _=cancel.cancelled()=>Err(InstrumentError::Cancelled),
            result=tokio::time::timeout(self.limits.receiver_timeout,async {
                self.send(command).await?;
                loop {
                    if let Some(newline)=self.stash.iter().position(|&c|c==b'\n') {
                        let raw:Vec<u8>=self.stash.drain(..=newline).collect();
                        if !self.stash.is_empty() { return Err(InstrumentError::Protocol("trailing receiver response".into())); }
                        let text=std::str::from_utf8(&raw).map_err(|_|InstrumentError::Protocol("non-ASCII receiver frame".into()))?;
                        if !text.is_ascii() { return Err(InstrumentError::Protocol("non-ASCII receiver frame".into())); }
                        return Ok(text.trim().to_owned());
                    }
                    self.chunk(self.limits.max_text_bytes).await?;
                }
            })=>result.map_err(|_|InstrumentError::Timeout).and_then(|r|r),
        };
        if result.is_ok() {
            guard.complete();
        }
        result
    }
    pub async fn binary(
        &mut self,
        command: &str,
        size: Option<usize>,
        deadline: Duration,
    ) -> Result<Vec<u8>> {
        validate_command(command)?;
        if size.is_some_and(|size| size > self.limits.max_binary_bytes) {
            return Err(InstrumentError::InvalidRequest(
                "binary request exceeds limit".into(),
            ));
        }
        let guard = self.begin()?;
        let cancel = self.cancellation_handle();
        let result = tokio::select! { biased;
            _=cancel.cancelled()=>Err(InstrumentError::Cancelled),
            result=tokio::time::timeout(deadline,async {
                self.send(command).await?; let echo=format!("{command}\r\n");
                if self.exact(echo.len()).await?!=echo.as_bytes() { return Err(InstrumentError::Protocol("unexpected binary echo".into())); }
                let count=match size { Some(count)=>count, None=>u32::from_le_bytes(self.exact(4).await?.try_into().unwrap()) as usize };
                let payload=self.exact(count).await?;
                if self.exact(4).await?!=b"ch> " || !self.stash.is_empty() { return Err(InstrumentError::Protocol("invalid binary terminal or trailing data".into())); }
                Ok(payload)
            })=>result.map_err(|_|InstrumentError::Timeout).and_then(|r|r),
        };
        if result.is_ok() {
            guard.complete();
        }
        result
    }
}
fn validate_command(command: &str) -> Result<()> {
    if command.is_empty()
        || command.len() > 200
        || !command.is_ascii()
        || command.bytes().any(|c| c < 32 || c == 127)
    {
        return Err(InstrumentError::InvalidRequest(
            "invalid bounded ASCII command".into(),
        ));
    }
    Ok(())
}
fn parse_text(raw: &[u8], command: &str, leading_prompt: bool) -> Result<String> {
    if !raw.is_ascii()
        || raw
            .iter()
            .any(|&c| (c < 32 && !matches!(c, b'\r' | b'\n' | b'\t')) || c == 127)
    {
        return Err(InstrumentError::Protocol(
            "invalid tinySA ASCII frame".into(),
        ));
    }
    let text = std::str::from_utf8(raw).unwrap();
    let mut lines: Vec<&str> = text
        .lines()
        .map(|line| line.trim_matches([' ', '\t', '\r']))
        .filter(|line| !line.is_empty())
        .collect();
    if leading_prompt && lines.len() > 1 && lines[0] == "ch>" {
        lines.remove(0);
    }
    if lines.last() != Some(&"ch>")
        || lines[..lines.len().saturating_sub(1)]
            .iter()
            .any(|line| line.contains("ch>"))
    {
        return Err(InstrumentError::Protocol(
            "incomplete or ambiguous tinySA prompt".into(),
        ));
    }
    lines.pop();
    if lines.first() == Some(&command) {
        lines.remove(0);
    }
    Ok(lines.join("\n"))
}
