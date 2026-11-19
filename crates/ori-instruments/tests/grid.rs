use ori_instruments::*;
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream};

const EXACT: &str = "tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1";
fn config(start_hz: u64, points: u32, rbw_hz: u32) -> ReceiveConfig {
    ReceiveConfig {
        start_hz,
        stop_hz: start_hz + u64::from(points - 1) * 10_000,
        points,
        rbw_hz,
    }
}
fn device(
    firmware: &'static str,
    middle_token: Option<&'static str>,
    reject_ultra: bool,
    wide: bool,
) -> (TinySa<DuplexStream>, Arc<Mutex<Vec<String>>>) {
    device_with_help(firmware, middle_token, reject_ultra, wide, "")
}
fn device_with_help(
    firmware: &'static str,
    middle_token: Option<&'static str>,
    reject_ultra: bool,
    wide: bool,
    help: &'static str,
) -> (TinySa<DuplexStream>, Arc<Mutex<Vec<String>>>) {
    let (host, target) = tokio::io::duplex(16384);
    let commands = Arc::new(Mutex::new(Vec::new()));
    let sent = commands.clone();
    tokio::spawn(async move {
        let mut target = BufReader::new(target);
        loop {
            let mut bytes = Vec::new();
            if target.read_until(b'\r', &mut bytes).await.unwrap() == 0 {
                break;
            }
            let command = std::str::from_utf8(&bytes[..bytes.len() - 1]).unwrap();
            sent.lock().unwrap().push(command.to_string());
            let body = match command {
                "info" => "tinySA ULTRA ZS405\r\n".to_owned(),
                "version" => format!("{firmware}\r\n"),
                "deviceid" => "  synthetic  \r\n".to_owned(),
                "help" => format!("{help}\r\n"),
                "ultra on" if reject_ultra => "error\r\n".to_owned(),
                v if v.starts_with("scan ") => {
                    let args = v.split_whitespace().collect::<Vec<_>>();
                    let first = [
                        ("G", 1_000_000_000),
                        ("M", 1_000_000),
                        ("k", 1_000),
                        ("", 1),
                    ]
                    .into_iter()
                    .find_map(|(suffix, scale)| {
                        args[1]
                            .strip_suffix(suffix)?
                            .parse::<u64>()
                            .ok()
                            .map(|n| n * scale)
                    })
                    .unwrap();
                    (0..args[3].parse::<u64>().unwrap())
                        .map(|i| {
                            let expected = (first + i * 10_000).to_string();
                            let token = if i == 1 {
                                middle_token.unwrap_or(&expected)
                            } else {
                                &expected
                            };
                            format!("{token} -80.25\r\n")
                        })
                        .collect::<String>()
                }
                _ => String::new(),
            };
            if target
                .get_mut()
                .write_all(format!("{command}\r\n{body}ch> ").as_bytes())
                .await
                .is_err()
            {
                break;
            }
        }
    });
    (
        TinySa::new_for_grid(host, config(100_000, 3, 30_000), IoLimits::default(), wide).unwrap(),
        commands,
    )
}

#[tokio::test]
async fn extended_range_requires_exact_revision_before_any_configuration() {
    for fw in [
        FIRMWARE,
        "tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.2",
        "tinySA4_v1.4-234-gea71dfd\r\nHW Version:V0.4.5.1",
    ] {
        let (mut sa, sent) = device(fw, None, false, true);
        assert!(sa.prepare().await.is_err());
        assert!(sa.state().restart_required);
        assert_eq!(&*sent.lock().unwrap(), &["info", "version", "deviceid"]);
    }
    let (mut sa, sent) = device(FIRMWARE, None, false, false);
    sa.prepare().await.unwrap();
    assert!(sa.poll_grid(config(900_000_000, 2, 30_000)).await.is_err());
    assert!(
        !sent
            .lock()
            .unwrap()
            .iter()
            .any(|s| s == "ultra on" || s.starts_with("scan "))
    );
}

#[tokio::test]
async fn exact_unit_switches_only_on_path_or_bandwidth_change_and_archives_returned_grid() {
    let (mut sa, sent) = device(EXACT, None, false, true);
    sa.prepare().await.unwrap();
    for (first, rbw) in [
        (799_990_000, 30_000),
        (900_000_000, 30_000),
        (1_299_990_000, 10_000),
        (100_000, 10_000),
    ] {
        let s = sa.poll_grid(config(first, 2, rbw)).await.unwrap();
        assert_eq!(s.frequencies_hz, [first, first + 10000]);
        assert!(s.interval.started_at <= s.interval.ended_at);
        assert_eq!(s.settings["rbw_hz"], rbw);
        let evidence = serde_json::json!({
            "device_id_sha256":"b3cc0475bb78a5026098858e9889acf666d31062d513d303314eca31d36e72f2","source":"command_acknowledged","device_mode":"input",
            "lna":false,"attenuation_db":"auto","calculation":"off","ultra":first + 10_000 > 800_000_000,
            "rbw_hz":rbw
        });
        assert_eq!(s.settings["scan_rx_evidence"], evidence);
        let public = legacy_monitoring_settings(&s, &sa.state(), None).unwrap();
        assert_eq!(public["scan_rx_evidence"], evidence);
        assert!(public["device_identity"].get("deviceid").is_none());
        let mut changed = s;
        changed.settings["scan_rx_evidence"]["lna"] = serde_json::json!(true);
        assert!(legacy_monitoring_settings(&changed, &sa.state(), None).is_err());
    }
    let commands = sent.lock().unwrap();
    assert_eq!(
        commands.iter().filter(|s| s.as_str() == "ultra on").count(),
        1
    );
    assert_eq!(
        commands
            .iter()
            .filter(|s| s.as_str() == "ultra off")
            .count(),
        2
    );
    assert_eq!(commands.iter().filter(|s| s.starts_with("rbw ")).count(), 2);
    assert_eq!(
        commands
            .iter()
            .filter(|s| s.as_str() == "output off")
            .count(),
        1
    );
    assert_eq!(
        commands
            .iter()
            .filter(|s| s.as_str() == "mode input")
            .count(),
        1
    );
    assert!(
        !commands
            .iter()
            .any(|s| s == "mode output" || s == "output on" || s.starts_with("freq "))
    );
}

#[tokio::test]
async fn interior_grid_mismatch_or_unacknowledged_extended_input_blocks_further_scans() {
    for (middle, reject) in [(Some("900010001"), false), (None, true)] {
        let (mut sa, sent) = device(EXACT, middle, reject, true);
        sa.prepare().await.unwrap();
        assert!(sa.poll_grid(config(900_000_000, 3, 30_000)).await.is_err());
        assert!(sa.state().restart_required);
        let count = sent.lock().unwrap().len();
        assert!(sa.poll_grid(config(100_000, 2, 10_000)).await.is_err());
        assert_eq!(sent.lock().unwrap().len(), count);
        if reject {
            assert!(!sent.lock().unwrap().iter().any(|s| s.starts_with("scan ")));
        }
    }
}

#[tokio::test]
async fn grid_resume_requires_supported_rx_commands_and_reacknowledges_path_and_rbw() {
    for (help, complete) in [
        ("pause output mode lna attenuate calc ultra rbw", true),
        ("pause output mode lna calc ultra rbw", false),
    ] {
        let (mut analyzer, sent) = device_with_help(EXACT, None, false, true, help);
        analyzer.prepare().await.unwrap();
        analyzer
            .poll_grid(config(900_000_000, 2, 30_000))
            .await
            .unwrap();
        analyzer.execute(TinySaControl::EnterLab).await.unwrap();
        let before = sent.lock().unwrap().len();
        let resume = analyzer.execute(TinySaControl::ResumeMonitoring).await;
        assert_eq!(resume.is_ok(), complete);
        let poll = analyzer.poll_grid(config(900_000_000, 2, 30_000)).await;
        assert_eq!(poll.is_ok(), complete);
        let sent = sent.lock().unwrap();
        if complete {
            for expected in [
                "lna off",
                "attenuate auto",
                "calc off",
                "ultra on",
                "rbw 30",
            ] {
                assert!(sent[before..].iter().any(|cmd| cmd == expected));
            }
        } else {
            assert!(!sent[before..].iter().any(|cmd| cmd.starts_with("scan ")));
        }
    }
}

#[tokio::test]
async fn grid_wire_tokens_must_represent_exact_integer_hz_even_with_unit_suffixes() {
    for (token, valid) in [
        ("900010.0004k", false),
        ("0.900010000000000001G", false),
        ("900010000.01", false),
        ("900.010000000M", true),
        ("0.90001G", true),
        ("900010k", true),
    ] {
        let (mut sa, _) = device(EXACT, Some(token), false, true);
        sa.prepare().await.unwrap();
        let result = sa.poll_grid(config(900_000_000, 3, 30_000)).await;
        assert_eq!(result.is_ok(), valid, "{token}");
        assert_eq!(sa.state().restart_required, !valid, "{token}");
        if let Ok(sample) = result {
            assert_eq!(
                sample.frequencies_hz,
                [900_000_000, 900_010_000, 900_020_000]
            );
        }
    }
}
