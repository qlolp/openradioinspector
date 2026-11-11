//! Core-owned, bounded, original mono PCM16 capture. Construction opens nothing;
//! only an explicit selected-input request can create a native stream.
mod actor;
mod native;
mod types;
pub use actor::{
    AudioBackend, AudioClose, AudioService, AudioStream, CaptureHandle, LiveSubscription, PcmSink,
};
pub use types::*;
