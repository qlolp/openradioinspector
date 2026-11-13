//! Test-only port allocation for a fixed-address backend across handovers.
//! Never reissue a fixture address, and stay outside the OS TCP client range.
use std::{
    collections::BTreeSet,
    io,
    net::{Ipv4Addr, SocketAddr, TcpListener},
    ops::RangeInclusive,
    sync::{LazyLock, Mutex},
};

pub fn select_from(
    issued: &mut BTreeSet<u16>,
    client_ports: RangeInclusive<u16>,
    candidates: impl IntoIterator<Item = u16>,
) -> io::Result<SocketAddr> {
    for port in candidates.into_iter().take(65_536) {
        if port < 1024 || client_ports.contains(&port) || issued.contains(&port) {
            continue;
        }
        let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
        match TcpListener::bind(address) {
            Ok(listener) => {
                issued.insert(port);
                drop(listener);
                return Ok(address);
            }
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::AddrInUse | io::ErrorKind::PermissionDenied
                ) => {}
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AddrNotAvailable,
        "no unused fixture port outside the TCP client range",
    ))
}

fn client_ports() -> RangeInclusive<u16> {
    #[cfg(target_os = "linux")]
    let ports = std::fs::read_to_string("/proc/sys/net/ipv4/ip_local_port_range").unwrap();
    #[cfg(not(target_os = "linux"))]
    let ports = {
        let output = std::process::Command::new("sysctl")
            .args([
                "-n",
                "net.inet.ip.portrange.first",
                "net.inet.ip.portrange.last",
                "net.inet.ip.portrange.hifirst",
                "net.inet.ip.portrange.hilast",
            ])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "cannot read TCP client port ranges"
        );
        String::from_utf8(output.stdout).unwrap()
    };
    let values: Vec<u16> = ports
        .split_whitespace()
        .map(|v| v.parse().unwrap())
        .collect();
    assert!(values.len() == 2 || values.len() == 4);
    *values.iter().min().unwrap()..=*values.iter().max().unwrap()
}

pub fn address() -> SocketAddr {
    static ISSUED: Mutex<BTreeSet<u16>> = Mutex::new(BTreeSet::new());
    static CLIENT_PORTS: LazyLock<RangeInclusive<u16>> = LazyLock::new(client_ports);
    // All candidates are unprivileged. Do not modify host network settings,
    // serialise backend tests, retry real handovers, or touch occupied owners.
    let offset = std::process::id() % 64_512;
    let candidates = (0..64_512).map(|i| (1024 + (offset + i) % 64_512) as u16);
    select_from(
        &mut ISSUED.lock().unwrap(),
        CLIENT_PORTS.clone(),
        candidates,
    )
    .unwrap()
}
