//! Monitoring business domain over the canonical Archive actor and explicit
//! core-owned effects. Physical acceptance remains a separate release gate.
#[cfg(test)]
extern crate self as ori_guard;

pub mod control;
pub use control::*;
mod aor;
mod audio_evidence;
mod baseline;
mod rfe;
pub use audio_evidence::AudioClip;
mod duty;
mod duty_notifications;
mod receiver_duty;
pub use duty::DutyTrigger;
mod grants;
pub use grants::InstrumentControlGrant;
mod history;
mod signals;
pub use signals::band_plan;
mod manual;
mod operations;
mod preflight;
mod qualification;
mod storage_forecast;
pub use qualification::measurement_qualification;
mod paired_rechecks;
mod receiver;
mod receiver_checks;
pub use receiver_checks::receiver_tune_context;
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
pub use ori_monitoring::counts::ardv1_plan_key;
pub use ori_monitoring::{ScanBand, ScanPlan, ScanWindow};
pub use service::{Clock, GuardError, GuardService, IdKind, IdSource, Result, project_view};
pub use supervisor::GuardShutdown;
