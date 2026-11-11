#[derive(Debug, Clone, thiserror::Error)]
pub enum HistoryError {
    #[error("invalid history request: {0}")]
    Invalid(&'static str),
    #[error("unsafe or unowned derivative storage")]
    UnsafeStorage,
    #[error("shared heavy work is busy")]
    Busy,
    #[error("acquisition pressure pauses history work")]
    Pressure,
    #[error("history service is closing or closed")]
    Closed,
    #[error("history job is absent or expired")]
    NotFound,
    #[error("derivative resource budget exceeded")]
    ResourceLimit,
    #[error("source snapshot exceeds the admitted budget")]
    SourceLimit,
    #[error("history work was cancelled")]
    Cancelled,
    #[error("isolated installed research worker unavailable")]
    WorkerUnavailable,
    #[error("history worker failed")]
    Worker { code: String, retryable: bool },
    #[error("history operation failed; original sources remain unchanged")]
    Internal,
}
pub type Result<T> = std::result::Result<T, HistoryError>;
impl From<ori_runtime::qos::QosError> for HistoryError {
    fn from(error: ori_runtime::qos::QosError) -> Self {
        use ori_runtime::qos::QosError;
        match error {
            QosError::Pressure => Self::Pressure,
            QosError::Busy => Self::Busy,
            QosError::Cancelled => Self::Cancelled,
            QosError::InvalidCharge => Self::ResourceLimit,
        }
    }
}
impl From<ori_archive::ArchiveError> for HistoryError {
    fn from(error: ori_archive::ArchiveError) -> Self {
        use ori_archive::ArchiveError;
        match error {
            ArchiveError::QueueFull { .. } => Self::Busy,
            ArchiveError::Closed => Self::Closed,
            ArchiveError::NotFound => Self::NotFound,
            ArchiveError::QueryBudget => Self::ResourceLimit,
            ArchiveError::Invalid(_) => Self::Invalid("archive query"),
            _ => Self::Internal,
        }
    }
}
impl From<ori_runtime::Error> for HistoryError {
    fn from(error: ori_runtime::Error) -> Self {
        use ori_runtime::Error;
        match error {
            Error::Busy => Self::Busy,
            Error::Cancelled => Self::Cancelled,
            Error::ResourceLimit | Error::OutputLimit | Error::Deadline => Self::ResourceLimit,
            Error::Io(_) | Error::Unsafe(_) => Self::WorkerUnavailable,
            _ => Self::Internal,
        }
    }
}
impl HistoryError {
    pub(crate) fn failure(&self) -> crate::JobFailure {
        let (code, retryable) = match self {
            Self::Invalid(_) => ("invalid_request", false),
            Self::UnsafeStorage => ("unsafe_storage", false),
            Self::Busy => ("busy", true),
            Self::Pressure => ("acquisition_pressure", true),
            Self::Closed => ("closed", false),
            Self::NotFound => ("not_found", false),
            Self::ResourceLimit => ("resource_limit", true),
            Self::SourceLimit => ("source_limit", false),
            Self::Cancelled => ("cancelled", true),
            Self::WorkerUnavailable => ("worker_unavailable", false),
            Self::Worker { code, retryable } => {
                return crate::JobFailure {
                    code: code.clone(),
                    retryable: *retryable,
                };
            }
            Self::Internal => ("history_failed", false),
        };
        crate::JobFailure {
            code: code.into(),
            retryable,
        }
    }
}
