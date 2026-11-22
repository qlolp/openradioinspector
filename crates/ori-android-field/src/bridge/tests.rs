//! Scripted instrument peers on the same connected-socket boundary Kotlin supplies.
use super::Session;
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Read, Write},
    os::{fd::AsRawFd, unix::net::UnixStream},
    thread,
    time::Duration,
};

fn session(plan: Value) -> (Session, UnixStream) {
    let (socket, peer) = UnixStream::pair().unwrap();
    peer.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
    (
        Session::open(socket.as_raw_fd(), &plan.to_string()).unwrap(),
        peer,
    )
}
fn next(session: &mut Session) -> Value {
    serde_json::from_str(&session.next().unwrap().unwrap()).unwrap()
}
fn command(reader: &mut BufReader<UnixStream>) -> String {
    let mut bytes = Vec::new();
    reader.read_until(b'\r', &mut bytes).unwrap();
    assert_eq!(bytes.pop(), Some(b'\r'));
    String::from_utf8(bytes).unwrap()
}

#[test]
fn validates_entire_plan_before_sending_bytes() {
    for plan in [
        json!({"kind":"ardv1_list","frequencies_hz":[100000000,900000001]}),
        json!({"kind":"ardv1_range","start_hz":90000000,"stop_hz":100000000,"step_hz":25000,"mode":"AM"}),
        json!({"kind":"rf_explorer_sweep","start_hz":90000000}),
        json!({"kind":"rf_explorer_survey","step_hz":1000}),
        json!({"kind":"tinysa_rx","start_hz":100000,"stop_hz":800000001,"points":2,"rbw_hz":10000}),
        json!({"kind":"tinysa_rx","generator":true}),
        json!({"kind":"raw_command","command":"RE1"}),
    ] {
        let (socket, mut peer) = UnixStream::pair().unwrap();
        assert!(Session::open(socket.as_raw_fd(), &plan.to_string()).is_err());
        drop(socket);
        assert_eq!(peer.read(&mut [0; 16]).unwrap(), 0);
    }
}

#[test]
fn unsupported_receiver_never_receives_result_mode_or_tune_commands() {
    let (mut session, peer) = session(json!({"kind":"ardv1_single","frequency_hz":100000000}));
    let worker = thread::spawn(move || {
        let mut io = BufReader::new(peer);
        assert_eq!(command(&mut io), "VR");
        io.get_mut().write_all(b"20VR2504A\r\n").unwrap();
        assert_eq!(command(&mut io), "RN");
        io.get_mut().write_all(b"20RN00000000\r\n").unwrap();
        assert_eq!(io.read(&mut [0; 16]).unwrap(), 0);
    });
    assert!(session.next().is_err());
    assert!(session.next().is_err());
    session.close().unwrap();
    worker.join().unwrap();
}

#[test]
fn unplug_during_identity_ends_capture_without_waiting_for_scan_timeout() {
    let (mut session, mut peer) = session(json!({"kind":"rf_explorer_sweep"}));
    let worker = thread::spawn(move || {
        let mut query = [0; 4];
        peer.read_exact(&mut query).unwrap();
        assert_eq!(&query, b"#\x04C0");
    });
    assert!(session.next().is_err());
    session.close().unwrap();
    worker.join().unwrap();
}

#[test]
fn rf_sweep_and_survey_use_readback_and_preserve_each_source_window() {
    for kind in ["rf_explorer_sweep", "rf_explorer_survey"] {
        let (mut session, mut peer) =
            session(json!({"kind":kind,"start_hz":100000000,"stop_hz":101110000}));
        let worker = thread::spawn(move || {
            let mut query = [0; 4];
            peer.read_exact(&mut query).unwrap();
            assert_eq!(&query, b"#\x04C0");
            let mut bytes=b"RF Explorer 01.34 27-Apr-22 01.04.05\r\n#C2-M:003,005,01.34\r\n#C2-F:0100000,0010000,0000,-118,0112,1,000,0015000,2700000,0600000,00015,0000,004\r\n$Sp".to_vec();
            bytes.extend_from_slice(&[149; 112]);
            bytes.extend_from_slice(b"\r\n");
            peer.write_all(&bytes).unwrap();
            assert_eq!(peer.read(&mut [0; 32]).unwrap(), 0);
        });
        let sample = next(&mut session);
        assert_eq!(sample["frequencies_hz"][111], 101110000);
        assert_eq!(sample["levels"][111], -74.5);
        assert_eq!(
            sample["settings"]["survey"]["tile"],
            if kind == "rf_explorer_survey" {
                json!(1)
            } else {
                Value::Null
            }
        );
        session.close().unwrap();
        worker.join().unwrap();
    }
}

#[test]
fn verified_tinysa_uses_only_the_monitoring_command_sequence() {
    let (mut session, peer) = session(
        json!({"kind":"tinysa_rx","start_hz":100000000,"stop_hz":101000000,"points":3,"rbw_hz":10000}),
    );
    let worker = thread::spawn(move || {
        let mut io = BufReader::new(peer);
        for (request, body) in [
            ("info", "tinySA Ultra ZS405\r\n"),
            ("version", "tinySA4_v1.4-235-gea71dfd\r\n"),
            ("deviceid", "synthetic\r\n"),
            ("output off", ""),
            ("mode input", ""),
            ("lna off", ""),
            ("attenuate auto", ""),
            ("rbw 10", ""),
            ("calc off", ""),
            (
                "scan 100M 101M 3 3",
                "100000000 -90 0\r\n100500000 -42.5 0\r\n101000000 -80 0\r\n",
            ),
        ] {
            assert_eq!(command(&mut io), request);
            write!(io.get_mut(), "{request}\r\n{body}ch> ").unwrap();
        }
        assert_eq!(io.read(&mut [0; 32]).unwrap(), 0);
    });
    let sample = next(&mut session);
    assert_eq!(sample["levels"], json!([-90.0, -42.5, -80.0]));
    assert_eq!(
        sample["interval"]["started_at"]
            .as_str()
            .unwrap()
            .chars()
            .last(),
        Some('Z')
    );
    session.close().unwrap();
    worker.join().unwrap();
}

fn receiver_peer(peer: UnixStream) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut io = BufReader::new(peer);
        for (request, reply) in [
            ("VR", "VR2504A"),
            ("RN", "RN09520001"),
            ("RE1", "20"),
            ("VR", "20VR2504A"),
            ("RN", "20RN09520001"),
        ] {
            assert_eq!(command(&mut io), request);
            writeln!(io.get_mut(), "{reply}\r").unwrap();
        }
        let mut frequency = String::new();
        loop {
            let request = command(&mut io);
            let reply = match request.as_str() {
                "VFA" | "MD0F0" | "EX" => "20".to_string(),
                "RX" => format!("20RX VFA {frequency} ST025.00 MD0F0 LM1231"),
                rf if rf.starts_with("RF") => {
                    frequency = rf.to_owned();
                    "20".to_owned()
                }
                _ => panic!("unexpected command {request}"),
            };
            writeln!(io.get_mut(), "{reply}\r").unwrap();
            if request == "EX" {
                break;
            }
        }
    })
}

#[test]
fn receiver_list_repeats_in_order_and_preserves_s_meter_and_readback() {
    let hz = [100000000, 100500000, 100100000];
    let (mut session, peer) =
        session(json!({"kind":"ardv1_list","frequencies_hz":hz,"settle_ms":0,"continuous":true}));
    let worker = receiver_peer(peer);
    for i in 0..6 {
        let sample = next(&mut session);
        assert_eq!(sample["frequency_hz"], hz[i % 3]);
        assert_eq!(sample["metadata"]["scan_pass"], i / 3 + 1);
        assert_eq!(sample["metadata"]["scan_index"], i % 3);
        assert_eq!(sample["metadata"]["frequency_readback"], true);
        assert_eq!(sample["level_unit"], "s_meter_count");
        assert_eq!(sample["level"], 123);
    }
    session.close().unwrap();
    worker.join().unwrap();
}

#[test]
fn receiver_range_crosses_a_chunk_boundary_with_no_missing_or_duplicate_observations() {
    let start = 100000000;
    let step = 1000;
    let stop = start + 1000 * step - 10; // Final partial interval is exactly the requested endpoint.
    let (mut session, peer) = session(
        json!({"kind":"ardv1_range","start_hz":start,"stop_hz":stop,"step_hz":step,"settle_ms":0}),
    );
    let worker = receiver_peer(peer);
    for i in 0..=1000 {
        let sample = next(&mut session);
        assert_eq!(sample["frequency_hz"], (start + i * step).min(stop));
        assert_eq!(sample["metadata"]["scan_index"], i);
        assert_eq!(sample["metadata"]["scan_total"], 1001);
        assert_eq!(sample["metadata"]["scan_chunk_index"], i32::from(i >= 999));
        assert_eq!(sample["metadata"]["scan_chunk_total"], 2);
    }
    assert!(session.next().unwrap().is_none());
    session.close().unwrap();
    worker.join().unwrap();
}

#[test]
fn complete_synthetic_field_survey_passes_cover_both_requested_grids() {
    for (start, stop, tiles, overlap) in [
        (100_000_000u64, 400_000_000u64, 268u64, 15),
        (15_000_000, 2_700_000_000, 2398, 75),
    ] {
        let (mut session, mut peer) = session(
            json!({"kind":"rf_explorer_survey","start_hz":start,"stop_hz":stop,"step_hz":10000}),
        );
        let worker = thread::spawn(move || {
            let mut query = [0; 4];
            peer.read_exact(&mut query).unwrap();
            assert_eq!(&query, b"#\x04C0");
            peer.write_all(b"RF Explorer 01.34 27-Apr-22 01.04.05\r\n#C2-M:003,005,01.34\r\n#C2-F:0015000,1000000,-005,-119,0112,1,000,0015000,2700000,0600000,00030,-003,004\r\n").unwrap();
            for tile in 0..tiles {
                let hz = (start + tile * 1_120_000).min(stop - 1_110_000);
                let mut command = [0; 32];
                peer.read_exact(&mut command).unwrap();
                assert_eq!(
                    command.as_slice(),
                    format!(
                        "#\x20C2-F:{:07},{:07},-005,-119",
                        hz / 1000,
                        (hz + 1_110_000) / 1000
                    )
                    .as_bytes()
                );
                let mut bytes=format!("#C2-F:{:07},0010000,-005,-119,0112,1,000,0015000,2700000,0600000,00015,-003,004\r\n$Sp",hz/1000).into_bytes();
                bytes.extend_from_slice(&[144; 112]);
                bytes.extend_from_slice(b"\r\n");
                peer.write_all(&bytes).unwrap();
            }
        });
        let mut seen = vec![false; ((stop - start) / 10_000 + 1) as usize];
        let mut duplicates = 0;
        for tile in 1..=tiles {
            let sample = next(&mut session);
            let survey = &sample["settings"]["survey"];
            assert_eq!(survey["pass"], 1);
            assert_eq!(survey["tile"], tile);
            assert_eq!(survey["tiles"], tiles);
            let frequencies = sample["frequencies_hz"].as_array().unwrap();
            assert_eq!(frequencies.len(), 112);
            for frequency in frequencies {
                let offset = frequency.as_u64().unwrap() - start;
                assert!(offset.is_multiple_of(10_000));
                duplicates += u64::from(std::mem::replace(
                    &mut seen[(offset / 10_000) as usize],
                    true,
                ));
            }
        }
        assert!(seen.into_iter().all(|covered| covered));
        assert_eq!(duplicates, overlap);
        session.close().unwrap();
        worker.join().unwrap();
    }
}
