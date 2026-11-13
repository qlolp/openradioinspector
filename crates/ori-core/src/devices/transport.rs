//! Connectors are explicit operator configuration. No enumeration or opening
//! happens when a manager is constructed or a session is merely started.
use super::{DeviceFuture, DeviceResult};
use ori_contracts::DeviceStream;
use std::{
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context, Poll},
};
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    sync::Notify,
};

pub trait Transport: AsyncRead + AsyncWrite + Unpin + Send + 'static {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send + 'static> Transport for T {}
pub type BoxTransport = Box<dyn Transport>;

/// Trusted embedding/test boundary, never selected by a request body. A native
/// implementation must exclude other OS serial owners before configuring it.
/// Dropping an open future must not leave a detached operation that can open a
/// transport later; any already acquired handle must remain owned by that future.
pub trait TransportFactory: Send + Sync + 'static {
    fn identity(&self, stream: &DeviceStream) -> DeviceResult<String>;
    fn open<'a>(&'a self, stream: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport>;
}

#[cfg(feature = "serial")]
pub struct NativeSerialFactory;
#[cfg(feature = "serial")]
impl TransportFactory for NativeSerialFactory {
    fn identity(&self, stream: &DeviceStream) -> DeviceResult<String> {
        let path = stream.port.as_deref().ok_or_else(|| {
            super::DeviceError::Worker(
                "Для физического прибора требуется последовательный порт".into(),
            )
        })?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::{FileTypeExt, MetadataExt};
            let canonical = std::fs::canonicalize(path)?;
            let metadata = std::fs::metadata(canonical)?;
            if !metadata.file_type().is_char_device() {
                return Err(super::DeviceError::Invalid(
                    "Native serial requires a character device".into(),
                ));
            }
            Ok(format!("unix-character:{}", metadata.rdev()))
        }
        #[cfg(windows)]
        {
            Ok(ori_instruments::serial::validate_windows_com_port(path)?)
        }
        #[cfg(not(any(unix, windows)))]
        {
            let _ = path;
            Err(super::DeviceError::Unsupported(
                "Native serial ownership is unavailable on this platform".into(),
            ))
        }
    }
    fn open<'a>(&'a self, stream: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        Box::pin(async move {
            let adapter = stream
                .settings
                .as_ref()
                .and_then(|s| s.get("adapter"))
                .and_then(serde_json::Value::as_str);
            let default = if adapter == Some("tinysa-serial") {
                9600
            } else {
                115200
            };
            let settings = stream.settings.clone().unwrap_or_default();
            let options = ori_instruments::serial::SerialOptions {
                path: stream.port.clone().ok_or_else(|| {
                    super::DeviceError::Invalid("Explicit serial path required".into())
                })?,
                baud: super::simulator::integer(&settings, "baud", default, 9600, 115200)? as u32,
            };
            // Pinned native open is nonblocking and excludes peers before any
            // termios/DTR change. No detached blocking task can open it later.
            let fence = ori_runtime::power::ContinuityFence::system()?;
            let stream = ori_instruments::serial::open(&options)?;
            Ok(
                Box::new(ori_runtime::power::ContinuityTransport::new(stream, fence))
                    as BoxTransport,
            )
        })
    }
}

#[derive(Default)]
pub(super) struct Released {
    done: AtomicBool,
    notify: Notify,
}
impl Released {
    pub async fn wait(&self) {
        loop {
            let notified = self.notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.done.load(Ordering::Acquire) {
                return;
            }
            notified.await;
        }
    }
    fn complete(&self) {
        self.done.store(true, Ordering::Release);
        self.notify.notify_waiters();
    }
}
/// Close ACK precedes actor task exit. This wrapper confirms the actual owned
/// handle is dropped, including on actor panic, before a port reservation ends.
pub(super) struct OwnedTransport {
    inner: Option<BoxTransport>,
    released: Arc<Released>,
}
impl OwnedTransport {
    pub fn new(inner: BoxTransport) -> (Self, Arc<Released>) {
        let released = Arc::new(Released::default());
        (
            Self {
                inner: Some(inner),
                released: released.clone(),
            },
            released,
        )
    }
}
impl Drop for OwnedTransport {
    fn drop(&mut self) {
        drop(self.inner.take());
        self.released.complete();
    }
}
impl AsyncRead for OwnedTransport {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(self.inner.as_mut().expect("owned transport present")).poll_read(cx, buffer)
    }
}
impl AsyncWrite for OwnedTransport {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(self.inner.as_mut().expect("owned transport present")).poll_write(cx, bytes)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(self.inner.as_mut().expect("owned transport present")).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(self.inner.as_mut().expect("owned transport present")).poll_shutdown(cx)
    }
}
