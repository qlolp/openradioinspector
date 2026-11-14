use ori_archive::ArchiveError;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionError {
    pub http_status: u16,
    pub api_code: String,
    pub message: String,
}
pub type SessionResult<T> = Result<T, SessionError>;
impl SessionError {
    pub fn new(status: u16, code: &str, message: impl Into<String>) -> Self {
        Self {
            http_status: status,
            api_code: code.into(),
            message: message.into(),
        }
    }
    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new(400, "invalid_request", message)
    }
    pub fn missing(code: &str, id: &str) -> Self {
        let message = match code {
            "session_not_found" => format!("Сеанс не найден: {id}"),
            "candidate_not_found" => format!("Кандидат не найден: {id}"),
            "investigation_not_found" => format!("Исследование не найдено: {id}"),
            "sweep_not_found" => format!("Свип не найден: {id}"),
            _ => id.into(),
        };
        Self::new(404, code, message)
    }
    pub(crate) fn field(field: &str) -> Self {
        Self::new(400, "missing_field", format!("Отсутствует поле: {field}"))
    }
}
impl std::fmt::Display for SessionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for SessionError {}
impl From<ArchiveError> for SessionError {
    fn from(error: ArchiveError) -> Self {
        match error {
            ArchiveError::Invalid(_) | ArchiveError::Conflict => {
                Self::invalid("Некорректное измерение или нарушена связь архива")
            }
            ArchiveError::Capacity { .. } => Self::new(
                507,
                "archive_capacity_exceeded",
                "Архив заполнен; автоматическое удаление выключено",
            ),
            ArchiveError::QueryBudget => {
                Self::new(503, "archive_query_budget", "Ресурс временно недоступен")
            }
            ArchiveError::QueueFull { .. } => {
                Self::new(503, "archive_queue_full", "Ресурс временно недоступен")
            }
            ArchiveError::Closed | ArchiveError::WorkerStopped => {
                Self::new(503, "archive_unavailable", "Ресурс временно недоступен")
            }
            ArchiveError::NotFound => Self::missing("not_found", "Запись не найдена"),
            _ => Self::new(
                500,
                "archive_unavailable",
                "SQLite-архив недоступен; исходные измерения не удаляются",
            ),
        }
    }
}
