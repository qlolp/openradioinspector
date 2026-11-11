use ori_archive::{Archive, ArchiveConfig, ArchiveError, SweepQuery};

fn legacy() -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    std::fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &path,
    )
    .unwrap();
    (dir, path)
}

#[tokio::test]
async fn summary_filters_accept_nonnegative_i64_bounds_without_narrowing_raw_write_limits() {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let boundary = archive
        .sweep_ingestion_boundary("fixture-сессия")
        .await
        .unwrap();
    for maximum in [9_007_199_254_740_992, i64::MAX] {
        let mut query = SweepQuery::session("fixture-сессия");
        query.min_hz = Some(0);
        query.max_hz = Some(maximum);
        assert_eq!(
            archive
                .sweep_summaries(query, boundary)
                .await
                .unwrap()
                .items
                .len(),
            4
        );
    }
    let mut query = SweepQuery::session("fixture-сессия");
    query.min_hz = Some(i64::MAX);
    query.max_hz = Some(i64::MAX);
    assert!(
        archive
            .sweep_summaries(query, boundary)
            .await
            .unwrap()
            .items
            .is_empty()
    );
    for (minimum, maximum) in [(None, Some(-1)), (Some(-1), None), (Some(2), Some(1))] {
        let mut query = SweepQuery::session("fixture-сессия");
        query.min_hz = minimum;
        query.max_hz = maximum;
        assert!(matches!(
            archive.sweep_summaries(query, boundary).await,
            Err(ArchiveError::Invalid(_))
        ));
    }
    let mut source = archive.get_sweep("arrival-last").await.unwrap().unwrap();
    source["id"] = serde_json::json!("exact-raw-frequency-beyond-display");
    source["frequencies_hz"] = serde_json::to_value(vec![9_007_199_254_740_993_i64; 4]).unwrap();
    archive.append_sweeps(vec![source.clone()]).await.unwrap();
    assert_eq!(
        archive
            .get_sweep("exact-raw-frequency-beyond-display")
            .await
            .unwrap()
            .unwrap()["frequencies_hz"],
        source["frequencies_hz"]
    );
    for frequency in [
        serde_json::json!(-1),
        serde_json::json!(i64::MAX as u64 + 1),
    ] {
        source["id"] = serde_json::json!("invalid-raw-frequency");
        source["frequencies_hz"] = serde_json::json!([
            frequency.clone(),
            frequency.clone(),
            frequency.clone(),
            frequency
        ]);
        assert!(matches!(
            archive.append_sweeps(vec![source.clone()]).await,
            Err(ArchiveError::Invalid(_))
        ));
        assert!(
            archive
                .get_sweep("invalid-raw-frequency")
                .await
                .unwrap()
                .is_none()
        );
    }
    archive.close().await.unwrap();
}

#[tokio::test]
async fn artifact_offset_preserves_the_http_ten_million_bound() {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    for offset in [1_000_001, 10_000_000] {
        assert!(
            archive
                .list_artifacts("fixture-сессия", "spectrum/one", 1, offset)
                .await
                .unwrap()
                .is_empty()
        );
    }
    assert!(matches!(
        archive
            .list_artifacts("fixture-сессия", "spectrum/one", 1, 10_000_001)
            .await,
        Err(ArchiveError::Invalid(_))
    ));
    archive.close().await.unwrap();
}
