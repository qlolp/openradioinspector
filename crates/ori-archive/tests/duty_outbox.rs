use ori_archive::{
    Archive, ArchiveConfig, DutyChannel, DutyKind, DutyOccurrence, Mutation, NotificationCompletion,
};
use serde_json::json;

const NOW: &str = "2026-11-23T10:23:00.000000Z";
const DUE: &str = "2026-11-23T11:00:00.000000Z";
const LEASE: &str = "2026-11-23T11:00:30.000000Z";
fn occurrence(id: &str, kind: DutyKind) -> Mutation {
    Mutation::DutyOccurrence(DutyOccurrence {
        id: id.into(),
        project_id: "fixture".into(),
        at: NOW.into(),
        kind,
        event_id: Some("event-one".into()),
        frequency_hz: Some(433_920_000),
        delta: Some(7.0),
        delta_unit: Some("S-meter".into()),
    })
}
async fn configure(a: &Archive, telegram: bool) {
    a.put_monitoring("project",json!({"id":"fixture","created_at":NOW,"session_ids":[],
        "notification_channels":{"webhook":{"enabled":true,"configured":true},"telegram":{"enabled":telegram,"configured":true}}}))
    .await.unwrap();
}
#[tokio::test]
async fn channels_have_separate_durable_attempts_and_one_hour_digest_per_channel() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("outbox.sqlite");
    let a = Archive::open(ArchiveConfig::new(&path)).unwrap();
    configure(&a, true).await;
    a.apply(vec![
        occurrence("hit", DutyKind::NewUnknown),
        occurrence("hit", DutyKind::NewUnknown),
        occurrence("i", DutyKind::Impulse),
        occurrence("i", DutyKind::Impulse),
        occurrence("r", DutyKind::Repeat),
    ])
    .await
    .unwrap();
    let hook = a
        .claim_duty_delivery(DutyChannel::Webhook, NOW, LEASE)
        .await
        .unwrap()
        .unwrap();
    let tg = a
        .claim_duty_delivery(DutyChannel::Telegram, NOW, LEASE)
        .await
        .unwrap()
        .unwrap();
    assert_ne!(hook.row.id, tg.row.id);
    assert_eq!(tg.row.payload["delta_unit"], "S-meter");
    assert_eq!(tg.row.payload["frequency_hz"], 433_920_000);
    assert!(
        a.finish_duty_delivery(&hook.row, NotificationCompletion::Sent)
            .await
            .unwrap()
    );
    assert!(
        a.finish_duty_delivery(
            &tg.row,
            NotificationCompletion::Retry {
                next_at: DUE.into(),
                error: "HTTP 429".into()
            }
        )
        .await
        .unwrap()
    );
    assert!(
        a.claim_duty_delivery(DutyChannel::Telegram, NOW, LEASE)
            .await
            .unwrap()
            .is_none()
    );
    a.close().await.unwrap();
    let a = Archive::open(ArchiveConfig::new(path)).unwrap();
    let tg = a
        .claim_duty_delivery(DutyChannel::Telegram, DUE, LEASE)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(tg.row.attempts, 2);
    assert!(
        a.finish_duty_delivery(&tg.row, NotificationCompletion::Sent)
            .await
            .unwrap()
    );
    for channel in [DutyChannel::Webhook, DutyChannel::Telegram] {
        let digest = a
            .claim_duty_delivery(channel, DUE, LEASE)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(digest.row.payload["kind"], "hourly_digest");
        assert_eq!(digest.row.payload["impulses"], 1);
        assert_eq!(digest.row.payload["repeats"], 1);
        assert_eq!(digest.row.payload["event_id"], "event-one");
        a.finish_duty_delivery(&digest.row, NotificationCompletion::Sent)
            .await
            .unwrap();
        assert!(
            a.claim_duty_delivery(channel, DUE, LEASE)
                .await
                .unwrap()
                .is_none()
        );
    }
    assert_eq!(
        a.duty_delivery_journal("fixture", 20).await.unwrap().len(),
        4
    );
    a.close().await.unwrap();
}

#[tokio::test]
async fn disabling_one_channel_stops_its_queued_messages_without_blocking_coverage_alerts() {
    let dir = tempfile::tempdir().unwrap();
    let a = Archive::open(ArchiveConfig::new(dir.path().join("outbox.sqlite"))).unwrap();
    configure(&a, true).await;
    a.apply(vec![occurrence("worker-session", DutyKind::CoverageLost)])
        .await
        .unwrap();
    configure(&a, false).await;
    assert!(
        a.claim_duty_delivery(DutyChannel::Telegram, NOW, LEASE)
            .await
            .unwrap()
            .is_none()
    );
    let hook = a
        .claim_duty_delivery(DutyChannel::Webhook, NOW, LEASE)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(hook.row.payload["kind"], "coverage_lost");
    assert_eq!(
        a.duty_delivery_journal("fixture", 8)
            .await
            .unwrap()
            .iter()
            .find(|v| v["channel"] == "telegram")
            .unwrap()["state"],
        "disabled"
    );
    a.close().await.unwrap();
}

#[tokio::test]
async fn telegram_rate_limit_freezes_other_telegram_rows_and_preserves_original_destinations() {
    let dir = tempfile::tempdir().unwrap();
    let a = Archive::open(ArchiveConfig::new(dir.path().join("outbox.sqlite"))).unwrap();
    let project = json!({"id":"fixture","created_at":NOW,"session_ids":[],"notification_channels":{
        "telegram":{"enabled":true,"revision":"first"},"webhook":{"enabled":true,"revision":"other"}}});
    a.put_monitoring("project", project).await.unwrap();
    a.apply(vec![
        occurrence("first", DutyKind::NewUnknown),
        occurrence("second", DutyKind::NewUnknown),
    ])
    .await
    .unwrap();
    let tg = a
        .claim_duty_delivery(DutyChannel::Telegram, NOW, LEASE)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(tg.revision, "first");
    assert!(!String::from_utf8_lossy(&tg.body).contains("first"));
    a.finish_duty_delivery(
        &tg.row,
        NotificationCompletion::Retry {
            error: "HTTP 429".into(),
            next_at: DUE.into(),
        },
    )
    .await
    .unwrap();
    assert!(
        a.claim_duty_delivery(DutyChannel::Telegram, NOW, LEASE)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        a.claim_duty_delivery(DutyChannel::Webhook, NOW, LEASE)
            .await
            .unwrap()
            .is_some()
    );
    a.close().await.unwrap();
}

#[tokio::test]
async fn portable_copy_drops_actuation_and_pending_digests_but_retains_delivery_history() {
    let dir = tempfile::tempdir().unwrap();
    let a = Archive::open(ArchiveConfig::new(dir.path().join("source.sqlite"))).unwrap();
    let p = json!({"id":"fixture","created_at":NOW,"session_ids":[],
        "notification_channels":{"telegram":{"enabled":true,"configured":true,"revision":"local-revision"}},
        "duty":{"enabled":true,"resume_after_restart":true,"schedule":{"mode":"continuous"},"status":"armed","last_error":null}});
    a.put_monitoring("project", p).await.unwrap();
    a.apply(vec![
        occurrence("immediate", DutyKind::NewUnknown),
        occurrence("i", DutyKind::Impulse),
    ])
    .await
    .unwrap();
    let path = dir.path().join("portable.sqlite");
    a.export_portable(&path).await.unwrap();
    let portable = Archive::open(ArchiveConfig::new(path)).unwrap();
    let p = portable
        .monitoring("project", "fixture")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(p["duty"]["enabled"], false);
    assert!(!p.to_string().contains("local-revision"));
    assert_eq!(
        portable.duty_delivery_journal("fixture", 10).await.unwrap()[0]["state"],
        "disabled"
    );
    assert!(
        portable
            .claim_duty_delivery(DutyChannel::Telegram, DUE, LEASE)
            .await
            .unwrap()
            .is_none()
    );
    portable.close().await.unwrap();
    a.close().await.unwrap();
}

#[tokio::test]
async fn a_crash_at_each_claim_stops_after_five_and_stale_completions_cannot_overwrite() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("outbox.sqlite");
    let a = Archive::open(ArchiveConfig::new(&path)).unwrap();
    configure(&a, true).await;
    a.apply(vec![occurrence("excluded-device", DutyKind::CoverageLost)])
        .await
        .unwrap();
    let mut previous = None;
    for (second, attempt) in [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)] {
        let at = format!("2026-11-23T10:23:0{second}Z");
        let lease = format!("2026-11-23T10:23:0{}Z", second + 1);
        let claimed = a
            .claim_duty_delivery(DutyChannel::Webhook, &at, &lease)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(claimed.row.attempts, attempt);
        if let Some(old) = previous.take() {
            assert!(
                !a.finish_duty_delivery(&old, NotificationCompletion::Sent)
                    .await
                    .unwrap()
            );
        }
        previous = Some(claimed.row);
    }
    a.close().await.unwrap();
    let a = Archive::open(ArchiveConfig::new(&path)).unwrap();
    assert!(
        a.claim_duty_delivery(DutyChannel::Webhook, DUE, LEASE)
            .await
            .unwrap()
            .is_none()
    );
    let items = a.duty_delivery_journal("fixture", 5).await.unwrap();
    let hook = items.iter().find(|v| v["channel"] == "webhook").unwrap();
    assert_eq!(hook["state"], "failed");
    assert_eq!(hook["error"], "last_attempt_interrupted");
    a.close().await.unwrap();
}
