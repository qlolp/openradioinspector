use ori_archive::{Archive, ArchiveConfig, ArchiveError};
use serde_json::{Value, json};

const START: usize = 15_000_000;
const STEP: usize = 10_000;

fn stamp(seconds: usize) -> String {
    let start = chrono::DateTime::parse_from_rfc3339("2026-11-22T00:00:00Z").unwrap();
    (start + chrono::Duration::seconds(seconds as i64))
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}
fn window(pass_id: &str, pass: usize, total: usize, tile: usize, seconds: usize) -> Value {
    let first = ((tile - 1) * 112).min(total - 112);
    let frequencies: Vec<_> = (first..first + 112).map(|i| START + i * STEP).collect();
    json!({
        "id":format!("{pass_id}/{tile}/{seconds}"), "session_id":"session", "stream_id":"rf",
        "interval":{"started_at":stamp(seconds),"ended_at":stamp(seconds + 1)},
        "frequencies_hz":frequencies, "levels":vec![-90.0;112], "level_unit":"dBm",
        "settings":{
            "adapter":"rf-explorer-3g-serial", "device_mode":"spectrum_analyzer",
            "receive_profile":"rf-explorer-3g-combo-standard-1.34",
            "device_identity":{"main_model":3,"expansion_model":5,"firmware":"01.34"},
            "active_board":"expansion","active_input":"right","board_min_hz":START,
            "board_max_hz":2_700_000_000_u64,"board_max_span_hz":600_000_000,
            "start_hz":frequencies[0],"stop_hz":frequencies[111],"step_hz":STEP,"points":112,
            "rbw_hz":15_000,"amplitude_top_dbm":-10,"amplitude_bottom_dbm":-120,"offset_db":0,
            "level_encoding":"dBm = offset_db - u8 / 2","detector":"normal","detector_code":0,
            "artifact_kind":"raw_spectrum","settings_source":"device_readback","settings_readback":true,
            "timing_source":"host_frame_read","simulated":true,
            "survey":{"start_hz":START,"stop_hz":START+(total-1)*STEP,"step_hz":STEP,
                "pass":pass,"tile":tile,"tiles":total.div_ceil(112),"pass_id":pass_id}
        }
    })
}
async fn open(path: &std::path::Path) -> Archive {
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    archive.create_session(json!({"id":"session","name":"Synthetic RF survey", "created_at":stamp(0),
        "streams":[{"id":"rf","device":"synthetic","role":"spectrum_analyzer","level_unit":"dBm"},
                   {"id":"other","device":"synthetic","role":"spectrum_analyzer","level_unit":"dBm"}]})).await.unwrap();
    archive
}
async fn detail(archive: &Archive, id: &str, columns: usize, since: Option<usize>) -> Value {
    archive
        .rf_explorer_pass("session", "rf", id, columns, since.map(stamp))
        .await
        .unwrap()
}

#[tokio::test]
async fn partial_pass_tracks_measured_intervals_holes_and_both_tail_sources() {
    let dir = tempfile::tempdir().unwrap();
    let archive = open(&dir.path().join("source.sqlite")).await;
    let id = "run-1";
    let mut earlier = window(id, 1, 339, 3, 3);
    let mut tail = window(id, 1, 339, 4, 8);
    earlier["levels"][7] = json!(-14.5); // grid 231, also measured by tile 4
    tail["levels"][1] = json!(-20.5); // grid 228, stronger in tile 4
    tail["levels"][4] = json!(-14.5);
    archive
        .append_sweeps(vec![window(id, 1, 339, 1, 1), tail, earlier])
        .await
        .unwrap();
    let data = detail(&archive, id, 339, None).await;
    assert_eq!(data["summary"]["unique_points"], 227);
    assert_eq!(data["summary"]["received_tiles"], 3);
    assert_eq!(data["summary"]["expected_points"], 339);
    assert_eq!(data["summary"]["complete"], false);
    assert_eq!(
        data["coverage"],
        json!([{"start_hz":START,"stop_hz":START+111*STEP},
                                       {"start_hz":START+224*STEP,"stop_hz":START+338*STEP}])
    );
    assert!(data["columns"][112].is_null());
    assert!(data["hold_columns"].is_null());
    let strongest = &data["columns"][231];
    assert_eq!(strongest["sweep_id"], "run-1/3/3");
    assert_eq!(strongest["point_index"], 7);
    assert_eq!(strongest["started_at"], stamp(3));
    assert_eq!(strongest["ended_at"], stamp(4));
    assert_eq!(strongest["last_measured_at"], stamp(9));
    assert_eq!(data["columns"][228]["sweep_id"], "run-1/4/8");
    let reduced = detail(&archive, id, 1, None).await;
    assert_eq!(reduced["columns"][0], *strongest);
    let first = archive
        .rf_explorer_pass_windows("session", "rf", id, 0, 2)
        .await
        .unwrap();
    assert_eq!(first["items"][0]["tile"], 1);
    assert_eq!(first["items"][1]["sweep_id"], "run-1/3/3");
    assert_eq!(first["next_after_tile"], 3);
    let last = archive
        .rf_explorer_pass_windows("session", "rf", id, 3, 2)
        .await
        .unwrap();
    assert_eq!(last["items"][0]["sweep_id"], "run-1/4/8");
    assert!(last["next_after_tile"].is_null());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn history_is_scoped_cursor_paged_and_hold_preserves_source_and_reset() {
    let dir = tempfile::tempdir().unwrap();
    let archive = open(&dir.path().join("source.sqlite")).await;
    let mut first = window("old-1", 1, 115, 1, 0);
    first["levels"][3] = json!(-10);
    let mut second = window("old-2", 2, 115, 2, 21);
    second["levels"][0] = json!(-23.5);
    let mut other = window("old-2", 2, 115, 1, 10);
    other["stream_id"] = json!("other");
    other["levels"][0] = json!(-1.0);
    archive
        .append_sweeps(vec![
            first,
            window("old-1", 1, 115, 2, 3),
            window("old-2", 2, 115, 1, 17),
            second,
            other,
            window("restart-1", 1, 115, 1, 29),
        ])
        .await
        .unwrap();
    let data = detail(&archive, "old-2", 115, Some(0)).await;
    assert_eq!(data["summary"]["complete"], true);
    assert_eq!(data["summary"]["duration_s"], 5.0);
    assert_eq!(data["summary"]["revisit_s"], 17.0);
    assert_eq!(data["hold_columns"][3]["sweep_id"], "old-1/1/0");
    assert_eq!(data["hold_columns"][3]["last_measured_at"], stamp(22));
    assert_eq!(data["columns"][3]["sweep_id"], "old-2/2/21");
    assert_eq!(
        detail(&archive, "old-2", 115, Some(1)).await["hold_columns"][3]["sweep_id"],
        "old-2/2/21"
    );
    let reset = detail(&archive, "old-2", 115, Some(22)).await;
    assert_eq!(reset["hold_coverage"], json!([]));
    assert!(reset["hold_columns"][3].is_null());
    assert!(detail(&archive, "restart-1", 2, None).await["summary"]["revisit_s"].is_null());
    let restart = detail(&archive, "restart-1", 115, Some(0)).await;
    assert_eq!(restart["hold_columns"][3]["sweep_id"], "restart-1/1/29");
    assert!(restart["hold_columns"][114].is_null());
    let page = archive
        .rf_explorer_passes("session", "rf", 1, None)
        .await
        .unwrap();
    assert_eq!(page["items"][0]["pass_id"], "restart-1");
    let page = archive
        .rf_explorer_passes(
            "session",
            "rf",
            2,
            Some(page["next_before"].as_str().unwrap().to_owned()),
        )
        .await
        .unwrap();
    assert_eq!(page["items"][0]["pass_id"], "old-2");
    assert_eq!(page["items"][1]["pass_id"], "old-1");
    assert!(page["next_before"].is_null());
    assert!(matches!(
        archive
            .rf_explorer_pass("session", "other", "old-1", 1, None)
            .await,
        Err(ArchiveError::NotFound)
    ));
    archive.close().await.unwrap();
}

#[tokio::test]
async fn incompatible_readback_bad_geometry_overlaps_and_duplicates_fail_closed() {
    let dir = tempfile::tempdir().unwrap();
    let archive = open(&dir.path().join("source.sqlite")).await;
    let mut hot = window("old-1", 1, 115, 1, 0);
    hot["levels"][0] = json!(-1);
    let mut changed = window("old-2", 2, 115, 1, 3);
    changed["settings"]["rbw_hz"] = json!(30_000);
    archive
        .append_sweeps(vec![hot, changed, window("old-3", 3, 115, 1, 6)])
        .await
        .unwrap();
    assert_eq!(
        detail(&archive, "old-3", 1, Some(0)).await["hold_columns"][0]["level_dbm"],
        -90.0
    );

    for failure in [
        "profile",
        "lattice",
        "tail",
        "levels",
        "duplicate",
        "timeline",
        "offset",
        "unknown_key",
        "declared_geometry",
    ] {
        let first = window(failure, 1, 115, 1, 30);
        let mut bad = window(failure, 1, 115, 2, 33);
        match failure {
            "profile" => bad["settings"]["device_identity"]["firmware"] = json!("01.35"),
            "lattice" => bad["frequencies_hz"][6] = json!(START + 9 * STEP + 1),
            "tail" => bad["settings"]["start_hz"] = json!(START),
            "levels" => bad["levels"][1] = json!(-55.75),
            "duplicate" => bad = window(failure, 1, 115, 1, 32),
            "timeline" => bad = window(failure, 1, 115, 2, 30),
            "offset" => bad["settings"]["offset_db"] = json!(1),
            "unknown_key" => bad["settings"]["vendor_extension"] = json!(1),
            "declared_geometry" => bad["settings"]["survey"]["stop_hz"] = json!(i64::MAX),
            _ => unreachable!(),
        }
        let raw = bad["id"].as_str().unwrap().to_owned();
        archive.append_sweeps(vec![first, bad]).await.unwrap();
        assert!(
            archive
                .rf_explorer_pass("session", "rf", failure, 1, None)
                .await
                .is_err(),
            "{failure}"
        );
        assert!(archive.get_sweep(&raw).await.unwrap().is_some());
    }
    let first = window("rollback", 1, 115, 1, 52);
    assert!(
        archive
            .append_sweeps(vec![first.clone(), first])
            .await
            .is_err()
    );
    assert!(matches!(
        archive
            .rf_explorer_pass("session", "rf", "rollback", 1, None)
            .await,
        Err(ArchiveError::NotFound)
    ));
    let page = archive
        .rf_explorer_passes("session", "rf", 100, None)
        .await
        .unwrap();
    assert_eq!(page["items"].as_array().unwrap().len(), 3);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn legacy_writers_are_backfilled_incrementally_without_reading_unrelated_arrays() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("legacy.sqlite");
    let archive = open(&path).await;
    archive
        .append_sweeps(vec![window("run-1", 1, 115, 1, 2)])
        .await
        .unwrap();
    archive.close().await.unwrap();

    // Emulate a canonical v6 writer predating this index. It knows only the
    // raw sweep and ingestion tables. Its older UTC spelling normalizes on read.
    let db = rusqlite::Connection::open(&path).unwrap();
    let mut raw = window("run-1", 1, 115, 2, 8);
    raw["levels"][0] = json!(-12.5);
    db.execute("INSERT INTO sweeps(id,session_id,stream_id,started_at,ended_at,frequencies_json,levels_json,level_unit,settings_json)
        VALUES(?1,'session','rf','2026-11-22T03:00:08+03:00','2026-11-22T03:00:09+03:00',?2,?3,'dBm',?4)",
        rusqlite::params![raw["id"].as_str(),raw["frequencies_hz"].to_string(),raw["levels"].to_string(),raw["settings"].to_string()]).unwrap();
    db.execute(
        "INSERT INTO sweep_ingestion(sweep_id,session_id,stream_id) VALUES(?,'session','rf')",
        [raw["id"].as_str().unwrap()],
    )
    .unwrap();
    db.execute_batch("INSERT INTO sweeps VALUES('unrelated','session','other','x','x','invalid historical array','other opaque array','dBm','{}');
        INSERT INTO sweep_ingestion(sweep_id,session_id,stream_id) VALUES('unrelated','session','other')").unwrap();
    let bad = window("oversized", 1, 115, 1, 20);
    db.execute(
        "INSERT INTO sweeps VALUES('bad','session','rf',?1,?2,?3,?4,'dBm',?5)",
        rusqlite::params![
            stamp(20),
            stamp(21),
            bad["frequencies_hz"].to_string(),
            format!("[{}0]", "0,".repeat(100_000)),
            bad["settings"].to_string()
        ],
    )
    .unwrap();
    db.execute(
        "INSERT INTO sweep_ingestion(sweep_id,session_id,stream_id) VALUES('bad','session','rf')",
        [],
    )
    .unwrap();
    drop(db);
    for _ in 0..2 {
        let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
        let data = detail(&archive, "run-1", 115, None).await;
        assert_eq!(data["summary"]["received_tiles"], 2);
        assert_eq!(data["summary"]["complete"], true);
        assert_eq!(data["summary"]["duration_s"], 7.0);
        assert_eq!(data["columns"][3]["sweep_id"], raw["id"]);
        assert_eq!(data["columns"][3]["ended_at"], stamp(9));
        assert!(matches!(
            archive
                .rf_explorer_pass("session", "rf", "oversized", 1, None)
                .await,
            Err(ArchiveError::Conflict)
        ));
        archive.close().await.unwrap();
    }
}

#[tokio::test]
async fn full_standard_board_panorama_and_last_peak_fit_the_default_reader_budget() {
    let dir = tempfile::tempdir().unwrap();
    let archive = open(&dir.path().join("full-band.sqlite")).await;
    let count = 268_501_usize;
    let tiles = count.div_ceil(112);
    assert_eq!(tiles, 2398);
    for start in (1..=tiles).step_by(64) {
        let mut batch = Vec::new();
        for tile in start..=(start + 63).min(tiles) {
            let mut source = window("full-1", 1, count, tile, tile);
            if tile == tiles {
                source["levels"][111] = json!(-7.5)
            }
            batch.push(source);
        }
        archive.append_sweeps(batch).await.unwrap();
    }
    let started = std::time::Instant::now();
    let data = detail(&archive, "full-1", 1024, Some(0)).await;
    eprintln!(
        "full 2,398-tile current + software Max Hold read: {:?}",
        started.elapsed()
    );
    assert_eq!(data["summary"]["expected_points"], 268_501);
    assert_eq!(data["summary"]["unique_points"], 268_501);
    assert_eq!(data["summary"]["received_tiles"], 2398);
    assert_eq!(data["summary"]["complete"], true);
    assert_eq!(data["summary"]["duration_s"], 2398.0);
    assert_eq!(
        data["coverage"],
        json!([{"start_hz":START,"stop_hz":2_700_000_000_u64}])
    );
    assert_eq!(data["columns"].as_array().unwrap().len(), 1024);
    assert_eq!(data["columns"][1023]["frequency_hz"], 2_700_000_000_u64);
    assert_eq!(data["columns"][1023]["sweep_id"], "full-1/2398/2398");
    assert_eq!(data["columns"][1023]["point_index"], 111);
    assert_eq!(data["hold_columns"][1023], data["columns"][1023]);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn reconnect_missing_ordinal_and_unidentified_measurement_end_software_hold() {
    let dir = tempfile::tempdir().unwrap();
    let archive = open(&dir.path().join("boundaries.sqlite")).await;
    let mut hot = window("first-1", 1, 112, 1, 1);
    hot["levels"][0] = json!(-10);
    let mut unrelated = window("untagged", 1, 112, 1, 8);
    unrelated["settings"]["survey"]
        .as_object_mut()
        .unwrap()
        .remove("pass_id");
    let mut second = window("new-2", 2, 112, 1, 4);
    second["levels"][0] = json!(-20);
    let mut fourth = window("new-4", 4, 112, 1, 6);
    fourth["levels"][0] = json!(-30);
    archive
        .append_sweeps(vec![
            hot,
            second,
            fourth,
            unrelated,
            window("new-5", 5, 112, 1, 10),
        ])
        .await
        .unwrap();
    for (pass, level) in [("new-2", -20.0), ("new-4", -30.0), ("new-5", -90.0)] {
        let data = detail(&archive, pass, 1, Some(0)).await;
        assert_eq!(data["hold_columns"][0]["level_dbm"], level);
        assert!(data["summary"]["revisit_s"].is_null());
    }
    archive.close().await.unwrap();
}

#[tokio::test]
async fn cursors_require_the_original_scope_and_survive_a_late_earlier_tile() {
    let dir = tempfile::tempdir().unwrap();
    let archive = open(&dir.path().join("pagination.sqlite")).await;
    archive
        .append_sweeps(vec![
            window("run-1", 1, 115, 1, 1),
            window("run-2", 2, 115, 2, 10),
        ])
        .await
        .unwrap();
    let page = archive
        .rf_explorer_passes("session", "rf", 1, None)
        .await
        .unwrap();
    let cursor = page["next_before"].as_str().unwrap().to_string();
    archive
        .append_sweeps(vec![window("run-2", 2, 115, 1, 7)])
        .await
        .unwrap();
    let older = archive
        .rf_explorer_passes("session", "rf", 1, Some(cursor.clone()))
        .await
        .unwrap();
    assert_eq!(older["items"][0]["pass_id"], "run-1");
    assert!(matches!(
        archive
            .rf_explorer_passes("session", "other", 1, Some(cursor))
            .await,
        Err(ArchiveError::Invalid(_))
    ));
    assert!(matches!(
        archive
            .rf_explorer_passes("session", "rf", 1, Some(format!("{}|999999", stamp(10))))
            .await,
        Err(ArchiveError::Invalid(_))
    ));
    archive.close().await.unwrap();
}
