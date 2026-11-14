//! Test-client response framing. A reset after a complete reply is still closure.
use std::io;
use tokio::io::{AsyncRead, AsyncReadExt};

pub async fn read_closed_response(reader: &mut (impl AsyncRead + Unpin)) -> io::Result<Vec<u8>> {
    let mut response = Vec::new();
    while !response.ends_with(b"\r\n\r\n") {
        if response.len() >= 8192 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "response head exceeds test bound",
            ));
        }
        let byte = reader.read_u8().await.map_err(|error| {
            io::Error::new(
                error.kind(),
                format!(
                    "incomplete response head ({} bytes): {error}",
                    response.len()
                ),
            )
        })?;
        response.push(byte);
    }
    let head = std::str::from_utf8(&response)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid response head"))?;
    let mut length = None;
    for line in head.split("\r\n").skip(1).filter(|line| !line.is_empty()) {
        let (name, value) = line
            .split_once(':')
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "invalid response header"))?;
        if name.eq_ignore_ascii_case("transfer-encoding") {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unexpected response encoding",
            ));
        }
        if name.eq_ignore_ascii_case("content-length") {
            let value = value.trim();
            if length.is_some()
                || value.is_empty()
                || !value.bytes().all(|byte| byte.is_ascii_digit())
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "ambiguous response length",
                ));
            }
            let value = value.parse::<usize>().map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "invalid response length")
            })?;
            if value > 65_536 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "response body exceeds test bound",
                ));
            }
            length = Some(value);
        }
    }
    let length = length
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "missing response length"))?;
    let start = response.len();
    response.resize(start + length, 0);
    reader
        .read_exact(&mut response[start..])
        .await
        .map_err(|error| {
            io::Error::new(
                error.kind(),
                format!("incomplete response body (expected {length} bytes): {error}"),
            )
        })?;
    let mut extra = [0_u8; 1];
    match reader.read(&mut extra).await {
        Ok(0) => {}
        Err(error) if error.kind() == io::ErrorKind::ConnectionReset => {}
        Ok(_) => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "bytes after response body",
            ));
        }
        Err(error) => return Err(error),
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        pin::Pin,
        task::{Context, Poll},
    };
    use tokio::io::ReadBuf;

    const REPLY: &[u8] =
        b"HTTP/1.1 408 Request Timeout\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}";

    struct ResetAfter {
        bytes: &'static [u8],
    }
    impl AsyncRead for ResetAfter {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
            buffer: &mut ReadBuf<'_>,
        ) -> Poll<io::Result<()>> {
            if self.bytes.is_empty() {
                return Poll::Ready(Err(io::ErrorKind::ConnectionReset.into()));
            }
            let n = self.bytes.len().min(buffer.remaining());
            buffer.put_slice(&self.bytes[..n]);
            self.bytes = &self.bytes[n..];
            Poll::Ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn complete_reply_then_reset_is_a_complete_closed_http_response() {
        let mut reader = ResetAfter { bytes: REPLY };
        assert_eq!(read_closed_response(&mut reader).await.unwrap(), REPLY);
    }

    #[tokio::test]
    async fn early_reset_never_turns_a_partial_header_or_body_into_success() {
        for bytes in [
            b"HTTP/1.1 408\r\nContent-Len".as_slice(),
            b"HTTP/1.1 408 Request Timeout\r\nContent-Length: 3\r\n\r\n{}",
        ] {
            assert!(
                read_closed_response(&mut ResetAfter { bytes })
                    .await
                    .is_err()
            );
        }
    }

    #[tokio::test]
    async fn ambiguous_or_huge_length_is_refused_before_body_allocation() {
        for bytes in [
            b"HTTP/1.1 408 Request Timeout\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
                .as_slice(),
            b"HTTP/1.1 408 Request Timeout\r\nContent-Length: 1099511627776\r\n\r\n",
        ] {
            let error = read_closed_response(&mut ResetAfter { bytes })
                .await
                .unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn actual_tcp_reset_after_observed_reply_keeps_the_complete_frame() {
        use std::{os::fd::AsRawFd, time::Duration};
        use tokio::{
            io::AsyncWriteExt,
            net::{TcpListener, TcpStream},
            sync::oneshot,
        };
        struct ObservedReader {
            socket: TcpStream,
            count: usize,
            observed: Option<oneshot::Sender<()>>,
        }
        impl AsyncRead for ObservedReader {
            fn poll_read(
                self: Pin<&mut Self>,
                cx: &mut Context<'_>,
                buffer: &mut ReadBuf<'_>,
            ) -> Poll<io::Result<()>> {
                let this = self.get_mut();
                let before = buffer.filled().len();
                let result = Pin::new(&mut this.socket).poll_read(cx, buffer);
                this.count += buffer.filled().len() - before;
                if this.count >= REPLY.len()
                    && let Some(sender) = this.observed.take()
                {
                    let _ = sender.send(());
                }
                result
            }
        }
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let (observed, received) = oneshot::channel();
        let address = listener.local_addr().unwrap();
        let peer = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            socket.write_all(REPLY).await.unwrap();
            tokio::time::timeout(Duration::from_secs(2), received)
                .await
                .unwrap()
                .unwrap();
            let socket = socket.into_std().unwrap();
            let linger = libc::linger {
                l_onoff: 1,
                l_linger: 0,
            };
            // Only this owned fixture connection is reset, after the reader saw its bytes.
            assert_eq!(
                unsafe {
                    libc::setsockopt(
                        socket.as_raw_fd(),
                        libc::SOL_SOCKET,
                        libc::SO_LINGER,
                        (&linger as *const libc::linger).cast(),
                        std::mem::size_of_val(&linger) as libc::socklen_t,
                    )
                },
                0
            );
        });
        let socket = TcpStream::connect(address).await.unwrap();
        let mut reader = ObservedReader {
            socket,
            count: 0,
            observed: Some(observed),
        };
        let result =
            tokio::time::timeout(Duration::from_secs(2), read_closed_response(&mut reader))
                .await
                .unwrap();
        peer.await.unwrap();
        assert_eq!(result.unwrap(), REPLY);
    }
}
