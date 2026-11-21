//! Synthetic RF Explorer frames pass through the native driver, archive and scientific worker.
use super::*;
use ori_contracts::DeviceStream;
use ori_core::devices::{BoxTransport, DeviceFuture, DeviceResult, TransportFactory};
use ori_runtime::worker::WorkerSpec;
use std::io::Cursor;

const POINTS: usize = 112;
const EXPANSION: &str = "1,000,0015000,2700000,0600000";
const MAIN: &str = "0,000,0240000,0960000,0100000";
const SESSION: &str = "/api/v1/sessions/research";

struct SyntheticFrames;

impl TransportFactory for SyntheticFrames {
    fn identity(&self, _: &DeviceStream) -> DeviceResult<String> {
        Ok("synthetic-rfe-analysis".into())
    }

    fn open<'a>(&'a self, _: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        Box::pin(async {
            let mut wire = b"#C2-M:003,005,01.34\r\n".to_vec();
            // Both inputs cover the same 433 MHz grid. Only the specified readback changes.
            for (board, rbw, offset, detector, counts) in [
                (EXPANSION, 15, 0, 4, &[120, 140, 160][..]),
                (EXPANSION, 15, 0, 0, &[160]),
                (EXPANSION, 30, 0, 4, &[160]),
                (MAIN, 15, 0, 4, &[160]),
                (EXPANSION, 15, 3, 4, &[166]),
            ] {
                let config = format!(
                    "#C2-F:0433000,0010000,0000,-118,0112,{board},{rbw:05},{offset:04},{detector:03}\r\n"
                );
                wire.extend_from_slice(config.as_bytes());
                for &count in counts {
                    wire.extend_from_slice(b"$S\x70");
                    wire.extend_from_slice(&[count; POINTS]);
                    wire.extend_from_slice(b"\r\n");
                }
            }
            Ok(Box::new(tokio::io::join(Cursor::new(wire), tokio::io::sink())) as BoxTransport)
        })
    }
}

#[tokio::test]
async fn manual_analysis_compares_changed_levels_and_refuses_changed_rf_settings() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = CoreConfig::live(directory.path());
    config.devices.transport = Some(Arc::new(SyntheticFrames));
    let (python, args) = match std::env::var_os("ORI_RESEARCH_PYTHON") {
        Some(installed) => (
            installed,
            ["-I", "-B", "-m", "radioinspector.research_rpc"].map(String::from).to_vec(),
        ),
        None => (
            std::env::var_os("ORI_TEST_PYTHON").unwrap_or_else(|| "/usr/bin/python3".into()),
            [
                "-I", "-B", "-c",
                "import runpy, sys\nsys.path.insert(0, sys.argv[1])\nrunpy.run_module('radioinspector.research_rpc', run_name='__main__')",
                concat!(env!("CARGO_MANIFEST_DIR"), "/../.."),
            ].map(String::from).to_vec(),
        ),
    };
    config.analysis_worker = Some(WorkerSpec {
        program: python.into(),
        args,
        environment: Default::default(),
        deadline_ms: 10_000,
        require_hard_memory: false,
        linux_cgroup: None,
    });
    let server = Server::start(config).await;
    setup(
        &server,
        json!({"id":"research","name":"Synthetic RF Explorer readbacks","streams":[{
            "id":"rfe","device":"RF Explorer","role":"spectrum_analyzer","level_unit":"dBm",
            "port":"owned-frames","settings":{"adapter":"rf-explorer-3g-serial"}
        }]}),
    )
    .await;
    let (status, response) = server
        .request(
            "POST",
            &format!("{SESSION}/streams/rfe/start"),
            Some(&json!({})),
        )
        .await;
    assert_eq!(status, 200, "{response}");

    let sweeps = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let (status, records) = server
                .request(
                    "GET",
                    &format!("{SESSION}/sweeps?stream_id=rfe&limit=10"),
                    None,
                )
                .await;
            assert_eq!(status, 200, "{records}");
            if let Some(sweeps) = records["data"]
                .as_array()
                .filter(|sweeps| sweeps.len() == 7)
            {
                break sweeps.clone();
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("all seven synthetic sweeps must reach the canonical archive");
    let reference = sweeps.iter().find(|s| s["levels"][0] == -60.0).unwrap();
    let second = sweeps.iter().find(|s| s["levels"][0] == -70.0).unwrap();
    let candidates = sweeps
        .iter()
        .filter(|s| s["levels"][0] == -80.0)
        .collect::<Vec<_>>();
    assert_eq!(candidates.len(), 5);
    let reference_id = reference["id"].as_str().unwrap();
    let mut compatible = 0;
    for sweep in candidates {
        assert_eq!(sweep["frequencies_hz"], reference["frequencies_hz"]);
        let id = sweep["id"].as_str().unwrap();
        let (status, pair) = server
            .request(
                "GET",
                &format!("{SESSION}/sweep-comparison?reference_id={reference_id}&target_id={id}"),
                None,
            )
            .await;
        let settings = &sweep["settings"];
        if settings["detector"] != "max_hold"
            || settings["active_board"] != "expansion"
            || settings["rbw_hz"] != 15_000
            || settings["offset_db"] != 0
        {
            assert_eq!(status, 409, "{pair}");
            assert_eq!(pair["error"]["code"], "comparison_incompatible");
            continue;
        }
        compatible += 1;
        assert_eq!(status, 200, "{pair}");
        assert_eq!(pair["data"]["delta_levels"], json!(vec![-20.0; POINTS]));
        assert_eq!(pair["data"]["target"]["settings"], *settings);
        let body = json!({"reference_ids":[reference_id,second["id"]],"target_id":id});
        let (status, average) = server
            .request(
                "POST",
                &format!("{SESSION}/sweep-average-comparison"),
                Some(&body),
            )
            .await;
        assert_eq!(status, 200, "{average}");
        let baseline = &average["data"]["reference"];
        assert_eq!(baseline["sources"][0]["settings"], reference["settings"]);
        for level in baseline["levels"].as_array().unwrap() {
            assert!((level.as_f64().unwrap() + 62.596_373_105_057_56).abs() < 1e-9);
        }
    }
    assert_eq!(compatible, 1);
    server.stop().await;
}
