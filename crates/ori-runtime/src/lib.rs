//! Runtime ownership, controlled cutover, bounded children and LAN security.
pub mod lease;
pub mod security;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("I/O failure: {0}")]
    Io(#[from] std::io::Error),
    #[error("invalid or unsafe state: {0}")]
    Unsafe(String),
    #[error("resource is already owned")]
    Busy,
    #[error("request is unauthorized")]
    Unauthorized,
    #[error("pairing window expired, exhausted or unavailable")]
    PairingUnavailable,
    #[error("owned background job was cancelled")]
    Cancelled,
    #[error("owned background job exceeded its deadline")]
    Deadline,
    #[error("owned background job exceeded a resource budget")]
    ResourceLimit,
    #[error("owned background job exceeded its output bound")]
    OutputLimit,
    #[error("protocol failure: {0}")]
    Protocol(String),
    #[error("JSON failure: {0}")]
    Json(#[from] serde_json::Error),
}
pub type Result<T> = std::result::Result<T, Error>;
mod process;
pub mod service;
pub mod supervisor;
pub mod worker;
