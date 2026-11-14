use super::*;
use ori_automation::{DetectedRegion, DetectionRequest};
use ori_runtime::worker::Cancellation;
use serde_json::json;

fn protocol() -> SessionError {
    SessionError::new(
        502,
        "research_protocol_error",
        "Научный обработчик вернул некорректный ответ",
    )
}
fn runtime(error: ori_runtime::Error) -> SessionError {
    use ori_runtime::Error::*;
    match error {
        Busy => SessionError::new(503, "research_busy", "Ресурс временно недоступен"),
        Cancelled => SessionError::new(503, "research_cancelled", "Ресурс временно недоступен"),
        Deadline => SessionError::new(504, "research_timeout", "Истёк срок научной обработки"),
        ResourceLimit => {
            SessionError::new(503, "research_resource_limit", "Ресурс временно недоступен")
        }
        Protocol(message) if message == "private request byte limit" => {
            SessionError::new(413, "resource_limit", "Request byte limit exceeded")
        }
        OutputLimit | Protocol(_) | Json(_) => protocol(),
        _ => SessionError::new(503, "research_unavailable", "Ресурс временно недоступен"),
    }
}
fn response(value: Value, id: &str, maximum: u16) -> SessionResult<Vec<DetectedRegion>> {
    if !value.as_object().is_some_and(|object| object.len() == 5)
        || value["version"].as_u64() != Some(1)
        || value["request_id"] != id
        || value["operation"] != "analysis"
    {
        return Err(protocol());
    }
    match value["ok"].as_bool() {
        Some(true) if value.get("result").is_some() => {
            let values = value["result"]
                .as_array()
                .filter(|values| values.len() <= usize::from(maximum))
                .ok_or_else(protocol)?;
            serde_json::from_value(Value::Array(values.clone())).map_err(|_| protocol())
        }
        Some(false) if value.get("error").is_some() => {
            let error = &value["error"];
            if error.as_object().is_none_or(|object| object.len() != 6)
                || ["code", "type", "message", "api_code", "api_message"]
                    .iter()
                    .any(|field| error[*field].as_str().is_none())
            {
                return Err(protocol());
            }
            let code = error["api_code"].as_str().ok_or_else(protocol)?;
            let status = match (code, error["http_status"].as_u64()) {
                ("invalid_request" | "missing_field" | "protocol_error", Some(400)) => 400,
                ("resource_limit", Some(413)) => 413,
                ("shared_io_unavailable", Some(503)) => 503,
                ("internal_error", Some(500)) => 500,
                _ => return Err(protocol()),
            };
            Err(SessionError::new(
                status,
                code,
                error["api_message"].as_str().ok_or_else(protocol)?,
            ))
        }
        _ => Err(protocol()),
    }
}
pub(super) async fn detect(
    core: &Core,
    request: DetectionRequest,
    original: Arc<dyn TuneCompletionCheck>,
    cancel: Cancellation,
) -> SessionResult<Vec<DetectedRegion>> {
    original.revalidate()?;
    let spec = core.config.analysis_worker.clone().ok_or_else(|| {
        SessionError::new(503, "research_unavailable", "Ресурс временно недоступен")
    })?;
    let permit = core.research_pool.try_reserve().map_err(runtime)?;
    let id = crate::new_id();
    let maximum = request.maximum;
    let request = json!({"version":1,"request_id":id,"operation":"analysis","method":"automatic_detection_regions","sweep":request.sweep,"threshold_dbm":request.threshold_dbm,"maximum":maximum});
    let check = original.clone();
    let result = crate::private_worker::run(
        core,
        permit,
        spec,
        request,
        cancel,
        Some(Arc::new(move || {
            check
                .revalidate()
                .map_err(|_| ori_runtime::Error::Cancelled)
        })),
        crate::private_worker::IoClass::MemoryOnly,
    )
    .await;
    original.revalidate()?;
    let result = response(result.map_err(runtime)?.response, &id, maximum)?;
    original.revalidate()?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn success() -> Value {
        json!({"version":1,"request_id":"owned-source","operation":"analysis","ok":true,"result":[{"center_frequency_hz":9007199254740993u64,"bandwidth_hz":1,"peak_level":-0.0,"score":1.0000000000000002}]})
    }
    #[test]
    fn closed_detection_result_preserves_integer_hz_float_bits_and_rejects_extra_authority() {
        let regions = response(success(), "owned-source", 1).unwrap();
        assert_eq!(regions[0].center_frequency_hz, 9007199254740993);
        assert_eq!(regions[0].peak_level.to_bits(), (-0.0f64).to_bits());
        assert_eq!(regions[0].score.to_bits(), 1.0000000000000002f64.to_bits());
        for invalid in [
            json!({"version":1,"request_id":"owned-source","operation":"analysis","ok":true,"result":[],"new_grant":true}),
            json!({"version":1,"request_id":"another-source","operation":"analysis","ok":true,"result":[]}),
            json!({"version":1,"request_id":"owned-source","operation":"analysis","ok":true,"result":[{"center_frequency_hz":1.0,"bandwidth_hz":1,"peak_level":0.0,"score":0.0}]}),
            json!({"version":1,"request_id":"owned-source","operation":"analysis","ok":true,"result":[{"center_frequency_hz":1,"bandwidth_hz":1,"peak_level":0.0,"score":0.0,"settings":{}}]}),
        ] {
            assert!(response(invalid, "owned-source", 1).is_err());
        }
        assert!(response(success(), "owned-source", 0).is_err());
    }
}
