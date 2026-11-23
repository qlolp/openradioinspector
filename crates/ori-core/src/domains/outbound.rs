//! Only these request builders can turn a claimed notification into outbound HTTP.
use super::*;
use ori_archive::{ClaimedDutyDelivery, DutyChannel};
use ori_guard::DutyDeliveryReceipt;
use ori_runtime::notification_secrets::{self, Destination};
use serde_json::json;

pub(super) fn signed_request(
    client: &reqwest::Client,
    request: ori_guard::WebhookRequest,
) -> ControlResult<reqwest::RequestBuilder> {
    use hmac::{Hmac, KeyInit, Mac};
    let url = notification_secrets::webhook_url(&request.url)
        .map_err(|_| ControlError::Failed("invalid webhook URL".into()))?;
    let mut signature = Hmac::<sha2::Sha256>::new_from_slice(request.secret.as_bytes())
        .map_err(|_| ControlError::Failed("invalid webhook key".into()))?;
    signature.update(&request.body);
    let hex: String = signature
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Ok(client
        .post(url)
        .header("Content-Type", "application/json")
        .header(
            "X-OpenRadioInspector-Notification-ID",
            request.notification_id,
        )
        .header("X-OpenRadioInspector-Signature", format!("sha256={hex}"))
        .body(request.body))
}
pub(super) fn telegram_request(
    client: &reqwest::Client,
    token: &str,
    chat: &str,
    event: &Value,
) -> ControlResult<reqwest::RequestBuilder> {
    let mut text = match event["kind"].as_str() {
        Some("new_unknown") => "Новый устойчивый сигнал".to_owned(),
        Some("coverage_lost") => "Потеря покрытия".into(),
        Some("recording_lost") => "Потеря записи".into(),
        Some("hourly_digest") => format!(
            "Сводка за {} UTC. Одиночные импульсы {}. Повторы {}.",
            event["hour_start"].as_str().unwrap_or(""),
            event["impulses"].as_u64().unwrap_or(0),
            event["repeats"].as_u64().unwrap_or(0)
        ),
        Some("test") => "Проверка канала уведомлений".into(),
        _ => return Err(ControlError::Failed("unknown notification category".into())),
    };
    if let Some(t) = event["occurred_at"].as_str() {
        text.push_str(&format!("\n{t}"));
    }
    if let Some(hz) = event["frequency_hz"].as_u64() {
        text.push_str(&format!(" · {hz} Гц"));
    }
    if let (Some(delta), Some(unit)) = (event["delta"].as_f64(), event["delta_unit"].as_str()) {
        text.push_str(&format!(" · Δ {delta:+.2} {unit}"));
    }
    if let Some(id) = event["event_id"].as_str() {
        text.push_str(&format!("\nСобытие {id}"));
    }
    let body = serde_json::to_vec(&json!({"chat_id":chat,"text":text}))
        .map_err(|_| ControlError::Failed("telegram message".into()))?;
    Ok(client
        .post(format!("https://api.telegram.org/bot{token}/sendMessage"))
        .header("Content-Type", "application/json")
        .body(body))
}
fn retry_after(value: &str) -> Option<u64> {
    value.trim().parse::<u64>().ok().or_else(|| {
        let date = chrono::DateTime::parse_from_rfc2822(value).ok()?;
        Some(
            ((date.with_timezone(&chrono::Utc) - chrono::Utc::now())
                .num_milliseconds()
                .max(0) as u64)
                .div_ceil(1000),
        )
    })
}
pub(super) async fn response_receipt(
    mut response: reqwest::Response,
    telegram: bool,
) -> DutyDeliveryReceipt {
    let mut result = DutyDeliveryReceipt {
        status: Some(response.status().as_u16()),
        delivered: response.status().is_success(),
        retry_after_seconds: response
            .headers()
            .get("Retry-After")
            .and_then(|v| v.to_str().ok())
            .and_then(retry_after),
    };
    if telegram && (result.delivered || result.status == Some(429)) {
        result.delivered = false;
        let mut body = Vec::new();
        loop {
            match response.chunk().await {
                Ok(Some(chunk)) if chunk.len() <= 4096 - body.len() => {
                    body.extend_from_slice(&chunk)
                }
                Ok(None) => break,
                _ => return result,
            }
        }
        if let Ok(value) = serde_json::from_slice::<Value>(&body) {
            result.delivered =
                result.status.is_some_and(|s| (200..300).contains(&s)) && value["ok"] == true;
            if let Some(seconds) = value["parameters"]["retry_after"].as_u64() {
                result.retry_after_seconds =
                    Some(seconds.max(result.retry_after_seconds.unwrap_or(0)));
            }
        }
    }
    result
}
pub(super) async fn configure(core: &Core, project: &str, patch: Value) -> ControlResult<Value> {
    let directory = core.config.data_directory.clone();
    let id = project.to_owned();
    tokio::task::spawn_blocking(move || notification_secrets::configure(&directory, &id, patch))
        .await
        .map_err(|_| unavailable())?
        .map_err(|_| {
            ControlError::Failed("invalid or unavailable private notification settings".into())
        })
}
pub(super) async fn deliver(
    core: &Core,
    channel: DutyChannel,
    claim: &ClaimedDutyDelivery,
) -> ControlResult<DutyDeliveryReceipt> {
    let directory = core.config.data_directory.clone();
    let project = claim.row.project_id.clone();
    let revision = claim.revision.clone();
    let dest = tokio::task::spawn_blocking(move || {
        notification_secrets::destination(&directory, &project, channel.name(), &revision)
    })
    .await
    .map_err(|_| unavailable())?
    .map_err(|_| ControlError::Unavailable("notification_store"))?
    .ok_or(ControlError::Denied)?;
    let request = match dest {
        Destination::Telegram { bot_token, chat_id } => telegram_request(
            &core.webhook_client,
            &bot_token,
            &chat_id,
            &claim.row.payload,
        )?,
        Destination::Webhook { url, secret } => signed_request(
            &core.webhook_client,
            ori_guard::WebhookRequest {
                notification_id: claim.row.id.clone(),
                body: claim.body.clone(),
                url,
                secret,
            },
        )?,
    };
    let response = request
        .send()
        .await
        .map_err(|_| ControlError::Failed("network_error".into()))?;
    Ok(response_receipt(response, channel == DutyChannel::Telegram).await)
}
