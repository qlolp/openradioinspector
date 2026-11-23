use crate::{Archive, ArchiveConfig, ArchiveError};
use serde_json::{Value, json};

async fn new_archive(dir: &tempfile::TempDir) -> Archive {
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
    archive.create_session(json!({"id":"s","name":"Synthetic receiver","state":"running",
        "created_at":"2026-11-11T00:00:00Z","started_at":"2026-11-11T00:00:00Z","streams":[
        {"id":"r","role":"receiver","device":"Synthetic original","level_unit":"s_meter_count"},
        {"id":"other","role":"receiver","device":"Independent stream","level_unit":"s_meter_count"}]})).await.unwrap();
    archive
}

// Six frequencies, the last clipped to the stop (88.00045 MHz).
fn point(id: &str, pass: i64, index: i64, level: u32, second: i64) -> Value {
    let frequency = [
        88_000_000, 88_000_100, 88_000_200, 88_000_300, 88_000_400, 88_000_450,
    ][index as usize];
    let t = format!("2026-11-11T00:{:02}:{:02}Z", second / 60, second % 60);
    let raw = format!(
        "RX VFA RF0088.{:05} ST010.00 MD0F0 LM{level}1",
        (frequency - 88_000_000) / 10
    );
    json!({"id":id,"session_id":"s","stream_id":"r","interval":{"started_at":t,"ended_at":t},
        "frequency_hz":frequency,"mode":"FM","level":level,"level_unit":"s_meter_count","squelch_open":true,
        "metadata":{"adapter":"ardv1-serial","receiver_profile":"ardv1-monitor-v1","receiver_firmware":"2504A",
        "scan_plan_key":"plan-A","scan_strategy_requested":"rf","scope_source":"sequential_receiver_readback",
        "scan_pass":pass,"scan_index":index,"scan_total":6,"scan_start_hz":88_000_000,"scan_stop_hz":88_000_450,
        "scan_step_hz":100,"settle_s":0.025,"scan_segment_index":0,"observed_frequency_hz":frequency,
        "requested_frequency_hz":frequency,"observed_mode":"FM","requested_mode":"FM","frequency_readback":true,
        "frequency_source":"receiver_RX","mode_source":"receiver_RX","raw_squelch_status":1,"raw_mode_status":"MD0F0",
        "simulated":false,"raw_receiver_status":raw,"evidence_note":"original preserved"}})
}

#[tokio::test]
async fn compact_panorama_counts_unique_verified_points_and_keeps_original_minimum() {
    let dir = tempfile::tempdir().unwrap();
    let a = new_archive(&dir).await;
    let mut wrong_rx = point("wrong-rx", 1, 1, 2, 3);
    wrong_rx["metadata"]["observed_frequency_hz"] = json!(88_000_000);
    let mut wrong_wire = point("wrong-wire", 1, 1, 1, 3);
    wrong_wire["metadata"]["raw_receiver_status"] =
        json!("RX VFA RF0088.00010 ST010.00 MD0F0 LM991");
    let mut other = point("other", 1, 1, 4, 3);
    other["stream_id"] = json!("other");
    let mut requested_only = point("requested-only", 1, 1, 1, 1);
    requested_only["metadata"]["frequency_readback"] = json!(false);
    a.append_observations(vec![
        point("first-tie", 1, 2, 7, 2),
        point("second-tie", 1, 0, 7, 1),
        point("stronger-duplicate", 1, 2, 3, 5),
        point("later-equal", 1, 0, 3, 1),
        point("stop", 1, 5, 12, 8),
        wrong_rx,
        wrong_wire,
        requested_only,
        other,
    ])
    .await
    .unwrap();
    let panorama = a.receiver_panorama("s", "r", Some(1), 2).await.unwrap();
    assert_eq!(panorama["total_points"], 6);
    assert_eq!(panorama["measured_points"], 3);
    assert_eq!(panorama["complete"], false);
    assert_eq!(panorama["coverage_hex"], "25");
    assert_eq!(panorama["level_unit"], "s_meter_count");
    assert_eq!(panorama["lower_is_stronger"], true);
    let bins = panorama["bins"].as_array().unwrap();
    assert_eq!(bins.len(), 2);
    assert_eq!(bins[0]["start_index"], 0);
    assert_eq!(bins[0]["end_index"], 2);
    assert_eq!(bins[0]["measured_points"], 2);
    assert_eq!(bins[0]["observation_id"], "stronger-duplicate");
    assert_eq!(bins[0]["frequency_hz"], 88_000_200);
    assert_eq!(bins[0]["level"], 3);
    assert_eq!(bins[1]["frequency_hz"], 88_000_450);
    assert_eq!(bins[0]["started_at"], "2026-11-11T00:00:05.000000Z");
    for hz in [88_000_100, 88_000_449, 88_000_500, 87_999_999] {
        assert!(matches!(
            a.receiver_panorama_point("s", "r", 1, hz).await,
            Err(ArchiveError::NotFound)
        ));
    }
    let stop = a
        .receiver_panorama_point("s", "r", 1, 88_000_450)
        .await
        .unwrap();
    assert_eq!(Some(stop), a.get_observation("stop").await.unwrap());
    assert_eq!(
        a.receiver_panorama("s", "other", Some(1), 1024)
            .await
            .unwrap()["measured_points"],
        1
    );
    assert!(matches!(
        a.receiver_panorama("wrong", "r", Some(1), 2).await,
        Err(ArchiveError::NotFound)
    ));
    assert!(a.get_observation("wrong-rx").await.unwrap().is_some());
    a.close().await.unwrap();
}

#[tokio::test]
async fn checkpoint_follows_commits_and_new_pass_persists_across_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let a = new_archive(&dir).await;
    a.append_observations(
        (0..6)
            .map(|i| point(&format!("a{i}"), 1, i, 12, 30 + i))
            .collect(),
    )
    .await
    .unwrap();
    let mut wrong_plan = point("unrelated", 1, 3, 1, 48);
    wrong_plan["metadata"]["scan_plan_key"] = json!("plan-B");
    let mut invalid = point("invalid", 2, 1, 1, 59);
    invalid["metadata"]["scan_total"] = json!(7);
    let mut manual = point("ordinary", 1, 1, 1, 58);
    manual["metadata"]
        .as_object_mut()
        .unwrap()
        .remove("scan_pass");
    a.append_observations(vec![
        wrong_plan,
        point("new-cycle", 2, 0, 24, 2),
        manual,
        invalid,
    ])
    .await
    .unwrap();
    a.close().await.unwrap();
    let a = Archive::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
    assert_eq!(
        a.latest_receiver_scan_point("s", "r", "plan-A")
            .await
            .unwrap()
            .unwrap()["id"],
        "new-cycle"
    );
    assert!(
        a.latest_receiver_scan_point("s", "other", "plan-A")
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        a.latest_receiver_scan_point("s", "r", "absent")
            .await
            .unwrap()
            .is_none()
    );
    let latest = a.receiver_panorama("s", "r", None, 3).await.unwrap();
    assert_eq!(latest["pass"], 2);
    assert_eq!(latest["measured_points"], 1);
    let first = a.receiver_panorama("s", "r", Some(1), 1024).await.unwrap();
    assert_eq!(first["complete"], true);
    assert_eq!(first["bins"].as_array().unwrap().len(), 6);
    assert_eq!(first["bins"][5]["end_index"], 5);
    a.append_observations(
        (1..6)
            .map(|i| point(&format!("b{i}"), 2, i, 7, i + 2))
            .collect(),
    )
    .await
    .unwrap();
    assert_eq!(
        a.receiver_panorama("s", "r", None, 3).await.unwrap()["complete"],
        true
    );
    a.close().await.unwrap();
}

#[tokio::test]
async fn guard_progress_requires_every_index_and_freezes_independent_passes() {
    let dir = tempfile::tempdir().unwrap();
    let a = new_archive(&dir).await;
    let empty = a.ardv1_reference_progress("s", "r").await.unwrap();
    assert_eq!(empty["boundary"], 0);
    assert_eq!(empty["ready"], false);
    let mut points = Vec::new();
    for pass in 1..=21 {
        for idx in 0..5 {
            points.push(point(
                &format!("{pass}/{idx}"),
                pass,
                idx,
                pass as u32,
                pass * 4,
            ));
        }
    }
    points.push(point("duplicate", 21, 4, 1, 99));
    a.append_observations(points).await.unwrap();
    let pending = a.ardv1_reference_progress("s", "r").await.unwrap();
    assert_eq!(pending["ready"], false);
    assert_eq!(pending["ready_points"], 5);
    assert_eq!(pending["plan_key"], "plan-A");
    assert_eq!(pending["minimum_passes_observed"], 0);
    let boundary = pending["boundary"].as_u64().unwrap();
    let key = pending["context_key"].as_str().unwrap();
    let samples = a
        .ardv1_reference_points("s", "r", key, boundary, 4, 2)
        .await
        .unwrap();
    assert_eq!(samples.len(), 2);
    assert_eq!(samples[0]["samples"].as_array().unwrap().len(), 21);
    assert_eq!(samples[0]["samples"][20]["observation_id"], "21/4");
    assert_eq!(samples[1]["samples"], json!([]));
    a.append_observations(
        (1..=20)
            .map(|p| point(&format!("{p}/5"), p, 5, 24, p * 4))
            .collect(),
    )
    .await
    .unwrap();
    let ready = a.ardv1_reference_progress("s", "r").await.unwrap();
    assert_eq!(ready["ready"], true);
    assert_eq!(ready["minimum_passes_observed"], 20);
    assert_eq!(ready["elapsed_seconds"], 76.0);
    assert!(
        a.ardv1_reference_points("s", "r", key, boundary, 5, 1)
            .await
            .unwrap()[0]["samples"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let changes = a.ardv1_points_after("s", "r", boundary, 2).await.unwrap();
    assert_eq!(changes.len(), 2);
    assert_eq!(changes[0].1["observation"]["id"], "1/5");
    assert_eq!(changes[0].1["context_key"], key);
    assert_eq!(
        a.ardv1_points_after("s", "r", changes[1].0, 1)
            .await
            .unwrap()[0]
            .1["observation"]["id"],
        "3/5"
    );
    assert!(
        a.ardv1_reference_points("s", "r", key, boundary, 0, 65)
            .await
            .is_err()
    );
    a.close().await.unwrap();
}

fn write_canonical(db: &rusqlite::Connection, observation: &Value) {
    db.execute("INSERT INTO observations(id,session_id,stream_id,started_at,ended_at,frequency_hz,mode,level,level_unit,squelch_open,metadata_json)
        VALUES(?,'s','r',?, ?,?,'FM',?,'s_meter_count',1,?)", rusqlite::params![
        observation["id"].as_str(),observation["interval"]["started_at"].as_str(),observation["interval"]["ended_at"].as_str(),
        observation["frequency_hz"].as_i64(),observation["level"].as_f64(),observation["metadata"].to_string()
    ]).unwrap();
}

#[tokio::test]
async fn historical_and_rollback_writer_originals_are_indexed_without_changes_to_raw_rows() {
    let dir = tempfile::tempdir().unwrap();
    new_archive(&dir).await.close().await.unwrap();
    let path = dir.path().join("archive.sqlite");
    let original = point("before-upgrade", 1, 0, 36, 1);
    {
        let old = rusqlite::Connection::open(&path).unwrap();
        // The archive before this extension was installed had no cyclic indices.
        old.execute_batch(
            "DROP TRIGGER ori_ardv1_enqueue_v1; DROP TABLE ori_ardv1_pending_v1;
            DROP TABLE ori_ardv1_progress_v1; DROP TABLE ori_ardv1_points_v1;
            DROP TABLE ori_ardv1_passes_v1; DROP TABLE ori_ardv1_contexts_v1;",
        )
        .unwrap();
        write_canonical(&old, &original);
        let mut single = point("old-single-shot", 1, 1, 8, 2);
        single["metadata"]
            .as_object_mut()
            .unwrap()
            .remove("scan_pass");
        write_canonical(&old, &single);
    }
    let a = Archive::open(ArchiveConfig::new(&path)).unwrap();
    assert_eq!(
        a.receiver_panorama("s", "r", None, 2).await.unwrap()["measured_points"],
        1
    );
    let committed = a.ardv1_reference_progress("s", "r").await.unwrap()["boundary"]
        .as_u64()
        .unwrap();
    // A failing multi-item commit rolls back the projection and checkpoint too.
    assert!(
        a.append_observations(vec![point("rolled-back", 2, 0, 99, 1), original.clone()])
            .await
            .is_err()
    );
    assert!(
        a.ardv1_points_after("s", "r", committed, 10)
            .await
            .unwrap()
            .is_empty()
    );
    a.close().await.unwrap();
    let later = point("rollback-writer", 1, 1, 42, 0);
    {
        let old = rusqlite::Connection::open(&path).unwrap();
        write_canonical(&old, &later);
    }
    let a = Archive::open(ArchiveConfig::new(&path)).unwrap();
    assert_eq!(
        a.ardv1_points_after("s", "r", committed, 10).await.unwrap()[0].1["observation"]["id"],
        "rollback-writer"
    );
    assert_eq!(
        a.latest_receiver_scan_point("s", "r", "plan-A")
            .await
            .unwrap()
            .unwrap()["metadata"],
        later["metadata"]
    );
    a.close().await.unwrap();
    let db = rusqlite::Connection::open(&path).unwrap();
    let metadata: String = db
        .query_row(
            "SELECT metadata_json FROM observations WHERE id='before-upgrade'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(metadata, original["metadata"].to_string());
    assert_eq!(
        db.query_row("SELECT count(*) FROM sweeps", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[tokio::test]
#[ignore = "full 129,991-original SQLite scale fixture"]
async fn full_grid_remains_bounded_and_its_last_original_is_exact() {
    let dir = tempfile::tempdir().unwrap();
    new_archive(&dir).await.close().await.unwrap();
    let path = dir.path().join("archive.sqlite");
    let template = point("template", 1, 0, 30, 0)["metadata"].clone();
    {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute("WITH RECURSIVE indices(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM indices WHERE i<129990),
                grid AS (SELECT i,100000+i*10000 AS hz,30+i%500 AS level FROM indices)
            INSERT INTO observations(id,session_id,stream_id,started_at,ended_at,frequency_hz,mode,level,level_unit,squelch_open,metadata_json)
            SELECT printf('wide-%d',i),'s','r','2026-11-11T00:00:00Z','2026-11-11T00:00:00Z',hz,'FM',level,'s_meter_count',1,
                json_set(?,'$.scan_start_hz',100000,'$.scan_stop_hz',1300000000,'$.scan_step_hz',10000,'$.scan_total',129991,
                    '$.scan_index',i,'$.scan_segment_index',i/1000,'$.requested_frequency_hz',hz,'$.observed_frequency_hz',hz,
                    '$.raw_receiver_status',printf('RX VFA RF%04d.%05d ST010.00 MD0F0 LM%d1',hz/1000000,hz%1000000/10,level))
            FROM grid", [template.to_string()]).unwrap();
    }
    let a = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let pass = a.receiver_panorama("s", "r", Some(1), 1024).await.unwrap();
    assert_eq!(pass["total_points"], 129_991);
    assert_eq!(pass["measured_points"], 129_991);
    assert_eq!(pass["complete"], true);
    assert_eq!(pass["bins"].as_array().unwrap().len(), 1024);
    let coverage = pass["coverage_hex"].as_str().unwrap();
    assert_eq!(coverage.len(), 32_498);
    assert!(coverage.ends_with("7f"));
    assert_eq!(pass["bins"][1023]["end_index"], 129_990);
    assert_eq!(
        a.receiver_panorama_point("s", "r", 1, 1_300_000_000)
            .await
            .unwrap()["id"],
        "wide-129990"
    );
    a.close().await.unwrap();
}

#[tokio::test]
async fn immutable_original_resolves_the_same_context_after_a_stronger_duplicate() {
    let dir = tempfile::tempdir().unwrap();
    let archive = new_archive(&dir).await;
    let original = point("first", 1, 0, 55, 2);
    archive
        .append_observations(vec![original.clone(), point("repeat", 1, 0, 37, 5)])
        .await
        .unwrap();
    let identified = crate::verified_receiver_scan_context(
        &archive.get_observation("first").await.unwrap().unwrap(),
    );
    assert_eq!(
        identified.as_deref(),
        archive.ardv1_reference_progress("s", "r").await.unwrap()["context_key"].as_str()
    );
    for (field, value) in [
        (
            "raw_receiver_status",
            json!("RX VFA RF0088.00000 ST010.00 MD0F0 LM371"),
        ),
        ("observed_mode", json!("AM")),
        ("scan_segment_index", json!(1)),
    ] {
        let mut invalid = original.clone();
        invalid["metadata"][field] = value;
        assert!(crate::verified_receiver_scan_context(&invalid).is_none());
    }
    let mut invalid = original;
    invalid["interval"]["started_at"] = json!("2026-11-12T00:00:00Z");
    assert!(crate::verified_receiver_scan_context(&invalid).is_none());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn stored_grid_dimensions_must_still_fit_the_native_limit_before_projection() {
    let dir = tempfile::tempdir().unwrap();
    let archive = new_archive(&dir).await;
    archive
        .append_observations(vec![point("original", 1, 0, 8, 1)])
        .await
        .unwrap();
    archive.close().await.unwrap();
    let path = dir.path().join("archive.sqlite");
    {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute("UPDATE ori_ardv1_contexts_v1 SET total=129992", [])
            .unwrap();
    }
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    assert!(matches!(
        archive.receiver_panorama("s", "r", None, 1024).await,
        Err(ArchiveError::Corrupt(_))
    ));
    assert!(matches!(
        archive.ardv1_reference_progress("s", "r").await,
        Err(ArchiveError::Corrupt(_))
    ));
    assert!(archive.get_observation("original").await.unwrap().is_some());
    archive.close().await.unwrap();
}
