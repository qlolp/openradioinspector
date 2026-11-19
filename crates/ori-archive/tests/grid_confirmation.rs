use ori_archive::{Archive, ArchiveConfig, Mutation};
use serde_json::{Value, json};

fn batch(incorrect: bool) -> Vec<Mutation> {
    let stamp = "2026-11-19T00:00:00Z";
    let mut links = json!({});
    let mut records = Vec::with_capacity(450);
    for index in 0..449 {
        let id = format!("baseline-{index}");
        links[format!("rx:{index}")] = json!(id);
        let n = if index == 448 { 71 } else { 290 };
        let first = 100_000 + index as u64 * 290 * 10_000;
        let hz = (0..n)
            .map(|i| first + i as u64 * 10_000)
            .collect::<Vec<_>>();
        let refs = (0..21)
            .map(|pass| {
                json!({"session_id":"synthetic-collection",
            "stream_id":"synthetic-analyzer","sweep_id":format!("pass-{pass}-window-{index}"),
            "started_at":stamp,"ended_at":stamp})
            })
            .collect::<Vec<_>>();
        records.push(Mutation::Monitoring {
            kind: "baseline".into(),
            value: json!({
            "project_id":"project","created_at":stamp,"id":id,"scan_band_id":"rx",
            "frequencies_hz":hz,"p50_dbm":vec![-85.12345678901567;n],"p95_dbm":vec![-80.12345678901567;n],
            "mad_db":vec![1.12345678901567;n],"fingerprint":"a".repeat(64),"source_refs":refs,
            "scan_plan_id":"plan","scan_plan_revision":1,"scan_window_count":449,
            "scan_window_index":if incorrect && index==448 {0} else {index}}),
        });
    }
    records.push(Mutation::Monitoring{kind:"project".into(),value:json!({
        "id":"project","created_at":stamp,"session_ids":[],"scan_plan":{"id":"plan","revision":1},"band_baselines":links})});
    records
}

#[tokio::test]
async fn complete_449_window_confirmation_is_one_bounded_atomic_publication() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("grid.sqlite"))).unwrap();
    assert!(archive.apply(batch(true)).await.is_err());
    assert!(
        archive
            .monitoring("baseline", "baseline-0")
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        archive
            .monitoring("project", "project")
            .await
            .unwrap()
            .is_none()
    );
    archive.apply(batch(false)).await.unwrap();
    let project = archive
        .monitoring("project", "project")
        .await
        .unwrap()
        .unwrap();
    for index in [0, 289, 448] {
        let id = &project["band_baselines"][format!("rx:{index}")];
        let baseline = archive
            .monitoring("baseline", id.as_str().unwrap())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(baseline["scan_window_index"], index);
    }
    assert_eq!(project["band_baselines"]["rx:449"], Value::Null);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn grid_scope_is_published_atomically_and_survives_private_restore() {
    use ori_archive::restore_backup;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("live.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive
        .create_session(
            json!({"id":"session","name":"Grid restore","created_at":"2026-01-01T00:00:00Z",
        "streams":[{"id":"s","device":"synthetic","role":"spectrum_analyzer","level_unit":"dBm"}]}),
        )
        .await
        .unwrap();
    let sweep = |id, index| {
        json!({"id":id,"session_id":"session","stream_id":"s","level_unit":"dBm",
        "interval":{"started_at":"2026-01-01T00:00:00Z","ended_at":"2026-01-01T00:00:01Z"},
        "frequencies_hz":[100000,110000],"levels":[-81.1,-83.3],
        "settings":{"scan_plan_id":"plan","scan_plan_revision":3,"scan_band_id":"rx","scan_window_index":index}})
    };
    let original = sweep("one", 1);
    archive.append_sweeps(vec![original.clone()]).await.unwrap();
    assert!(
        archive
            .append_sweeps(vec![sweep("rollback", 0), original])
            .await
            .is_err()
    );
    assert!(archive.get_sweep("rollback").await.unwrap().is_none());
    let before = archive
        .scan_window_counts("session", "s", "plan", 3, "rx")
        .await
        .unwrap();
    assert_eq!(before, vec![(1, 1, 1.0)]);
    let bundle = dir.path().join("recovery");
    archive.backup(&bundle).await.unwrap();
    archive.close().await.unwrap();
    let restored = dir.path().join("restored.sqlite");
    restore_backup(&bundle, &restored).unwrap();
    let archive = Archive::open(ArchiveConfig::new(restored)).unwrap();
    assert_eq!(
        archive
            .scan_window_counts("session", "s", "plan", 3, "rx")
            .await
            .unwrap(),
        before
    );
    assert!(
        archive
            .scan_window_counts("session", "s", "plan", 4, "rx")
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        archive
            .baseline_for_scan_window("session", "s", "plan", 3, "rx", 1)
            .await
            .unwrap()
            .sweep_ids,
        vec!["one"]
    );
    assert_eq!(
        archive.get_sweep("one").await.unwrap().unwrap()["frequencies_hz"],
        json!([100000, 110000])
    );
    archive.close().await.unwrap();
}
