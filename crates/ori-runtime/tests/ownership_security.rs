use ori_runtime::{
    lease::{InstanceLease, WriterLease},
    security::{AccessPolicy, Credentials, PairingWindow},
};
use std::fs;

#[test]
fn independent_instance_and_writer_locks_exclude_other_owners_and_release_on_drop() {
    let dir = tempfile::tempdir().unwrap();
    let owner = InstanceLease::acquire(dir.path()).unwrap();
    assert!(
        InstanceLease::acquire(dir.path()).is_err(),
        "two supervisors admitted"
    );
    let writer = WriterLease::acquire(dir.path()).unwrap();
    assert!(
        WriterLease::acquire(dir.path()).is_err(),
        "two archive writers admitted"
    );
    drop(writer);
    WriterLease::acquire(dir.path()).unwrap();
    drop(owner);
    InstanceLease::acquire(dir.path()).unwrap();
}

#[cfg(unix)]
#[test]
fn persisted_tokens_are_opaque_hashed_and_revocation_survives_restart() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::Builder::new()
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir()
        .unwrap();
    let mut creds = Credentials::open(dir.path()).unwrap();
    let first = creds.issue("browser", 10).unwrap();
    let second = creds.issue("native", 10).unwrap();
    assert_ne!(first, second);
    assert!(first.len() >= 40);
    creds.authorize(&first).unwrap();
    assert!(creds.authorize("untrusted").is_err());
    let state = fs::read_to_string(dir.path().join("credentials.json")).unwrap();
    assert!(!state.contains(&first));
    assert!(!state.contains(&second));
    assert!(creds.revoke(&first).unwrap());
    let restarted = Credentials::open(dir.path()).unwrap();
    assert!(restarted.authorize(&first).is_err());
    restarted.authorize(&second).unwrap();
}

#[test]
fn pairing_is_single_use_expiring_and_rate_limited() {
    let (mut pairing, code) = PairingWindow::begin(100, 60, 3).unwrap();
    pairing.redeem(&code, 101).unwrap();
    assert!(pairing.redeem(&code, 102).is_err());
    let (mut pairing, code) = PairingWindow::begin(100, 60, 3).unwrap();
    assert!(pairing.redeem(&code, 160).is_err());
    let (mut pairing, code) = PairingWindow::begin(100, 60, 2).unwrap();
    assert!(pairing.redeem("wrong", 101).is_err());
    assert!(pairing.redeem("wrong", 102).is_err());
    assert!(pairing.redeem(&code, 103).is_err());
    assert!(PairingWindow::begin(100, 3601, 3).is_err());
    let (mut pairing, code) = PairingWindow::begin(100, 60, 3).unwrap();
    assert!(
        pairing.redeem(&code, 99).is_err(),
        "clock rollback extended a pairing window"
    );
}

#[test]
fn lan_rejects_plaintext_foreign_hosts_and_cross_origin_browser_requests() {
    let policy = AccessPolicy::lan(&["ori.local:9443"], &["https://ori.local:9443"]).unwrap();
    policy
        .check("ori.local:9443", Some("https://ori.local:9443"), true)
        .unwrap();
    policy.check("ori.local:9443", None, true).unwrap();
    for (host, origin, tls) in [
        ("attacker.test", None, true),
        ("ori.local:9443", Some("https://attacker.test"), true),
        ("ori.local:9443", Some("null"), true),
        ("ori.local:9443", None, false),
        ("ori.local:9443@attacker.test", None, true),
    ] {
        assert!(policy.check(host, origin, tls).is_err());
    }
}

#[cfg(unix)]
#[test]
fn private_state_rejects_symlinks_and_public_credentials() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("real");
    fs::create_dir(&target).unwrap();
    symlink(&target, dir.path().join("link")).unwrap();
    assert!(Credentials::open(&dir.path().join("link")).is_err());
    let private = dir.path().join("private");
    let mut creds = Credentials::open(&private).unwrap();
    creds.issue("test", 0).unwrap();
    fs::set_permissions(
        private.join("credentials.json"),
        fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    assert!(Credentials::open(&private).is_err());
    assert!(WriterLease::acquire(&dir.path().join("link")).is_err());
}

#[cfg(unix)]
#[test]
fn writable_untrusted_parent_cannot_replace_the_runtime_lease_directory() {
    use std::os::unix::fs::PermissionsExt;
    let temp = tempfile::tempdir().unwrap();
    let shared = temp.path().join("untrusted");
    std::fs::create_dir(&shared).unwrap();
    std::fs::set_permissions(&shared, std::fs::Permissions::from_mode(0o777)).unwrap();
    assert!(WriterLease::acquire(&shared).is_err());
    let private = shared.join("private");
    std::fs::create_dir(&private).unwrap();
    std::fs::set_permissions(&private, std::fs::Permissions::from_mode(0o700)).unwrap();
    assert!(Credentials::open(&private).is_err());
}
