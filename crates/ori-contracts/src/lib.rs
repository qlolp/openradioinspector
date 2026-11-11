//! Additive v1 wire models. Use the strict codec at untrusted JSON boundaries;
//! semantic acquisition and hardware policy remain the core's responsibility.
pub const CONTRACT_NAME: &str = "openradioinspector-platform";
pub const CONTRACT_VERSION: u64 = 1;
pub mod codec;
pub mod models;
pub mod operations;
pub use codec::{
    CodecError, RestoreKeys, UtcTimestamp, encode_path_segment, from_json_slice, normalize_utc,
    parse_json_value, to_json_vec,
};
pub use models::*;
