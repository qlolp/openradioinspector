use ori_archive::{Archive, ArchiveConfig, ArchiveError, Mutation};
use serde_json::json;

#[tokio::test]
async fn position_marks_scope_current_search_before_limit_even_with_more_than_a_thousand_other_marks()
 {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("marks.sqlite"))).unwrap();
    archive
        .put_monitoring(
            "position-mark",
            json!({"id":"active-old","project_id":"project",
        "created_at":"2025-01-01T00:00:00.000001Z","search_session_id":"active","level":-0.0}),
        )
        .await
        .unwrap();
    for start in (0..1025).step_by(256) {
        let values = (start..(start + 256).min(1025))
            .map(|i| Mutation::Monitoring {
                kind: "position-mark".into(),
                value: json!({"id":format!("other-{i:04}"),"project_id":"project",
                "created_at":"2026-11-11T00:00:00.123456Z","search_session_id":"other"}),
            })
            .collect();
        archive.apply(values).await.unwrap();
    }
    archive
        .put_monitoring(
            "position-mark",
            json!({"id":"foreign-newer","project_id":"foreign",
        "created_at":"2027-01-01T00:00:00Z","search_session_id":"active"}),
        )
        .await
        .unwrap();
    let active = archive
        .position_marks("project", Some("active"), 256)
        .await
        .unwrap();
    assert_eq!(active.len(), 1);
    assert_eq!(active[0]["id"], "active-old");
    assert_eq!(
        active[0]["level"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    let recent = archive.position_marks("project", None, 256).await.unwrap();
    assert_eq!(recent.len(), 256);
    assert_eq!(recent[0]["id"], "other-1024");
    assert!(
        archive
            .position_marks("project", Some("absent"), 256)
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        archive
            .position_marks("absent", None, 256)
            .await
            .unwrap()
            .is_empty()
    );
    for limit in [0, 257] {
        assert!(matches!(
            archive.position_marks("project", None, limit).await,
            Err(ArchiveError::Invalid(_))
        ));
    }
    archive.close().await.unwrap();
}
