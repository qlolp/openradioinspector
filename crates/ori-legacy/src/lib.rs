//! Bounded legacy panorama state and one .pan SQL owner. Scientific algorithms
//! remain the original Python implementation through an isolated callback.
mod sql;
mod types;
mod workspace;
pub use types::*;
pub use workspace::LegacyWorkspace;
