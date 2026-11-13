#![cfg(unix)]
#[path = "support/backend_ports.rs"]
mod backend_ports;
#[path = "support/backend_python.rs"]
mod backend_python;
use ori_runtime::{
    lease::WriterLease,
    service::{self, AdminCommand, ServiceConfig},
    supervisor::BackendSpec,
};
use std::{collections::BTreeMap, path::Path, time::Duration};

struct OwnedService(tokio::task::JoinHandle<ori_runtime::Result<()>>);
impl Drop for OwnedService {
    fn drop(&mut self) {
        self.0.abort();
    }
}
async fn request(data: &Path, command: AdminCommand) -> serde_json::Value {
    tokio::time::timeout(Duration::from_secs(5), service::admin(data, command))
        .await
        .unwrap()
        .unwrap()
}

#[tokio::test]
async fn registered_rc_stays_idle_until_explicit_start_and_never_duplicates_its_owner() {
    let root = tempfile::tempdir().unwrap();
    let data = root.path();
    let address = backend_ports::address();
    let spec = BackendSpec {
        name: "owned-rc-fixture".into(),
        program: backend_python::program(),
        args: vec![
            format!("{}/tests/fake_backend.py", env!("CARGO_MANIFEST_DIR")),
            data.display().to_string(),
            address.port().to_string(),
            "candidate".into(),
        ],
        environment: BTreeMap::new(),
        control_addr: address,
        ports: vec![address],
        startup_timeout_ms: 2000,
        shutdown_timeout_ms: 2000,
    };
    let mut service = OwnedService(tokio::spawn(service::run_personal_rc(ServiceConfig {
        data_directory: data.to_owned(),
        backend: spec.clone(),
    })));
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    while !service::admin_socket_path(data).unwrap().exists() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "private RC socket not ready"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let idle = request(data, AdminCommand::Status).await;
    assert_eq!(idle["service_state"], "stopped");
    assert!(WriterLease::acquire(data).is_ok());
    assert!(
        !data.join("committed-data").exists(),
        "registration started a backend"
    );
    let first = request(data, AdminCommand::Start).await;
    assert_eq!(first["ok"], true);
    assert_eq!(first["status"]["writer_owned"], true);
    let owner = first["status"]["instance_id"].clone();
    assert!(WriterLease::acquire(data).is_err());
    // Leaving one client and attaching another never tears down the owner.
    assert_eq!(
        request(data, AdminCommand::Status).await["status"]["instance_id"],
        owner
    );
    assert_eq!(
        request(data, AdminCommand::Start).await["status"]["instance_id"],
        owner
    );
    let refused = request(
        data,
        AdminCommand::Cutover {
            candidate: spec,
            acknowledge_coverage_gap: true,
        },
    )
    .await;
    assert_eq!(refused["error"], "rc_update_requires_stopped_service");
    assert_eq!(
        request(data, AdminCommand::Status).await["status"]["instance_id"],
        owner
    );
    assert_eq!(request(data, AdminCommand::Shutdown).await["ok"], true);
    tokio::time::timeout(Duration::from_secs(3), &mut service.0)
        .await
        .expect("service did not join")
        .unwrap()
        .unwrap();
    assert!(WriterLease::acquire(data).is_ok());
    assert!(std::net::TcpListener::bind(address).is_ok());
    assert!(!service::admin_socket_path(data).unwrap().exists());
    assert_eq!(
        std::fs::read_to_string(data.join("committed-data"))
            .unwrap()
            .lines()
            .count(),
        1
    );
}
