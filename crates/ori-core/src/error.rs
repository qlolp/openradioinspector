use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum CoreError {
    #[error("invalid candidate configuration: {0}")]
    Invalid(&'static str),
    #[error("archive startup/closure failed: {0}")]
    Archive(#[from] ori_archive::ArchiveError),
    #[error("runtime ownership failed: {0}")]
    Runtime(#[from] ori_runtime::Error),
    #[error("device ownership/cleanup failed: {0}")]
    Device(#[from] crate::devices::DeviceError),
    #[error("guard startup/cleanup failed: {0}")]
    Guard(#[from] ori_guard::GuardError),
    #[error("task scheduler startup/cleanup failed: {0}")]
    Tasks(#[from] ori_tasks::TaskError),
    #[error("audio startup/cleanup failed: {0}")]
    Audio(#[from] ori_audio::AudioError),
    #[error("legacy workspace startup/cleanup failed: {0}")]
    Legacy(#[from] ori_legacy::LegacyError),
    #[error("instrument control startup/cleanup failed: {0}")]
    InstrumentControl(#[from] ori_control::ControlError),
    #[error("history startup/cleanup failed: {0}")]
    History(#[from] ori_history::HistoryError),
    #[error("background lifecycle failed: {0}")]
    Background(#[from] ori_runtime::qos::QosError),
    #[error("automation startup/cleanup failed: {0}")]
    Automation(#[from] ori_session::SessionError),
    #[error("candidate I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("candidate task failed")]
    Task,
}
pub type Result<T> = std::result::Result<T, CoreError>;
#[derive(Debug)]
pub(crate) struct ApiError {
    pub status: StatusCode,
    pub code: String,
    pub message: String,
}
pub(crate) type ApiResult<T> = std::result::Result<T, ApiError>;
impl ApiError {
    pub fn new(status: StatusCode, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.into(),
            message: message.into(),
        }
    }
    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, "invalid_request", message)
    }
    pub fn missing(code: &'static str) -> Self {
        Self::new(StatusCode::NOT_FOUND, code, "Запись не найдена")
    }
    pub fn unavailable(code: &'static str) -> Self {
        Self::new(
            StatusCode::SERVICE_UNAVAILABLE,
            code,
            "Ресурс временно недоступен",
        )
    }
}
impl From<ori_archive::ArchiveError> for ApiError {
    fn from(error: ori_archive::ArchiveError) -> Self {
        use ori_archive::ArchiveError::*;
        match error {
            Invalid(_) | Conflict => {
                Self::invalid("Некорректное измерение или нарушена связь архива")
            }
            Sql(rusqlite_error)
                if rusqlite_error.sqlite_error_code()
                    == Some(rusqlite::ErrorCode::ConstraintViolation) =>
            {
                Self::invalid("ID уже существует или нарушена связь архива")
            }
            NotFound => Self::missing("not_found"),
            Capacity { .. } => Self::new(
                StatusCode::INSUFFICIENT_STORAGE,
                "archive_capacity_exceeded",
                "Архив заполнен; автоматическое удаление выключено",
            ),
            QueueFull { .. } => Self::unavailable("archive_queue_full"),
            QueryBudget => Self::unavailable("archive_query_budget"),
            Closed | WorkerStopped => Self::unavailable("archive_unavailable"),
            _ => Self::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "archive_unavailable",
                "SQLite-архив недоступен; исходные измерения не удаляются",
            ),
        }
    }
}
impl From<crate::devices::DeviceError> for ApiError {
    fn from(error: crate::devices::DeviceError) -> Self {
        use crate::devices::DeviceError::*;
        match error {
            Archive(error) => error.into(),
            Unsupported(message) => {
                Self::new(StatusCode::BAD_REQUEST, "unsupported_adapter", message)
            }
            Invalid(message) => Self::invalid(message),
            Denied => Self::new(
                StatusCode::CONFLICT,
                "monitoring_conflict",
                "Control ownership changed; command was not executed",
            ),
            QueueFull => Self::unavailable("device_queue_full"),
            error => Self::new(StatusCode::BAD_REQUEST, "worker_error", error.to_string()),
        }
    }
}
impl From<ori_session::SessionError> for ApiError {
    fn from(error: ori_session::SessionError) -> Self {
        Self::new(
            StatusCode::from_u16(error.http_status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
            error.api_code,
            error.message,
        )
    }
}
impl From<ori_guard::GuardError> for ApiError {
    fn from(error: ori_guard::GuardError) -> Self {
        Self::new(
            StatusCode::from_u16(error.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
            error.api_code(),
            error.to_string(),
        )
    }
}
impl From<ori_tasks::TaskError> for ApiError {
    fn from(error: ori_tasks::TaskError) -> Self {
        Self::new(
            StatusCode::from_u16(error.http_status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
            error.api_code,
            error.message,
        )
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        super::http::json_response(
            self.status,
            &json!({"contract":ori_contracts::CONTRACT_NAME,"contract_version":1,"error":{"code":self.code,"message":self.message}}),
        )
    }
}
