use ori_archive::{Archive, ArchiveConfig};
use ori_session::{
    SessionEffects, SessionError, SessionFuture, SessionService, TuneReceipt, TuneRequest,
};
use serde_json::{Value, json};
use std::sync::Arc;

struct NoEffects;
impl SessionEffects for NoEffects {
    fn tune(&self, _request: TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async {
            Err(SessionError::new(
                503,
                "unavailable",
                "no hardware effects in validation fixture",
            ))
        })
    }
    fn validate_reference(&self, _session: Value, _sweep: Value) -> SessionFuture<'_, ()> {
        Box::pin(async {
            Err(SessionError::new(
                503,
                "unavailable",
                "no scientific callback in validation fixture",
            ))
        })
    }
}

#[tokio::test]
async fn nested_signal_json_errors_precede_session_lookup_and_keep_authority_categories() {
    let directory = tempfile::tempdir().unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite"))).unwrap();
    let service = SessionService::new(archive.clone(), Arc::new(NoEffects));
    for (raw, message) in [
        ("NaN", "JSON не должен содержать NaN или Infinity"),
        ("Infinity", "JSON не должен содержать NaN или Infinity"),
        ("-Infinity", "JSON не должен содержать NaN или Infinity"),
        ("1e999", "JSON не должен содержать неконечные числа"),
        ("{\"name\":1,\"name\":2}", "Поля JSON не должны повторяться"),
        ("{broken}", "Некорректный JSON UTF-8"),
    ] {
        let error = service
            .signal_list_import("missing", json!({"stream_id":"absent","list_json":raw}))
            .await
            .unwrap_err();
        assert_eq!(error.http_status, 400);
        assert_eq!(error.api_code, "invalid_request");
        assert_eq!(error.message, message, "{raw}");
    }
    archive.close().await.unwrap();
}

#[tokio::test]
async fn result_and_reference_validation_precede_missing_session_without_writes() {
    let directory = tempfile::tempdir().unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite"))).unwrap();
    let service = SessionService::new(archive.clone(), Arc::new(NoEffects));
    let before = archive.latest_events(1).await.unwrap();
    assert_eq!(
        service
            .list_measurement_results("missing", Some("invalid"), Some(0))
            .await
            .unwrap_err()
            .message,
        "Неизвестная методика: invalid"
    );
    assert_eq!(
        service
            .list_measurement_results("missing", None, Some(0))
            .await
            .unwrap_err()
            .message,
        "limit должен быть целым от 1 до 1000"
    );
    for name in ["x\n", "x\u{200d}", "x\u{e000}", "x\u{0378}"] {
        assert_eq!(
            service
                .create_sweep_reference("missing", json!({"name":name,"sweep_id":"absent"}))
                .await
                .unwrap_err()
                .message,
            "Имя эталона не должно содержать управляющие символы Unicode"
        );
    }
    assert_eq!(archive.latest_events(1).await.unwrap(), before);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn new_candidate_float_bits_survive_get_list_transition_and_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("archive.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let source: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/session-domain-v1.json"
    ))
    .unwrap();
    archive
        .create_session(source["cases"][0]["seed"]["sessions"][0].clone())
        .await
        .unwrap();
    let service = SessionService::new(archive.clone(), Arc::new(NoEffects));
    let value = service.create_candidate("session-α", json!({"id":"signed-zero","source_stream_id":"spectrum/α",
        "interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.123456Z"},
        "center_frequency_hz":100000003,"level_unit":"dBm","peak_level":-0.0,"score":-1.0000000000000002})).await.unwrap();
    fn bits(value: &Value) {
        assert_eq!(
            value["peak_level"].as_f64().unwrap().to_bits(),
            (-0.0_f64).to_bits()
        );
        assert_eq!(
            value["score"].as_f64().unwrap().to_bits(),
            (-1.0000000000000002_f64).to_bits()
        );
    }
    bits(&value);
    bits(
        &service
            .get_candidate("session-α", "signed-zero")
            .await
            .unwrap(),
    );
    bits(&service.list_candidates("session-α", None).await.unwrap()[0]);
    bits(
        &service
            .dismiss_candidate("session-α", "signed-zero")
            .await
            .unwrap(),
    );
    drop(service);
    archive.close().await.unwrap();
    drop(archive);
    let reopened = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let service = SessionService::new(reopened.clone(), Arc::new(NoEffects));
    bits(
        &service
            .get_candidate("session-α", "signed-zero")
            .await
            .unwrap(),
    );
    assert_eq!(
        service
            .get_candidate("session-α", "signed-zero")
            .await
            .unwrap()["state"],
        "dismissed"
    );
    drop(service);
    reopened.close().await.unwrap();
}

#[tokio::test]
async fn candidate_float_coercion_matches_authority_before_missing_session() {
    let directory = tempfile::tempdir().unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite"))).unwrap();
    let service = SessionService::new(archive.clone(), Arc::new(NoEffects));
    let payload = json!({"id":"candidate","source_stream_id":"spectrum/α",
        "interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.123456Z"},
        "center_frequency_hz":100000003,"level_unit":"dBm"});
    for score in [json!("1_000.25"), json!("١٢.٥"), json!(true), Value::Null] {
        let mut value = payload.clone();
        value["score"] = score;
        assert_eq!(
            service
                .create_candidate("missing", value)
                .await
                .unwrap_err()
                .api_code,
            "session_not_found"
        );
    }
    for (score, message) in [
        (json!("wat"), "could not convert string to float: 'wat'"),
        (
            json!([]),
            "float() argument must be a string or a real number, not 'list'",
        ),
        (
            json!({}),
            "float() argument must be a string or a real number, not 'dict'",
        ),
        (json!("1e999"), "score должно быть конечным числом"),
    ] {
        let mut value = payload.clone();
        value["score"] = score;
        assert_eq!(
            service
                .create_candidate("missing", value)
                .await
                .unwrap_err()
                .message,
            message
        );
    }
    archive.close().await.unwrap();
}
