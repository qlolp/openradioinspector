//! Only a PTY pair allocated by this test is opened; no physical port discovery.
#![cfg(all(unix, feature = "serial"))]
use ori_instruments::serial;
use serialport::{SerialPort, TTYPort};
use std::{mem::MaybeUninit, os::fd::AsRawFd};

fn attributes(fd: std::os::fd::RawFd) -> libc::termios {
    let mut termios = MaybeUninit::<libc::termios>::zeroed();
    // SAFETY: fd belongs to the test-created live PTY; tcgetattr writes a
    // termios object before it is assumed initialized on a successful return.
    assert_eq!(unsafe { libc::tcgetattr(fd, termios.as_mut_ptr()) }, 0);
    unsafe { termios.assume_init() }
}
#[tokio::test]
async fn python_style_flock_denies_open_before_changing_owned_pty_termios() {
    let (_master, slave) = TTYPort::pair().unwrap();
    let path = slave.name().unwrap();
    let fd = slave.as_raw_fd();
    // Same advisory ownership primitive used by PosixSerialTransport. An
    // independently opened fd must not configure the terminal before acquiring it.
    assert_eq!(unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) }, 0);
    let before = attributes(fd);
    let result = serial::open(&serial::SerialOptions {
        path: path.clone(),
        baud: 115200,
    });
    assert!(result.is_err(), "connector must reject a Python-owned PTY");
    let after = attributes(fd);
    assert_eq!(before.c_iflag, after.c_iflag);
    assert_eq!(before.c_oflag, after.c_oflag);
    assert_eq!(before.c_cflag, after.c_cflag);
    assert_eq!(before.c_lflag, after.c_lflag);
    assert_eq!(before.c_cc, after.c_cc);
    assert_eq!(unsafe { libc::cfgetispeed(&before) }, unsafe {
        libc::cfgetispeed(&after)
    });
    assert_eq!(unsafe { libc::cfgetospeed(&before) }, unsafe {
        libc::cfgetospeed(&after)
    });
    let competitor = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    assert_ne!(
        unsafe { libc::flock(competitor.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
        0,
        "failed connector must leave the original advisory lock held"
    );
}
