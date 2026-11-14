#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TaskError {
    pub http_status: u16,
    pub api_code: String,
    pub message: String,
}
pub type TaskResult<T> = Result<T, TaskError>;
impl TaskError {
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
    pub(crate) fn resource() -> Self {
        Self::new(
            503,
            "task_book_resource_limit",
            "TaskBook превышает установленный предел ресурсов; задания не удаляются",
        )
    }
    pub(crate) fn book_invalid() -> Self {
        Self::new(
            503,
            "task_book_invalid",
            "Некорректная TaskBook; существующие задания не изменены",
        )
    }
    pub(crate) fn io() -> Self {
        Self::new(
            503,
            "task_book_io",
            "Не удалось надёжно сохранить или прочитать TaskBook; блокировка действующего владельца сохраняется",
        )
    }
    pub(crate) fn closed() -> Self {
        Self::new(503, "task_scheduler_closed", "Scheduler уже закрыт")
    }
    pub(crate) fn busy() -> Self {
        Self::new(
            503,
            "task_scheduler_busy",
            "Планировщик ещё выполняет предыдущую операцию",
        )
    }
    pub(crate) fn queue_full() -> Self {
        Self::new(
            503,
            "task_scheduler_queue_full",
            "Очередь планировщика заполнена",
        )
    }
}
impl std::fmt::Display for TaskError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for TaskError {}
