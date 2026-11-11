mod common;
use common::*;
use ori_guard::HistoryQuery;
use serde_json::json;

#[tokio::test]
async fn python_history_keyset_decimated_view_and_exact_original_metadata() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let f = fixture();
    h.archive
        .append_sweeps(f["sweeps"].as_array().unwrap()[20..].to_vec())
        .await
        .unwrap();
    let query = HistoryQuery {
        limit: 3,
        min_hz: 100020000,
        max_hz: 100060000,
        ..Default::default()
    };
    let page = h.service.history(&id, query.clone()).await.unwrap();
    assert_eq!(page, f["history"]);
    let mut after = query;
    after.after = page["next_cursor"].as_str().map(str::to_owned);
    assert_eq!(
        h.service.history(&id, after).await.unwrap(),
        f["history_tail"]
    );
    let last = f["sweeps"].as_array().unwrap().last().unwrap();
    let sweep_id = last["id"].as_str().unwrap();
    assert_eq!(
        h.service.history_original(&id, sweep_id).await.unwrap(),
        *last
    );
    assert_eq!(
        h.service.history_comparison(&id, sweep_id).await.unwrap(),
        f["comparison"]
    );
    let other = h
        .service
        .create(json!({"name":"Other","site":"Site","session_id":f["session"]["id"]}))
        .await
        .unwrap();
    // A second project deliberately shares this session and is an explicit member.
    assert!(
        h.service
            .history_original(other["id"].as_str().unwrap(), sweep_id)
            .await
            .is_ok()
    );
    assert!(h.service.history_original(&id, "absent").await.is_err());
    h.close().await;
}

#[tokio::test]
async fn history_filters_and_event_sources_require_scoped_membership() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let f = fixture();
    h.archive
        .append_sweeps(f["sweeps"].as_array().unwrap()[20..23].to_vec())
        .await
        .unwrap();
    h.service.analyze(&id).await.unwrap();
    let event = f["first_event"]["id"].as_str().unwrap();
    let page = h
        .service
        .history(
            &id,
            HistoryQuery {
                event_id: Some(event.into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(page["items"].as_array().unwrap().len(), 3);
    for query in [
        HistoryQuery {
            limit: 0,
            ..Default::default()
        },
        HistoryQuery {
            limit: 65,
            ..Default::default()
        },
        HistoryQuery {
            min_hz: 2,
            max_hz: 1,
            ..Default::default()
        },
        HistoryQuery {
            after: Some("invalid-cursor".into()),
            ..Default::default()
        },
    ] {
        assert!(h.service.history(&id, query).await.is_err());
    }
    let mut session = f["session"].clone();
    session["id"] = json!("private-session");
    for stream in session["streams"].as_array_mut().unwrap() {
        stream["session_id"] = json!("private-session");
        stream["id"] = json!(format!("private-{}", stream["id"].as_str().unwrap()));
    }
    h.archive.create_session(session.clone()).await.unwrap();
    let other = h
        .service
        .create(json!({"name":"Private","site":"Site","session_id":session["id"]}))
        .await
        .unwrap();
    let other = other["id"].as_str().unwrap();
    assert!(
        h.service
            .history(
                other,
                HistoryQuery {
                    event_id: Some(event.into()),
                    ..Default::default()
                }
            )
            .await
            .is_err()
    );
    assert!(
        h.service
            .history_original(other, f["sweeps"][0]["id"].as_str().unwrap())
            .await
            .is_err()
    );
    h.close().await;
}

#[tokio::test]
async fn original_history_preserves_float_bits_utc_raw_ids_and_untyped_metadata() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let mut raw = fixture()["sweeps"][0].clone();
    raw["id"] = json!("original/%+?é");
    raw["levels"][0] = json!(-0.0);
    raw["levels"][1] = json!(f64::from_bits(0x3ff0000000000001));
    h.archive.append_sweeps(vec![raw.clone()]).await.unwrap();
    let value = h
        .service
        .history_original(&id, raw["id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(value["id"], raw["id"]);
    assert_eq!(
        value["interval"]["started_at"],
        "2026-11-11T12:00:00.123456Z"
    );
    let bytes = ori_contracts::to_json_vec(&value).unwrap();
    let sweep: ori_contracts::Sweep = ori_contracts::from_json_slice(&bytes, 1024 * 1024).unwrap();
    assert_eq!(sweep.levels[0].to_bits(), 0x8000000000000000);
    assert_eq!(sweep.levels[1].to_bits(), 0x3ff0000000000001);
    let metadata = &value["settings"]["operator_metadata"];
    assert_eq!(metadata["huge"].to_string(), "1208925819614629174706176");
    assert_eq!(
        metadata["negative_zero"].as_f64().unwrap().to_bits(),
        0x8000000000000000
    );
    assert_ne!(metadata["é"], metadata["é"]);
    assert_eq!(
        metadata["reserved"]["$serde_json::private::Number"],
        "source"
    );
    h.close().await;
}
