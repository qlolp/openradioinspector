//! Trusted injected task effects; no task-file interpreter or native owner.
mod executor;
mod types;
mod validation;

pub use executor::{TaskCloseHandle, TaskExecution};
pub use types::*;
