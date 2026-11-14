//! Runtime-only legacy policies. Science/device ownership remain Core callbacks.
mod completion;
mod detection;
mod effects;
mod handoff;
mod policy;
mod service;
mod state;
pub use effects::*;
pub use service::AutomationService;
pub use state::AutomationConfig;
