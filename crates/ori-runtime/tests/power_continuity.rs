use ori_runtime::power::{ClockSample, ContinuityFence};

#[test]
fn rc_configuration_uses_only_bundled_rust_and_an_explicit_isolated_archive() {
    let bundle = std::path::Path::new("/Applications/OpenRadioInspector RC.app");
    let data = std::path::Path::new(
        "/Users/operator/Library/Application Support/OpenRadioInspector/macOS-RC",
    );
    let config =
        ori_runtime::macos_agent::configuration(bundle, data, 64 * 1024 * 1024 * 1024).unwrap();
    assert_eq!(
        config.backend.program,
        bundle.join("Contents/Helpers/ori-core")
    );
    assert_eq!(config.backend.control_addr.to_string(), "127.0.0.1:8772");
    let args = &config.backend.args;
    assert_eq!(
        &args[..4],
        ["--mode", "live", "--data-dir", data.to_str().unwrap()]
    );
    assert!(args.iter().any(|arg| arg == "--allow-serial"));
    assert!(
        !args
            .iter()
            .any(|arg| arg.contains("audio") || arg.contains("python"))
    );
    assert!(config.backend.supervisor_lifeline);
    assert!(args.iter().any(|arg| arg == "--stdin-lifeline"));
    assert!(ori_runtime::macos_agent::configuration(bundle, data, 0).is_err());
    assert!(
        ori_runtime::macos_agent::configuration(std::path::Path::new("relative"), data, 1).is_err()
    );
}

fn sample(absolute: u64, continuous: u64) -> ClockSample {
    ClockSample::new(absolute, continuous, absolute + 100).unwrap()
}

#[test]
fn normal_progress_and_sampling_jitter_do_not_break_continuity() {
    let mut fence = ContinuityFence::new(sample(10_000, 90_000));
    fence.check(sample(20_000, 100_030)).unwrap();
    fence.check(sample(40_000, 119_970)).unwrap();
}

#[test]
fn suspend_is_latched_even_when_the_next_sample_has_the_same_offset() {
    let mut fence = ContinuityFence::new(sample(10_000, 90_000));
    assert!(fence.check(sample(20_000, 8_100_000)).is_err());
    assert!(fence.check(sample(30_000, 8_110_000)).is_err());
    // Reopening an instrument establishes a fresh continuity epoch.
    ContinuityFence::new(sample(30_000, 8_110_000))
        .check(sample(40_000, 8_120_000))
        .unwrap();
}

#[test]
fn reversed_or_unbounded_clock_samples_are_never_accepted() {
    assert!(ClockSample::new(200, 300, 100).is_err());
    assert!(ClockSample::new(0, 10_000_000, 10_000_000).is_err());
}

#[test]
fn fresh_boot_can_sample_across_zero_offset_without_a_false_failure() {
    let sample = ClockSample::new(100, 150, 200).unwrap();
    ContinuityFence::new(sample)
        .check(ClockSample::new(200, 250, 300).unwrap())
        .unwrap();
}

#[test]
fn clock_epoch_reset_requires_a_new_connection() {
    let mut fence = ContinuityFence::new(sample(20_000_000, 40_000_000));
    assert!(fence.check(sample(30_000_000, 30_000_100)).is_err());
}

#[tokio::test]
async fn broken_physical_epoch_cannot_send_or_consume_bytes_but_can_close() {
    tokio::time::timeout(std::time::Duration::from_secs(1), async {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let (socket, mut peer) = tokio::io::duplex(32);
        let mut fence = ContinuityFence::new(sample(10_000, 90_000));
        assert!(fence.check(sample(20_000, 8_100_000)).is_err());
        let mut socket = ori_runtime::power::ContinuityTransport::new(socket, fence);
        assert!(socket.write_all(b"scanraw\n").await.is_err());
        peer.write_all(b"old tuning epoch").await.unwrap();
        let mut byte = [0];
        assert!(socket.read(&mut byte).await.is_err());
        socket.shutdown().await.unwrap();
        assert_eq!(peer.read(&mut byte).await.unwrap(), 0);
    })
    .await
    .expect("a broken epoch must fail without AsyncRead/Write Interrupted retry loops");
}
