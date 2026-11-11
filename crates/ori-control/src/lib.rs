//! Core-owned coordination only: no port opening, adapter creation or SQL.
mod types;
pub use types::*;
mod request;
pub use request::{
    MAX_REQUEST_BYTES, Submission, parse_operation, parse_setting, parse_submission,
};
mod plan;
pub use plan::{ExecutionPlan, plan_operation, status_from_tinysa};
mod artifact;
pub use artifact::{MAX_BLOB_BYTES, raw_document, screen_png, validate_wav};
mod result;
pub use result::{catalog, service_guide};
mod coordinator;
pub use coordinator::{ControlConfig, Coordinator};
