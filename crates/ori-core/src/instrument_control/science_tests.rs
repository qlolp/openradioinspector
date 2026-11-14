//! Real Core/QoS/authentication and owned child processes; the child fixture
//! deliberately does no science. The existing 36-case HTTP oracle checks math.
use super::*;
use crate::security::{AuthContext, AuthReceipt, ListenerRole};
use axum::http::{HeaderMap, Method, header};
use ori_contracts::parse_json_value;
use ori_control::{MeasurementMethod, OwnerAuthority, OwnerReceipt, Target};
use ori_instruments::{OperationInterval, RawSpectrumSample, SpectrumKind};
use ori_runtime::worker::WorkerSpec;
use std::{collections::BTreeMap, path::Path, time::Duration};

struct Authority {
    target: Target,
    auth: AuthReceipt,
}
impl OwnerAuthority for Authority {
    fn target(&self) -> &Target {
        &self.target
    }
    fn worker_instance(&self) -> &str {
        "owned-scientific-input-worker"
    }
    fn connection_id(&self) -> &str {
        "owned-scientific-input-connection"
    }
    fn revalidate(&self) -> Result<()> {
        self.auth.revalidate().map_err(|_| ControlError::Denied)
    }
}

async fn configured(
    directory: &Path,
    marker: &Path,
    mode: &str,
) -> (Arc<Core>, AuthContext, OwnerReceipt) {
    use std::os::unix::fs::PermissionsExt;
    let security = directory.join("security");
    std::fs::create_dir(&security).unwrap();
    std::fs::set_permissions(&security, std::fs::Permissions::from_mode(0o700)).unwrap();
    ori_runtime::security::TlsIdentity::create(&security, vec!["127.0.0.1".into()]).unwrap();
    let mut config = crate::CoreConfig::replay(directory);
    config.lan_security = Some(crate::LanSecurityConfig {
        directory: security,
        hosts: vec!["127.0.0.1:8555".into()],
        origins: vec!["https://127.0.0.1:8555".into()],
    });
    config.analysis_worker = Some(WorkerSpec {
        program: std::env::var_os("ORI_TEST_PYTHON")
            .map(Into::into)
            .unwrap_or_else(|| "/usr/bin/python3".into()),
        args: vec![
            format!(
                "{}/tests/private_worker_echo.py",
                env!("CARGO_MANIFEST_DIR")
            ),
            marker.to_str().unwrap().into(),
            mode.into(),
        ],
        environment: BTreeMap::new(),
        deadline_ms: 10_000,
        require_hard_memory: false,
        linux_cgroup: None,
    });
    let core = Core::open(config).await.unwrap();
    let mut headers = HeaderMap::new();
    headers.insert(header::HOST, "127.0.0.1:8555".parse().unwrap());
    headers.insert(header::ORIGIN, "https://127.0.0.1:8555".parse().unwrap());
    let code = core.security.begin(300, 10).unwrap();
    let response = core
        .security
        .pair(
            json!({"pairing_code":code,"label":"Owned control-science test","client":"native"}),
            &headers,
        )
        .await
        .unwrap();
    let bytes = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    let response = parse_json_value(&bytes, 4096).unwrap();
    headers.insert(
        header::AUTHORIZATION,
        format!("Bearer {}", response["data"]["token"].as_str().unwrap())
            .parse()
            .unwrap(),
    );
    let auth = core
        .security
        .check(
            &Method::POST,
            "/api/v1/sessions/science/streams/source/instrument/operations",
            &headers,
            ListenerRole::Tls,
        )
        .await
        .unwrap();
    let owner = OwnerReceipt::from_backend(Arc::new(Authority {
        target: Target {
            session_id: "science".into(),
            stream_id: "source".into(),
        },
        auth: auth.receipt(),
    }))
    .unwrap();
    (core, auth, owner)
}

fn request(owner: OwnerReceipt) -> MeasurementRequest {
    let at = chrono::DateTime::parse_from_rfc3339("2026-11-11T00:00:00.000001Z")
        .unwrap()
        .to_utc();
    MeasurementRequest {
        owner,
        method: MeasurementMethod::Peak,
        rbw_applied_hz: 10_000.0,
        source: RawSpectrumSample {
            kind: SpectrumKind::RawSpectrum,
            frequencies_hz: vec![100_000_000, 100_500_000, 101_000_000],
            levels: vec![-90.0, -80.0, -70.0],
            level_unit: "dBm".into(),
            interval: OperationInterval {
                started_at: at,
                ended_at: at,
            },
            settings: json!({"requested_stop_hz":101_500_000}),
            wire: vec![0, 1, 2, 3, 4, 5],
            zero_db: 174,
        },
    }
}

async fn child_pid(marker: &Path) -> i32 {
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if let Ok(bytes) = std::fs::read(marker) {
                return parse_json_value(&bytes, 1024).unwrap()["pid"]
                    .as_i64()
                    .unwrap() as i32;
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .unwrap()
}
fn reaped(pid: i32) -> bool {
    (unsafe { libc::kill(pid, 0) }) == -1
        && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
}

#[tokio::test]
async fn pressure_denies_control_science_before_any_child_is_spawned() {
    let directory = tempfile::tempdir().unwrap();
    let marker = directory.path().join("must-not-exist.pid");
    let (core, _auth, owner) = configured(directory.path(), &marker, "success").await;
    let qos = core.background.as_ref().unwrap().qos.clone();
    qos.pressure(true).await.unwrap();
    let outcome = analyze(&core, request(owner)).await;
    let child_spawned = marker.exists();
    let heavy_released = core.research_pool.try_reserve().is_ok();
    let status = qos.status();
    core.close().await.unwrap();
    assert!(outcome.is_err(), "pressure admitted Control science");
    assert!(
        !child_spawned,
        "pressure allowed a scientific child to spawn"
    );
    assert!(heavy_released);
    assert_eq!(status.registered_work, 0);
    assert_eq!(status.logical_bytes, 0, "pure-memory work charged fake I/O");
}

#[tokio::test]
async fn pressure_cancels_control_science_and_waits_for_actual_child_join() {
    let directory = tempfile::tempdir().unwrap();
    let marker = directory.path().join("pressure.pid");
    let (core, auth, owner) = configured(directory.path(), &marker, "block").await;
    let qos = core.background.as_ref().unwrap().qos.clone();
    let own = core.clone();
    let mut task = tokio::spawn(async move { analyze(&own, request(owner)).await });
    let pid = child_pid(&marker).await;
    let registered_at_child = qos.status().registered_work;
    let pressure_finished = tokio::time::timeout(Duration::from_secs(2), qos.pressure(true))
        .await
        .is_ok_and(|result| result.is_ok());
    let reaped_at_pressure = reaped(pid);
    let before_cleanup = tokio::time::timeout(Duration::from_millis(250), &mut task).await;
    let completed_by_pressure = before_cleanup.is_ok();
    let outcome = match before_cleanup {
        Ok(result) => result.unwrap(),
        Err(_) => {
            // RED cleanup still revokes the original real credential and JOINs
            // the old path. It cannot make the recorded pressure proof pass.
            core.security.revoke(&auth, &json!({})).await.unwrap();
            task.await.unwrap()
        }
    };
    let heavy_released = core.research_pool.try_reserve().is_ok();
    let status = qos.status();
    core.close().await.unwrap();
    assert_eq!(registered_at_child, 1, "Control child bypassed shared QoS");
    assert!(pressure_finished && completed_by_pressure && reaped_at_pressure);
    assert!(outcome.is_err());
    assert!(heavy_released);
    assert_eq!(status.registered_work, 0);
    assert_eq!(status.logical_bytes, 0, "pure-memory work charged fake I/O");
}

#[tokio::test]
async fn original_credential_revoke_cancels_control_science_before_worker_deadline() {
    let directory = tempfile::tempdir().unwrap();
    let marker = directory.path().join("original-auth.pid");
    let (core, auth, owner) = configured(directory.path(), &marker, "block").await;
    let qos = core.background.as_ref().unwrap().qos.clone();
    let own = core.clone();
    let mut task = tokio::spawn(async move { analyze(&own, request(owner)).await });
    let pid = child_pid(&marker).await;
    core.security.revoke(&auth, &json!({})).await.unwrap();
    let before_cleanup = tokio::time::timeout(Duration::from_secs(2), &mut task).await;
    let completed_by_original_revoke = before_cleanup.is_ok();
    let reaped_at_revoke = reaped(pid);
    let original_revoked = auth.receipt().revalidate().is_err();
    let outcome = match before_cleanup {
        Ok(result) => result.unwrap(),
        Err(_) => {
            core.request_shutdown();
            task.await.unwrap()
        }
    };
    let heavy_released = core.research_pool.try_reserve().is_ok();
    let status = qos.status();
    core.close().await.unwrap();
    assert!(completed_by_original_revoke && reaped_at_revoke);
    assert!(matches!(outcome, Err(ControlError::Denied)));
    assert!(original_revoked);
    assert!(heavy_released);
    assert_eq!(status.registered_work, 0);
    assert_eq!(status.logical_bytes, 0, "pure-memory work charged fake I/O");
}
