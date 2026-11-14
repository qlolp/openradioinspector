use super::*;
use ori_tasks::{TaskError, TaskResult};
use serde_json::Value;
use std::{collections::BTreeMap, io::Write, path::Component, time::Duration};

pub(super) fn invalid() -> TaskError {
    TaskError::new(
        400,
        "task_execution_binding_invalid",
        "Task execution binding is invalid",
    )
}
pub(super) fn denied() -> TaskError {
    TaskError::new(
        409,
        "task_execution_authority_changed",
        "Original task execution authority changed",
    )
}
pub(super) fn pending() -> TaskError {
    TaskError::new(
        503,
        "task_execution_cleanup_pending",
        "Task execution cleanup is not confirmed",
    )
}
pub(super) fn failed() -> TaskError {
    TaskError::new(
        503,
        "task_execution_effect_failed",
        "Task execution effect failed",
    )
}
fn text(value: &str) -> bool {
    !value.trim().is_empty()
}

pub(super) fn bindings(config: &TaskExecutionConfig) -> TaskResult<BTreeMap<String, TaskBinding>> {
    if !(1..=64).contains(&config.bindings.len())
        || !(1..=16).contains(&config.max_active_runs)
        || config.close_timeout.is_zero()
        || config.close_timeout > Duration::from_secs(30)
    {
        return Err(invalid());
    }
    let mut result = BTreeMap::new();
    let mut bytes = 0usize;
    for binding in &config.bindings {
        let path = match &binding.input {
            TaskInput::FixedFrequencyList(path) | TaskInput::Panorama(path) => path,
        };
        let path_text = path.to_str().ok_or_else(invalid)?;
        if !path.is_absolute()
            || path
                .components()
                .any(|part| matches!(part, Component::ParentDir))
            || [
                &binding.task_name,
                &binding.session_id,
                &binding.stream_id,
                &binding.level_unit,
            ]
            .into_iter()
            .any(|value| !text(value))
        {
            return Err(invalid());
        }
        for value in [
            &binding.task_name,
            &binding.session_id,
            &binding.stream_id,
            &binding.level_unit,
        ] {
            bytes = bytes.checked_add(value.len()).ok_or_else(invalid)?;
        }
        bytes = bytes.checked_add(path_text.len()).ok_or_else(invalid)?;
        if bytes > 1024 * 1024
            || result
                .insert(binding.task_name.clone(), binding.clone())
                .is_some()
        {
            return Err(invalid());
        }
    }
    Ok(result)
}

pub(super) fn task_binding<'a>(
    bindings: &'a BTreeMap<String, TaskBinding>,
    task: &Value,
) -> TaskResult<&'a TaskBinding> {
    let name = task
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(invalid)?;
    let binding = bindings.get(name).ok_or_else(invalid)?;
    let (field, other, path) = match &binding.input {
        TaskInput::FixedFrequencyList(path) => ("task_file", "panorama_file", path),
        TaskInput::Panorama(path) => ("panorama_file", "task_file", path),
    };
    if task.get(field).and_then(Value::as_str) != path.to_str()
        || task
            .get(other)
            .is_some_and(|value| !value.is_null() && value.as_str() != Some(""))
    {
        return Err(invalid());
    }
    Ok(binding)
}

pub(super) fn task_inventory(
    bindings: &BTreeMap<String, TaskBinding>,
    tasks: &[Value],
) -> TaskResult<()> {
    if tasks.len() > 10_000 {
        return Err(invalid());
    }
    for task in tasks {
        let name = task
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(invalid)?;
        let status = task
            .get("status")
            .and_then(Value::as_str)
            .ok_or_else(invalid)?;
        if status == "Выполняется" {
            return Err(denied());
        }
        if status == "В очереди на выполнение" && !bindings.contains_key(name) {
            return Err(invalid());
        }
    }
    Ok(())
}

struct Count(usize);
impl Write for Count {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > (1024 * 1024usize).saturating_sub(self.0) {
            return Err(std::io::Error::other("bounded task completion"));
        }
        self.0 += bytes.len();
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
pub(super) fn completion(binding: &TaskBinding, output: &TaskCompletion) -> TaskResult<()> {
    if output.session_id != binding.session_id
        || output.stream_id != binding.stream_id
        || output.level_unit != binding.level_unit
        || !(1..=1024).contains(&output.source_ids.len())
        || output.interval.ended_at < output.interval.started_at
        || output.source_ids.iter().any(|id| !text(id))
    {
        return Err(failed());
    }
    let mut ids = std::collections::BTreeSet::new();
    if output.source_ids.iter().any(|id| !ids.insert(id)) {
        return Err(failed());
    }
    serde_json::to_writer(&mut Count(0), output).map_err(|_| failed())
}
