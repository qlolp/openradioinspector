#![cfg(unix)]
use ori_runtime::security::{SecurityMode, TlsIdentity};
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::Arc,
};
#[test]
fn new_lan_requires_tls_existing_loopback_requires_explicit_migration() {
    assert_eq!(
        SecurityMode::for_installation(false, false),
        SecurityMode::LanTls
    );
    assert_eq!(
        SecurityMode::for_installation(true, false),
        SecurityMode::LegacyLoopback
    );
    assert_eq!(
        SecurityMode::for_installation(true, true),
        SecurityMode::LanTls
    );
    assert!(
        SecurityMode::LegacyLoopback
            .validate_bind("0.0.0.0:8770".parse().unwrap())
            .is_err()
    );
    SecurityMode::LegacyLoopback
        .validate_bind("127.0.0.1:8770".parse().unwrap())
        .unwrap();
}

#[test]
fn private_identity_reloads_and_authenticates_real_tls_with_installed_local_ca() {
    let temp = tempfile::tempdir().unwrap();
    let identity = TlsIdentity::create(temp.path(), vec!["ori.local".into()]).unwrap();
    let fingerprint = identity.fingerprint_sha256();
    assert_eq!(fingerprint.len(), 64);
    assert_eq!(
        TlsIdentity::load(temp.path()).unwrap().fingerprint_sha256(),
        fingerprint
    );
    assert!(
        TlsIdentity::create(temp.path(), vec!["ori.local".into()]).is_err(),
        "silent key rotation must be refused"
    );
    let server = Arc::new(identity.server_config().unwrap());
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let thread = std::thread::spawn(move || {
        let (mut tcp, _) = listener.accept().unwrap();
        let mut connection = rustls::ServerConnection::new(server).unwrap();
        let mut stream = rustls::Stream::new(&mut connection, &mut tcp);
        let mut ping = [0u8; 4];
        stream.read_exact(&mut ping).unwrap();
        assert_eq!(&ping, b"ping");
        stream.write_all(b"pong").unwrap();
    });
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls_pemfile::certs(&mut identity.trust_anchor_pem().as_bytes()) {
        roots.add(cert.unwrap()).unwrap();
    }
    let client = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(roots)
    .with_no_client_auth();
    let mut connection =
        rustls::ClientConnection::new(Arc::new(client), "ori.local".try_into().unwrap()).unwrap();
    let mut tcp = TcpStream::connect(address).unwrap();
    let mut stream = rustls::Stream::new(&mut connection, &mut tcp);
    stream.write_all(b"ping").unwrap();
    let mut pong = [0u8; 4];
    stream.read_exact(&mut pong).unwrap();
    assert_eq!(&pong, b"pong");
    thread.join().unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(
        temp.path().join("identity.json"),
        std::fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    assert!(TlsIdentity::load(temp.path()).is_err());
}
