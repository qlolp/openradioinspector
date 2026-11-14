//! Pure deterministic robust-city-v1 parity detector. No archive, HTTP, runtime
//! ownership or hardware I/O; the core persists checkpoints and evidence.
mod canonical;
mod detector;
mod models;
pub use canonical::{python_canonical_json, stable};
pub use detector::{Detector, build_baseline, canonical_fingerprint_json, fingerprint};
pub use models::*;
pub use ori_contracts::Sweep;
