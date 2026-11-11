//! Explicit native serial connector. This module never enumerates ports.
//! Core ownership/cutover policy must be established before calling `open`.
use crate::{InstrumentError, Result};
use serde::{Deserialize, Serialize};
use tokio_serial::{
    ClearBuffer, DataBits, FlowControl, Parity, SerialPort, SerialPortBuilderExt, SerialStream,
    StopBits,
};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SerialOptions {
    pub path: String,
    pub baud: u32,
}
impl SerialOptions {
    /// Validation performs no physical I/O.
    pub fn validate(&self) -> Result<()> {
        if self.path.trim().is_empty()
            || self.path.contains('\0')
            || self.path.contains("://")
            || !matches!(self.baud, 9600 | 19200 | 38400 | 57600 | 115200)
        {
            return Err(InstrumentError::InvalidRequest(
                "explicit native serial path and supported baud required".into(),
            ));
        }
        #[cfg(windows)]
        validate_windows_com_port(&self.path)?;
        Ok(())
    }
}
pub fn validate_windows_com_port(path: &str) -> Result<String> {
    let name = path
        .strip_prefix("\\\\.\\")
        .unwrap_or(path)
        .to_ascii_uppercase();
    let digits = name.strip_prefix("COM").unwrap_or("");
    if digits.is_empty() || digits.starts_with('0') || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(InstrumentError::InvalidRequest(
            "native COM name required".into(),
        ));
    }
    Ok(name)
}
/// Opens only the supplied native path, configures 8N1/no flow control, and
/// excludes cooperating Unix serial owners. This function requires hardware.
pub fn open(options: &SerialOptions) -> Result<SerialStream> {
    options.validate()?;
    let builder = tokio_serial::new(&options.path, options.baud)
        .data_bits(DataBits::Eight)
        .parity(Parity::None)
        .stop_bits(StopBits::One)
        .flow_control(FlowControl::None)
        .dtr_on_open(false);
    // Pinned serialport 4.10.1 acquires TIOCEXCL + nonblocking exclusive
    // flock before reading or changing termios. Never replace this with a
    // post-open set_exclusive call; the existing Python core uses flock.
    #[cfg(unix)]
    let builder = builder.exclusive(true);
    let mut stream = builder.open_native_async().map_err(serial_error)?;
    stream
        .write_data_terminal_ready(false)
        .map_err(serial_error)?;
    stream.write_request_to_send(false).map_err(serial_error)?;
    stream.clear(ClearBuffer::All).map_err(serial_error)?;
    Ok(stream)
}
fn serial_error(error: tokio_serial::Error) -> InstrumentError {
    InstrumentError::Io(std::io::Error::other(error))
}
