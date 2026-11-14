//! Exact original PCM and archive ownership, without an audio device.
mod common;
use common::{Harness, fixture};
use ori_archive::{Archive, ArchiveConfig, Artifact, Mutation};
use ori_guard::GuardService;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn checksum(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

const SAMPLES: [i16; 8] = [i16::MIN, -123, 0, 1, i16::MAX, 1800, -9, 77];
fn pcm() -> Vec<u8> {
    let mut bytes = b"RIFF".to_vec();
    bytes.extend(52u32.to_le_bytes());
    bytes.extend(b"WAVEfmt ");
    bytes.extend(16u32.to_le_bytes());
    bytes.extend(1u16.to_le_bytes());
    bytes.extend(1u16.to_le_bytes());
    bytes.extend(8000u32.to_le_bytes());
    bytes.extend(16000u32.to_le_bytes());
    bytes.extend(2u16.to_le_bytes());
    bytes.extend(16u16.to_le_bytes());
    bytes.extend(b"data");
    bytes.extend(16u32.to_le_bytes());
    for sample in SAMPLES {
        bytes.extend(sample.to_le_bytes());
    }
    bytes
}
async fn source(h: &Harness) -> (String, Artifact) {
    let project = h.prepared().await;
    let f = fixture();
    let payload = pcm();
    let item = Artifact {
        id: "fixture-audio".into(),
        session_id: f["session"]["id"].as_str().unwrap().into(),
        stream_id: f["session"]["streams"][1]["id"].as_str().unwrap().into(),
        schema_version: 1,
        kind: "audio".into(),
        mime_type: "audio/wav".into(),
        filename: "original.wav".into(),
        sha256: checksum(&payload),
        created_at: f["clock"].as_str().unwrap().into(),
        payload,
        metadata: json!({"frequency_hz":100_050_000,"requested_mode":"FM","sample_rate_hz":8000,"channels":1,
            "capture_started_at":f["clock"],"source":"synthetic_fixture","tuning_epoch":"fixture-only-47"}),
    };
    h.archive.apply(vec![Mutation::Artifact(item.clone()), Mutation::Monitoring { kind:"event".into(), value:json!({
        "id":"fixture-event","project_id":project,"created_at":f["clock"],"analysis":{"audio":{
            "id":item.id,"session_id":item.session_id,"stream_id":item.stream_id,"sha256":item.sha256
        }}
    }) }]).await.unwrap();
    (project, item)
}
fn chunks(wav: &[u8]) -> Vec<(&[u8], &[u8])> {
    assert_eq!(&wav[..4], b"RIFF");
    assert_eq!(&wav[8..12], b"WAVE");
    assert_eq!(
        u32::from_le_bytes(wav[4..8].try_into().unwrap()) as usize + 8,
        wav.len()
    );
    let mut result = vec![];
    let mut cursor = 12;
    while cursor < wav.len() {
        let length = u32::from_le_bytes(wav[cursor + 4..cursor + 8].try_into().unwrap()) as usize;
        result.push((
            &wav[cursor..cursor + 4],
            &wav[cursor + 8..cursor + 8 + length],
        ));
        cursor += 8 + length + length % 2;
    }
    assert_eq!(cursor, wav.len());
    result
}

#[tokio::test]
async fn clip_exports_exact_half_open_frames_with_source_provenance_without_rewriting_original() {
    let h = Harness::new().await;
    let (project, original) = source(&h).await;
    let info = h
        .service
        .event_audio_info(&project, "fixture-event")
        .await
        .unwrap();
    assert_eq!(info["frame_count"], 8);
    assert_eq!(info["sample_rate_hz"], 8000);
    for (start, end) in [(0, 1), (7, 8), (1, 7), (0, 8)] {
        let clip = h
            .service
            .event_audio_clip(
                &project,
                "fixture-event",
                start,
                end,
                &original.sha256,
                &original.id,
            )
            .await
            .unwrap();
        let parts = chunks(&clip.bytes);
        let samples = parts.iter().find(|part| part.0 == b"data").unwrap().1;
        let expected = SAMPLES[start as usize..end as usize]
            .iter()
            .flat_map(|x| x.to_le_bytes())
            .collect::<Vec<_>>();
        assert_eq!(samples, expected);
        let provenance: Value =
            serde_json::from_slice(parts.iter().find(|p| p.0 == b"ori ").unwrap().1).unwrap();
        assert_eq!(provenance["schema"], "ori-audio-clip-v1");
        assert_eq!(provenance["source_artifact_id"], original.id);
        assert_eq!(provenance["source_sha256"], original.sha256);
        assert_eq!(provenance["source_context"], original.metadata);
        assert_eq!(
            (
                provenance["start_frame"].as_u64().unwrap(),
                provenance["end_frame"].as_u64().unwrap()
            ),
            (start, end)
        );
        assert_eq!(clip.sha256, checksum(&clip.bytes));
    }
    let saved = h
        .archive
        .get_artifact(&original.session_id, &original.stream_id, &original.id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(saved.payload, original.payload);
    assert_eq!(saved.sha256, original.sha256);
    assert_eq!(
        h.archive
            .list_artifacts(&original.session_id, &original.stream_id, 10, 0)
            .await
            .unwrap()
            .len(),
        1
    );
    assert!(h.control.audio_requests.lock().unwrap().is_empty());
    h.close().await;
}

#[tokio::test]
async fn marks_are_immutable_paged_scoped_and_survive_reopening_the_archive() {
    let h = Harness::new().await;
    let (project, original) = source(&h).await;
    let first = h.service.add_audio_mark(&project, "fixture-event", json!({"start_frame":0,"end_frame":1,"label":"Начало","source_artifact_id":original.id,"source_sha256":original.sha256})).await.unwrap();
    let second = h.service.add_audio_mark(&project, "fixture-event", json!({"start_frame":7,"end_frame":8,"label":"Конец","source_artifact_id":original.id,"source_sha256":original.sha256})).await.unwrap();
    assert_ne!(first["id"], second["id"]);
    assert_eq!(first["source_sha256"], original.sha256);
    let page = h
        .service
        .audio_marks(&project, "fixture-event", "", 1)
        .await
        .unwrap();
    assert_eq!(page["items"], json!([first.clone()]));
    let next = h
        .service
        .audio_marks(
            &project,
            "fixture-event",
            page["next_cursor"].as_str().unwrap(),
            1,
        )
        .await
        .unwrap();
    assert_eq!(next["items"], json!([second]));
    assert!(next["next_cursor"].is_null());
    let other = h
        .service
        .create(
            json!({"name":"Other","site":"Other fixture","session_id":fixture()["session"]["id"]}),
        )
        .await
        .unwrap();
    assert!(
        h.service
            .audio_marks(other["id"].as_str().unwrap(), "fixture-event", "", 32)
            .await
            .is_err()
    );
    assert!(
        h.service
            .event_audio_clip(
                other["id"].as_str().unwrap(),
                "fixture-event",
                0,
                1,
                &original.sha256,
                &original.id
            )
            .await
            .is_err()
    );
    h.service.close().await.unwrap();
    h.archive.close().await.unwrap();
    let archive = Archive::open(ArchiveConfig::new(h.dir.path().join("archive.sqlite"))).unwrap();
    let service = GuardService::with_sources(
        archive.clone(),
        h.control.clone(),
        h.sources.clone(),
        h.sources.clone(),
    );
    let restored = service
        .audio_marks(&project, "fixture-event", "", 32)
        .await
        .unwrap();
    assert_eq!(restored["items"].as_array().unwrap().len(), 2);
    assert_eq!(restored["items"][0], first);
    service.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn invalid_marks_and_clip_ranges_never_change_archive_or_start_capture() {
    let h = Harness::new().await;
    let (project, original) = source(&h).await;
    for mut payload in [
        json!({"start_frame":0,"end_frame":0,"label":"x"}),
        json!({"start_frame":7,"end_frame":6,"label":"x"}),
        json!({"start_frame":0,"end_frame":9,"label":"x"}),
        json!({"start_frame":-1,"end_frame":1,"label":"x"}),
        json!({"start_frame":0.5,"end_frame":1,"label":"x"}),
        json!({"start_frame":false,"end_frame":1,"label":"x"}),
        json!({"start_frame":0,"end_frame":1,"label":"  "}),
        json!({"start_frame":0,"end_frame":1,"label":"x".repeat(201)}),
        json!({"start_frame":0,"end_frame":1,"label":"x","source_sha256":"replacement"}),
    ] {
        payload["source_artifact_id"] = json!(original.id);
        if payload.get("source_sha256").is_none() {
            payload["source_sha256"] = json!(original.sha256);
        }
        assert!(
            h.service
                .add_audio_mark(&project, "fixture-event", payload.clone())
                .await
                .is_err(),
            "{payload}"
        );
    }
    for (start, end) in [(0, 0), (8, 8), (8, 7), (0, 9), (u64::MAX, u64::MAX)] {
        assert!(
            h.service
                .event_audio_clip(
                    &project,
                    "fixture-event",
                    start,
                    end,
                    &original.sha256,
                    &original.id
                )
                .await
                .is_err()
        );
    }
    assert_eq!(
        h.service
            .event_audio_clip(
                &project,
                "fixture-event",
                0,
                1,
                &"0".repeat(64),
                &original.id
            )
            .await
            .err()
            .unwrap()
            .http_status(),
        409
    );
    assert_eq!(h.service.add_audio_mark(&project,"fixture-event",json!({"start_frame":0,"end_frame":1,"label":"stale","source_artifact_id":original.id,"source_sha256":"0".repeat(64)})).await.err().unwrap().http_status(),409);
    assert!(
        h.service
            .audio_marks(&project, "fixture-event", "", 65)
            .await
            .is_err()
    );
    assert!(
        h.service
            .audio_marks(&project, "fixture-event", &"x".repeat(1025), 32)
            .await
            .is_err()
    );
    assert_eq!(
        h.service
            .audio_marks(&project, "fixture-event", "", 32)
            .await
            .unwrap()["items"],
        json!([])
    );
    let saved = h
        .archive
        .get_artifact(&original.session_id, &original.stream_id, &original.id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(saved.payload, original.payload);
    assert!(h.control.audio_requests.lock().unwrap().is_empty());
    h.close().await;
}

#[tokio::test]
async fn identical_pcm_from_another_tuning_artifact_cannot_replace_the_selected_source() {
    let h = Harness::new().await;
    let (project, original) = source(&h).await;
    let result=h.service.add_audio_mark(&project,"fixture-event",json!({"start_frame":0,"end_frame":1,"label":"Silence","source_sha256":original.sha256,"source_artifact_id":"other-tuning-with-identical-pcm"})).await;
    let status = result.err().unwrap().http_status();
    h.close().await;
    assert_eq!(
        status, 409,
        "source identity must be checked together with byte identity"
    );
}

#[tokio::test]
async fn oversized_export_provenance_is_rejected_without_rewriting_the_source() {
    let h = Harness::new().await;
    let (project, mut original) = source(&h).await;
    original.id = "audio-large-context".into();
    original.metadata = json!({"operator_comment":"x".repeat(65536)});
    let event = json!({"id":"large-event","project_id":project,"created_at":fixture()["clock"],"analysis":{"audio":{
        "id":original.id,"session_id":original.session_id,"stream_id":original.stream_id,"sha256":original.sha256
    }}});
    h.archive
        .apply(vec![
            Mutation::Artifact(original.clone()),
            Mutation::Monitoring {
                kind: "event".into(),
                value: event,
            },
        ])
        .await
        .unwrap();
    let error = h
        .service
        .event_audio_clip(
            &project,
            "large-event",
            0,
            1,
            &original.sha256,
            &original.id,
        )
        .await
        .err()
        .unwrap();
    assert_eq!(error.http_status(), 400);
    let saved = h
        .archive
        .get_artifact(&original.session_id, &original.stream_id, &original.id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(saved.payload, original.payload);
    assert_eq!(saved.metadata, original.metadata);
    h.close().await;
}
