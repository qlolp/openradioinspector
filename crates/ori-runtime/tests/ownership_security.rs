#[cfg(unix)]
use ori_runtime::security::Credentials;
use ori_runtime::{
    lease::{InstanceLease, WriterLease},
    security::{AccessPolicy, CredentialScope, PairingWindow},
};
#[cfg(unix)]
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
fn pairing_scope_is_set_by_the_window_and_mismatches_spend_an_attempt() {
    let (mut window, code) =
        PairingWindow::begin_scoped(100, 60, 3, CredentialScope::Reader).unwrap();
    assert!(window.redeem(&code, 101).is_err());
    assert!(
        window
            .redeem_scoped(&code, 101, Some(CredentialScope::Operator))
            .is_err()
    );
    assert_eq!(
        window.redeem_scoped(&code, 101, None).unwrap(),
        CredentialScope::Reader
    );
    assert!(window.redeem_scoped(&code, 101, None).is_err());

    let (mut operator, code) = PairingWindow::begin(100, 60, 1).unwrap();
    assert!(
        operator
            .redeem_scoped(&code, 101, Some(CredentialScope::Reader))
            .is_err()
    );
    assert!(operator.redeem(&code, 101).is_err());
}

#[cfg(unix)]
#[test]
fn version_one_operators_survive_downgrade_and_reader_tokens_never_match_the_legacy_verifier() {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    use sha2::{Digest, Sha256};
    let legacy_hash = |token: &str| URL_SAFE_NO_PAD.encode(Sha256::digest(token.as_bytes()));
    let temp = tempfile::tempdir().unwrap();
    let dir = temp.path().join("security");
    Credentials::open(&dir).unwrap();
    let path = dir.join("credentials.json");
    let operator = "A".repeat(43);
    let old = serde_json::to_vec(&serde_json::json!({"version":1,"tokens":[
        {"hash":legacy_hash(&operator),"label":"historic desktop","created_at_unix":10}]}))
    .unwrap();
    fs::write(&path, &old).unwrap();
    let mut credentials = Credentials::open(&dir).unwrap();
    credentials.authorize(&operator).unwrap();
    assert_eq!(fs::read(&path).unwrap(), old);

    let reader = credentials
        .issue_scoped("phone", 11, CredentialScope::Reader)
        .unwrap();
    let reopened = Credentials::open(&dir).unwrap();
    assert_eq!(
        reopened.credential_scope(&reader).unwrap(),
        CredentialScope::Reader
    );
    assert!(reopened.authorize(&reader).is_err());
    let saved = fs::read_to_string(&path).unwrap();
    assert!(!saved.contains(&reader));
    let mut store: serde_json::Value = serde_json::from_str(&saved).unwrap();
    assert_eq!(store["version"], 1);
    let records = store["tokens"].as_array().unwrap();
    assert!(
        records
            .iter()
            .all(|record| record.as_object().unwrap().len() == 3)
    );
    assert!(
        records
            .iter()
            .any(|record| record["hash"] == legacy_hash(&operator))
    );
    assert!(
        records
            .iter()
            .all(|record| record["hash"] != legacy_hash(&reader))
    );
    let suffix = records[1]["hash"]
        .as_str()
        .unwrap()
        .strip_prefix("reader-v1:")
        .unwrap()
        .to_owned();
    assert_ne!(suffix, legacy_hash(&reader));
    store["tokens"][1]["hash"] = suffix.into();
    fs::write(&path, serde_json::to_vec(&store).unwrap()).unwrap();
    assert!(reopened.credential_scope(&reader).is_err());
    reopened.authorize(&operator).unwrap();
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
