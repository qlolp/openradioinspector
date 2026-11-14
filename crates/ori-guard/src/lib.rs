//! Monitoring business domain over the canonical Archive actor and explicit
//! core-owned effects. Physical acceptance remains a separate release gate.
#[cfg(test)]
extern crate self as ori_guard;

pub mod control;
pub use control::*;
mod audio_evidence;
mod baseline;
pub use audio_evidence::AudioClip;
mod grants;
pub use grants::InstrumentControlGrant;
mod history;
mod manual;
mod operations;
mod preflight;
mod receiver;
mod runtime;
mod scan_plan;
#[cfg(test)]
mod scan_plan_tests;
mod search;
mod service;
mod supervisor;
#[cfg(test)]
#[path = "../tests/common/mod.rs"]
mod test_common;
mod util;
pub use history::HistoryQuery;
pub use ori_monitoring::{ScanBand, ScanPlan};
pub use service::{Clock, GuardError, GuardService, IdKind, IdSource, Result};
pub use supervisor::GuardShutdown;
