use ori_runtime::notification_secrets::{Destination, configure, destination};
use serde_json::json;
#[test]
fn destinations_are_private_write_only_and_invalid_edits_keep_previous_config() {
    let dir = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let tg = "43210:local_synthetic_fixture";
    let url = "http://127.0.0.1:9997/synthetic-private";
    let signing = "synthetic-signature-only";
    let state = configure(
        dir.path(),
        "../../fixture",
        json!({
        "telegram":{"enabled":true,"bot_token":tg,"chat_id":"-1001234"},
        "webhook":{"enabled":true,"url":url,"secret":signing}}),
    )
    .unwrap();
    assert_eq!(state["telegram"]["configured"], true);
    for private in [tg, url, signing, "-1001234"] {
        assert!(!state.to_string().contains(private));
    }
    let revision = state["telegram"]["revision"].as_str().unwrap();
    let Some(Destination::Telegram { bot_token, chat_id }) =
        destination(dir.path(), "../../fixture", "telegram", revision).unwrap()
    else {
        panic!("telegram")
    };
    assert_eq!((bot_token.as_str(), chat_id.as_str()), (tg, "-1001234"));
    assert!(
        configure(
            dir.path(),
            "../../fixture",
            json!({"webhook":{"enabled":true,"url":"https://user@invalid.example/"}})
        )
        .is_err()
    );
    assert!(
        destination(
            dir.path(),
            "../../fixture",
            "webhook",
            state["webhook"]["revision"].as_str().unwrap()
        )
        .unwrap()
        .is_some()
    );
    let disabled = configure(
        dir.path(),
        "../../fixture",
        json!({"telegram":{"enabled":false}}),
    )
    .unwrap();
    assert_eq!(disabled["telegram"]["enabled"], false);
    assert!(
        destination(dir.path(), "../../fixture", "telegram", revision)
            .unwrap()
            .is_none()
    );
    assert!(
        destination(
            dir.path(),
            "another",
            "webhook",
            state["webhook"]["revision"].as_str().unwrap()
        )
        .unwrap()
        .is_none()
    );
}
