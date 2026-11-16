#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[test]
    fn ring_is_bounded_and_freezing_preserves_the_last_exchange() {
        let root = tempfile::tempdir().unwrap();
        let ring = WireRing::new(root.path(), 1024).unwrap();
        for n in 0..40 {
            ring.record("analyzer", "read", format!("reply-{n:03}").as_bytes());
        }
        ring.freeze();
        let before: Vec<_> = ring
            .paths
            .iter()
            .map(|p| std::fs::read(p).unwrap())
            .collect();
        assert!(before.iter().map(Vec::len).sum::<usize>() <= 2048);
        assert!(
            before
                .iter()
                .any(|b| String::from_utf8_lossy(b).contains("7265706c792d303339"))
        );
        ring.record("analyzer", "read", b"must not replace failure");
        for (path, bytes) in ring.paths.iter().zip(before) {
            assert_eq!(std::fs::read(path).unwrap(), bytes);
        }
        assert!(!ring.failed());
    }

    #[tokio::test]
    async fn expected_usb_error_does_not_destroy_the_later_unexpected_failure_window() {
        let root = tempfile::tempdir().unwrap();
        let ring = Arc::new(WireRing::new(root.path(), 4096).unwrap());
        let (a, b) = tokio::io::duplex(16);
        drop(b);
        let mut logged = LoggedTransport {
            inner: Box::new(a),
            ring: ring.clone(),
            identity: "controlled-usb".into(),
        };
        assert!(logged.write_all(b"scan\r").await.is_err());
        ring.record("reconnected", "read", b"later-reply");
        ring.freeze();
        let trace = std::fs::read_to_string(&ring.paths[0]).unwrap();
        assert!(trace.contains("write_error"));
        assert!(
            trace.contains("6c617465722d7265706c79"),
            "Only the controller can distinguish expected unplug from spontaneous failure"
        );
    }

    #[tokio::test]
    async fn logging_observes_only_bytes_really_read_or_written_and_never_changes_them() {
        let root = tempfile::tempdir().unwrap();
        let ring = Arc::new(WireRing::new(root.path(), 4096).unwrap());
        let (a, mut b) = tokio::io::duplex(64);
        let mut logged = LoggedTransport {
            inner: Box::new(a),
            ring: ring.clone(),
            identity: "actual-port".into(),
        };
        logged.write_all(b"scan 1 2 3\r").await.unwrap();
        let mut command = [0; 11];
        b.read_exact(&mut command).await.unwrap();
        assert_eq!(&command, b"scan 1 2 3\r");
        b.write_all(b"bad-token\r\n").await.unwrap();
        let mut reply = [0; 11];
        logged.read_exact(&mut reply).await.unwrap();
        assert_eq!(&reply, b"bad-token\r\n");
        ring.freeze();
        let trace = std::fs::read_to_string(&ring.paths[0]).unwrap();
        assert!(trace.contains("7363616e2031203220330d"));
        assert!(trace.contains("6261642d746f6b656e0d0a"));
    }
}
use crate::devices::{BoxTransport, DeviceFuture, DeviceResult, TransportFactory};
use ori_contracts::DeviceStream;
use serde_json::json;
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context, Poll},
};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

#[derive(Debug)]
struct State {
    file: File,
    index: usize,
    bytes: usize,
    frozen: bool,
}
/// Two alternating files, 32 MiB each. The controller freezes unexpected failures;
/// an intentional USB checkpoint must leave the later diagnostic window alive.
#[derive(Debug)]
pub(super) struct WireRing {
    paths: [PathBuf; 2],
    state: Mutex<State>,
    limit: usize,
    failed: AtomicBool,
}
fn open(path: &Path, new: bool) -> std::io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true);
    if new {
        options.create_new(true);
    } else {
        options.truncate(true);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    options.open(path)
}
impl WireRing {
    pub fn new(root: &Path, limit: usize) -> std::io::Result<Self> {
        let paths = [
            root.join(format!("wire-{}-0.jsonl", std::process::id())),
            root.join(format!("wire-{}-1.jsonl", std::process::id())),
        ];
        let file = open(&paths[0], true)?;
        open(&paths[1], true)?;
        Ok(Self {
            paths,
            state: Mutex::new(State {
                file,
                index: 0,
                bytes: 0,
                frozen: false,
            }),
            limit,
            failed: AtomicBool::new(false),
        })
    }
    pub fn failed(&self) -> bool {
        self.failed.load(Ordering::Acquire)
    }
    pub fn freeze(&self) {
        if let Ok(mut state) = self.state.lock() {
            state.frozen = true;
            if state.file.sync_all().is_err() {
                self.failed.store(true, Ordering::Release);
            }
        } else {
            self.failed.store(true, Ordering::Release);
        }
    }
    pub fn record(&self, identity: &str, direction: &str, bytes: &[u8]) {
        let result = (|| -> std::io::Result<()> {
            let mut state = self
                .state
                .lock()
                .map_err(|_| std::io::Error::other("wire ring lock"))?;
            if state.frozen {
                return Ok(());
            }
            for block in bytes.chunks(4096) {
                let hex: String = block.iter().map(|b| format!("{b:02x}")).collect();
                let mut line = serde_json::to_vec(
                    &json!({"at_ns":super::monotonic_ns(),"port_identity":identity,"direction":direction,"hex":hex}),
                )?;
                line.push(b'\n');
                if line.len() > self.limit {
                    return Err(std::io::Error::other("wire record exceeds ring"));
                }
                if state.bytes + line.len() > self.limit {
                    state.file.sync_all()?;
                    state.index = 1 - state.index;
                    state.file = open(&self.paths[state.index], false)?;
                    state.bytes = 0;
                }
                state.file.write_all(&line)?;
                state.bytes += line.len();
            }
            Ok(())
        })();
        if result.is_err() {
            self.failed.store(true, Ordering::Release);
            self.freeze();
        }
    }
}
pub(super) struct LoggedFactory {
    pub inner: Arc<dyn TransportFactory>,
    pub ring: Arc<WireRing>,
}
impl TransportFactory for LoggedFactory {
    fn identity(&self, stream: &DeviceStream) -> DeviceResult<String> {
        self.inner.identity(stream)
    }
    fn open<'a>(&'a self, stream: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        Box::pin(async move {
            let identity = self.inner.identity(stream)?;
            let inner = self.inner.open(stream).await?;
            Ok(Box::new(LoggedTransport {
                inner,
                ring: self.ring.clone(),
                identity,
            }) as BoxTransport)
        })
    }
}
struct LoggedTransport {
    inner: BoxTransport,
    ring: Arc<WireRing>,
    identity: String,
}
impl AsyncRead for LoggedTransport {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let before = buffer.filled().len();
        let result = Pin::new(&mut self.inner).poll_read(cx, buffer);
        if let Poll::Ready(outcome) = &result {
            match outcome {
                Ok(()) => self
                    .ring
                    .record(&self.identity, "read", &buffer.filled()[before..]),
                Err(e) => {
                    self.ring
                        .record(&self.identity, "read_error", e.to_string().as_bytes());
                }
            }
        }
        result
    }
}
impl AsyncWrite for LoggedTransport {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        let result = Pin::new(&mut self.inner).poll_write(cx, bytes);
        if let Poll::Ready(outcome) = &result {
            match outcome {
                Ok(count) => self.ring.record(&self.identity, "write", &bytes[..*count]),
                Err(e) => {
                    self.ring
                        .record(&self.identity, "write_error", e.to_string().as_bytes());
                }
            }
        }
        result
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}
