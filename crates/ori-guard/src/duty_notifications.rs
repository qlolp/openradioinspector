use crate::{DutyDeliveryReceipt, GuardService, Result, util::*};
use ori_archive::{DutyChannel, DutyKind, DutyOccurrence, Mutation, NotificationCompletion};
use serde_json::{Value, json};
use std::{sync::atomic::Ordering, time::Duration};

impl GuardService {
    /// Source references stay in the archive. Only this fixed text representation is exported.
    pub(crate) async fn duty_detection(
        &self,
        p: &Value,
        event: &mut Value,
        occurrence: &str,
        source: &str,
    ) -> Result<Mutation> {
        let id = s(event, "id")?;
        let project = s(p, "id")?;
        let enabled = DutyChannel::ALL
            .iter()
            .any(|ch| p["notification_channels"][ch.name()]["enabled"] == true);
        let candidate =
            matches!(occurrence, "sustained" | "new_unknown") && event["duty_confirmed"] != true;
        let kind = if candidate && enabled {
            let unknown = event["kind"] != "authorized_changed"
                && event["allowed_id"].is_null()
                && !self.duty_matches_known(project, event).await?;
            if unknown {
                DutyKind::NewUnknown
            } else {
                DutyKind::Repeat
            }
        } else if occurrence == "impulse" {
            DutyKind::Impulse
        } else {
            DutyKind::Repeat
        };
        let stable = if kind == DutyKind::NewUnknown {
            id.to_owned()
        } else {
            format!("{id}/{source}")
        };
        let (delta, unit) = if let Some(delta) = event["excess_counts"].as_f64() {
            (Some(delta), Some("s_meter_count".to_owned()))
        } else {
            (event["peak_excess_db"].as_f64(), Some("dB".to_owned()))
        };
        let result = DutyOccurrence {
            id: stable,
            project_id: project.into(),
            at: event["last_seen"]
                .as_str()
                .unwrap_or(self.clock.now().as_str())
                .to_owned(),
            kind,
            event_id: Some(id.into()),
            frequency_hz: event["center_frequency_hz"].as_u64(),
            delta,
            delta_unit: unit.filter(|_| delta.is_some()),
        };
        if candidate && enabled {
            event["duty_confirmed"] = json!(true);
        }
        Ok(Mutation::DutyOccurrence(result))
    }
    pub(crate) fn recording_fault(&self, p: &Value, event: &Value) -> Result<Mutation> {
        Ok(Mutation::DutyOccurrence(DutyOccurrence {
            id: s(event, "id")?.into(),
            project_id: s(p, "id")?.into(),
            at: self.clock.now().as_str().into(),
            kind: DutyKind::RecordingLost,
            event_id: Some(s(event, "id")?.into()),
            frequency_hz: event["center_frequency_hz"].as_u64(),
            delta: None,
            delta_unit: None,
        }))
    }
    pub(crate) fn public_notification_settings(&self, p: &Value) -> Value {
        let mut settings = json!({});
        for c in DutyChannel::ALL {
            let channel = &p["notification_channels"][c.name()];
            settings[c.name()] = json!({"enabled":channel["enabled"]==true,"configured":channel["configured"]==true});
        }
        settings
    }
    pub async fn test_notification(&self, id: &str, request: Value) -> Result<Value> {
        fields(&request, &["channel"], true)?;
        let channel: DutyChannel = serde_json::from_value(request["channel"].clone())
            .map_err(|_| invalid("Неверный канал уведомлений"))?;
        let _project = self.lock_project(id).await?;
        self.ensure_open()?;
        let p = self.project(id).await?;
        let setup = &p["notification_channels"][channel.name()];
        if setup["enabled"] != true || setup["configured"] != true {
            return Err(invalid("Включите и настройте канал уведомлений"));
        }
        let occurrence = DutyOccurrence {
            id: self.ids.next_id(crate::IdKind::Record),
            project_id: id.into(),
            at: self.clock.now().as_str().to_owned(),
            kind: DutyKind::Test,
            event_id: None,
            frequency_hz: None,
            delta: None,
            delta_unit: None,
        };
        let notification = occurrence.delivery_id(channel);
        self.archive
            .apply(vec![Mutation::TestDutyChannel {
                occurrence,
                channel,
            }])
            .await?;
        self.wake.notify_waiters();
        Ok(json!({"notification_id":notification,"channel":channel,"state":"pending"}))
    }
    pub(crate) fn spawn_duty_channels(self: &std::sync::Arc<Self>) -> Result<()> {
        for channel in DutyChannel::ALL {
            let weak = self.weak.clone();
            let wake = self.wake.clone();
            self.spawn_job("",match channel {DutyChannel::Telegram=>crate::runtime::JobKind::Telegram,DutyChannel::Webhook=>crate::runtime::JobKind::Webhook},async move{
                loop {
                    {
                        let Some(service)=weak.upgrade() else{return Ok(())};
                        if service.closed.load(Ordering::Acquire) {return Ok(());}
                        if service.duty_runtime.channel_active(channel)? {
                            match service.deliver_duty_channel(channel).await {
                                Ok(true)=>continue,
                                Ok(false)=>{},
                                Err(_)=>service.startup.lock().await.delivery_error=Some("notification_archive_error".into()),
                            }
                        }
                    }
                    tokio::select!{_=tokio::time::sleep(Duration::from_secs(1))=>{},_=wake.notified()=>{}}
                }
            })?;
        }
        Ok(())
    }
    pub async fn deliver_duty_channel(&self, channel: DutyChannel) -> Result<bool> {
        self.ensure_open()?;
        let now = self.clock.now();
        let Some(claim) = self
            .archive
            .claim_duty_delivery(
                channel,
                now.as_str(),
                &crate::operations::plus_seconds(&now, 30)?,
            )
            .await?
        else {
            return Ok(false);
        };
        let delivery = self.control.deliver_duty(channel, &claim).await;
        let completion = delivery_completion(delivery, &claim.row, self.clock.now())?;
        self.archive
            .finish_duty_delivery(&claim.row, completion)
            .await?;
        Ok(true)
    }
}
fn delivery_completion(
    result: crate::ControlResult<DutyDeliveryReceipt>,
    row: &ori_archive::NotificationRow,
    now: ori_contracts::UtcTimestamp,
) -> Result<NotificationCompletion> {
    let (error, retry) = match result {
        Ok(reply) if reply.delivered && reply.status.is_some_and(|s| (200..300).contains(&s)) => {
            return Ok(NotificationCompletion::Sent);
        }
        Ok(reply) => {
            let status = reply.status;
            let code = status.map_or_else(|| "network_error".into(), |s| format!("HTTP {s}"));
            if status.is_some_and(|s| !(s == 408 || s == 429 || (500..=599).contains(&s))) {
                return Ok(NotificationCompletion::Failed { error: code });
            }
            (code, reply.retry_after_seconds)
        }
        Err(crate::ControlError::Denied) => {
            return Ok(NotificationCompletion::Failed {
                error: "destination_changed".into(),
            });
        }
        Err(_) => ("network_error".into(), None),
    };
    if row.attempts >= 5 {
        return Ok(NotificationCompletion::Failed { error });
    }
    let backoff = (5_i64 << row.attempts.min(5)).min(300);
    // Never retry before a server's deadline. An unreasonable deadline fails closed.
    if retry.is_some_and(|s| s > 7 * 86400) {
        return Ok(NotificationCompletion::Failed {
            error: "retry_window_exceeded".into(),
        });
    }
    let delay = backoff.max(retry.unwrap_or(0) as i64);
    Ok(NotificationCompletion::Retry {
        error,
        next_at: crate::operations::plus_seconds(&now, delay)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_common::Harness;

    #[tokio::test]
    async fn single_impulse_promotes_once_and_later_observations_stay_in_the_digest() {
        let h = Harness::new().await;
        let id = h.prepared().await;
        let mut project = h.service.project(&id).await.unwrap();
        project["notification_channels"] = json!({"webhook":{"enabled":true,"configured":true}});
        h.archive
            .put_monitoring("project", project.clone())
            .await
            .unwrap();
        let mut event = json!({"id":"synthetic-episode","kind":"impulse","last_seen":"2026-11-23T10:14:00Z",
            "center_frequency_hz":400000000,"start_frequency_hz":400000000,"end_frequency_hz":400000000,
            "excess_counts":8,"source_refs":[{"observation_id":"sensitive-original"}]});
        for (occurrence, source) in [("impulse", "o1"), ("sustained", "o3"), ("sustained", "o4")] {
            let mutation = h
                .service
                .duty_detection(&project, &mut event, occurrence, source)
                .await
                .unwrap();
            h.archive.apply(vec![mutation]).await.unwrap();
        }
        let at = "2026-11-23T10:20:00Z";
        let lease = "2026-11-23T10:20:30Z";
        let alert = h
            .archive
            .claim_duty_delivery(DutyChannel::Webhook, at, lease)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(alert.row.payload["kind"], "new_unknown");
        assert_eq!(alert.row.payload["delta_unit"], "s_meter_count");
        assert!(
            !String::from_utf8(alert.body)
                .unwrap()
                .contains("sensitive-original")
        );
        h.archive
            .finish_duty_delivery(&alert.row, NotificationCompletion::Sent)
            .await
            .unwrap();
        assert!(
            h.archive
                .claim_duty_delivery(DutyChannel::Webhook, at, lease)
                .await
                .unwrap()
                .is_none()
        );
        let summary = h
            .archive
            .claim_duty_delivery(
                DutyChannel::Webhook,
                "2026-11-23T11:00:00Z",
                "2026-11-23T11:00:30Z",
            )
            .await
            .unwrap()
            .unwrap();
        assert_eq!(summary.row.payload["impulses"], 1);
        assert_eq!(summary.row.payload["repeats"], 1);
        h.close().await;
    }
}
