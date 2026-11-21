//! All level bytes are synthetic. The geometry and identity are the Standard
//! 3G Combo's documented wire format for the 003/005 board pair on FW 01.34.
use ori_instruments::{
    ExecutionCheck, InstrumentError, IoLimits, RfExplorer, SpectrumKind,
    validate_rf_explorer_window,
};
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream, duplex};

const MODEL: &[u8] = b"#C2-M:003,005,01.34\r\n";
const CONFIG: &str =
    "#C2-F:0100000,0010000,0000,-118,0112,1,000,0015000,2700000,0600000,00015,0000,004\r\n";
const NEW_WINDOW: (u64, u64) = (101_120_000, 102_230_000);
const QUERY: &[u8] = b"#\x04C0";
const TUNE: &[u8] = b"#\x20C2-F:0101120,0102230,0000,-118";

fn sweep(counts: &[u8]) -> Vec<u8> {
    let mut bytes = vec![b'$', b'S', counts.len().try_into().unwrap()];
    bytes.extend_from_slice(counts);
    bytes.extend_from_slice(b"\r\n");
    bytes
}

async fn expect_bytes(peer: &mut DuplexStream, expected: &[u8]) {
    let mut bytes = vec![0; expected.len()];
    peer.read_exact(&mut bytes).await.unwrap();
    assert_eq!(bytes, expected);
}

fn setup(window: Option<(u64, u64)>) -> (RfExplorer<DuplexStream>, DuplexStream) {
    let (wire, peer) = duplex(8192);
    (
        RfExplorer::new(wire, window, IoLimits::default()).unwrap(),
        peer,
    )
}

async fn readback(peer: &mut DuplexStream) {
    expect_bytes(peer, QUERY).await;
    peer.write_all(MODEL).await.unwrap();
    peer.write_all(CONFIG.as_bytes()).await.unwrap();
}

#[tokio::test]
async fn fragmented_frames_follow_device_and_preserve_raw_counts_detector_and_timing() {
    // The tiny duplex capacity forces fragmentation even within the binary header.
    let (wire, mut peer) = duplex(2);
    let mut device = RfExplorer::new(wire, None, IoLimits::default()).unwrap();
    let mut counts = [149; 112];
    counts[..8].copy_from_slice(&[0, 1, 255, b'\r', b'\n', b'#', b'$', b'S']);
    let producer = tokio::spawn(async move {
        expect_bytes(&mut peer, QUERY).await;
        // Old stream data arrives before both pieces of evidence. Never relabel it.
        peer.write_all(&sweep(&[42; 112])).await.unwrap();
        peer.write_all(CONFIG.as_bytes()).await.unwrap();
        peer.write_all(&sweep(&[66; 112])).await.unwrap();
        peer.write_all(MODEL).await.unwrap();
        peer.write_all(&sweep(&counts)).await.unwrap();
        let mut extra = Vec::new();
        peer.read_to_end(&mut extra).await.unwrap();
        assert!(extra.is_empty(), "follow-device acquisition sent {extra:?}");
    });

    device.prepare().await.unwrap();
    let before = chrono::Utc::now();
    let sample = device.poll().await.unwrap();
    let after = chrono::Utc::now();
    assert!(before <= sample.interval.started_at);
    assert!(sample.interval.started_at <= sample.interval.ended_at);
    assert!(sample.interval.ended_at <= after);
    assert_eq!(sample.kind, SpectrumKind::ProcessedTrace);
    assert_eq!(sample.level_unit, "dBm");
    assert_eq!(&sample.levels[..4], &[0.0, -0.5, -127.5, -6.5]);
    assert_eq!(sample.levels[111], -74.5);
    assert_eq!(sample.frequencies_hz.len(), 112);
    assert_eq!(
        sample.frequencies_hz[..3],
        [100_000_000, 100_010_000, 100_020_000]
    );
    assert_eq!(sample.frequencies_hz[111], 101_110_000);
    let settings = &sample.settings;
    assert_eq!(settings["level_encoding"], "dBm = offset_db - u8 / 2");
    assert!(settings.get("wire_counts").is_none());
    assert_eq!(settings["device_identity"]["firmware"], "01.34");
    assert_eq!(settings["device_identity"]["main_model"], 3);
    assert_eq!(settings["device_identity"]["expansion_model"], 5);
    assert_eq!(settings["device_mode"], "spectrum_analyzer");
    assert_eq!(settings["active_board"], "expansion");
    assert_eq!(settings["active_input"], "right");
    assert_eq!(settings["board_min_hz"], 15_000_000);
    assert_eq!(settings["board_max_hz"], 2_700_000_000u64);
    assert_eq!(settings["board_max_span_hz"], 600_000_000);
    assert_eq!(settings["detector"], "max_hold");
    assert_eq!(settings["detector_code"], 4);
    assert_eq!(settings["rbw_hz"], 15_000);
    assert_eq!(device.status()["profile_verified"], true);
    device.close().await.unwrap();
    assert_eq!(device.status()["closed"], true);
    producer.await.unwrap();
}

#[tokio::test]
async fn every_original_byte_is_recoverable_with_stable_settings_across_sweeps() {
    let (mut device, mut peer) = setup(None);
    let producer = tokio::spawn(async move {
        expect_bytes(&mut peer, QUERY).await;
        peer.write_all(MODEL).await.unwrap();
        peer.write_all(CONFIG.replace("00015,0000", "00015,0007").as_bytes())
            .await
            .unwrap();
        for first in [0, 112, 224] {
            let counts: Vec<u8> = (first..first + 112).map(|i| (i % 256) as u8).collect();
            peer.write_all(&sweep(&counts)).await.unwrap();
        }
    });

    device.prepare().await.unwrap();
    let mut settings = None;
    for first in [0, 112, 224] {
        let sample = device.poll().await.unwrap();
        assert_eq!(sample.settings["offset_db"], 7);
        for (i, level) in sample.levels.iter().enumerate() {
            assert_eq!(2.0 * (7.0 - level), ((first + i) % 256) as f64);
        }
        assert!(
            !sample
                .settings
                .as_object()
                .unwrap()
                .values()
                .any(|value| value.is_array())
        );
        assert_eq!(
            settings.get_or_insert(sample.settings.clone()),
            &sample.settings
        );
    }
    producer.await.unwrap();
}

#[tokio::test]
async fn calibration_payloads_are_skipped_by_length_even_when_they_contain_messages() {
    let (mut device, mut peer) = setup(None);
    let producer = tokio::spawn(async move {
        readback(&mut peer).await;
        let fake = b"\r\n#C2-M:060,061,01.34\r\n$S";
        for header in [b"$q".as_slice(), b"$Q", b"$Cc", b"$Cd"] {
            peer.write_all(header).await.unwrap();
            peer.write_u8(fake.len() as u8).await.unwrap();
            if header == b"$Q" {
                peer.write_u8(0).await.unwrap();
            }
            peer.write_all(fake).await.unwrap();
            peer.write_all(b"\r\n").await.unwrap();
        }
        // $Cb uses byte 4 to encode 16-byte blocks and a 10-byte header.
        let mut block = b"$Cb\0\x01abcde".to_vec();
        let mut payload = [0; 32];
        payload[..fake.len()].copy_from_slice(fake);
        block.extend_from_slice(&payload);
        block.extend_from_slice(b"\r\n");
        peer.write_all(&block).await.unwrap();
        peer.write_all(&sweep(&[1; 112])).await.unwrap();
    });
    device.prepare().await.unwrap();
    assert_eq!(device.poll().await.unwrap().levels, vec![-0.5; 112]);
    producer.await.unwrap();
}

#[tokio::test]
async fn front_panel_change_uses_new_board_step_offset_and_detector_for_next_sweep() {
    let (mut device, mut peer) = setup(None);
    let producer = tokio::spawn(async move {
        readback(&mut peer).await;
        peer.write_all(&sweep(&[100; 112])).await.unwrap();
        peer.write_all(b"#C2-F:0300000,0012345,0000,-110,0112,0,000,0240000,0960000,0100000,00030,-003,000\r\n")
            .await.unwrap();
        peer.write_all(&sweep(&[101; 112])).await.unwrap();
    });
    device.prepare().await.unwrap();
    assert_eq!(device.poll().await.unwrap().frequencies_hz[0], 100_000_000);
    let sample = device.poll().await.unwrap();
    assert_eq!(sample.frequencies_hz[0], 300_000_000);
    assert_eq!(sample.frequencies_hz[1], 300_012_345);
    assert_eq!(sample.frequencies_hz[111], 301_370_295);
    assert_eq!(sample.kind, SpectrumKind::RawSpectrum);
    assert_eq!(sample.levels, vec![-53.5; 112]);
    assert_eq!(sample.settings["rbw_hz"], 30_000);
    assert_eq!(sample.settings["active_board"], "main");
    assert_eq!(sample.settings["active_input"], "left");
    assert_eq!(device.status()["detector"], "normal");
    producer.await.unwrap();
}

#[tokio::test]
async fn bad_readback_and_corrupt_sweeps_invalidate_previous_settings() {
    let mut broken_frame = sweep(&[70; 112]);
    *broken_frame.last_mut().unwrap() = b'!';
    for response in [
        b"#C2-M:003,006,01.34\r\n".to_vec(),
        b"#C2-M:003,005,01.33\r\n".to_vec(),
        b"#C3-M:060,061,01.34\r\n".to_vec(),
        CONFIG.replace(",1,000,", ",1,005,").into_bytes(), // tracking mode
        CONFIG.replace(",1,000,", ",0,000,").into_bytes(), // expansion limits on main board
        CONFIG.replace("0100000,", "2699999,").into_bytes(), // last bin outside board
        CONFIG.replace("0015000,", "0000000,").into_bytes(), // impossible board minimum
        CONFIG.replace("0600000,", "0000001,").into_bytes(), // sweep exceeds reported max span
        CONFIG.replace("0010000,", "0000000,").into_bytes(), // zero step
        CONFIG
            .replace("00015,0000,004", "00000,0000,004")
            .into_bytes(),
        CONFIG
            .replace("00015,0000,004", "00015,0000,255")
            .into_bytes(),
        CONFIG.replace("00015,0000,004", "00015,0000").into_bytes(),
        b"#C2-F:malformed\r\n".to_vec(),
        b"#C4-F:0000000\r\n".to_vec(),
        b"#C9-F:unsupported\r\n".to_vec(),
        b"#a1\r\n".to_vec(),
        CONFIG.replace("0010000,", "0000001,").into_bytes(), // below minimum Standard span
        b"(C) Ariel Rocholl RF Explorer\r\n".to_vec(),       // a reset invalidates cached readback
        sweep(&[100; 111]),
        broken_frame,
        b"$Sp\x01\x02".to_vec(), // truncated sweep preceding a complete frame
    ] {
        let (mut device, mut peer) = setup(None);
        let producer = tokio::spawn(async move {
            readback(&mut peer).await;
            peer.write_all(&response).await.unwrap();
            peer.write_all(&sweep(&[40; 112])).await.unwrap();
        });
        device.prepare().await.unwrap();
        assert!(device.poll().await.is_err());
        assert_eq!(device.status()["profile_verified"], false);
        assert_eq!(device.status()["restart_required"], true);
        assert!(matches!(device.poll().await, Err(InstrumentError::Tainted)));
        producer.await.unwrap();
    }
}

#[tokio::test]
async fn an_early_end_marker_prevents_publishing_a_sweep_spliced_with_another_frame() {
    let (mut device, mut peer) = setup(None);
    let producer = tokio::spawn(async move {
        readback(&mut peer).await;
        // A serial interruption after 104 values. The next record happens to
        // supply exactly enough bytes for a naive length-plus-CRLF check.
        peer.write_all(b"$Sp").await.unwrap();
        peer.write_all(&[41; 104]).await.unwrap();
        peer.write_all(b"\xff\xfe\xff\xfe\x00#OK\r\n")
            .await
            .unwrap();
    });
    device.prepare().await.unwrap();
    assert!(matches!(
        device.poll().await,
        Err(InstrumentError::Protocol(_))
    ));
    producer.await.unwrap();
}

#[tokio::test]
async fn partial_or_out_of_profile_counts_cannot_be_published_even_if_lengths_match() {
    for points in [0, 1, 2, 5, 15] {
        let (mut device, mut peer) = setup(None);
        let producer = tokio::spawn(async move {
            expect_bytes(&mut peer, QUERY).await;
            peer.write_all(MODEL).await.unwrap();
            let config = CONFIG
                .replace(",0112,", &format!(",{points:04},"))
                .replace("0010000,", "0200000,");
            peer.write_all(config.as_bytes()).await.unwrap();
            peer.write_all(&sweep(&vec![98; points])).await.unwrap();
        });
        assert!(device.prepare().await.is_err(), "admitted {points} points");
        producer.await.unwrap();
    }
}

#[tokio::test(start_paused = true)]
async fn missing_metadata_cannot_be_replaced_by_valid_sweeps() {
    for missing_model in [true, false] {
        let (mut device, mut peer) = setup(None);
        tokio::spawn(async move {
            expect_bytes(&mut peer, QUERY).await;
            peer.write_all(if missing_model {
                CONFIG.as_bytes()
            } else {
                MODEL
            })
            .await
            .unwrap();
            peer.write_all(&sweep(&[79; 112])).await.unwrap();
            std::future::pending::<()>().await;
        });
        assert!(matches!(
            device.prepare().await,
            Err(InstrumentError::Timeout)
        ));
        assert!(device.cancellation_handle().is_tainted());
    }
}

#[test]
fn requested_window_requires_khz_endpoints_and_profile_span() {
    assert!(validate_rf_explorer_window(None).is_ok());
    for window in [
        (15_000_000, 15_112_000),
        NEW_WINDOW,
        (2_100_000_000, 2_700_000_000),
    ] {
        assert!(validate_rf_explorer_window(Some(window)).is_ok());
    }
    for window in [
        (100_000_001, 101_110_000),
        (100_000_000, 101_110_001),
        (0, 101_110_000),
        (100_000_000, 100_000_000),
        (100_112_000, 100_000_000),
        (100_000_000, 100_111_000),
        (100_000_000, 700_001_000),
        (2_699_000_000, 2_700_001_000),
        (0, u64::MAX),
    ] {
        assert!(
            validate_rf_explorer_window(Some(window)).is_err(),
            "{window:?}"
        );
    }
}

#[tokio::test]
async fn requested_window_preserves_amplitude_limits_and_waits_for_the_matching_response() {
    let (mut device, mut peer) = setup(Some(NEW_WINDOW));
    let producer = tokio::spawn(async move {
        readback(&mut peer).await;
        expect_bytes(&mut peer, TUNE).await;
        peer.write_all(&sweep(&[254; 112])).await.unwrap(); // stale sweep from previous window
        let confirmation = CONFIG
            .replace("0100000,", "0101120,")
            .replace("00015,0000", "00030,0000");
        peer.write_all(confirmation.as_bytes()).await.unwrap();
        peer.write_all(&sweep(&[61; 112])).await.unwrap();
        peer.write_all(CONFIG.as_bytes()).await.unwrap(); // front panel moved away
        peer.write_all(&sweep(&[92; 112])).await.unwrap();
    });
    device.prepare().await.unwrap();
    let sample = device.poll().await.unwrap();
    assert_eq!(sample.frequencies_hz[0], NEW_WINDOW.0);
    assert_eq!(sample.frequencies_hz[111], NEW_WINDOW.1);
    assert_eq!(sample.settings["rbw_hz"], 30_000);
    assert_eq!(sample.levels[0], -30.5);
    assert!(device.poll().await.is_err());
    producer.await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn quantized_unconfirmed_or_changed_settings_do_not_complete_retune() {
    for confirmation in [
        None,
        Some(CONFIG.to_owned()), // old config repeated
        Some(
            CONFIG
                .replace("0100000,", "0101120,")
                .replace("0010000,", "0009999,"),
        ),
        Some(
            CONFIG
                .replace("0100000,", "0101120,")
                .replace("0000,-118", "0000,-117"),
        ),
        Some(
            CONFIG
                .replace("0100000,", "0101120,")
                .replace(",004\r", ",000\r"),
        ),
    ] {
        let (mut device, mut peer) = setup(Some(NEW_WINDOW));
        tokio::spawn(async move {
            readback(&mut peer).await;
            expect_bytes(&mut peer, TUNE).await;
            if let Some(config) = confirmation {
                peer.write_all(config.as_bytes()).await.unwrap();
            }
            peer.write_all(&sweep(&[91; 112])).await.unwrap();
            std::future::pending::<()>().await;
        });
        assert!(device.prepare().await.is_err());
        assert!(device.cancellation_handle().is_tainted());
    }
}

#[tokio::test]
async fn active_board_limits_reject_requested_window_before_a_tuning_command() {
    let (mut device, mut peer) = setup(Some((99_000_000, 100_110_000)));
    let producer = tokio::spawn(async move {
        expect_bytes(&mut peer, QUERY).await;
        peer.write_all(MODEL).await.unwrap();
        peer.write_all(b"#C2-F:0300000,0010000,0000,-118,0112,0,000,0240000,0960000,0100000,00015,0000,004\r\n").await.unwrap();
        let mut extra = Vec::new();
        peer.read_to_end(&mut extra).await.unwrap();
        assert!(extra.is_empty());
    });
    assert!(device.prepare().await.is_err());
    device.close().await.unwrap();
    producer.await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn cancelling_a_partial_frame_permanently_invalidates_the_stream() {
    let (mut device, mut peer) = setup(None);
    let handle = device.cancellation_handle();
    let producer = tokio::spawn(async move {
        readback(&mut peer).await;
        peer.write_all(b"$Sp\x01").await.unwrap();
        tokio::time::sleep(Duration::from_millis(5)).await;
        handle.cancel();
        std::future::pending::<()>().await;
    });
    device.prepare().await.unwrap();
    assert!(matches!(
        device.poll().await,
        Err(InstrumentError::Cancelled)
    ));
    assert!(matches!(device.poll().await, Err(InstrumentError::Tainted)));
    producer.abort();
}

#[tokio::test]
async fn revoked_execution_authority_prevents_the_second_startup_write() {
    let (mut device, mut peer) = setup(Some(NEW_WINDOW));
    let allowed = Arc::new(AtomicBool::new(true));
    let receipt = allowed.clone();
    let check: ExecutionCheck = Arc::new(move || {
        if receipt.load(Ordering::Acquire) {
            Ok(())
        } else {
            Err(InstrumentError::PolicyDenied)
        }
    });
    let producer = tokio::spawn(async move {
        expect_bytes(&mut peer, QUERY).await;
        allowed.store(false, Ordering::Release);
        peer.write_all(MODEL).await.unwrap();
        peer.write_all(CONFIG.as_bytes()).await.unwrap();
        let mut extra = Vec::new();
        peer.read_to_end(&mut extra).await.unwrap();
        assert!(extra.is_empty());
    });
    assert!(matches!(
        device.prepare_checked(check).await,
        Err(InstrumentError::PolicyDenied)
    ));
    device.close().await.unwrap();
    producer.await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn impossible_integer_step_window_sends_no_tuning_command() {
    let (mut device, mut peer) = setup(Some((100_000_000, 120_000_000)));
    let producer = tokio::spawn(async move {
        readback(&mut peer).await;
        let mut extra = Vec::new();
        peer.read_to_end(&mut extra).await.unwrap();
        extra
    });
    let error = device.prepare().await.unwrap_err();
    device.close().await.unwrap();
    assert!(producer.await.unwrap().is_empty());
    assert!(matches!(error, InstrumentError::InvalidRequest(_)));
}
