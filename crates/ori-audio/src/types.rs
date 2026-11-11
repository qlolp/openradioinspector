use ori_contracts::UtcTimestamp;
use std::{sync::Arc, time::Duration};

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum AudioError {
    #[error("native audio is disabled")]
    Disabled,
    #[error("native audio support was not compiled")]
    NativeUnavailable,
    #[error("audio service is closed")]
    Closed,
    #[error("audio input is already owned")]
    Busy,
    #[error("audio authorization or tune epoch was revoked")]
    Revoked,
    #[error("audio capture was cancelled")]
    Cancelled,
    #[error("selected audio input is unavailable or changed")]
    InputUnavailable,
    #[error("selected input does not support exact mono PCM16 at this rate")]
    UnsupportedFormat,
    #[error("invalid audio request: {0}")]
    InvalidRequest(&'static str),
    #[error("audio callback delivered malformed PCM")]
    MalformedPcm,
    #[error("audio callback exceeded its bounded block size")]
    BlockTooLarge,
    #[error("audio input overflowed its bounded ring")]
    Overflow,
    #[error("audio input did not deliver the requested complete interval")]
    Timeout,
    #[error("audio callback panicked")]
    CallbackPanicked,
    #[error("audio owner thread panicked")]
    ThreadPanicked,
    #[error("audio owner thread could not be started: {0}")]
    ThreadInitialization(String),
    #[error("audio backend failed: {0}")]
    Backend(String),
    #[error("audio UTC clock is unavailable")]
    Clock,
}
pub type AudioResult<T> = Result<T, AudioError>;

/// Describes original host samples. Only I16, one channel, and an explicitly
/// advertised requested rate can be captured. Other formats are never converted.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SampleEncoding {
    I16,
    F32,
    Other(String),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AudioFormatRange {
    pub channels: u16,
    pub encoding: SampleEncoding,
    pub min_sample_rate_hz: u32,
    pub max_sample_rate_hz: u32,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AudioInput {
    /// Ephemeral host enumeration index; always recheck the exact name at open.
    pub index: u32,
    pub name: String,
    pub formats: Vec<AudioFormatRange>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InputSelection {
    pub index: u32,
    pub name: String,
}
#[derive(Clone, Debug)]
pub struct CaptureRequest {
    pub input: InputSelection,
    pub sample_rate_hz: u32,
    pub frames: u64,
    /// Original acknowledged tune time. Actual open UTC is also a lower bound.
    pub not_before: UtcTimestamp,
}
#[derive(Clone, Debug)]
pub struct RecordedAudio {
    pub wav_bytes: Vec<u8>,
    pub started_at: UtcTimestamp,
    pub ended_at: UtcTimestamp,
    pub sample_rate_hz: u32,
    pub channels: u16,
    pub frames: u64,
}

/// Synchronous, cache-only gate. Capture the original immutable permit and tune
/// receipt; never renew them here. This runs on the native audio callback too,
/// so it must not wait for an actor, archive, network, or other external work.
pub trait CaptureAuthorization: Send + Sync + 'static {
    fn revalidate(&self) -> AudioResult<()>;
}
pub trait AudioClock: Send + Sync + 'static {
    fn now(&self) -> AudioResult<UtcTimestamp>;
}

#[derive(Clone, Debug)]
pub struct CaptureLimits {
    pub ring_blocks: usize,
    pub max_block_frames: usize,
    pub live_capacity: usize,
    pub max_duration: Duration,
    pub max_pcm_bytes: usize,
    pub stall_timeout: Duration,
}
impl Default for CaptureLimits {
    fn default() -> Self {
        Self {
            ring_blocks: 8,
            max_block_frames: 4096,
            live_capacity: 16,
            max_duration: Duration::from_secs(30),
            max_pcm_bytes: 2_880_000,
            stall_timeout: Duration::from_secs(5),
        }
    }
}
impl CaptureLimits {
    pub(crate) fn validate(&self) -> AudioResult<()> {
        if !(1..=256).contains(&self.ring_blocks)
            || !(1..=65536).contains(&self.max_block_frames)
            || !(1..=256).contains(&self.live_capacity)
            || self.max_duration.is_zero()
            || self.max_duration > Duration::from_secs(30)
            || self.max_pcm_bytes < 2
            || self.max_pcm_bytes > 2_880_000
            || self.stall_timeout.is_zero()
            || self.stall_timeout > Duration::from_secs(30)
        {
            return Err(AudioError::InvalidRequest("capture resource limits"));
        }
        Ok(())
    }
    pub(crate) fn validate_request(&self, request: &CaptureRequest) -> AudioResult<()> {
        let bytes = request.frames.checked_mul(2);
        let max_frames =
            (self.max_duration.as_nanos() * u128::from(request.sample_rate_hz)) / 1_000_000_000;
        if ![8000, 12000, 24000, 48000].contains(&request.sample_rate_hz)
            || request.frames == 0
            || u128::from(request.frames) > max_frames
            || bytes.is_none_or(|n| n > self.max_pcm_bytes as u64)
            || request.input.name.is_empty()
            || request.input.name.len() > 4096
        {
            return Err(AudioError::InvalidRequest(
                "rate, frame count, size or selected input",
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Default)]
pub struct NativeOptions {
    pub enabled: bool,
    pub limits: CaptureLimits,
}

#[derive(Clone, Debug)]
pub struct LiveEvent {
    /// Service-global event sequence. Neither capture nor retune resets it.
    pub sequence: u64,
    pub capture_id: u64,
    pub kind: LiveEventKind,
}
#[derive(Clone, Debug)]
pub enum LiveEventKind {
    Started,
    Pcm {
        source_sequence: u64,
        first_frame: u64,
        started_at: UtcTimestamp,
        bytes: Arc<[u8]>,
    },
    SourceGap {
        dropped_blocks: u64,
        dropped_frames: u64,
    },
    Finished {
        frames: u64,
        error: Option<AudioError>,
    },
}
#[derive(Clone, Debug)]
pub enum LiveItem {
    Event(Arc<LiveEvent>),
    /// Slow subscriber loss is explicit; it never alters the capture WAV.
    Gap {
        missed_events: u64,
    },
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PushOutcome {
    Accepted,
    Discarded,
    Dropped,
    Rejected,
}
