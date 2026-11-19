//! Synthetic byte peer through the native worker, committed archive, status,
//! and stop paths. This does not open a USB port.
use crate::{Core, CoreConfig, devices::*};
use ori_contracts::DeviceStream;
use serde_json::json;
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

struct Peer {
    commands: Arc<Mutex<Vec<String>>>,
}
impl TransportFactory for Peer {
    fn identity(&self, _: &DeviceStream) -> DeviceResult<String> {
        Ok("grid-test-peer".into())
    }
    fn open<'a>(&'a self, _: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        let commands = self.commands.clone();
        Box::pin(async move {
            let (host, peer) = tokio::io::duplex(32768);
            tokio::spawn(async move {
                let mut peer = BufReader::new(peer);
                loop {
                    let mut bytes = vec![];
                    if peer.read_until(b'\r', &mut bytes).await.unwrap() == 0 {
                        break;
                    }
                    let cmd = std::str::from_utf8(&bytes[..bytes.len() - 1]).unwrap();
                    commands.lock().unwrap().push(cmd.to_owned());
                    let body = match cmd {
                        "info" => "tinySA Ultra ZS405\r\n".into(),
                        "version" => "tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n".into(),
                        "deviceid" => "synthetic\r\n".into(),
                        "scan 1297100k 1299980k 289 3" => (0..289)
                            .map(|i| format!("{} -99.5\r\n", 1_297_100_000 + i * 10000))
                            .collect::<String>(),
                        "scan 1299990k 1300M 2 3" => {
                            "1299990000 -81.75\r\n1300000000 -75.5\r\n".into()
                        }
                        "output off" | "mode input" | "lna off" | "attenuate auto" | "rbw 30"
                        | "calc off" | "ultra on" => String::new(),
                        _ => "rejected\r\n".into(),
                    };
                    if peer
                        .get_mut()
                        .write_all(format!("{cmd}\r\n{body}ch> ").as_bytes())
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            });
            Ok(Box::new(host) as BoxTransport)
        })
    }
}

#[tokio::test]
async fn high_grid_native_worker_commits_every_window_with_time_identity_and_one_setup() {
    let dir = tempfile::tempdir().unwrap();
    let commands = Arc::new(Mutex::new(Vec::new()));
    let mut config = CoreConfig::replay(dir.path());
    config.devices.transport = Some(Arc::new(Peer {
        commands: commands.clone(),
    }));
    let core = Core::open(config).await.unwrap();
    let p = json!({"id":"grid","name":"Survey","state":"running","contract_version":1,
        "created_at":"2026-01-01T00:00:00Z","started_at":"2026-01-01T00:00:00Z","ended_at":null,"metadata":{},"streams":[{"id":"a","session_id":"grid","device":"ZS405",
        "role":"spectrum_analyzer","state":"disconnected","level_unit":"dBm","port":"synthetic",
        "settings":{"adapter":"tinysa-serial","baud":115200,"period_s":0.01,"scan_plan":{"id":"received-plan","revision":7,
        "config":{"name":"Grid","bands":[{"id":"upper","name":"Upper","start_hz":1297100000,"stop_hz":1300000000,
        "points":291,"step_hz":10000,"rbw_hz":30000,"max_revisit_seconds":10.0}]}}}}]});
    let archive = core.archive().unwrap();
    archive.create_session(p).await.unwrap();
    let devices = core.devices.as_ref().unwrap();
    devices
        .start_stream("grid", "a", devices.public_gate(Target::new("grid", "a")))
        .await
        .unwrap();
    let current = tokio::time::timeout(Duration::from_secs(8), async {
        loop {
            let status = devices.list(Some("grid")).await.unwrap().remove(0);
            assert_ne!(status.state, "error", "{:?}", status.last_error);
            if status.measurements >= 4 {
                break status.current;
            }
            tokio::time::sleep(Duration::from_millis(3)).await;
        }
    })
    .await
    .unwrap();
    devices.stop_stream("grid", "a").await.unwrap();
    let mut sweeps = archive.list_sweeps("grid", Some("a"), 32).await.unwrap();
    sweeps.sort_by_key(|s| s["settings"]["sequence"].as_u64().unwrap());
    let sequence = &sweeps[..4];
    for (i, s) in sequence.iter().enumerate() {
        assert_eq!(s["settings"]["scan_window_index"], i % 2);
        assert_eq!(s["settings"]["scan_serial_baud"], 115200);
        assert_eq!(
            s["settings"]["scan_rx_evidence"],
            json!({"device_id_sha256":"b3cc0475bb78a5026098858e9889acf666d31062d513d303314eca31d36e72f2",
            "source":"command_acknowledged","device_mode":"input","ultra":true,"lna":false,
            "attenuation_db":"auto","calculation":"off","rbw_hz":30000})
        );
        assert!(s["settings"]["device_identity"].get("deviceid").is_none());
        assert_eq!(s["settings"]["scan_band_id"], "upper");
        assert_eq!(s["settings"]["scan_plan_revision"], 7);
        assert_eq!(s["settings"]["scan_plan_id"], "received-plan");
        assert_eq!(s["settings"]["simulated"], false); // synthetic test transport on the real adapter
        assert!(
            s["interval"]["started_at"].as_str().unwrap()
                <= s["interval"]["ended_at"].as_str().unwrap()
        );
    }
    let expected = (0..291)
        .map(|i| 1_297_100_000 + i * 10_000)
        .collect::<Vec<u64>>();
    let freqs = sequence[..2]
        .iter()
        .flat_map(|s| {
            s["frequencies_hz"]
                .as_array()
                .unwrap()
                .iter()
                .map(|n| n.as_u64().unwrap())
        })
        .collect::<Vec<_>>();
    assert_eq!(freqs, expected);
    let coverage = &current["scan_coverage"]["bands"][0];
    assert_eq!(coverage["state"], "fresh");
    assert_eq!(coverage["observed_windows"], 2);
    assert!(coverage["completed_passes"].as_u64().unwrap() >= 2);
    let last = &coverage["last_window"];
    let raw = archive
        .get_sweep(last["measurement_id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(last["ended_at"], raw["interval"]["ended_at"]);
    assert_eq!(last["started_at"], raw["interval"]["started_at"]);
    assert_eq!(last["index"], raw["settings"]["scan_window_index"]);
    assert!(
        current["scan_timing"]["between_polls_seconds"]
            .as_f64()
            .unwrap()
            >= 0.
    );
    assert!(current["scan_timing"]["commit_seconds"].as_f64().unwrap() >= 0.);
    assert_eq!(
        commands
            .lock()
            .unwrap()
            .iter()
            .filter(|s| s.as_str() == "mode input")
            .count(),
        1
    );
    core.close().await.unwrap();
}

#[tokio::test]
async fn guard_collects_and_rearms_simulated_workers_on_independent_physical_window_fingerprints() {
    let dir = tempfile::tempdir().unwrap();
    let core = Core::open(CoreConfig::replay(dir.path())).await.unwrap();
    let archive = core.archive().unwrap();
    archive.create_session(json!({"id":"grid-project-template","name":"Grid template","state":"running","contract_version":1,
        "created_at":"2026-01-01T00:00:00Z","started_at":"2026-01-01T00:00:00Z","metadata":{},
        "streams":[
        {"id":"a","session_id":"grid-project-template","device":"Synthetic analyzer","role":"spectrum_analyzer","state":"disconnected",
        "level_unit":"dBm","port":"sim://analyzer","settings":{"adapter":"tinysa-sim","period_s":0.02,"noise_sigma_db":0}},
        {"id":"r","session_id":"grid-project-template","device":"Synthetic receiver","role":"receiver","state":"disconnected",
        "level_unit":"s_meter_count","port":"sim://receiver","settings":{"adapter":"ardv1-sim","period_s":0.02}}]})).await.unwrap();
    let guard = &core.domains.get().unwrap().guard;
    let project = guard
        .create(
            json!({"name":"Wide survey","site":"synthetic","session_id":"grid-project-template"}),
        )
        .await
        .unwrap();
    let id = project["id"].as_str().unwrap();
    guard.configure(id,json!({"scan_plan":{"name":"Grid","bands":[{"id":"rx","name":"Upper","start_hz":1297100000,
        "stop_hz":1300000000,"points":291,"step_hz":10000,"rbw_hz":30000,"max_revisit_seconds":30}]}})).await.unwrap();
    guard.save_preparation(id,json!({"antenna_notes":"synthetic","receive_path_confirmed":true,"location_confirmed":true})).await.unwrap();
    let report = guard.preflight(id, true).await.unwrap();
    assert_eq!(report["ready"], true, "{}", report["checks"]);
    let prep = guard.start_preparation(id, json!({})).await.unwrap()["project"].clone();
    let original = tokio::time::timeout(Duration::from_secs(8), async {
        loop {
            let mut raw = archive
                .list_sweeps(
                    prep["active_session_id"].as_str().unwrap(),
                    Some(prep["analyzer_stream_id"].as_str().unwrap()),
                    64,
                )
                .await
                .unwrap();
            raw.sort_by_key(|s| s["settings"]["sequence"].as_u64().unwrap());
            if raw.len() >= 4 {
                break [raw[0].clone(), raw[1].clone()];
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    guard.stop_preparation(id, json!({})).await.unwrap();
    // Historical synthetic originals provide 60 s of baseline duration without
    // adding a real minute to the test. The collection and arm sessions both
    // run through the core's normal scheduler with its actual current settings.
    let mut historic = Vec::new();
    let now = chrono::Utc::now();
    for i in 0..20 {
        for template in &original {
            let mut s = template.clone();
            s["id"] = json!(format!("ref-{i}-{}", s["settings"]["scan_window_index"]));
            let start = now - chrono::TimeDelta::seconds(120 - i * 4);
            s["interval"] = json!({"started_at":start.to_rfc3339_opts(chrono::SecondsFormat::Micros,true),
                "ended_at":(start+chrono::TimeDelta::milliseconds(3)).to_rfc3339_opts(chrono::SecondsFormat::Micros,true)});
            historic.push(s);
        }
    }
    archive.append_sweeps(historic).await.unwrap();
    guard
        .create_baseline(
            id,
            json!({"band_id":"rx","name":"Quiet","location":"synthetic","confirm":true}),
        )
        .await
        .unwrap();
    let armed = guard.arm(id, json!({})).await.unwrap();
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let rows = archive
                .list_sweeps(
                    armed["active_session_id"].as_str().unwrap(),
                    Some(armed["analyzer_stream_id"].as_str().unwrap()),
                    10,
                )
                .await
                .unwrap();
            if rows.len() >= 2 {
                guard.analyze(id).await.unwrap();
                for raw in rows.iter().take(2) {
                    let comparison = guard
                        .history_comparison(id, raw["id"].as_str().unwrap())
                        .await
                        .unwrap();
                    assert_eq!(comparison["compatible"], true);
                    let index = raw["settings"]["scan_window_index"].as_u64().unwrap();
                    assert_eq!(
                        comparison["baseline_id"],
                        armed["band_baselines"][format!("rx:{index}")]
                    );
                }
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    guard.disarm(id, json!({})).await.unwrap();
    core.close().await.unwrap();
}
