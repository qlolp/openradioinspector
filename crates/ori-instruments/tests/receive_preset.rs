//! Real Python command-oracle parity over an owned duplex, never a serial port.
use ori_instruments::*;
use serde_json::Value;
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/control_python.json")).unwrap()
}
async fn device() -> (
    TinySa<tokio::io::DuplexStream>,
    Arc<Mutex<Vec<String>>>,
    tokio::task::JoinHandle<()>,
) {
    let (client, server) = tokio::io::duplex(4096);
    let sent = Arc::new(Mutex::new(Vec::new()));
    let log = sent.clone();
    let task = tokio::spawn(async move {
        let mut io = BufReader::new(server);
        loop {
            let mut command = Vec::new();
            if io.read_until(b'\r', &mut command).await.unwrap() == 0 {
                break;
            }
            let command = String::from_utf8(command[..command.len() - 1].to_vec()).unwrap();
            log.lock().unwrap().push(command.clone());
            let body = match command.as_str() {
                "info" => "tinySA Ultra ZS405\r\n",
                "version" => "tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n",
                "deviceid" => "synthetic\r\n",
                "help" => {
                    "info version deviceid help output mode lna attenuate rbw calc pause resume capture release refresh caloutput sweep ultra repeat\r\n"
                }
                _ => "",
            };
            io.get_mut()
                .write_all(format!("{command}\r\n{body}ch> ").as_bytes())
                .await
                .unwrap();
        }
    });
    let mut device = TinySa::new(
        client,
        ReceiveConfig {
            start_hz: 100_000_000,
            stop_hz: 101_000_000,
            points: 3,
            rbw_hz: 10_000,
        },
        IoLimits::default(),
    )
    .unwrap();
    device.prepare().await.unwrap();
    device.inspect().await.unwrap();
    sent.lock().unwrap().clear();
    (device, sent, task)
}
fn preset(settings: Vec<Setting>) -> TinySaControl {
    TinySaControl::ApplyReceivePreset(ReceivePreset {
        settings,
        confirmed: true,
    })
}
#[tokio::test]
async fn complete_receive_preset_matches_real_python_order_and_merged_state() {
    let (mut device, sent, task) = device().await;
    device.execute(TinySaControl::EnterLab).await.unwrap();
    sent.lock().unwrap().clear();
    let state = device
        .execute(preset(vec![
            Setting::RbwHz(Rbw::Hz(30_000)),
            Setting::StopHz(901_000_000),
            Setting::Repeat(3),
            Setting::StartHz(900_000_000),
            Setting::Points(7),
            Setting::Ultra(true),
            Setting::AttenuationDb(Attenuation::Db(0)),
            Setting::Lna(true),
        ]))
        .await
        .unwrap();
    assert_eq!(
        serde_json::json!(*sent.lock().unwrap()),
        oracle()["preset_commands"]
    );
    assert_eq!(state.settings, oracle()["preset_settings"]);
    assert_eq!(state.device_mode, DeviceMode::Input);
    assert_eq!(state.control_mode, ControlMode::Lab);
    drop(device);
    task.await.unwrap();
}
#[tokio::test]
async fn every_preset_field_and_merged_context_is_checked_before_any_bytes() {
    let (mut device, sent, task) = device().await;
    device.execute(TinySaControl::EnterLab).await.unwrap();
    sent.lock().unwrap().clear();
    let original = device.state().settings;
    for request in [
        preset(vec![Setting::OutputHz(100_000_000)]),
        preset(vec![Setting::CenterHz(100_000_000)]),
        preset(vec![Setting::Points(0)]),
        preset(vec![Setting::Repeat(2), Setting::Repeat(3)]),
        preset(vec![Setting::StartHz(900_000_000)]),
        preset(vec![
            Setting::Lna(true),
            Setting::AttenuationDb(Attenuation::Db(10)),
        ]),
        TinySaControl::ApplyReceivePreset(ReceivePreset {
            settings: vec![],
            confirmed: false,
        }),
    ] {
        assert!(matches!(
            device.execute(request).await,
            Err(InstrumentError::InvalidRequest(_))
        ));
        assert!(sent.lock().unwrap().is_empty());
        assert_eq!(device.state().settings, original);
        assert!(!device.state().restart_required);
    }
    drop(device);
    task.await.unwrap();
}
#[tokio::test]
async fn reenter_lab_is_additive_and_preserves_old_enter_lab_rejection() {
    let (mut device, sent, task) = device().await;
    device.execute(TinySaControl::EnterLab).await.unwrap();
    assert_eq!(
        serde_json::json!(*sent.lock().unwrap()),
        oracle()["acquire_commands"]
    );
    sent.lock().unwrap().clear();
    assert!(device.execute(TinySaControl::EnterLab).await.is_err());
    assert!(sent.lock().unwrap().is_empty());
    device.execute(TinySaControl::OutputOff).await.unwrap();
    assert_eq!(
        serde_json::json!(*sent.lock().unwrap()),
        oracle()["release_commands"]
    );
    sent.lock().unwrap().clear();
    device.execute(TinySaControl::ReenterLab).await.unwrap();
    assert_eq!(
        serde_json::json!(*sent.lock().unwrap()),
        oracle()["reenter_commands"]
    );
    drop(device);
    task.await.unwrap();
}
