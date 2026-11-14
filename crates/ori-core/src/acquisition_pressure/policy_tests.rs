use super::*;
use crate::devices::AcquisitionHealth;

fn options() -> AcquisitionPressureOptions {
    AcquisitionPressureOptions {
        sample_interval: Duration::from_millis(10),
        initial_grace: Duration::from_secs(90),
        lag_floor: Duration::from_secs(2),
        lag_slack: Duration::from_millis(500),
        recovery_dwell: Duration::from_secs(1),
    }
}
fn health(started_at: Instant) -> AcquisitionHealth {
    AcquisitionHealth {
        worker_instance: "original-analyzer".into(),
        configured_interval: Duration::ZERO,
        active: true,
        intentionally_paused: false,
        started_at,
        configuration_epoch: 0,
        configuration_changed_at: started_at,
        poll_started_at: Some(started_at),
        last_poll_duration: None,
        commit_started_at: None,
        last_commit_duration: None,
        last_cycle_finished_at: None,
        completed_cycles: 0,
    }
}
fn completed(row: &mut AcquisitionHealth, at: Instant, poll: Duration, commit: Duration) {
    row.poll_started_at = None;
    row.commit_started_at = None;
    row.last_poll_duration = Some(poll);
    row.last_commit_duration = Some(commit);
    row.last_cycle_finished_at = Some(at);
    row.completed_cycles += 1;
}

#[test]
fn natural_long_scans_and_zero_sleep_are_calibrated_without_treating_busy_as_lag() {
    let start = Instant::now();
    let mut row = health(start);
    let mut policy = Policy::new(options());
    assert!(!policy.observe(std::slice::from_ref(&row), start).pressure);
    assert!(
        !policy
            .observe(std::slice::from_ref(&row), start + Duration::from_secs(30))
            .pressure
    );
    for cycle in 1..=3 {
        let at = start + Duration::from_secs(cycle * 40);
        completed(
            &mut row,
            at,
            Duration::from_secs(39),
            Duration::from_secs(1),
        );
        assert!(!policy.observe(std::slice::from_ref(&row), at).pressure);
        row.poll_started_at = Some(at);
    }
    // This is still within the learned natural cadence's lag budget. A fixed
    // two-second timeout or a busy=100% signal would incorrectly block science.
    let at = row.poll_started_at.unwrap() + Duration::from_secs(80);
    assert!(!policy.observe(std::slice::from_ref(&row), at).pressure);
    assert!(!policy.observe(std::slice::from_ref(&row), at).pressure);
}

#[test]
fn sustained_poll_lag_requires_new_healthy_commits_and_dwell_before_recovery() {
    let start = Instant::now();
    let mut row = health(start);
    let mut policy = Policy::new(options());
    for cycle in 1..=3 {
        let at = start + Duration::from_millis(cycle * 100);
        completed(
            &mut row,
            at,
            Duration::from_millis(90),
            Duration::from_millis(10),
        );
        assert!(!policy.observe(std::slice::from_ref(&row), at).pressure);
    }
    let poll = start + Duration::from_secs(1);
    row.poll_started_at = Some(poll);
    let delayed = poll + Duration::from_secs(3);
    assert!(!policy.observe(std::slice::from_ref(&row), delayed).pressure);
    assert!(
        policy
            .observe(
                std::slice::from_ref(&row),
                delayed + Duration::from_millis(10)
            )
            .pressure
    );
    completed(
        &mut row,
        delayed + Duration::from_millis(100),
        Duration::from_secs(3),
        Duration::from_millis(10),
    );
    assert!(
        policy
            .observe(
                std::slice::from_ref(&row),
                delayed + Duration::from_millis(100)
            )
            .pressure
    );
    // The bad three-second sample must not become the new normal.
    for cycle in 1..=3 {
        let at = delayed + Duration::from_millis(100 + cycle * 100);
        completed(
            &mut row,
            at,
            Duration::from_millis(90),
            Duration::from_millis(10),
        );
        assert!(policy.observe(std::slice::from_ref(&row), at).pressure);
    }
    let recovered = delayed + Duration::from_millis(1300);
    assert!(
        !policy
            .observe(std::slice::from_ref(&row), recovered)
            .pressure
    );
    row.poll_started_at = Some(recovered);
    let lag_again = recovered + Duration::from_secs(3);
    assert!(
        !policy
            .observe(std::slice::from_ref(&row), lag_again)
            .pressure
    );
    assert!(
        policy
            .observe(std::slice::from_ref(&row), lag_again)
            .pressure
    );
}

#[test]
fn repeated_healthy_snapshot_is_not_recovery_and_commit_stalls_are_observed() {
    let start = Instant::now();
    let mut row = health(start);
    let mut policy = Policy::new(options());
    for cycle in 1..=3 {
        let at = start + Duration::from_millis(cycle * 100);
        completed(
            &mut row,
            at,
            Duration::from_millis(90),
            Duration::from_millis(10),
        );
        policy.observe(std::slice::from_ref(&row), at);
    }
    row.commit_started_at = Some(start + Duration::from_secs(1));
    let delayed = start + Duration::from_secs(4);
    policy.observe(std::slice::from_ref(&row), delayed);
    assert!(policy.observe(std::slice::from_ref(&row), delayed).pressure);
    completed(
        &mut row,
        delayed,
        Duration::from_millis(90),
        Duration::from_millis(10),
    );
    assert!(policy.observe(std::slice::from_ref(&row), delayed).pressure);
    for _ in 0..10 {
        assert!(
            policy
                .observe(std::slice::from_ref(&row), delayed + Duration::from_secs(1))
                .pressure
        );
    }
}

#[test]
fn intentional_sleep_pause_absence_and_replacement_do_not_inherit_another_instances_lag() {
    let start = Instant::now();
    let mut row = health(start);
    row.configured_interval = Duration::from_secs(3600);
    let mut policy = Policy::new(options());
    for cycle in 1..=3 {
        let at = start + Duration::from_secs(cycle);
        completed(
            &mut row,
            at,
            Duration::from_millis(90),
            Duration::from_millis(10),
        );
        policy.observe(std::slice::from_ref(&row), at);
    }
    let intentional_sleep = start + Duration::from_secs(3500);
    assert!(
        !policy
            .observe(std::slice::from_ref(&row), intentional_sleep)
            .pressure
    );
    assert!(
        !policy
            .observe(std::slice::from_ref(&row), intentional_sleep)
            .pressure
    );
    row.poll_started_at = Some(intentional_sleep);
    let delayed = intentional_sleep + Duration::from_secs(3);
    policy.observe(std::slice::from_ref(&row), delayed);
    assert!(policy.observe(std::slice::from_ref(&row), delayed).pressure);
    row.intentionally_paused = true;
    assert!(!policy.observe(std::slice::from_ref(&row), delayed).pressure);
    row.intentionally_paused = false;
    row.worker_instance = "replacement-instance".into();
    row.started_at = delayed;
    row.configuration_changed_at = delayed;
    row.poll_started_at = Some(delayed);
    row.last_cycle_finished_at = None;
    row.last_poll_duration = None;
    row.last_commit_duration = None;
    row.completed_cycles = 0;
    assert!(!policy.observe(std::slice::from_ref(&row), delayed).pressure);
    assert!(
        !policy
            .observe(&[], delayed + Duration::from_secs(1000))
            .pressure
    );
    assert!(
        policy.trackers.is_empty(),
        "retired instance state must be bounded"
    );
}

#[test]
fn first_capture_grace_cannot_turn_persistent_overload_into_calibration() {
    let start = Instant::now();
    let mut row = health(start);
    let mut policy = Policy::new(options());
    assert!(
        !policy
            .observe(std::slice::from_ref(&row), start + Duration::from_secs(89))
            .pressure
    );
    let delayed = start + Duration::from_secs(91);
    assert!(!policy.observe(std::slice::from_ref(&row), delayed).pressure);
    assert!(policy.observe(std::slice::from_ref(&row), delayed).pressure);
    for cycle in 1..=6 {
        let at = delayed + Duration::from_secs(cycle * 91);
        completed(
            &mut row,
            at,
            Duration::from_secs(91),
            Duration::from_millis(1),
        );
        assert!(
            policy.observe(std::slice::from_ref(&row), at).pressure,
            "a completed but persistently over-grace capture must not train away overload"
        );
    }
}

#[test]
fn one_lagging_analyzer_holds_pressure_until_its_own_recovery_or_stop() {
    let start = Instant::now();
    let slow = health(start);
    let mut healthy = health(start);
    healthy.worker_instance = "independent-fast-analyzer".into();
    let mut policy = Policy::new(options());
    let delayed = start + Duration::from_secs(91);
    completed(
        &mut healthy,
        delayed,
        Duration::from_millis(90),
        Duration::from_millis(10),
    );
    let rows = [slow.clone(), healthy.clone()];
    policy.observe(&rows, delayed);
    assert!(policy.observe(&rows, delayed).pressure);
    for cycle in 1..=4 {
        let at = delayed + Duration::from_secs(cycle);
        completed(
            &mut healthy,
            at,
            Duration::from_millis(90),
            Duration::from_millis(10),
        );
        assert!(
            policy
                .observe(&[slow.clone(), healthy.clone()], at)
                .pressure
        );
    }
    assert!(
        !policy
            .observe(&[healthy], delayed + Duration::from_secs(4))
            .pressure
    );
}

#[test]
fn trusted_startup_rejects_unbounded_or_degenerate_timing_without_overflow() {
    assert!(AcquisitionPressureOptions::default().validate().is_ok());
    for change in 0..6 {
        let mut value = options();
        match change {
            0 => value.sample_interval = Duration::ZERO,
            1 => value.initial_grace = Duration::MAX,
            2 => value.lag_floor = Duration::ZERO,
            3 => value.lag_slack = Duration::MAX,
            4 => value.recovery_dwell = Duration::ZERO,
            _ => value.sample_interval = value.lag_floor * 2,
        }
        assert!(
            value.validate().is_err(),
            "invalid timing option {change} was accepted"
        );
    }
}

#[test]
fn only_a_confirmed_configuration_epoch_restarts_calibration_on_the_same_worker() {
    let start = Instant::now();
    let mut row = health(start);
    let mut policy = Policy::new(options());
    for cycle in 1..=3 {
        let at = start + Duration::from_millis(cycle * 100);
        completed(
            &mut row,
            at,
            Duration::from_millis(90),
            Duration::from_millis(10),
        );
        policy.observe(std::slice::from_ref(&row), at);
    }
    let changed = start + Duration::from_secs(1000);
    row.configuration_epoch = 1;
    row.configuration_changed_at = changed;
    row.poll_started_at = Some(changed);
    // Existing raw counters/old successful timings remain, but cannot train the
    // new configuration or make its first naturally long scan overdue.
    assert!(!policy.observe(std::slice::from_ref(&row), changed).pressure);
    assert!(
        !policy
            .observe(
                std::slice::from_ref(&row),
                changed + Duration::from_secs(30)
            )
            .pressure
    );
    assert!(
        !policy
            .observe(
                std::slice::from_ref(&row),
                changed + Duration::from_secs(30)
            )
            .pressure
    );
    for cycle in 1..=3 {
        let at = changed + Duration::from_secs(cycle * 40);
        completed(
            &mut row,
            at,
            Duration::from_secs(39),
            Duration::from_secs(1),
        );
        assert!(!policy.observe(std::slice::from_ref(&row), at).pressure);
        row.poll_started_at = Some(at);
    }
    let late = changed + Duration::from_secs(242);
    assert!(!policy.observe(std::slice::from_ref(&row), late).pressure);
    assert!(policy.observe(std::slice::from_ref(&row), late).pressure);
    // Status reads and another long-but-unconfirmed cycle do not create an epoch.
    completed(
        &mut row,
        late,
        Duration::from_secs(122),
        Duration::from_secs(1),
    );
    assert!(policy.observe(std::slice::from_ref(&row), late).pressure);
}

#[test]
fn resuming_a_long_intentional_pause_relearns_cadence_without_reusing_stale_progress() {
    let start = Instant::now();
    let mut row = health(start);
    row.intentionally_paused = true;
    let mut policy = Policy::new(options());
    let paused = start + Duration::from_secs(1000);
    assert!(!policy.observe(std::slice::from_ref(&row), paused).pressure);
    row.intentionally_paused = false;
    row.poll_started_at = Some(paused);
    assert!(!policy.observe(std::slice::from_ref(&row), paused).pressure);
    assert!(
        !policy
            .observe(std::slice::from_ref(&row), paused + Duration::from_secs(30))
            .pressure
    );
    assert!(
        !policy
            .observe(std::slice::from_ref(&row), paused + Duration::from_secs(30))
            .pressure
    );
}
