use crate::{
    TaskError, TaskResult,
    time::{self, Moment},
};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::{Value, json};
use unicode_general_category::{GeneralCategory, get_general_category};

pub(crate) const QUEUED: &str = "В очереди на выполнение";
pub(crate) const RUNNING: &str = "Выполняется";
pub(crate) const COMPLETED: &str = "Выполнено";
pub(crate) const ABORTED: &str = "Снято с выполнения";
pub(crate) const OPERATOR: &str = "принудительно завершено оператором";
pub(crate) const CRASH: &str = "аварийное завершение приложения";
pub(crate) const MISSING_FILE: &str = "файл задания (на сканирование частот) не найден";
pub(crate) fn strip(value: &str) -> &str {
    value.trim_matches(|c: char| c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}'))
}
pub(crate) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64() != Some(0.),
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}
fn integer(value: &Value) -> bool {
    value
        .as_number()
        .is_some_and(|v| !v.to_string().contains(['.', 'e', 'E']))
}
fn float(value: &Value) -> TaskResult<f64> {
    let result = match value {
        Value::Bool(v) => Some(u8::from(*v) as f64),
        Value::String(v) => numeric_string(v)?.trim().parse::<f64>().ok(),
        _ => value.as_f64(),
    };
    result
        .filter(|v| v.is_finite())
        .ok_or_else(TaskError::book_invalid)
}
fn int(value: &Value) -> TaskResult<Value> {
    let raw = match value {
        Value::Bool(v) => u8::from(*v).to_string(),
        Value::Number(_) if integer(value) => value.to_string(),
        Value::Number(_) => format!("{:.0}", float(value)?.trunc()),
        Value::String(v) => numeric_string(strip(v))?,
        _ => return Err(TaskError::book_invalid()),
    };
    let raw = raw.strip_prefix('+').unwrap_or(&raw);
    let negative = raw.starts_with('-');
    let digits = raw.strip_prefix('-').unwrap_or(raw);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(TaskError::book_invalid());
    }
    let digits = digits.trim_start_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    let canonical = format!(
        "{}{digits}",
        if negative && digits != "0" { "-" } else { "" }
    );
    serde_json::from_str(&canonical).map_err(|_| TaskError::book_invalid())
}
fn numeric_string(value: &str) -> TaskResult<String> {
    let normalized = value
        .chars()
        .map(|c| {
            if c.is_ascii() || get_general_category(c) != GeneralCategory::DecimalNumber {
                return c;
            }
            let mut first = c as u32;
            while first > 0
                && char::from_u32(first - 1)
                    .is_some_and(|c| get_general_category(c) == GeneralCategory::DecimalNumber)
            {
                first -= 1;
            }
            char::from_digit((c as u32 - first) % 10, 10).expect("decimal digit")
        })
        .collect::<String>();
    let bytes = normalized.as_bytes();
    if bytes.iter().enumerate().any(|(i, c)| {
        *c == b'_'
            && (i == 0
                || i + 1 == bytes.len()
                || !bytes[i - 1].is_ascii_digit()
                || !bytes[i + 1].is_ascii_digit())
    }) {
        return Err(TaskError::book_invalid());
    }
    Ok(normalized.replace('_', ""))
}
fn list(value: &Value) -> TaskResult<Value> {
    match value {
        Value::Array(_) => Ok(value.clone()),
        Value::String(v) => Ok(json!(v.chars().map(|c| c.to_string()).collect::<Vec<_>>())),
        Value::Object(v) => Ok(json!(v.keys().collect::<Vec<_>>())),
        _ => Err(TaskError::book_invalid()),
    }
}

/// Canonical Python fields, retaining user text and reference IDs verbatim.
/// Debug is intentionally absent because callbacks can access private paths.
#[derive(Clone, Serialize)]
#[serde(transparent)]
pub struct ControlTask(Value);
impl ControlTask {
    pub fn from_dict(input: Value) -> TaskResult<Self> {
        let input = input.as_object().ok_or_else(TaskError::book_invalid)?;
        let defaults = json!({"name":"","purpose":"","work_type":"","basis":"","operator":"","place":"","equipment":"",
            "task_file":null,"panorama_file":null,"start_at":null,"duration_s":0.0,"status":"Сформировано","outcome":"","created_by":"","completed_cycles":0,
            "profiles":[],"task_type_id":null,"purpose_id":null,"rkp_id":null,"department_id":null,"group_pa_id":null,"basis_id":null,"carrier_id":null,
            "created_at":null,"prepared_at":null,"real_started_at":null,"real_stopped_at":null,"break_at":null,"break_reason":"","created_by_operator":"","prepared_by_operator":"",
            "device_names":[],"device_ids":[],"observers":[],"extra_info":"","result_code":"","result_text":"","process_position":"","mobility":"","next_times":[],"attachments":[],"protocol_file":"","sleep_interval_s":0.0});
        let mut value = defaults;
        for (key, default) in value.as_object_mut().unwrap() {
            if let Some(existing) = input.get(key) {
                *default = existing.clone();
            }
        }
        value["start_at"] = time::stored_moment(&value["start_at"], true)?;
        for key in [
            "created_at",
            "prepared_at",
            "real_started_at",
            "real_stopped_at",
            "break_at",
        ] {
            value[key] = time::stored_moment(&value[key], false)?;
        }
        for key in ["duration_s", "sleep_interval_s"] {
            value[key] = json!(float(&value[key])?);
        }
        value["completed_cycles"] = int(&value["completed_cycles"])?;
        for key in ["device_names", "device_ids", "observers", "attachments"] {
            value[key] = list(&value[key])?;
        }
        let moments = list(&value["next_times"])?;
        value["next_times"] = json!(
            moments
                .as_array()
                .unwrap()
                .iter()
                .map(|v| time::stored_moment(v, false))
                .collect::<TaskResult<Vec<_>>>()?
                .into_iter()
                .filter(|v| !v.is_null())
                .collect::<Vec<_>>()
        );
        let profiles=value["profiles"].as_array().ok_or_else(TaskError::book_invalid)?.iter().map(|profile|{
            let profile=profile.as_object().ok_or_else(TaskError::book_invalid)?;
            let mut canonical=json!({"points":7,"dynamic_threshold_db":30.0,"action":"control_bandwidth","bandwidth_hz":0.0,"signal_bandwidth_hz":0.0,"span_hz":0.0,"screen_fill":1.0,"measurement_limit":0,"phonogram_limit":0});
            for (key,default)in canonical.as_object_mut().unwrap() {if let Some(value)=profile.get(key){*default=value.clone();}}
            Ok(canonical)
        }).collect::<TaskResult<Vec<_>>>()?;
        value["profiles"] = json!(profiles);
        Ok(Self(value))
    }
    pub fn as_dict(&self) -> Value {
        self.0.clone()
    }
    pub(crate) fn value(&self, key: &str) -> &Value {
        &self.0[key]
    }
    pub(crate) fn is(&self, state: &str) -> bool {
        self.0["status"] == state
    }
    pub(crate) fn start(&mut self) {
        self.0["status"] = json!(RUNNING);
    }
    pub(crate) fn abort(&mut self, reason: &str) {
        self.0["status"] = json!(ABORTED);
        self.0["outcome"] = json!(reason);
    }
    pub(crate) fn complete(&mut self) -> TaskResult<()> {
        let current = self.0["completed_cycles"].to_string();
        let negative = current.starts_with('-');
        let mut digits = current.trim_start_matches('-').as_bytes().to_vec();
        if negative {
            let mut index = digits.len();
            while index > 0 {
                index -= 1;
                if digits[index] > b'0' {
                    digits[index] -= 1;
                    break;
                }
                digits[index] = b'9';
            }
        } else {
            let mut carry = true;
            for digit in digits.iter_mut().rev() {
                if *digit == b'9' {
                    *digit = b'0';
                } else {
                    *digit += 1;
                    carry = false;
                    break;
                }
            }
            if carry {
                digits.insert(0, b'1');
            }
        }
        let digits = String::from_utf8(digits).map_err(|_| TaskError::book_invalid())?;
        let digits = digits.trim_start_matches('0');
        let digits = if digits.is_empty() { "0" } else { digits };
        self.0["completed_cycles"] = serde_json::from_str(&format!(
            "{}{digits}",
            if negative && digits != "0" { "-" } else { "" }
        ))
        .map_err(|_| TaskError::book_invalid())?;
        self.0["status"] = json!(COMPLETED);
        Ok(())
    }
    pub(crate) fn due(&self, now: DateTime<Utc>) -> TaskResult<bool> {
        if !self.is(QUEUED) || self.0["start_at"].is_null() {
            return Ok(false);
        }
        self.moment()?.due(now)
    }
    fn moment(&self) -> TaskResult<Moment> {
        Moment::parse(
            self.0["start_at"]
                .as_str()
                .ok_or_else(TaskError::book_invalid)?,
        )
        .ok_or_else(TaskError::book_invalid)
    }
    pub(crate) fn expired(&self, now: DateTime<Utc>) -> TaskResult<bool> {
        if self.0["start_at"].is_null() {
            return Ok(false);
        }
        self.moment()?
            .ends(
                self.0["duration_s"]
                    .as_f64()
                    .ok_or_else(TaskError::book_invalid)?,
            )?
            .due(now)
    }
    pub(crate) fn require_files(&self) -> TaskResult<()> {
        for key in ["task_file", "panorama_file"] {
            if truthy(&self.0[key]) {
                let Some(value) = self.0[key].as_str() else {
                    return Err(TaskError::invalid(
                        "expected str, bytes or os.PathLike object",
                    ));
                };
                if !std::path::Path::new(value).is_file() {
                    return Err(TaskError::invalid(MISSING_FILE));
                }
            }
        }
        Ok(())
    }
    pub(crate) fn public(&self) -> TaskResult<Value> {
        let mut value = self.0.clone();
        let object = value.as_object_mut().unwrap();
        object.shift_remove("task_file");
        object.shift_remove("panorama_file");
        if !value["start_at"].is_null() {
            value["start_at"] = json!(time::utc(time::parse_api(
                value["start_at"]
                    .as_str()
                    .ok_or_else(TaskError::book_invalid)?
            )?));
        }
        Ok(value)
    }
    pub(crate) fn create(payload: Value) -> TaskResult<Self> {
        let object = payload
            .as_object()
            .ok_or_else(|| TaskError::invalid("Задача должна быть JSON-объектом"))?;
        let allowed = [
            "name",
            "task_file",
            "panorama_file",
            "start_at",
            "duration_s",
            "purpose",
            "work_type",
            "basis",
            "operator",
            "place",
            "equipment",
            "created_by",
        ];
        let mut unknown = object
            .keys()
            .filter(|k| !allowed.contains(&k.as_str()))
            .cloned()
            .collect::<Vec<_>>();
        unknown.sort();
        if !unknown.is_empty() {
            return Err(TaskError::invalid(format!(
                "Неизвестные поля задачи: {}",
                unknown.join(", ")
            )));
        }
        let name = payload["name"]
            .as_str()
            .filter(|v| !strip(v).is_empty())
            .ok_or_else(|| TaskError::invalid("name обязано быть непустой строкой"))?;
        let start = payload["start_at"]
            .as_str()
            .ok_or_else(|| TaskError::invalid("start_at обязано быть ISO 8601 временем"))?;
        let duration = &payload["duration_s"];
        let duration = if duration.is_number() {
            duration.as_f64().ok_or_else(|| {
                TaskError::invalid(if integer(duration) {
                    "int too large to convert to float"
                } else {
                    "duration_s обязано быть положительным конечным числом"
                })
            })?
        } else {
            return Err(TaskError::invalid(
                "duration_s обязано быть положительным конечным числом",
            ));
        };
        if !duration.is_finite() || duration <= 0. {
            return Err(TaskError::invalid(
                "duration_s обязано быть положительным конечным числом",
            ));
        }
        if truthy(&payload["task_file"]) == truthy(&payload["panorama_file"]) {
            return Err(TaskError::invalid(
                "Укажите ровно один task_file или panorama_file",
            ));
        }
        for key in ["task_file", "panorama_file"] {
            if !payload[key].is_null() && payload[key].as_str().is_none_or(|v| strip(v).is_empty())
            {
                return Err(TaskError::invalid(
                    "Путь входного файла должен быть непустой строкой",
                ));
            }
        }
        let mut raw = json!({"name":strip(name),"task_file":payload["task_file"],"panorama_file":payload["panorama_file"],"duration_s":duration,"status":QUEUED});
        for key in [
            "purpose",
            "work_type",
            "basis",
            "operator",
            "place",
            "equipment",
            "created_by",
        ] {
            let value = payload.get(key).cloned().unwrap_or(json!(""));
            if !value.is_string() {
                return Err(TaskError::invalid(format!("{key} должно быть строкой")));
            }
            raw[key] = value;
        }
        raw["start_at"] = json!(time::parse_api(start)?.to_rfc3339());
        Self::from_dict(raw)
    }
}

#[derive(Clone, Serialize)]
pub struct TaskReference {
    pub reference_id: usize,
    pub name: Value,
    pub code: Value,
}
#[derive(Default)]
pub struct ReferenceBook {
    items: serde_json::Map<String, Value>,
}
impl ReferenceBook {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn add(&mut self, kind: &str, name: Value, code: Value) -> TaskReference {
        let items = self
            .items
            .entry(kind)
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .unwrap();
        let item = TaskReference {
            reference_id: items.len() + 1,
            name,
            code,
        };
        items.push(json!(item));
        item
    }
    pub fn get(&self, kind: &str, id: usize) -> Option<TaskReference> {
        self.list(kind).into_iter().find(|v| v.reference_id == id)
    }
    pub fn list(&self, kind: &str) -> Vec<TaskReference> {
        self.items
            .get(kind)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|v| TaskReference {
                reference_id: v["reference_id"].as_u64().unwrap() as usize,
                name: v["name"].clone(),
                code: v["code"].clone(),
            })
            .collect()
    }
    pub fn name_of(&self, kind: &str, id: Option<usize>) -> Value {
        id.filter(|v| *v > 0)
            .and_then(|v| self.get(kind, v))
            .map_or_else(|| json!(""), |v| v.name)
    }
    pub fn as_dict(&self) -> Value {
        Value::Object(self.items.clone())
    }
    pub fn from_dict(value: Value) -> TaskResult<Self> {
        let mut book = Self::new();
        for (kind, values) in value.as_object().ok_or_else(TaskError::book_invalid)? {
            for raw in values.as_array().ok_or_else(TaskError::book_invalid)? {
                let object = raw.as_object().ok_or_else(TaskError::book_invalid)?;
                book.add(
                    kind,
                    object.get("name").cloned().unwrap_or(json!("")),
                    object.get("code").cloned().unwrap_or(json!("")),
                );
            }
        }
        Ok(book)
    }
}
