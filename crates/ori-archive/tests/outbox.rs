use ori_archive::{
    Archive, ArchiveConfig, ArchiveError, MeasurementCursor, Mutation, NotificationClaim,
    NotificationCompletion, NotificationRow,
};
use serde_json::json;

const NOW: &str = "2026-11-11T00:00:00.123456Z";
const LEASE: &str = "2026-11-11T00:00:30.123456Z";
const KEY: &str = "public-synthetic-outbox-key";

fn stamp(seconds: i64) -> String {
    (chrono::DateTime::parse_from_rfc3339(NOW).unwrap() + chrono::TimeDelta::seconds(seconds))
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}
fn notification(id: &str) -> NotificationRow {
    NotificationRow {
        id: id.into(),
        project_id: "project".into(),
        created_at: NOW.into(),
        payload: json!({"notification_id":id,"project_id":"project","message":"Fixture π","raw":-0.0}),
        state: "pending".into(),
        attempts: 0,
        next_at: Some(NOW.into()),
        error: None,
        lease_until: None,
        acknowledged_at: None,
    }
}
async fn project(archive: &Archive, enabled: bool, configured: bool) {
    archive
        .put_monitoring(
            "project",
            json!({"id":"project","project_id":"project","created_at":NOW,
        "session_ids":[],"webhook_enabled":enabled}),
        )
        .await
        .unwrap();
    if configured {
        archive
            .apply(vec![Mutation::Webhook {
                project_id: "project".into(),
                url: "https://synthetic.invalid/notify".into(),
                secret: KEY.into(),
            }])
            .await
            .unwrap();
    }
}

#[tokio::test]
async fn dedup_retry_claim_cas_acknowledgement_and_reopen_preserve_durable_state() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("outbox.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    project(&archive, true, true).await;
    archive
        .enqueue_notification(notification("notice"))
        .await
        .unwrap();
    let NotificationClaim::Deliver(first) = archive.claim_notification(NOW, LEASE).await.unwrap()
    else {
        panic!("first claim")
    };
    assert_eq!(first.row.attempts, 1);
    assert_eq!(first.row.state, "delivering");
    assert_eq!(first.config.url(), "https://synthetic.invalid/notify");
    assert_eq!(first.config.secret(), KEY);
    assert!(matches!(
        archive.claim_notification(NOW, LEASE).await.unwrap(),
        NotificationClaim::Empty
    ));
    assert!(
        !archive
            .finish_notification("notice", 2, LEASE, NotificationCompletion::Sent)
            .await
            .unwrap()
    );
    assert!(
        !archive
            .finish_notification("notice", 1, &stamp(29), NotificationCompletion::Sent)
            .await
            .unwrap()
    );
    assert!(
        archive
            .finish_notification(
                "notice",
                1,
                LEASE,
                NotificationCompletion::Retry {
                    next_at: stamp(10),
                    error: "network_error".into()
                }
            )
            .await
            .unwrap()
    );
    assert!(matches!(
        archive
            .claim_notification(&stamp(9), &stamp(39))
            .await
            .unwrap(),
        NotificationClaim::Empty
    ));
    let NotificationClaim::Deliver(second) = archive
        .claim_notification(&stamp(10), &stamp(40))
        .await
        .unwrap()
    else {
        panic!("retry claim")
    };
    assert_eq!(second.row.attempts, 2);
    assert!(
        !archive
            .finish_notification("notice", 1, LEASE, NotificationCompletion::Sent)
            .await
            .unwrap()
    );
    assert!(
        !archive
            .acknowledge_notification("foreign", "notice", &stamp(50))
            .await
            .unwrap()
    );
    assert!(
        archive
            .acknowledge_notification("project", "notice", &stamp(50))
            .await
            .unwrap()
    );
    assert!(
        archive
            .finish_notification("notice", 2, &stamp(40), NotificationCompletion::Sent)
            .await
            .unwrap()
    );
    assert!(
        archive
            .acknowledge_notification("project", "notice", &stamp(51))
            .await
            .unwrap()
    );
    archive
        .enqueue_notification(notification("notice"))
        .await
        .unwrap();
    let saved = archive.notification("notice").await.unwrap().unwrap();
    assert_eq!(saved.state, "sent");
    assert_eq!(saved.attempts, 2);
    assert_eq!(saved.acknowledged_at, Some(stamp(50)));
    assert!(saved.lease_until.is_none() && saved.next_at.is_none() && saved.error.is_none());
    assert!(archive.events(0, 100).await.unwrap().is_empty());
    archive.close().await.unwrap();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    assert_eq!(
        archive.notification("notice").await.unwrap().unwrap(),
        saved
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn typed_enqueue_composes_atomically_and_never_reparents_an_existing_id() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("outbox.sqlite"))).unwrap();
    project(&archive, true, true).await;
    let source = json!({"id":"source-session","name":"Fixture","created_at":NOW,
        "streams":[{"id":"source-stream","device":"synthetic","role":"simulator","level_unit":"dBm"}]});
    archive.create_session(source.clone()).await.unwrap();
    assert!(matches!(
        archive
            .apply(vec![
                Mutation::EnqueueNotification(notification("atomic")),
                Mutation::CreateSession(source)
            ])
            .await,
        Err(ArchiveError::Conflict)
    ));
    assert!(archive.notification("atomic").await.unwrap().is_none());
    archive
        .enqueue_notification(notification("owned"))
        .await
        .unwrap();
    let mut foreign = notification("owned");
    foreign.project_id = "foreign".into();
    assert!(matches!(
        archive.enqueue_notification(foreign).await,
        Err(ArchiveError::Conflict)
    ));
    assert_eq!(
        archive
            .notification("owned")
            .await
            .unwrap()
            .unwrap()
            .project_id,
        "project"
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn response_budget_failure_rolls_back_the_claim_before_transport_can_run() {
    let dir = tempfile::tempdir().unwrap();
    let mut claimed = notification("notice");
    claimed.state = "delivering".into();
    claimed.attempts = 1;
    claimed.lease_until = Some(LEASE.into());
    let mut config = ArchiveConfig::new(dir.path().join("outbox.sqlite"));
    config.max_response_bytes = serde_json::to_vec(&claimed).unwrap().len() + 16;
    let archive = Archive::open(config).unwrap();
    project(&archive, true, true).await;
    archive
        .enqueue_notification(notification("notice"))
        .await
        .unwrap();
    assert!(matches!(
        archive.claim_notification(NOW, LEASE).await,
        Err(ArchiveError::QueryBudget)
    ));
    let saved = archive.notification("notice").await.unwrap().unwrap();
    assert_eq!(saved.state, "pending");
    assert_eq!(saved.attempts, 0);
    assert!(saved.lease_until.is_none());
    archive.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn concurrent_claims_never_deliver_the_same_live_lease_and_expired_claims_reject_old_cas() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("outbox.sqlite"))).unwrap();
    project(&archive, true, true).await;
    archive
        .enqueue_notification(notification("notice"))
        .await
        .unwrap();
    let clone = archive.clone();
    let (first, second) = tokio::join!(
        archive.claim_notification(NOW, LEASE),
        clone.claim_notification(NOW, LEASE)
    );
    let claims = [first.unwrap(), second.unwrap()];
    assert_eq!(
        claims
            .iter()
            .filter(|claim| matches!(claim, NotificationClaim::Deliver(_)))
            .count(),
        1
    );
    assert_eq!(
        claims
            .iter()
            .filter(|claim| matches!(claim, NotificationClaim::Empty))
            .count(),
        1
    );
    let NotificationClaim::Deliver(reclaimed) =
        archive.claim_notification(LEASE, &stamp(60)).await.unwrap()
    else {
        panic!("expired lease")
    };
    assert_eq!(reclaimed.row.attempts, 2);
    assert!(
        !archive
            .finish_notification("notice", 1, LEASE, NotificationCompletion::Sent)
            .await
            .unwrap()
    );
    assert!(
        archive
            .finish_notification(
                "notice",
                2,
                &stamp(60),
                NotificationCompletion::Failed {
                    error: "network_error".into()
                }
            )
            .await
            .unwrap()
    );
    assert_eq!(
        archive.notification("notice").await.unwrap().unwrap().state,
        "failed"
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn expired_final_attempt_and_disabled_paths_follow_python_without_transport() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("outbox.sqlite"))).unwrap();
    project(&archive, true, true).await;
    let mut last = notification("final");
    last.state = "delivering".into();
    last.attempts = 5;
    last.lease_until = Some(NOW.into());
    archive
        .apply(vec![Mutation::Notification(
            serde_json::to_value(&last).unwrap(),
        )])
        .await
        .unwrap();
    assert!(matches!(
        archive.claim_notification(NOW, LEASE).await.unwrap(),
        NotificationClaim::Empty
    ));
    let saved = archive.notification("final").await.unwrap().unwrap();
    assert_eq!(saved.state, "failed");
    assert_eq!(saved.attempts, 5);
    assert_eq!(saved.error.as_deref(), Some("last_attempt_interrupted"));
    assert!(saved.lease_until.is_none());
    assert_eq!(saved.next_at, last.next_at); // Python cleanup does not rewrite this column.
    project(&archive, false, true).await;
    let mut disabled = notification("disabled");
    disabled.state = "delivering".into();
    disabled.attempts = 1;
    disabled.lease_until = Some(NOW.into());
    archive
        .apply(vec![Mutation::Notification(
            serde_json::to_value(&disabled).unwrap(),
        )])
        .await
        .unwrap();
    assert!(
        matches!(archive.claim_notification(NOW,LEASE).await.unwrap(),NotificationClaim::Disabled{notification_id} if notification_id=="disabled")
    );
    let saved = archive.notification("disabled").await.unwrap().unwrap();
    assert_eq!(saved.state, "disabled");
    assert_eq!(saved.attempts, 1);
    assert_eq!(saved.lease_until, disabled.lease_until); // Preserve actual original UPDATE semantics.
    project(&archive, true, true).await;
    archive
        .enqueue_notification(notification("disabled"))
        .await
        .unwrap();
    assert!(matches!(
        archive.claim_notification(NOW, LEASE).await.unwrap(),
        NotificationClaim::Empty
    ));
    archive.close().await.unwrap();

    let archive =
        Archive::open(ArchiveConfig::new(dir.path().join("missing-config.sqlite"))).unwrap();
    project(&archive, true, false).await;
    archive
        .enqueue_notification(notification("no-config"))
        .await
        .unwrap();
    assert!(
        matches!(archive.claim_notification(NOW,LEASE).await.unwrap(),NotificationClaim::Disabled{notification_id} if notification_id=="no-config")
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn claim_body_is_exact_stored_json_and_webhook_secret_never_enters_public_rows_or_debug() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("outbox.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    project(&archive, true, true).await;
    archive
        .enqueue_notification(notification("opaque-body"))
        .await
        .unwrap();
    let debug = format!(
        "{:?}",
        Mutation::Webhook {
            project_id: "project".into(),
            url: "https://synthetic.invalid/notify".into(),
            secret: KEY.into()
        }
    );
    assert!(!debug.contains(KEY));
    archive.close().await.unwrap();
    let raw = "{\"z\": 1, \"a\":-0,\"float\":-0.0,\"large\":1208925819614629174706176,\"$serde_json::private::Number\":\"literal\"}";
    {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute(
            "UPDATE monitoring_notifications SET payload=? WHERE id='opaque-body'",
            [raw],
        )
        .unwrap();
    }
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let NotificationClaim::Deliver(claimed) = archive.claim_notification(NOW, LEASE).await.unwrap()
    else {
        panic!("body claim")
    };
    assert_eq!(claimed.body(), raw.as_bytes());
    assert_eq!(claimed.row.payload["a"].to_string(), "0");
    assert_eq!(
        claimed.row.payload["float"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    let page = archive
        .notification_page("project", None, 100)
        .await
        .unwrap();
    assert_eq!(page.items.len(), 1);
    assert!(!serde_json::to_string(&page).unwrap().contains(KEY));
    assert!(!format!("{:?}", page.items[0]).contains(KEY));
    assert_eq!(
        archive
            .webhook_config("project")
            .await
            .unwrap()
            .unwrap()
            .secret(),
        KEY
    );
    assert!(archive.webhook_config("absent").await.unwrap().is_none());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn notification_pages_are_scoped_bounded_and_cursor_stable() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("outbox.sqlite"))).unwrap();
    project(&archive, true, true).await;
    for i in 0..105 {
        archive
            .enqueue_notification(notification(&format!("notice-{i:03}")))
            .await
            .unwrap();
    }
    let first = archive
        .notification_page("project", None, 100)
        .await
        .unwrap();
    assert_eq!(first.items.len(), 100);
    assert_eq!(first.items[0].id, "notice-104");
    let second = archive
        .notification_page("project", first.next_cursor, 100)
        .await
        .unwrap();
    assert_eq!(second.items.len(), 5);
    assert!(second.next_cursor.is_none());
    assert_eq!(second.items[0].id, "notice-004");
    assert!(
        archive
            .notification_page("foreign", None, 100)
            .await
            .unwrap()
            .items
            .is_empty()
    );
    for limit in [0, 101] {
        assert!(matches!(
            archive.notification_page("project", None, limit).await,
            Err(ArchiveError::Invalid(_))
        ));
    }
    assert!(matches!(
        archive
            .notification_page(
                "project",
                Some(MeasurementCursor {
                    started_at: "invalid".into(),
                    id: "x".into()
                }),
                1
            )
            .await,
        Err(ArchiveError::Invalid(_))
    ));
    assert!(matches!(
        archive.claim_notification(NOW, NOW).await,
        Err(ArchiveError::Invalid(_))
    ));
    archive.close().await.unwrap();
}

#[tokio::test]
async fn notification_states_and_claim_body_match_the_actual_python_delivery_oracle() {
    let oracle = ori_archive::parse_json_value(
        include_bytes!("fixtures/outbox_python_oracle.json"),
        1024 * 1024,
    )
    .unwrap();
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("outbox.sqlite"))).unwrap();
    archive
        .put_monitoring("project", oracle["project"].clone())
        .await
        .unwrap();
    archive
        .apply(vec![Mutation::Webhook {
            project_id: "project".into(),
            url: oracle["url"].as_str().unwrap().into(),
            secret: oracle["secret"].as_str().unwrap().into(),
        }])
        .await
        .unwrap();
    let original: NotificationRow = serde_json::from_value(oracle["original"].clone()).unwrap();
    let id = original.id.clone();
    let compare = |row: &NotificationRow, expected: &serde_json::Value| {
        let mut expected = expected.clone();
        expected.as_object_mut().unwrap().remove("payload_json");
        assert_eq!(serde_json::to_value(row).unwrap(), expected);
    };
    archive
        .enqueue_notification(original.clone())
        .await
        .unwrap();
    let mut duplicate = original.clone();
    duplicate.payload["message"] = json!("changed duplicate");
    archive.enqueue_notification(duplicate).await.unwrap();
    compare(
        &archive.notification(&id).await.unwrap().unwrap(),
        &oracle["original"],
    );
    let NotificationClaim::Deliver(first) = archive.claim_notification(NOW, LEASE).await.unwrap()
    else {
        panic!("first oracle claim")
    };
    compare(&first.row, &oracle["transport"][0]["claim"]);
    assert_eq!(
        first.body(),
        oracle["transport"][0]["body"].as_str().unwrap().as_bytes()
    );
    assert!(
        archive
            .finish_notification(
                &id,
                first.row.attempts,
                LEASE,
                NotificationCompletion::Retry {
                    next_at: oracle["retry"]["next_at"].as_str().unwrap().into(),
                    error: oracle["retry"]["error"].as_str().unwrap().into()
                }
            )
            .await
            .unwrap()
    );
    compare(
        &archive.notification(&id).await.unwrap().unwrap(),
        &oracle["retry"],
    );
    assert!(matches!(
        archive
            .claim_notification(&stamp(9), &stamp(39))
            .await
            .unwrap(),
        NotificationClaim::Empty
    ));
    let NotificationClaim::Deliver(second) = archive
        .claim_notification(&stamp(10), &stamp(40))
        .await
        .unwrap()
    else {
        panic!("second oracle claim")
    };
    compare(&second.row, &oracle["transport"][1]["claim"]);
    assert!(
        archive
            .finish_notification(
                &id,
                second.row.attempts,
                &stamp(40),
                NotificationCompletion::Sent
            )
            .await
            .unwrap()
    );
    compare(
        &archive.notification(&id).await.unwrap().unwrap(),
        &oracle["sent"],
    );
    archive
        .acknowledge_notification("project", &id, &stamp(50))
        .await
        .unwrap();
    archive
        .acknowledge_notification("project", &id, &stamp(51))
        .await
        .unwrap();
    compare(
        &archive.notification(&id).await.unwrap().unwrap(),
        &oracle["acknowledged"],
    );
    archive.close().await.unwrap();
}
