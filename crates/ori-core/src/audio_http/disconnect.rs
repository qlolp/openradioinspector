//! Observe the original PCM peer's EOF in the same connection task. Hyper's
//! global half-close support intentionally leaves other finite responses live.
use std::{
    io,
    pin::Pin,
    task::{Context, Poll},
};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

pub(crate) struct DisconnectIo<I> {
    inner: I,
    monitor: bool,
    output_started: bool,
    output_flushed: bool,
    buffered: Option<u8>,
}
impl<I> DisconnectIo<I> {
    /// Called only after the existing head gate has fully validated this head.
    pub fn new(inner: I, head: &[u8]) -> Self {
        let mut headers = [httparse::EMPTY_HEADER; 100];
        let mut request = httparse::Request::new(&mut headers);
        let monitor = matches!(request.parse(head), Ok(httparse::Status::Complete(end)) if end == head.len())
            && request.method == Some("GET")
            && request
                .headers
                .iter()
                .filter(|header| header.name.eq_ignore_ascii_case("Content-Length"))
                .all(|header| {
                    !header.value.is_empty() && header.value.iter().all(|byte| *byte == b'0')
                })
            && request
                .path
                .and_then(|target| target.parse::<axum::http::Uri>().ok())
                .and_then(|uri| crate::http::path_parts(uri.path()).ok())
                .is_some_and(|parts| matches!(super::route(&parts), Some((_, "audio", "stream"))));
        Self {
            inner,
            monitor,
            output_started: false,
            output_flushed: false,
            buffered: None,
        }
    }
}
impl<I: AsyncRead + Unpin> DisconnectIo<I> {
    fn peer(&mut self, cx: &mut Context<'_>) -> io::Result<()> {
        if !self.monitor || !self.output_flushed || self.buffered.is_some() {
            return Ok(());
        }
        let mut byte = [0];
        let mut read = ReadBuf::new(&mut byte);
        match Pin::new(&mut self.inner).poll_read(cx, &mut read) {
            Poll::Ready(Err(error)) => Err(error),
            Poll::Ready(Ok(())) if read.filled().is_empty() => Err(io::Error::new(
                io::ErrorKind::ConnectionAborted,
                "uploaded PCM peer closed",
            )),
            Poll::Ready(Ok(())) => {
                // Preserve unexpected input instead of consuming/discarding a
                // subsequent byte. Like the original MSG_PEEK loop, unread
                // data prevents interpreting an EOF behind it as disconnect.
                self.buffered = Some(byte[0]);
                Ok(())
            }
            Poll::Pending => Ok(()), // The connection's own read waker is armed.
        }
    }
}
impl<I: AsyncRead + Unpin> AsyncRead for DisconnectIo<I> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if buf.remaining() > 0
            && let Some(byte) = this.buffered.take()
        {
            buf.put_slice(&[byte]);
            return Poll::Ready(Ok(()));
        }
        Pin::new(&mut this.inner).poll_read(cx, buf)
    }
}
impl<I: AsyncRead + AsyncWrite + Unpin> AsyncWrite for DisconnectIo<I> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        this.peer(cx)?;
        let result = Pin::new(&mut this.inner).poll_write(cx, buf);
        if matches!(result, Poll::Ready(Ok(count)) if count > 0) {
            this.output_started = true;
        }
        result
    }
    fn poll_write_vectored(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bufs: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        this.peer(cx)?;
        let result = Pin::new(&mut this.inner).poll_write_vectored(cx, bufs);
        if matches!(result, Poll::Ready(Ok(count)) if count > 0) {
            this.output_started = true;
        }
        result
    }
    fn is_write_vectored(&self) -> bool {
        self.inner.is_write_vectored()
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let result = Pin::new(&mut this.inner).poll_flush(cx);
        if matches!(result, Poll::Ready(Ok(()))) && this.output_started {
            this.output_flushed = true;
        }
        // Hyper polls flush after a pending response body, including when its
        // output buffer is empty. EOF wakes that same task and fails the next
        // flush; Hyper then disposes its actual body/subscription.
        this.peer(cx)?;
        result
    }
    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_shutdown(cx)
    }
}
