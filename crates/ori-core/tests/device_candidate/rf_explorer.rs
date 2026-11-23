use super::*;
use ori_contracts::DeviceStream;
use ori_core::devices::{BoxTransport, DeviceFuture, DeviceResult, TransportFactory};
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const ADAPTER: &str = "rf-explorer-3g-serial";
// Model/configuration from a 3G Combo Standard. Sample counts are synthetic.
const MODEL: &[u8] = b"#C2-M:003,005,01.34\r\n";
const CONFIG: &[u8] =
    b"#C2-F:0100000,0010000,0000,-118,0112,1,000,0015000,2700000,0600000,00015,0000,004\r\n";

#[derive(Default)]
struct DuplexFactory {
    opens: AtomicUsize,
}
impl TransportFactory for DuplexFactory {
    fn identity(&self, _: &DeviceStream) -> DeviceResult<String> {
        // Different session/port labels can refer to the same physical connection.
        Ok("owned-rfe-duplex".into())
    }
    fn open<'a>(&'a self, stream: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        Box::pin(async move {
            self.opens.fetch_add(1, Ordering::AcqRel);
            let bad_identity = stream.port.as_deref() == Some("wrong-device");
            let tuned = stream.port.as_deref() == Some("owned-window");
            let survey = stream.port.as_deref() == Some("owned-survey");
            let (host, mut peer) = tokio::io::duplex(4096);
            tokio::spawn(async move {
                let mut command = [0; 4];
                if peer.read_exact(&mut command).await.is_err() {
                    return;
                }
                assert_eq!(&command, b"#\x04C0");
                let model = if bad_identity {
                    b"#C2-M:003,005,01.33\r\n"
                } else {
                    MODEL
                };
                let banner = b"RF Explorer 01.34 27-Apr-22 01.04.05\r\n";
                let mut reply = [banner.as_slice(), model, CONFIG].concat();
                if survey {
                    peer.write_all(&reply).await.unwrap();
                    for (start, stop) in
                        [(200_000, 201_110), (201_120, 202_230), (200_000, 201_110)]
                    {
                        let mut command = [0; 32];
                        if peer.read_exact(&mut command).await.is_err() {
                            return;
                        }
                        assert_eq!(
                            &command,
                            format!("#\x20C2-F:{start:07},{stop:07},0000,-118").as_bytes()
                        );
                        let mut frame = CONFIG.to_vec();
                        frame[6..13].copy_from_slice(format!("{start:07}").as_bytes());
                        frame.extend_from_slice(b"$S\x70");
                        frame.extend(130..242);
                        frame.extend_from_slice(b"\r\n");
                        peer.write_all(&frame).await.unwrap();
                    }
                    let mut rest = Vec::new();
                    let _ = peer.read_to_end(&mut rest).await;
                    return;
                }
                if tuned {
                    peer.write_all(&reply).await.unwrap();
                    let mut command = [0; 32];
                    peer.read_exact(&mut command).await.unwrap();
                    assert_eq!(&command, b"#\x20C2-F:1001120,1002230,0000,-118");
                    reply = CONFIG.to_vec();
                    reply[6..13].copy_from_slice(b"1001120");
                }
                reply.extend_from_slice(b"$S\x70");
                reply.extend(130..242);
                reply.extend_from_slice(b"\r\n");
                if peer.write_all(&reply).await.is_ok() {
                    let mut rest = Vec::new();
                    let _ = peer.read_to_end(&mut rest).await;
                }
            });
            Ok(Box::new(host) as BoxTransport)
        })
    }
}

fn explorer(port: &str) -> Value {
    json!({"id":"rfe","device":"RF Explorer 3G Combo Standard","role":"spectrum_analyzer",
        "level_unit":"dBm","port":port,"settings":{"adapter":ADAPTER}})
}

#[tokio::test]
async fn capability_requires_a_live_core_with_a_native_connector() {
    for (mode, connector, expected) in [
        (ori_core::CoreMode::Live, true, true),
        (ori_core::CoreMode::Live, false, false),
        (ori_core::CoreMode::Replay, true, false),
        (ori_core::CoreMode::Shadow, false, false),
    ] {
        let directory = tempfile::tempdir().unwrap();
        let mut config = CoreConfig::new(directory.path());
        config.mode = mode;
        let factory = Arc::new(DuplexFactory::default());
        if connector {
            config.devices.transport = Some(factory.clone());
        }
        let server = Server::start(config).await;
        let (_, capabilities) = server.request("GET", "/api/v1/capabilities", None).await;
        assert_eq!(
            capabilities["data"]["features"]["rf_explorer_3g_spectrum"]
                .as_bool()
                .unwrap_or(false),
            expected,
            "{mode:?}, connector={connector}"
        );
        assert_eq!(
            capabilities["data"]["features"]["rf_explorer_10khz_survey"], expected,
            "{mode:?}, connector={connector}"
        );
        assert_eq!(
            capabilities["data"]["features"]["rf_explorer_survey_panorama"],
            mode != ori_core::CoreMode::Shadow,
            "reading archived passes is independent of the live USB connector"
        );
        if mode == ori_core::CoreMode::Replay && connector {
            setup(
                &server,
                json!({"id":"replay","name":"Replay","streams":[explorer("owned-replay")]}),
            )
            .await;
            let (code, error) = worker(&server, "replay", "rfe", "start").await;
            assert_eq!(code, 400, "{error}");
            assert_eq!(error["error"]["code"], "unsupported_adapter");
            assert_eq!(factory.opens.load(Ordering::Acquire), 0);
        }
        server.stop().await;
    }
}

async fn worker(server: &Server, session: &str, stream: &str, operation: &str) -> (u16, Value) {
    server
        .request(
            "POST",
            &format!("/api/v1/sessions/{session}/streams/{stream}/{operation}"),
            Some(&json!({})),
        )
        .await
}

#[tokio::test]
async fn requested_window_is_forwarded_and_archived_on_its_reported_grid() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::live(directory.path());
    config.devices.transport = Some(Arc::new(DuplexFactory::default()));
    let server = Server::start(config).await;
    let mut stream = explorer("owned-window");
    stream["settings"] = json!({"adapter":ADAPTER,"baud":500_000,
        "start_hz":1_001_120_000,"stop_hz":1_002_230_000});
    setup(
        &server,
        json!({"id":"window","name":"Requested RF window","streams":[stream]}),
    )
    .await;
    let (status, result) = worker(&server, "window", "rfe", "start").await;
    assert_eq!(status, 200, "{result}");
    wait_measurements(&server, "window", "rfe").await;
    let (_, sweeps) = server
        .request("GET", "/api/v1/sessions/window/sweeps?stream_id=rfe", None)
        .await;
    let sweep = &sweeps["data"][0];
    assert_eq!(sweep["frequencies_hz"][0], 1_001_120_000);
    assert_eq!(sweep["frequencies_hz"][111], 1_002_230_000);
    assert_eq!(
        sweep["settings"]["measurement_qualification"],
        json!({
            "status":"unverified","reason":"reference_not_verified","verified_through_hz":null
        })
    );
    server.stop().await;
}

#[tokio::test]
async fn segmented_survey_archives_each_raw_window_with_a_distinct_pass_identity() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::live(directory.path());
    let factory = Arc::new(DuplexFactory::default());
    config.devices.transport = Some(factory.clone());
    let server = Server::start(config).await;
    let mut stream = explorer("owned-survey");
    stream["settings"] = json!({"adapter":ADAPTER, "survey_step_hz":10_000,
        "start_hz":200_000_000, "stop_hz":202_230_000});
    setup(
        &server,
        json!({"id":"survey", "name":"survey", "streams":[stream]}),
    )
    .await;
    let (code, response) = worker(&server, "survey", "rfe", "start").await;
    assert_eq!(code, 200, "{response}");
    let mut rows = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let (_, archived) = server
                .request("GET", "/api/v1/sessions/survey/sweeps?stream_id=rfe", None)
                .await;
            let rows = archived["data"].as_array().unwrap();
            if rows.len() == 3 {
                break rows.clone();
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    rows.sort_by_key(|r| {
        let s = &r["settings"]["survey"];
        (s["pass"].as_u64().unwrap(), s["tile"].as_u64().unwrap())
    });
    let mut identities = Vec::new();
    for (row, (pass, tile, start)) in rows.iter().zip([
        (1, 1, 200_000_000),
        (1, 2, 201_120_000),
        (2, 1, 200_000_000),
    ]) {
        let s = &row["settings"]["survey"];
        assert_eq!(
            (s["pass"].as_u64(), s["tile"].as_u64(), s["tiles"].as_u64()),
            (Some(pass), Some(tile), Some(2))
        );
        assert_eq!(s["start_hz"], 200_000_000);
        assert_eq!(s["stop_hz"], 202_230_000);
        assert_eq!(s["step_hz"], 10_000);
        assert_eq!(row["frequencies_hz"].as_array().unwrap().len(), 112);
        assert_eq!(row["frequencies_hz"][0], start);
        assert_eq!(row["frequencies_hz"][111], start + 1_110_000);
        assert_eq!(row["settings"]["detector"], "max_hold");
        let id = s["pass_id"].as_str().unwrap();
        assert!(!id.is_empty());
        identities.push(id);
    }
    assert_eq!(identities[0], identities[1]);
    assert_ne!(identities[0], identities[2]);
    let base = "/api/v1/sessions/survey/streams/rfe/rf-explorer/passes";
    let (status, list) = server
        .request("GET", &format!("{base}?limit=1"), None)
        .await;
    assert_eq!(status, 200, "{list}");
    let latest = &list["data"]["items"][0];
    assert_eq!(latest["pass_id"], identities[2]);
    assert_eq!(latest["unique_points"], 112);
    assert_eq!(latest["complete"], false);
    let path = format!("{base}/{}", identities[0]);
    let (status, detail) = server
        .request("GET", &format!("{path}?columns=2"), None)
        .await;
    assert_eq!(status, 200, "{detail}");
    let data = &detail["data"];
    assert_eq!(data["summary"]["complete"], true);
    assert_eq!(data["summary"]["unique_points"], 224);
    assert_eq!(data["columns"][0]["sweep_id"], rows[0]["id"]);
    assert_eq!(data["columns"][1]["frequency_hz"], 201_120_000);
    assert_eq!(data["columns"][1]["point_index"], 0);
    assert_eq!(data["columns"][1]["level_dbm"], rows[1]["levels"][0]);
    assert!(data["hold_columns"].is_null());
    let marker = format!("{path}/point");
    let (status, result) = server
        .request("GET", &format!("{marker}?frequency_hz=201130000"), None)
        .await;
    assert_eq!(status, 200, "{result}");
    let current = &result["data"]["current"];
    assert_eq!(current["sweep_id"], rows[1]["id"]);
    assert_eq!(current["point_index"], 1);
    assert_eq!(current["frequency_hz"], 201_130_000);
    assert_eq!(current["level_dbm"], rows[1]["levels"][1]);
    assert_eq!(result["data"]["held"], Value::Null);
    let (status, gap) = server
        .request("GET", &format!("{marker}?frequency_hz=201130001"), None)
        .await;
    assert_eq!(status, 200, "{gap}");
    assert_eq!(gap["data"], json!({"current":null,"held":null}));
    for query in [
        "",
        "frequency_hz=1.0",
        "frequency_hz=-1",
        "frequency_hz=23&frequency_hz=24",
        "frequency_hz=9007199254740992",
        "frequency_hz=11&hold_since=wrong",
    ] {
        let (status, detail) = server
            .request("GET", &format!("{marker}?{query}"), None)
            .await;
        assert_eq!(status, 400, "{detail}");
    }
    let (_, windows) = server
        .request("GET", &format!("{path}/windows?limit=1"), None)
        .await;
    assert_eq!(windows["data"]["items"][0]["sweep_id"], rows[0]["id"]);
    assert_eq!(windows["data"]["next_after_tile"], 1);
    let (_, next) = server
        .request("GET", &format!("{path}/windows?after_tile=1"), None)
        .await;
    assert_eq!(next["data"]["items"][0]["sweep_id"], rows[1]["id"]);
    assert!(next["data"]["next_after_tile"].is_null());
    for bad in [
        "columns=0",
        "columns=2049",
        "columns=1&columns=2",
        "hold_since=bogus",
        "unexpected=1",
    ] {
        assert_eq!(
            server
                .request("GET", &format!("{path}?{bad}"), None)
                .await
                .0,
            400
        );
    }
    assert_eq!(
        server
            .request("GET", &format!("{base}/missing"), None)
            .await
            .0,
        404
    );

    assert_eq!(factory.opens.load(Ordering::Acquire), 1);
    server.stop().await;
}

#[tokio::test]
async fn independent_worker_archives_reported_max_hold_and_publishes_the_same_sweep() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::live(directory.path());
    let factory = Arc::new(DuplexFactory::default());
    config.devices.transport = Some(factory.clone());
    let server = Server::start(config).await;
    let mut session = fixture("station");
    session["streams"]
        .as_array_mut()
        .unwrap()
        .push(explorer("owned-fixture"));
    setup(&server, session).await;
    assert_eq!(factory.opens.load(Ordering::Acquire), 0);
    let mut events = reqwest::get(format!("{}/api/v1/events", server.url))
        .await
        .unwrap();
    assert_eq!(events.status(), 200);
    let (status, workers) = server
        .request(
            "POST",
            "/api/v1/sessions/station/workers/start",
            Some(&json!({})),
        )
        .await;
    assert_eq!(status, 200, "{workers}");
    assert!(
        workers["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["alive"] == true),
        "{workers}"
    );
    let status = wait_measurements(&server, "station", "rfe").await;
    assert_eq!(status["adapter"], ADAPTER);
    assert!(status["current"].get("generator_output").is_none());
    assert!(status["current"].get("command_preparation").is_none());
    let (code, refused) = server
        .request(
            "GET",
            "/api/v1/sessions/station/streams/rfe/instrument/capabilities",
            None,
        )
        .await;
    assert_eq!(code, 400, "{refused}");
    assert_eq!(refused["error"]["code"], "instrument_invalid");
    let (_, sweeps) = server
        .request(
            "GET",
            "/api/v1/sessions/station/sweeps?stream_id=rfe&limit=1",
            None,
        )
        .await;
    let sweep = &sweeps["data"][0];
    assert_eq!(sweep["stream_id"], "rfe");
    assert_eq!(sweep["session_id"], "station");
    assert_eq!(sweep["level_unit"], "dBm");
    assert_eq!(sweep["frequencies_hz"].as_array().unwrap().len(), 112);
    assert_eq!(sweep["frequencies_hz"][0], 100_000_000);
    assert_eq!(sweep["frequencies_hz"][1], 100_010_000);
    assert_eq!(sweep["frequencies_hz"][111], 101_110_000);
    assert_eq!(sweep["levels"][0], -65.0);
    assert_eq!(sweep["levels"][111], -120.5);
    let settings = &sweep["settings"];
    assert_eq!(settings["adapter"], ADAPTER);
    assert_eq!(settings["detector"], "max_hold");
    assert_eq!(settings["artifact_kind"], "processed_trace");
    assert_eq!(settings["device_identity"]["firmware"], "01.34");
    assert_eq!(settings["active_board"], "expansion");
    assert_eq!(settings["rbw_hz"], 15_000);
    assert_eq!(settings["level_encoding"], "dBm = offset_db - u8 / 2");
    let offset = settings["offset_db"].as_f64().unwrap();
    for (level, original_byte) in sweep["levels"].as_array().unwrap().iter().zip(130..242) {
        assert_eq!(
            (offset - level.as_f64().unwrap()) * 2.,
            f64::from(original_byte)
        );
    }
    assert_eq!(
        settings["measurement_qualification"]["status"],
        "unverified"
    );
    let began =
        chrono::DateTime::parse_from_rfc3339(sweep["interval"]["started_at"].as_str().unwrap())
            .unwrap();
    let ended =
        chrono::DateTime::parse_from_rfc3339(sweep["interval"]["ended_at"].as_str().unwrap())
            .unwrap();
    assert!(began <= ended);
    let received = tokio::time::timeout(Duration::from_secs(3), async {
        let mut bytes = Vec::new();
        loop {
            bytes.extend_from_slice(&events.chunk().await.unwrap().unwrap());
            while let Some(end) = bytes.windows(2).position(|w| w == b"\n\n") {
                let frame: Vec<_> = bytes.drain(..end + 2).collect();
                let data = std::str::from_utf8(&frame)
                    .unwrap()
                    .lines()
                    .find_map(|line| line.strip_prefix("data: "));
                if let Some(data) = data {
                    let envelope = parse_json_value(data.as_bytes(), 65536).unwrap();
                    let event = &envelope["data"];
                    if event["event_type"] == "sweep.received"
                        && event["payload"]["sweep"]["stream_id"] == "rfe"
                    {
                        return event["payload"]["sweep"].clone();
                    }
                }
            }
        }
    })
    .await
    .unwrap();
    drop(events);
    assert_eq!(received, *sweep);
    let (code, stop) = worker(&server, "station", "rfe", "stop").await;
    assert_eq!(code, 200, "{stop}");
    assert_eq!(stop["data"]["alive"], false);
    for other in ["analyzer", "receiver"] {
        assert_eq!(
            wait_measurements(&server, "station", other).await["alive"],
            true
        );
    }
    server.stop().await;
}

#[tokio::test]
async fn invalid_profiles_are_rejected_before_the_connector_runs() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::live(directory.path());
    let factory = Arc::new(DuplexFactory::default());
    config.devices.transport = Some(factory.clone());
    let server = Server::start(config).await;
    let mut invalid = Vec::new();
    for patch in [
        json!({"baud":115200}),
        json!({"baud":"500000"}),
        json!({"start_hz":100_000_000}),
        json!({"start_hz":100_000_001,"stop_hz":101_110_000}),
        json!({"start_hz":100_000_000,"stop_hz":99_000_000}),
        json!({"start_hz":2_000_000,"stop_hz":101_110_000}),
        json!({"stop_hz":null}),
        json!({"points":112}),
        json!({"rbw_hz":15000}),
        json!({"survey_step_hz":10_001}),
        json!({"survey_step_hz":"10000"}),
        json!({"survey_step_hz":null}),
        json!({"survey_step_hz":10_000,"start_hz":100_000_000,"stop_hz":102_231_000}),
        json!({"generator_output":"on"}),
        json!({"period_s":true}),
    ] {
        let mut stream = explorer("fixture");
        stream["settings"]
            .as_object_mut()
            .unwrap()
            .extend(patch.as_object().unwrap().clone());
        invalid.push(stream);
    }
    for (field, value) in [("role", "receiver"), ("level_unit", "dBuV"), ("port", "")] {
        let mut stream = explorer("fixture");
        stream[field] = json!(value);
        invalid.push(stream);
    }
    for (index, mut stream) in invalid.into_iter().enumerate() {
        let session = format!("invalid{index}");
        stream["id"] = json!(session);
        setup(
            &server,
            json!({"id":session,"name":"Rejected profile","streams":[stream]}),
        )
        .await;
        let (status, error) = worker(&server, &session, &session, "start").await;
        assert_eq!(status, 400, "{error}");
        assert_eq!(error["error"]["code"], "invalid_request", "{error}");
    }
    assert_eq!(factory.opens.load(Ordering::Acquire), 0);
    server.stop().await;
}

#[tokio::test]
async fn explorer_receiver_pair_has_supported_profile_and_requires_confirmed_path_and_baseline() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::live(directory.path());
    let factory = Arc::new(DuplexFactory::default());
    config.devices.transport = Some(factory.clone());
    let server = Server::start(config).await;
    let receiver = json!({"id":"ar","role":"receiver","level_unit":"s_meter_count",
        "device":"AR-DV1","port":"owned-ar","settings":{"adapter":"ardv1-serial"}});
    setup(&server, json!({"id":"project-source","name":"ordinary capture","streams":[explorer("owned-rfe"),receiver]})).await;
    let (code, project) = server
        .request(
            "POST",
            "/api/v1/monitoring-projects",
            Some(&json!({
                "name":"Draft","site":"Fixture","session_id":"project-source"
            })),
        )
        .await;
    assert_eq!(code, 201, "{project}");
    let project = project["data"]["id"].as_str().unwrap();
    let path = format!("/api/v1/monitoring-projects/{project}");
    let (_, preflight) = server
        .request("GET", &format!("{path}/preflight"), None)
        .await;
    assert_eq!(preflight["data"]["ready"], false, "{preflight}");
    assert!(
        preflight["data"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|check| check["name"] == "adapters" && check["ok"] == true)
    );
    for operation in ["preparation/start", "arm"] {
        let (code, error) = server
            .request("POST", &format!("{path}/{operation}"), Some(&json!({})))
            .await;
        assert_eq!(code, 400, "{error}");
    }
    assert_eq!(factory.opens.load(Ordering::Acquire), 0);
    server.stop().await;
}

#[tokio::test]
async fn failed_identity_and_stop_both_release_the_shared_port() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::live(directory.path());
    let factory = Arc::new(DuplexFactory::default());
    config.devices.transport = Some(factory.clone());
    let server = Server::start(config).await;
    for (id, port) in [
        ("wrong", "wrong-device"),
        ("one", "port-a"),
        ("two", "alias-a"),
    ] {
        let mut stream = explorer(port);
        stream["id"] = json!(id);
        setup(&server, json!({"name":id,"id":id,"streams":[stream]})).await;
    }
    assert_ne!(worker(&server, "wrong", "wrong", "start").await.0, 200);
    assert_eq!(factory.opens.load(Ordering::Acquire), 1);
    let (code, result) = worker(&server, "one", "one", "start").await;
    assert_eq!(code, 200, "{result}");
    assert_ne!(worker(&server, "two", "two", "start").await.0, 200);
    assert_eq!(factory.opens.load(Ordering::Acquire), 2);
    assert_eq!(worker(&server, "one", "one", "stop").await.0, 200);
    let (code, result) = worker(&server, "two", "two", "start").await;
    assert_eq!(code, 200, "{result}");
    wait_measurements(&server, "two", "two").await;
    assert_eq!(factory.opens.load(Ordering::Acquire), 3);
    server.stop().await;
}

#[tokio::test]
async fn named_frequency_sets_are_session_scoped_versioned_and_idempotent() {
    let directory = tempfile::tempdir().unwrap();
    let server = Server::start(CoreConfig::replay(directory.path())).await;
    setup(&server, fixture("marked")).await;
    setup(&server, json!({"id":"other","name":"other","streams":[]})).await;
    let path = "/api/v1/sessions/marked/frequency-bookmarks";
    let mut request =
        json!({"name":" эфир ","entries":[{"frequency_hz":100_010_000,"label":"M1"}]});
    let (status, first) = server.request("POST", path, Some(&request)).await;
    assert_eq!(status, 201, "{first}");
    assert_eq!(first["data"]["name"], "эфир");
    assert_eq!(first["data"]["revision"], 1);
    assert_eq!(server.request("POST", path, Some(&request)).await.1, first);
    request["entries"][0]["frequency_hz"] = json!(100_020_000);
    let (status, next) = server.request("POST", path, Some(&request)).await;
    assert_eq!(status, 201);
    assert_eq!(next["data"]["revision"], 2);
    request["entries"] = json!([request["entries"][0], request["entries"][0]]);
    assert_eq!(server.request("POST", path, Some(&request)).await.0, 400);
    assert_eq!(
        server.request("GET", path, None).await.1["data"]["items"],
        json!([next["data"], first["data"]])
    );
    let (_, other) = server
        .request("GET", "/api/v1/sessions/other/frequency-bookmarks", None)
        .await;
    assert_eq!(other["data"]["items"], json!([]));
    server.stop().await;
}
