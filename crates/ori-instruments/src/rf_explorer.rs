//! Receive-only RF Explorer 3G Combo Standard on a single Core-owned transport.
mod protocol;

use crate::{
    CancellationHandle, ExecutionCheck, InstrumentError, IoLimits, Result, SpectrumSample,
    transport::Connection,
};
use protocol::{Configuration, Frame, MAX_HZ, MAX_SPAN_HZ, MIN_HZ, MIN_SPAN_HZ, next_frame, send};
use serde_json::{Value, json};
use tokio::io::{AsyncRead, AsyncWrite};

// Also stop when an always-readable stream supplies only unrelated frames.
const MAX_INTERVENING_FRAMES: usize = 1024;
fn missing_frame() -> InstrumentError {
    InstrumentError::Protocol(
        "RF Explorer supplied too many frames without the expected data".into(),
    )
}

/// Validate command precision and the Standard combo's envelope before opening a port.
/// The instrument's reported active-board limits are checked before any tuning write.
pub fn validate_rf_explorer_window(window: Option<(u64, u64)>) -> Result<()> {
    if let Some((start, stop)) = window
        && (!start.is_multiple_of(1000)
            || !stop.is_multiple_of(1000)
            || start < MIN_HZ
            || stop > MAX_HZ
            || !stop
                .checked_sub(start)
                .is_some_and(|span| (MIN_SPAN_HZ..=MAX_SPAN_HZ).contains(&span)))
    {
        return Err(InstrumentError::InvalidRequest(
            "RF Explorer requires kHz-aligned endpoints within 15–2700 MHz and a 112 kHz–600 MHz span".into(),
        ));
    }
    Ok(())
}

/// A pushed stream of length-delimited sweeps. In follow-device mode the sole
/// outgoing command is the C0 readback request.
pub struct RfExplorer<T> {
    connection: Connection<T>,
    requested: Option<(u64, u64)>,
    config: Option<Configuration>,
}

impl<T: AsyncRead + AsyncWrite + Unpin + Send> RfExplorer<T> {
    pub fn new(io: T, window: Option<(u64, u64)>, limits: IoLimits) -> Result<Self> {
        validate_rf_explorer_window(window)?;
        Ok(Self {
            connection: Connection::new(io, limits)?,
            requested: window,
            config: None,
        })
    }

    pub fn cancellation_handle(&self) -> CancellationHandle {
        self.connection.cancellation_handle()
    }

    /// Cached readback, invalidated immediately when the transport is tainted or closed.
    pub fn status(&self) -> Value {
        let config = self
            .config
            .as_ref()
            .filter(|_| !self.connection.tainted() && !self.connection.closed());
        let mut status = config
            .map(Configuration::settings)
            .unwrap_or_else(|| json!({}));
        status["profile_verified"] = json!(config.is_some());
        status["restart_required"] = json!(self.connection.tainted());
        status["closed"] = json!(self.connection.closed());
        status
    }

    pub async fn prepare_checked(&mut self, check: ExecutionCheck) -> Result<()> {
        let _scope = self.connection.execution_scope(check)?;
        self.prepare().await
    }

    pub async fn prepare(&mut self) -> Result<()> {
        self.connection.ensure()?;
        if self.config.is_some() {
            return Ok(());
        }
        let limits = self.connection.limits.clone();
        let requested = self.requested;
        let cancel = self.cancellation_handle();
        let config = self
            .connection
            .streaming(limits.text_timeout, async |io| {
                send(io, b"#\x04C0", &limits).await?;
                let original = read_profile(io, &limits).await?;
                match requested {
                    Some(window) => tune_window(io, &limits, &cancel, original, window).await,
                    None => Ok(original),
                }
            })
            .await?;
        self.config = Some(config);
        Ok(())
    }

    pub async fn poll_checked(&mut self, check: ExecutionCheck) -> Result<SpectrumSample> {
        let _scope = self.connection.execution_scope(check)?;
        self.poll().await
    }

    pub async fn poll(&mut self) -> Result<SpectrumSample> {
        self.connection.ensure()?;
        let config = self.config.as_mut().ok_or_else(|| {
            InstrumentError::UnsafeState(
                "RF Explorer identity and receive configuration required before acquisition".into(),
            )
        })?;
        let requested = self.requested;
        let limits = self.connection.limits.clone();
        let (counts, interval) = self
            .connection
            .streaming(limits.scan_timeout, async |io| {
                for _ in 0..MAX_INTERVENING_FRAMES {
                    match next_frame(io, &limits).await? {
                        Frame::Sweep(counts, interval) => {
                            if counts.len() != config.points {
                                return Err(InstrumentError::Protocol(
                                    "RF Explorer sweep length differs from device configuration"
                                        .into(),
                                ));
                            }
                            return Ok((counts, interval));
                        }
                        Frame::Configuration(updated) => {
                            if requested.is_some_and(|window| {
                                updated.window() != window || !updated.same_board(config)
                            }) {
                                return Err(InstrumentError::UnsafeState(
                                    "RF Explorer left the requested receive window or board".into(),
                                ));
                            }
                            *config = updated;
                        }
                        Frame::Detector(mode) => config.detector = mode,
                        Frame::Reset => return Err(reset_error()),
                        Frame::Model | Frame::Auxiliary => {}
                    }
                }
                Err(missing_frame())
            })
            .await?;
        Ok(SpectrumSample {
            kind: config.detector.kind(),
            frequencies_hz: (0..config.points as u64)
                .map(|i| config.start_hz + i * config.step_hz)
                .collect(),
            levels: counts
                .into_iter()
                .map(|count| f64::from(config.offset_db) - f64::from(count) / 2.0)
                .collect(),
            level_unit: "dBm".into(),
            interval,
            settings: config.settings(),
        })
    }

    /// The monitoring connection owns no RF output. Shutdown the existing transport.
    pub async fn close(&mut self) -> Result<()> {
        if self.connection.closed() {
            return Ok(());
        }
        self.connection.close().await
    }
}

fn reset_error() -> InstrumentError {
    InstrumentError::UnsafeState(
        "RF Explorer restarted; fresh identity and configuration required".into(),
    )
}

async fn read_profile(
    io: &mut (impl AsyncRead + Unpin),
    limits: &IoLimits,
) -> Result<Configuration> {
    let mut identified = false;
    let mut config: Option<Configuration> = None;
    for _ in 0..MAX_INTERVENING_FRAMES {
        match next_frame(io, limits).await? {
            Frame::Model => identified = true,
            Frame::Configuration(updated) => config = Some(updated),
            Frame::Detector(mode) => {
                if let Some(config) = &mut config {
                    config.detector = mode;
                }
            }
            Frame::Reset => {
                identified = false;
                config = None;
            }
            Frame::Auxiliary | Frame::Sweep(_, _) => {}
        }
        if identified && let Some(config) = config.take() {
            return Ok(config);
        }
    }
    Err(missing_frame())
}

async fn tune_window(
    io: &mut (impl AsyncRead + AsyncWrite + Unpin),
    limits: &IoLimits,
    cancel: &CancellationHandle,
    original: Configuration,
    window: (u64, u64),
) -> Result<Configuration> {
    if !original.contains(window) {
        return Err(InstrumentError::InvalidRequest(
            "requested window exceeds the active RF Explorer board limits".into(),
        ));
    }
    if original.window() == window {
        return Ok(original);
    }
    if !(window.1 - window.0).is_multiple_of(original.points as u64 - 1) {
        return Err(InstrumentError::InvalidRequest(
            "requested RF Explorer span cannot produce an exact integer-Hz bin step".into(),
        ));
    }
    if cancel.is_cancelled() {
        return Err(InstrumentError::Cancelled);
    }
    send(io, &original.tune_command(window), limits).await?;
    for _ in 0..MAX_INTERVENING_FRAMES {
        match next_frame(io, limits).await? {
            Frame::Configuration(updated) if updated.confirms_tuning(&original, window) => {
                return Ok(updated);
            }
            Frame::Configuration(_) => {
                return Err(InstrumentError::Rejected(
                    "RF Explorer readback did not confirm the exact window and preserved settings"
                        .into(),
                ));
            }
            Frame::Detector(mode) if mode != original.detector => {
                return Err(InstrumentError::Rejected(
                    "RF Explorer detector changed while tuning".into(),
                ));
            }
            Frame::Reset => return Err(reset_error()),
            // Sweeps already in flight cannot confirm that the command took effect.
            _ => {}
        }
    }
    Err(missing_frame())
}
