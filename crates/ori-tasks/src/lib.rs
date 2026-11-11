//! Runtime-only opt-in scheduling; input files are never dispatched or measured.
// The existing planning record has 43 fields; this is a compile-time macro
// bound only, without changing the input decoder's recursion limit.
#![recursion_limit = "256"]
mod book;
mod effects;
mod error;
mod model;
mod scheduler;
mod time;
pub use effects::*;
pub use error::*;
pub use model::{ControlTask, ReferenceBook, TaskReference};
pub use scheduler::{TaskScheduler, TaskSchedulerConfig};
