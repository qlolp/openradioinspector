use super::*;
use ori_instruments::validate_rf_explorer_survey_bounds;
use serde_json::json;

const BROAD: &str =
    "#C2-F:0015000,1000000,-005,-119,0112,1,000,0015000,2700000,0600000,00030,-003,004\r\n";

async fn identify(peer: &mut DuplexStream, config: &str) {
    expect_bytes(peer, QUERY).await;
    peer.write_all(BANNER).await.unwrap();
    peer.write_all(MODEL).await.unwrap();
    peer.write_all(config.as_bytes()).await.unwrap();
}

fn survey_device(
    bounds: Option<(u64, u64)>,
    limits: IoLimits,
) -> (RfExplorer<DuplexStream>, DuplexStream) {
    let (io, peer) = duplex(8192);
    (RfExplorer::new_survey(io, bounds, limits).unwrap(), peer)
}

#[test]
fn bounds_accept_board_wide_surveys_and_require_112_real_points_on_the_requested_grid() {
    assert!(validate_rf_explorer_survey_bounds(None).is_ok());
    for bounds in [
        (15_000_000, 2_700_000_000),
        (240_000_000, 960_000_000),
        (100_001_000, 101_111_000),
    ] {
        assert!(
            validate_rf_explorer_survey_bounds(Some(bounds)).is_ok(),
            "{bounds:?}"
        );
    }
    for bounds in [
        (14_990_000, 16_110_000),
        (2_698_900_000, 2_700_010_000),
        (100_000_000, 101_100_000),
        (100_000_000, 101_111_000),
        (100_000_001, 101_110_001),
        (100_000_000, 99_000_000),
        (0, u64::MAX),
    ] {
        assert!(
            validate_rf_explorer_survey_bounds(Some(bounds)).is_err(),
            "{bounds:?}"
        );
    }
}

#[tokio::test(start_paused = true)]
async fn reads_one_verified_tile_per_poll_shifts_the_last_window_and_wraps_on_the_same_port() {
    let (mut device, mut peer) =
        survey_device(Some((100_001_000, 103_001_000)), IoLimits::default());
    let producer = tokio::spawn(async move {
        identify(&mut peer, BROAD).await;
        for (command, start, marker) in [
            (b"#\x20C2-F:0100001,0101111,-005,-119", "0100001", 0u8),
            (b"#\x20C2-F:0101121,0102231,-005,-119", "0101121", 112),
            (b"#\x20C2-F:0101891,0103001,-005,-119", "0101891", 224),
            (b"#\x20C2-F:0100001,0101111,-005,-119", "0100001", 12),
        ] {
            expect_bytes(&mut peer, command).await;
            peer.write_all(&sweep(&[254; 112])).await.unwrap(); // previous window
            let confirmed = format!(
                "#C2-F:{start},0010000,-005,-119,0112,1,000,0015000,2700000,0600000,00015,-003,004\r\n"
            );
            peer.write_all(confirmed.as_bytes()).await.unwrap();
            let counts: Vec<_> = (0..112).map(|i| marker.wrapping_add(i)).collect();
            peer.write_all(&sweep(&counts)).await.unwrap();
        }
        let mut extra = Vec::new();
        peer.read_to_end(&mut extra).await.unwrap();
        assert!(extra.is_empty(), "unexpected USB command {extra:?}");
    });

    device.prepare().await.unwrap();
    for (pass, tile, start, marker) in [
        (1, 1, 100_001_000, 0u8),
        (1, 2, 101_121_000, 112),
        (1, 3, 101_891_000, 224),
        (2, 1, 100_001_000, 12),
    ] {
        let before = chrono::Utc::now();
        let sample = device.poll().await.unwrap();
        assert!(before <= sample.interval.started_at);
        assert!(sample.interval.started_at <= sample.interval.ended_at);
        assert!(sample.interval.ended_at <= chrono::Utc::now());
        assert_eq!(sample.kind, SpectrumKind::ProcessedTrace);
        assert_eq!(sample.settings["detector"], "max_hold");
        assert_eq!(sample.settings["rbw_hz"], 15_000);
        assert_eq!(sample.settings["offset_db"], -3);
        assert_eq!(sample.settings["active_board"], "expansion");
        assert_eq!(sample.settings["step_hz"], 10_000);
        assert_eq!(sample.settings["points"], 112);
        assert_eq!(
            sample.settings["survey"],
            json!({"start_hz": 100_001_000, "stop_hz": 103_001_000, "step_hz": 10_000,
                   "pass": pass, "tile": tile, "tiles": 3})
        );
        assert_eq!(
            sample.frequencies_hz,
            (0..112).map(|i| start + 10_000 * i).collect::<Vec<_>>()
        );
        assert_eq!(sample.levels.len(), 112);
        for (i, dbm) in sample.levels.iter().enumerate() {
            assert_eq!(2.0 * (-3.0 - dbm), f64::from(marker.wrapping_add(i as u8)));
        }
    }
    device.close().await.unwrap();
    producer.await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn omitted_bounds_start_at_the_verified_main_input_minimum_and_cover_its_reported_maximum() {
    let (mut device, mut peer) = survey_device(None, IoLimits::default());
    let producer = tokio::spawn(async move {
        let main =
            "#C2-F:0300000,0400000,0000,-118,0112,0,000,0240000,0960000,0100000,00100,0000,000\r\n";
        identify(&mut peer, main).await;
        expect_bytes(&mut peer, b"#\x20C2-F:0240000,0241110,0000,-118").await;
        peer.write_all(&sweep(&[150; 112])).await.unwrap();
        peer.write_all(b"#C2-F:0240000,0010000,0000,-118,0112,0,000,0240000,0960000,0100000,00015,0000,000\r\n").await.unwrap();
        peer.write_all(&sweep(&[111; 112])).await.unwrap();
    });
    device.prepare().await.unwrap();
    let sample = device.poll().await.unwrap();
    assert_eq!(sample.kind, SpectrumKind::RawSpectrum);
    assert_eq!(sample.settings["active_input"], "left");
    assert_eq!(sample.settings["detector"], "normal");
    assert_eq!(sample.levels, vec![-55.5; 112]);
    assert_eq!(sample.frequencies_hz[0], 240_000_000);
    assert_eq!(sample.frequencies_hz[111], 241_110_000);
    assert_eq!(
        sample.settings["survey"],
        json!({"start_hz": 240_000_000, "stop_hz": 960_000_000, "step_hz": 10_000,
               "pass": 1, "tile": 1, "tiles": 643})
    );
    producer.await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn checks_the_whole_requested_range_112_points_and_window_span_before_any_tuning_write() {
    let main =
        "#C2-F:0300000,0010000,0000,-118,0112,0,000,0240000,0960000,0100000,00015,0000,004\r\n";
    for (bounds, config) in [
        (Some((300_000_000, 960_010_000)), main.to_string()),
        (
            Some((100_000_000, 103_000_000)),
            CONFIG.replace("0112,", "0075,"),
        ),
        // The initial configuration fits this board's maximum span; a 10 kHz tile cannot.
        (
            None,
            CONFIG
                .replace("0010000,", "0002000,")
                .replace("0600000,", "0001000,"),
        ),
        (None, CONFIG.replace("2700000,", "2699999,")),
    ] {
        let (mut device, mut peer) = survey_device(bounds, IoLimits::default());
        let producer = tokio::spawn(async move {
            identify(&mut peer, &config).await;
            let mut extra = Vec::new();
            peer.read_to_end(&mut extra).await.unwrap();
            extra
        });
        assert!(device.prepare().await.is_err(), "{bounds:?}");
        device.close().await.unwrap();
        assert!(producer.await.unwrap().is_empty());
    }
}

#[tokio::test(start_paused = true)]
async fn a_single_window_survey_uses_each_new_sweep_for_the_next_pass_without_retuning() {
    let (mut device, mut peer) =
        survey_device(Some((100_000_000, 101_110_000)), IoLimits::default());
    let producer = tokio::spawn(async move {
        readback(&mut peer).await;
        peer.write_all(&sweep(&[13; 112])).await.unwrap();
        peer.write_all(&sweep(&[57; 112])).await.unwrap();
        let mut extra = Vec::new();
        peer.read_to_end(&mut extra).await.unwrap();
        assert!(extra.is_empty());
    });
    device.prepare().await.unwrap();
    for (pass, dbm) in [(1, -6.5), (2, -28.5)] {
        let sample = device.poll().await.unwrap();
        assert_eq!(sample.settings["survey"]["pass"], pass);
        assert_eq!(sample.settings["survey"]["tile"], 1);
        assert_eq!(sample.settings["survey"]["tiles"], 1);
        assert_eq!(sample.levels, vec![dbm; 112]);
    }
    device.close().await.unwrap();
    producer.await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn retunes_and_source_sweeps_have_independent_deadlines_and_ignore_previous_window_data() {
    let limits = IoLimits {
        text_timeout: Duration::from_millis(100),
        scan_timeout: Duration::from_millis(100),
        ..IoLimits::default()
    };
    let (mut device, mut peer) = survey_device(Some((100_000_000, 102_230_000)), limits);
    let producer = tokio::spawn(async move {
        readback(&mut peer).await;
        peer.write_all(&sweep(&[11; 112])).await.unwrap();
        expect_bytes(&mut peer, TUNE).await;
        tokio::time::sleep(Duration::from_millis(70)).await;
        peer.write_all(&sweep(&[55; 112])).await.unwrap();
        peer.write_all(CONFIG.replace("0100000,", "0101120,").as_bytes())
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(70)).await;
        peer.write_all(&sweep(&[41; 112])).await.unwrap();
    });
    device.prepare().await.unwrap();
    assert_eq!(device.poll().await.unwrap().levels, vec![-5.5; 112]);
    let second = device.poll().await.unwrap();
    assert_eq!(second.levels, vec![-20.5; 112]);
    assert_eq!(second.settings["survey"]["tile"], 2);
    producer.await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn a_cancelled_or_unconfirmed_next_tile_taints_the_connection() {
    #[derive(Clone, Copy)]
    enum Reply {
        CancelPartial,
        PreviousWindow,
        Missing,
    }
    for reply in [Reply::CancelPartial, Reply::PreviousWindow, Reply::Missing] {
        let (mut device, mut peer) =
            survey_device(Some((100_000_000, 102_230_000)), IoLimits::default());
        let cancel = device.cancellation_handle();
        let producer = tokio::spawn(async move {
            readback(&mut peer).await;
            peer.write_all(&sweep(&[49; 112])).await.unwrap();
            expect_bytes(&mut peer, TUNE).await;
            peer.write_all(&sweep(&[55; 112])).await.unwrap();
            match reply {
                Reply::CancelPartial => {
                    peer.write_all(b"#C2-F:0101120,").await.unwrap();
                    cancel.cancel();
                }
                Reply::PreviousWindow => peer.write_all(CONFIG.as_bytes()).await.unwrap(),
                Reply::Missing => {}
            }
            let mut extra = Vec::new();
            peer.read_to_end(&mut extra).await.unwrap();
            assert!(extra.is_empty());
        });
        device.prepare().await.unwrap();
        assert_eq!(device.poll().await.unwrap().settings["survey"]["tile"], 1);
        let failed = device.poll().await.unwrap_err();
        assert!(
            match reply {
                Reply::CancelPartial => matches!(failed, InstrumentError::Cancelled),
                Reply::PreviousWindow => matches!(failed, InstrumentError::Rejected(_)),
                Reply::Missing => matches!(failed, InstrumentError::Timeout),
            },
            "{failed}"
        );
        assert!(matches!(device.poll().await, Err(InstrumentError::Tainted)));
        assert_eq!(device.status()["profile_verified"], false);
        device.close().await.unwrap();
        producer.await.unwrap();
    }
}

#[tokio::test(start_paused = true)]
async fn unsolicited_configuration_or_detector_drift_cannot_label_a_source_sweep() {
    for (change, points) in [
        (
            CONFIG
                .replace("0010000,", "0015000,")
                .replace("0112,", "0075,"),
            75,
        ), // same endpoints
        (CONFIG.replace("0010000,", "0040000,"), 112), // broad sweep
        (CONFIG.replace("00015,0000,", "00015,0007,"), 112), // changed offset
        ("#C+0\r\n".to_string(), 112),
    ] {
        let (mut device, mut peer) =
            survey_device(Some((100_000_000, 101_110_000)), IoLimits::default());
        let producer = tokio::spawn(async move {
            readback(&mut peer).await;
            peer.write_all(change.as_bytes()).await.unwrap();
            peer.write_all(&sweep(&vec![77; points])).await.unwrap();
        });
        device.prepare().await.unwrap();
        assert!(matches!(
            device.poll().await,
            Err(InstrumentError::UnsafeState(_))
        ));
        assert!(device.cancellation_handle().is_tainted());
        producer.await.unwrap();
    }
}
