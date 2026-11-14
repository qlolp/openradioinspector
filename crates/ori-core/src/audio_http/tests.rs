//! Independent Python vectors and bounded state regressions.
use super::*;
use axum::http::HeaderMap;
use serde_json::json;
use tokio::sync::oneshot;

pub(super) struct MutationLatch {
    entered: oneshot::Sender<()>,
    resume: oneshot::Receiver<()>,
}
impl MutationLatch {
    pub async fn pause(self) {
        self.entered.send(()).expect("admitted mutation observed");
        self.resume.await.expect("admitted mutation released");
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
fn oracle() -> Value {
    serde_json::from_slice(include_bytes!("tests/python_oracle.json")).unwrap()
}
fn assert_original_value(actual: &Value, expected: &Value) {
    // serde_json arbitrary_precision retains exponent spelling. Compare the
    // complete shape, integer types and exact binary64 bits, never a tolerance
    // or removal of common fields from the independent Python fixture.
    match (actual, expected) {
        (Value::Number(a), Value::Number(b)) if a.is_f64() || b.is_f64() => {
            assert_eq!(a.is_f64(), b.is_f64());
            assert_eq!(a.as_f64().unwrap().to_bits(), b.as_f64().unwrap().to_bits());
        }
        (Value::Object(a), Value::Object(b)) => {
            assert_eq!(a.keys().collect::<Vec<_>>(), b.keys().collect::<Vec<_>>());
            for (key, value) in a {
                assert_original_value(value, &b[key]);
            }
        }
        (Value::Array(a), Value::Array(b)) => {
            assert_eq!(a.len(), b.len());
            for (a, b) in a.iter().zip(b) {
                assert_original_value(a, b);
            }
        }
        _ => assert_eq!(actual, expected),
    }
}

#[test]
fn normalized_pcm_preserves_original_clip_and_truncation() {
    let samples = serde_json::json!([-2.0, -1.0, -0.5, -0.0, 0.0, 0.5, 1.0, 2.0]);
    let decoded = pcm::decode_samples(&samples).expect("finite uploaded samples");
    assert_eq!(decoded, [-32767, -32767, -16383, 0, 0, 16383, 32767, 32767]);
}

#[test]
fn sample_validation_preserves_rectangular_flattening_and_empty_nested_blocks() {
    assert_eq!(
        pcm::decode_samples(&serde_json::json!([[false, true], [-1.0, 0.5]])).unwrap(),
        [0, 32767, -32767, 16383]
    );
    assert!(
        pcm::decode_samples(&serde_json::json!([[]]))
            .unwrap()
            .is_empty()
    );
    assert!(pcm::decode_samples(&serde_json::json!([[0.0], [1.0, 2.0]])).is_err());
    assert!(pcm::decode_samples(&serde_json::json!(["NaN"])).is_err());
}

#[test]
fn actual_python_oracle_quantization_and_invalid_values_are_exact() {
    let source = oracle();
    for (name, vector) in source["quantization"].as_object().unwrap() {
        let decoded = pcm::decode_samples(&vector["samples"]).unwrap();
        let bytes = decoded
            .iter()
            .flat_map(|sample| sample.to_le_bytes())
            .collect::<Vec<_>>();
        assert_eq!(hex(&bytes), vector["pcm_hex"].as_str().unwrap(), "{name}");
    }
    for (name, vector) in source["invalid_samples"].as_object().unwrap() {
        let error = pcm::decode_samples(&vector["samples"]).unwrap_err();
        assert_eq!(error.message, vector["message"].as_str().unwrap(), "{name}");
        assert_eq!(
            error.status.as_u16(),
            if vector["error_type"] == "PhonogramError" {
                409
            } else {
                400
            },
            "{name}"
        );
    }
}

#[test]
fn shipped_limits_match_the_actual_original_recorder_and_hub() {
    let limits = &oracle()["limits"];
    assert_eq!(
        HISTORY_FRAMES as f64 / f64::from(RATE),
        limits["pre_history_seconds"].as_f64().unwrap()
    );
    assert_eq!(
        MAX_RECORDING_FRAMES as f64 / f64::from(RATE),
        limits["max_recording_seconds"].as_f64().unwrap()
    );
    assert_eq!(
        pcm::MAX_PUSH_FRAMES as u64,
        limits["max_push_samples"].as_u64().unwrap()
    );
    assert_eq!(
        pcm::MAX_LIVE_FRAMES as u64,
        limits["max_live_samples"].as_u64().unwrap()
    );
    assert_eq!(
        MAX_SUBSCRIBERS as u64,
        limits["max_subscribers"].as_u64().unwrap()
    );
    assert_eq!(
        MAX_QUEUED_BLOCKS as u64,
        limits["max_queued_blocks"].as_u64().unwrap()
    );
}

#[test]
fn recorder_prefix_trim_and_completed_wav_remain_stable() {
    let mut recorder = recorder::Recorder::new_for_test(4, 12, 4096).unwrap();
    recorder
        .push(&[1, 2, 3, 4, 5, 6], "2026-11-11T12:00:00.000000Z")
        .unwrap();
    recorder.start("2026-11-11T12:00:01.000000Z").unwrap();
    recorder
        .push(&[7, 8], "2026-11-11T12:00:02.000000Z")
        .unwrap();
    recorder.stop("2026-11-11T12:00:03.000000Z").unwrap();
    let completed = recorder.completed().unwrap();
    assert_eq!(completed.pre_history_frames(), 4);
    assert_eq!(
        completed.pcm_bytes(),
        vec![3, 0, 4, 0, 5, 0, 6, 0, 7, 0, 8, 0]
    );
    recorder.start("2026-11-11T12:00:04.000000Z").unwrap();
    recorder.push(&[9], "2026-11-11T12:00:05.000000Z").unwrap();
    assert_eq!(
        completed.pcm_bytes(),
        vec![3, 0, 4, 0, 5, 0, 6, 0, 7, 0, 8, 0]
    );
}

#[test]
fn actual_python_completed_artifact_and_summary_are_exact() {
    let source = oracle();
    let mut recorder =
        recorder::Recorder::new_for_test(HISTORY_FRAMES, MAX_RECORDING_FRAMES, 2 * 1024 * 1024)
            .unwrap();
    let mut prefix = pcm::decode_samples(&source["quantization"]["clipping"]["samples"]).unwrap();
    prefix.extend(pcm::decode_samples(&source["quantization"]["adjacent"]["samples"]).unwrap());
    recorder.push(&prefix, "unused").unwrap();
    assert_original_value(&recorder.status(), &source["recording"]["before"]);
    recorder.start("2026-11-11T12:00:00.123456Z").unwrap();
    recorder
        .push(
            &pcm::decode_samples(&json!([0.0, 1.0000000000000002, -0.0, -0.25])).unwrap(),
            "unused",
        )
        .unwrap();
    recorder.stop("2026-11-11T12:00:01.654321Z").unwrap();
    assert_original_value(&recorder.status(), &source["recording"]["status"]);
    let completed = recorder.completed().unwrap();
    let mut wav = completed.header().to_vec();
    wav.extend(completed.pcm_bytes());
    assert_eq!(hex(&wav), source["recording"]["wav_hex"].as_str().unwrap());
}

#[test]
fn auto_stop_preserves_full_new_history_and_the_exact_recording_boundary() {
    let mut recorder = recorder::Recorder::new_for_test(4, 10, 4096).unwrap();
    recorder.push(&[1, 2], "before").unwrap();
    recorder.start("start").unwrap();
    recorder
        .push(&[3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14], "auto-stop")
        .unwrap();
    let completed = recorder.completed().unwrap();
    assert_eq!(completed.summary()["recorded_samples"], 8);
    assert_eq!(completed.summary()["duration_seconds"], 10.0 / 48000.0);
    assert!(!recorder.status()["recording"].as_bool().unwrap());
    assert_eq!(
        completed.pcm_bytes(),
        (1i16..=10).flat_map(i16::to_le_bytes).collect::<Vec<_>>()
    );
    recorder.start("again").unwrap();
    recorder.stop("stopped").unwrap();
    assert_eq!(
        recorder.completed().unwrap().pcm_bytes(),
        (11i16..=14).flat_map(i16::to_le_bytes).collect::<Vec<_>>()
    );
}

#[test]
fn tiny_uploads_coalesce_and_old_response_storage_keeps_its_reservation() {
    let budget = budget::Budget::new(64 * 1024);
    let mut recorder = recorder::Recorder::with_test_budget(16, 4096, budget.clone()).unwrap();
    recorder.start("start").unwrap();
    for _ in 0..4096 {
        recorder.push(&[17], "auto-stop").unwrap();
    }
    let completed = recorder.completed().unwrap();
    assert_eq!(completed.summary()["recorded_samples"], 4096);
    assert!(
        budget.used() < 16 * 1024,
        "one-sample uploads must not allocate per-upload metadata"
    );
    let retained = budget.used();
    drop(recorder);
    assert!(budget.used() > 8192 && budget.used() < retained);
    assert_eq!(completed.pcm_bytes().len(), 8192);
    drop(completed);
    assert_eq!(budget.used(), 0);
}

#[test]
fn failed_capacity_reservation_changes_neither_history_nor_active_recording() {
    let mut recorder = recorder::Recorder::new_for_test(4, 100_000, 66_800).unwrap();
    recorder.start("start").unwrap();
    recorder.push(&[7; 32_000], "accepted").unwrap();
    let before = recorder.status();
    let error = recorder.push(&[9; 1_000], "must-not-commit").unwrap_err();
    assert_eq!(error.code, "audio_runtime_capacity");
    assert_eq!(recorder.status(), before);
    recorder.stop("end").unwrap();
    assert!(
        recorder
            .completed()
            .unwrap()
            .pcm_bytes()
            .as_chunks::<2>()
            .0
            .iter()
            .all(|pair| *pair == 7i16.to_le_bytes())
    );
    recorder.start("next").unwrap_err(); // The retained completed chunks still consume the same budget.
}

#[tokio::test]
async fn subscriber_limit_drop_oldest_order_and_disconnect_match_original() {
    let (_directory, core, receipt) = fixture().await;
    let budget = budget::Budget::new(128 * 1024);
    let mut hub = hub::Hub::new(budget.clone());
    let leases = Arc::new(Semaphore::new(RESPONSE_LEASES));
    let key = ("session".into(), "stream".into());
    let mut subscriptions = Vec::new();
    for _ in 0..4 {
        subscriptions.push(
            hub.subscribe(key.clone(), &leases, receipt.clone())
                .unwrap(),
        );
    }
    assert_eq!(
        hub.subscribe(key.clone(), &leases, receipt.clone())
            .err()
            .unwrap()
            .status,
        StatusCode::CONFLICT
    );
    for frame in 0i16..70 {
        hub.publish(&key, &[frame]).unwrap();
    }
    assert_eq!(hub.status(&key)["queued_blocks"], 256);
    assert_eq!(hub.status(&key)["dropped_blocks"], 24);
    for subscription in &subscriptions {
        for frame in 6i16..70 {
            assert_eq!(
                subscription.0.receive().await.unwrap().bytes(),
                frame.to_le_bytes()
            );
        }
    }
    assert_eq!(
        hub.status(&("other".into(), "stream".into()))["subscribers"],
        0
    );
    drop(subscriptions);
    assert_eq!(hub.status(&key)["subscribers"], 0);
    assert_eq!(leases.available_permits(), RESPONSE_LEASES);
    assert_eq!(budget.used(), 0);
    core.close().await.unwrap();
}

#[tokio::test]
async fn global_delivery_pressure_evicts_oldest_then_counts_pinned_packet_loss() {
    let (_directory, core, receipt) = fixture().await;
    let budget = budget::Budget::new(2500);
    let mut hub = hub::Hub::new(budget.clone());
    let leases = Arc::new(Semaphore::new(RESPONSE_LEASES));
    let key = ("session".into(), "stream".into());
    let subscription = hub.subscribe(key.clone(), &leases, receipt).unwrap();
    hub.publish(&key, &[1; 500]).unwrap();
    hub.publish(&key, &[2; 500]).unwrap();
    assert_eq!(hub.status(&key)["dropped_blocks"], 1);
    let pinned = subscription.0.receive().await.unwrap();
    assert!(
        pinned
            .bytes()
            .as_chunks::<2>()
            .0
            .iter()
            .all(|pair| *pair == 2i16.to_le_bytes())
    );
    hub.publish(&key, &[3; 500]).unwrap();
    assert_eq!(hub.status(&key)["queued_blocks"], 0);
    assert_eq!(hub.status(&key)["dropped_blocks"], 2);
    assert!(budget.used() <= 2500);
    drop(pinned);
    hub.publish(&key, &[4; 500]).unwrap();
    assert_eq!(subscription.0.receive().await.unwrap().bytes().len(), 1000);
    hub.close();
    assert!(!subscription.0.active());
    drop(subscription);
    assert_eq!(budget.used(), 0);
    core.close().await.unwrap();
}

async fn fixture() -> (tempfile::TempDir, Arc<Core>, AuthReceipt) {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(crate::CoreConfig::replay(directory.path()))
        .await
        .unwrap();
    add_session(&core).await;
    let receipt = core
        .security
        .check(
            &Method::POST,
            "/api/v1/sessions/audio-http/phonogram/frames",
            &HeaderMap::new(),
            crate::security::ListenerRole::Plain,
        )
        .await
        .unwrap()
        .receipt();
    (directory, core, receipt)
}
async fn add_session(core: &Core) {
    core.archive().unwrap().create_session(json!({"id":"audio-http","name":"Uploaded PCM fixture","state":"draft","created_at":"2026-11-11T00:00:00.000000Z","started_at":null,"ended_at":null,"contract_version":1,"metadata":{},"streams":[{"id":"r","session_id":"audio-http","device":"Synthetic uploaded samples","role":"receiver","state":"disconnected","level_unit":"s_meter_count","settings":{}}]})).await.unwrap();
}
fn path(family: &str, operation: &str) -> Vec<String> {
    ["api", "v1", "sessions", "audio-http", family, operation]
        .map(str::to_string)
        .to_vec()
}

#[tokio::test]
async fn recorder_admission_is_bounded_without_evicting_completed_artifacts() {
    let (_directory, core, _receipt) = fixture().await;
    let owner = owner(&core).unwrap();
    {
        let mut state = owner.state.lock().await;
        for index in 0..8 {
            let key = ("session".into(), format!("stream-{index}"));
            let recorder = state.recorder(&key).unwrap();
            recorder.start("start").unwrap();
            recorder.push(&[index], "accepted").unwrap();
            recorder.stop("end").unwrap();
        }
        let first = ("session".into(), "stream-0".into());
        let before = state.status(&first);
        assert_eq!(
            state
                .recorder(&("session".into(), "ninth".into()))
                .err()
                .unwrap()
                .code,
            "audio_runtime_capacity"
        );
        assert_eq!(state.recorders.len(), 8);
        assert_eq!(state.status(&first), before);
        assert_eq!(
            state.recorders[&first].completed().unwrap().pcm_bytes(),
            [0, 0]
        );
        assert_eq!(
            state.status(&("session".into(), "unseen".into())),
            recorder::Recorder::empty_status()
        );
        assert_eq!(
            state.recorders.len(),
            8,
            "read-only initial status consumes no activity slot"
        );
    }
    core.close().await.unwrap();
}

#[tokio::test]
async fn wav_response_admission_is_bounded_and_drop_releases_its_own_slot() {
    use futures_util::StreamExt;
    let (_directory, core, receipt) = fixture().await;
    for operation in ["start", "stop"] {
        post(
            &core,
            &path("phonogram", operation),
            json!({"stream_id":"r"}),
            receipt.clone(),
        )
        .await
        .unwrap();
    }
    let query = [("stream_id".into(), "r".into())].into();
    let mut held = Vec::new();
    for _ in 0..32 {
        held.push(
            get(&core, &path("phonogram", "wav"), &query, receipt.clone())
                .await
                .unwrap()
                .unwrap(),
        );
    }
    assert_eq!(
        get(&core, &path("phonogram", "wav"), &query, receipt.clone())
            .await
            .err()
            .unwrap()
            .code,
        "audio_runtime_capacity"
    );
    let mut consumed = held.pop().unwrap().into_body().into_data_stream();
    let in_flight_header = consumed.next().await.unwrap().unwrap();
    assert_eq!(in_flight_header.len(), 44);
    assert!(consumed.next().await.is_none());
    drop(consumed);
    assert_eq!(
        get(&core, &path("phonogram", "wav"), &query, receipt.clone())
            .await
            .err()
            .unwrap()
            .code,
        "audio_runtime_capacity",
        "yielded immutable Bytes still own their metadata/response reservation"
    );
    drop(in_flight_header);
    held.push(
        get(&core, &path("phonogram", "wav"), &query, receipt)
            .await
            .unwrap()
            .unwrap(),
    );
    tokio::time::timeout(Duration::from_secs(2), core.close())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        held.len(),
        32,
        "close does not depend on unpolled response disposal"
    );
}

#[tokio::test]
async fn close_joins_actual_latched_mutation_and_survives_cancelled_waiter() {
    let (_directory, core, receipt) = fixture().await;
    let owner = owner(&core).unwrap().clone();
    let (entered, ready) = oneshot::channel();
    let (resume, released) = oneshot::channel();
    *owner.mutation_latch.lock().unwrap() = Some(MutationLatch {
        entered,
        resume: released,
    });
    let task_core = core.clone();
    let mutation = tokio::spawn(async move {
        post(
            &task_core,
            &path("phonogram", "frames"),
            json!({"stream_id":"r","samples":[-1.0,0.5]}),
            receipt,
        )
        .await
    });
    ready.await.unwrap();
    let attempt = owner.begin_shutdown();
    assert!(
        tokio::time::timeout(Duration::from_millis(30), attempt.completed())
            .await
            .is_err()
    );
    let another = attempt.clone();
    let waiter = tokio::spawn(async move { another.completed().await });
    tokio::task::yield_now().await;
    waiter.abort();
    assert!(waiter.await.unwrap_err().is_cancelled());
    resume.send(()).unwrap();
    let response = mutation.await.unwrap().unwrap().unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let result: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        result["data"]["buffered_seconds"]
            .as_f64()
            .unwrap()
            .to_bits(),
        (2.0f64 / 48000.0).to_bits()
    );
    tokio::time::timeout(Duration::from_secs(1), attempt.completed())
        .await
        .unwrap()
        .unwrap();
    owner.begin_shutdown().completed().await.unwrap();
    assert!(owner.state.lock().await.recorders.is_empty());
    core.close().await.unwrap();
}

#[tokio::test]
async fn unpolled_pcm_and_wav_bodies_do_not_hold_archive_or_producer_ownership() {
    let (directory, core, receipt) = fixture().await;
    assert!(
        core.audio.is_none(),
        "uploaded PCM never constructs a native capture backend"
    );
    let query = [("stream_id".into(), "r".into())].into();
    let pcm = get(&core, &path("audio", "stream"), &query, receipt.clone())
        .await
        .unwrap()
        .unwrap();
    for (operation, payload) in [
        ("start", json!({"stream_id":"r"})),
        ("frames", json!({"stream_id":"r","samples":[-1.0,0.5]})),
        ("stop", json!({"stream_id":"r"})),
    ] {
        post(
            &core,
            &path("phonogram", operation),
            payload,
            receipt.clone(),
        )
        .await
        .unwrap();
    }
    let wav = get(&core, &path("phonogram", "wav"), &query, receipt)
        .await
        .unwrap()
        .unwrap();
    let budget = owner(&core).unwrap().state.lock().await.budget.clone();
    tokio::time::timeout(Duration::from_secs(2), core.close())
        .await
        .unwrap()
        .unwrap();
    // Both response objects are still held, including an unpolled PCM queue
    // and an immutable completed WAV view, while the actual writer is free.
    let writer = ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap();
    assert!(
        budget.used() > 0,
        "only accounted inert memory remains in the held WAV body"
    );
    assert!(
        axum::body::to_bytes(pcm.into_body(), 1024)
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        axum::body::to_bytes(wav.into_body(), 1024)
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(budget.used(), 0);
    drop(writer);
}

#[tokio::test]
async fn revoked_idle_pcm_receipt_wakes_without_another_uploaded_block() {
    use futures_util::StreamExt;
    let (_directory, core, receipt) = fixture().await;
    let query = [("stream_id".into(), "r".into())].into();
    let pcm = get(&core, &path("audio", "stream"), &query, receipt)
        .await
        .unwrap()
        .unwrap();
    let mut body = pcm.into_body().into_data_stream();
    core.security.begin_shutdown();
    assert!(
        tokio::time::timeout(Duration::from_secs(1), body.next())
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        owner(&core)
            .unwrap()
            .state
            .lock()
            .await
            .hub
            .status(&("audio-http".into(), "r".into()))["subscribers"],
        0
    );
    core.close().await.unwrap();
}

async fn nonreading_request(address: std::net::SocketAddr, path: &str) -> tokio::net::TcpStream {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let socket = tokio::net::TcpSocket::new_v4().unwrap();
    socket.set_recv_buffer_size(1024).unwrap();
    let mut socket = socket.connect(address).await.unwrap();
    socket
        .write_all(
            format!("GET {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n")
                .as_bytes(),
        )
        .await
        .unwrap();
    let headers = tokio::time::timeout(Duration::from_secs(2), async {
        let mut headers = Vec::new();
        while !headers.ends_with(b"\r\n\r\n") {
            assert!(headers.len() < 4096);
            headers.push(socket.read_u8().await.unwrap());
        }
        String::from_utf8(headers).unwrap()
    })
    .await
    .unwrap();
    assert!(headers.starts_with("HTTP/1.1 200"), "{headers}");
    socket // The caller deliberately never reads any PCM/WAV body bytes.
}

#[tokio::test]
async fn actual_nonreading_tcp_pcm_and_wav_do_not_deadlock_core_before_socket_shutdown() {
    let (directory, core, _receipt) = fixture().await;
    let owner = owner(&core).unwrap().clone();
    let key = ("audio-http".into(), "r".into());
    {
        let mut state = owner.state.lock().await;
        let recorder = state.recorder(&key).unwrap();
        recorder.start("2026-11-11T12:00:00.000000Z").unwrap();
        let samples = vec![8191; 480_000];
        for _ in 0..10 {
            recorder
                .push(&samples, "2026-11-11T12:00:01.000000Z")
                .unwrap();
        }
        recorder.stop("2026-11-11T12:00:02.000000Z").unwrap();
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let serving = core.clone();
    let server = tokio::spawn(async move { serving.serve(listener).await });
    let wav = nonreading_request(
        address,
        "/api/v1/sessions/audio-http/phonogram/wav?stream_id=r",
    )
    .await;
    let pcm = nonreading_request(
        address,
        "/api/v1/sessions/audio-http/audio/stream?stream_id=r",
    )
    .await;
    {
        let mut state = owner.state.lock().await;
        for _ in 0..64 {
            state.hub.publish(&key, &[8191; 48_000]).unwrap();
        }
    }
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    loop {
        let queued = owner.state.lock().await.hub.status(&key)["queued_blocks"]
            .as_u64()
            .unwrap();
        // At least one body poll happened, but the slow actual TCP peer keeps
        // queued PCM behind it. Neither socket has consumed any body bytes.
        if queued > 0 && queued < 64 {
            break;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "failed to establish actual PCM backpressure: {queued}"
        );
        tokio::task::yield_now().await;
    }
    core.request_shutdown();
    tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let writer = ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap();
    assert!(owner.state.lock().await.recorders.is_empty());
    assert_eq!(owner.state.lock().await.hub.status(&key)["subscribers"], 0);
    drop((wav, pcm, writer));
}

#[tokio::test]
async fn idle_actual_pcm_eof_releases_subscription_while_other_get_keeps_half_close() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let (_directory, core, _receipt) = fixture().await;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let serving = core.clone();
    let server = tokio::spawn(async move { serving.serve(listener).await });
    let key = ("audio-http".into(), "r".into());
    let relative = "/api/v1/sessions/audio-http/audio/stream?stream_id=r";
    for target in [relative.to_string(), format!("http://{address}{relative}")] {
        let pcm = nonreading_request(address, &target).await;
        assert_eq!(
            owner(&core).unwrap().state.lock().await.hub.status(&key)["subscribers"],
            1
        );
        drop(pcm);
        until_unsubscribed(&core, &key).await;
    }
    let mut finite = tokio::net::TcpStream::connect(address).await.unwrap();
    finite
        .write_all(
            format!("GET /api/v1/health HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n")
                .as_bytes(),
        )
        .await
        .unwrap();
    finite.shutdown().await.unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), finite.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    assert!(response.starts_with(b"HTTP/1.1 200"));
    let head = response
        .windows(4)
        .position(|part| part == b"\r\n\r\n")
        .unwrap()
        + 4;
    let body: Value = serde_json::from_slice(&response[head..]).unwrap();
    assert!(body["data"].is_object());
    core.request_shutdown();
    tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
}
async fn until_unsubscribed(core: &Core, key: &hub::Key) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    loop {
        if owner(core).unwrap().state.lock().await.hub.status(key)["subscribers"] == 0 {
            return;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "idle actual peer EOF retained PCM subscription"
        );
        tokio::task::yield_now().await;
    }
}

#[tokio::test]
async fn disconnect_adapter_is_inactive_for_other_routes_and_entity_bodies() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    for request in [
        "GET /api/v1/health HTTP/1.1\r\n\r\n",
        "GET /api/v1/events/ws HTTP/1.1\r\nConnection: upgrade\r\nUpgrade: websocket\r\n\r\n",
        "POST /api/v1/sessions/s/audio/stream HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
        "GET /api/v1/sessions/s/audio/stream HTTP/1.1\r\nContent-Length: 1\r\n\r\n",
    ] {
        let (transport, mut peer) = tokio::io::duplex(64);
        let mut transport = DisconnectIo::new(transport, request.as_bytes());
        peer.shutdown().await.unwrap();
        transport.write_all(b"finite response").await.unwrap();
        transport.flush().await.unwrap();
        let mut output = [0; 15];
        peer.read_exact(&mut output).await.unwrap();
        assert_eq!(&output, b"finite response");
    }
}

#[tokio::test]
async fn disconnect_adapter_preserves_unexpected_input_in_one_byte_buffer() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let (transport, mut peer) = tokio::io::duplex(64);
    let mut transport = DisconnectIo::new(transport, b"GET /api/v1/sessions/s/%61udio/stream?stream_id=r HTTP/1.1\r\nContent-Length: 000\r\n\r\n");
    transport.write_all(b"head").await.unwrap();
    transport.flush().await.unwrap();
    peer.write_all(b"xy").await.unwrap();
    transport.flush().await.unwrap();
    let mut received = [0; 2];
    transport.read_exact(&mut received).await.unwrap();
    assert_eq!(&received, b"xy");
    peer.shutdown().await.unwrap();
    assert!(transport.flush().await.is_err());
}

#[cfg(unix)]
#[tokio::test]
async fn actual_tls_idle_pcm_eof_releases_original_authenticated_subscription() {
    use std::os::unix::fs::PermissionsExt;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let directory = tempfile::tempdir().unwrap();
    let security = directory.path().join("security");
    std::fs::create_dir(&security).unwrap();
    std::fs::set_permissions(&security, std::fs::Permissions::from_mode(0o700)).unwrap();
    let identity =
        ori_runtime::security::TlsIdentity::create(&security, vec!["127.0.0.1".into()]).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let mut config = crate::CoreConfig::replay(directory.path());
    config.lan_security = Some(crate::LanSecurityConfig {
        directory: security,
        hosts: vec![address.to_string()],
        origins: vec![format!("https://{address}")],
    });
    let core = Core::open(config).await.unwrap();
    add_session(&core).await;
    let code = core.begin_pairing_window(60, 3).unwrap();
    let paired = core
        .security
        .pair(
            json!({"pairing_code":code,"label":"synthetic audio EOF","client":"native"}),
            &HeaderMap::new(),
        )
        .await
        .unwrap();
    let paired: Value = serde_json::from_slice(
        &axum::body::to_bytes(paired.into_body(), 4096)
            .await
            .unwrap(),
    )
    .unwrap();
    let token = paired["data"]["token"].as_str().unwrap();
    let mut roots = rustls::RootCertStore::empty();
    for certificate in rustls_pemfile::certs(&mut identity.trust_anchor_pem().as_bytes()) {
        roots.add(certificate.unwrap()).unwrap();
    }
    let client = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(roots)
    .with_no_client_auth();
    let serving = core.clone();
    let server = tokio::spawn(async move { serving.serve_tls(listener).await });
    let tcp = tokio::net::TcpStream::connect(address).await.unwrap();
    let mut tls = tokio_rustls::TlsConnector::from(Arc::new(client))
        .connect(
            rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(),
            tcp,
        )
        .await
        .unwrap();
    tls.write_all(format!("GET /api/v1/sessions/audio-http/audio/stream?stream_id=r HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
    let mut head = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), async {
        while !head.ends_with(b"\r\n\r\n") {
            assert!(head.len() < 4096);
            head.push(tls.read_u8().await.unwrap());
        }
    })
    .await
    .unwrap();
    assert!(head.starts_with(b"HTTP/1.1 200"));
    let key = ("audio-http".into(), "r".into());
    assert_eq!(
        owner(&core).unwrap().state.lock().await.hub.status(&key)["subscribers"],
        1
    );
    drop(tls);
    until_unsubscribed(&core, &key).await;
    core.request_shutdown();
    tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
}
