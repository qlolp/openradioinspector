use super::{DeviceError, DeviceResult, transport::OwnedTransport};
use ori_contracts::{DeviceRole, DeviceStream, JSONObject};
use ori_instruments::{CancellationHandle, ExecutionCheck, IoLimits, RfExplorer};
use serde_json::{Value, json};
use std::sync::Mutex;

pub(super) const ADAPTER: &str = "rf-explorer-3g-serial";
pub(super) type Window = Option<(u64, u64)>;

pub(super) fn window(stream: &DeviceStream, settings: &JSONObject) -> DeviceResult<Window> {
    if stream.role != DeviceRole::SpectrumAnalyzer || stream.level_unit != "dBm" {
        return Err(DeviceError::Invalid(
            "RF Explorer requires a spectrum_analyzer stream in dBm".into(),
        ));
    }
    if stream
        .port
        .as_deref()
        .is_none_or(|port| port.trim().is_empty() || port.contains('\0') || port.contains("://"))
    {
        return Err(DeviceError::Invalid(
            "Explicit RF Explorer serial port required".into(),
        ));
    }
    for key in settings.keys() {
        if !matches!(
            key.as_str(),
            "adapter" | "baud" | "start_hz" | "stop_hz" | "period_s" | "max_consecutive_errors"
        ) {
            return Err(DeviceError::Invalid(format!(
                "Unsupported RF Explorer setting: {key}"
            )));
        }
    }
    if settings.get("period_s").is_some_and(|v| !v.is_number()) {
        return Err(DeviceError::Invalid(
            "RF Explorer period_s must be a number".into(),
        ));
    }
    let window = match (settings.get("start_hz"), settings.get("stop_hz")) {
        (None, None) => None,
        (Some(start), Some(stop)) => Some(start.as_u64().zip(stop.as_u64()).ok_or_else(|| {
            DeviceError::Invalid("RF Explorer window endpoints must be integer Hz".into())
        })?),
        _ => {
            return Err(DeviceError::Invalid(
                "Supply both RF Explorer window endpoints in integer Hz".into(),
            ));
        }
    };
    ori_instruments::validate_rf_explorer_window(window)
        .map_err(|error| DeviceError::Invalid(error.to_string()))?;
    Ok(window)
}

/// The stream worker owns acquisition. Other tasks can inspect its cached
/// readback or cancel an in-flight read without borrowing the serial handle.
pub(super) struct Connection {
    driver: tokio::sync::Mutex<Option<RfExplorer<OwnedTransport>>>,
    pub cancel: CancellationHandle,
    status: Mutex<Value>,
}
impl Connection {
    pub fn new(io: OwnedTransport, window: Window, limits: IoLimits) -> DeviceResult<Self> {
        let driver = RfExplorer::new(io, window, limits)?;
        Ok(Self {
            cancel: driver.cancellation_handle(),
            status: Mutex::new(driver.status()),
            driver: tokio::sync::Mutex::new(Some(driver)),
        })
    }
    pub fn current(&self) -> Value {
        self.status
            .lock()
            .expect("RF Explorer status poisoned")
            .clone()
    }
    fn cache(&self, driver: &RfExplorer<OwnedTransport>) {
        *self.status.lock().expect("RF Explorer status poisoned") = driver.status();
    }
    pub async fn prepare(&self, check: ExecutionCheck) -> DeviceResult<()> {
        let mut slot = self.driver.lock().await;
        let driver = slot.as_mut().ok_or(DeviceError::Closed)?;
        let result = driver.prepare_checked(check).await;
        self.cache(driver);
        Ok(result?)
    }
    pub async fn poll(&self, stream: &DeviceStream, check: ExecutionCheck) -> DeviceResult<Value> {
        let mut slot = self.driver.lock().await;
        let driver = slot.as_mut().ok_or(DeviceError::Closed)?;
        let result = driver.poll_checked(check).await;
        self.cache(driver);
        let sample = result?;
        Ok(
            json!({"id":crate::new_id(), "session_id":stream.session_id, "stream_id":stream.id,
            "frequencies_hz":sample.frequencies_hz, "levels":sample.levels, "level_unit":sample.level_unit,
            "settings":sample.settings, "interval":sample.interval}),
        )
    }
    pub async fn close(&self) -> DeviceResult<()> {
        if let Some(mut driver) = self.driver.lock().await.take() {
            let result = driver.close().await;
            self.cache(&driver);
            result?;
        }
        Ok(())
    }
}
