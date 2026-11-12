//! Real owned sampler and blocked SQLite commit; no hardware or fake health.
use super::*;
#[tokio::test]
async fn analyzer_health_follows_poll_and_real_commit_without_receiver_or_ended_entries() {
    let data = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(data.path(), factory.clone()).await;
    let manager = core.devices.as_ref().unwrap();
    let cold: Vec<AcquisitionHealth> = manager.acquisition_health().unwrap();
    let cold_opens = factory.opens.load(Ordering::Acquire);
    manager
        .start_stream(
            "devices",
            "a",
            manager.public_gate(Target::new("devices", "a")),
        )
        .await
        .unwrap();
    let initial = tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            if let Some(sample) = manager
                .acquisition_health()
                .unwrap()
                .into_iter()
                .find(|sample| sample.completed_cycles >= 2)
            {
                break sample;
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .ok();
    let blocker = rusqlite::Connection::open(data.path().join("sessions.sqlite3")).unwrap();
    blocker.execute_batch("BEGIN IMMEDIATE").unwrap();
    let blocked = tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            if let Some(sample) = manager
                .acquisition_health()
                .unwrap()
                .into_iter()
                .find(|sample| sample.commit_started_at.is_some())
            {
                break sample;
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .ok();
    blocker.execute_batch("ROLLBACK").unwrap();
    drop(blocker);
    manager.stop_stream("devices", "a").await.unwrap();
    factory.pause_poll.store(true, Ordering::Release);
    manager
        .start_stream(
            "devices",
            "r",
            manager.public_gate(Target::new("devices", "r")),
        )
        .await
        .unwrap();
    let receiver_only = manager.acquisition_health().unwrap();
    core.close().await.unwrap();
    assert!(cold.is_empty() && cold_opens == 0 && receiver_only.is_empty());
    let initial = initial.expect("live analyzer did not expose its actual raw commit progress");
    assert!(initial.active && !initial.intentionally_paused);
    assert_eq!(initial.configuration_epoch, 0);
    assert_eq!(initial.configuration_changed_at, initial.started_at);
    assert_eq!(initial.configured_interval, Duration::from_millis(10));
    assert!(initial.last_poll_duration.is_some() && initial.last_commit_duration.is_some());
    assert!(initial.last_cycle_finished_at.unwrap() >= initial.started_at);
    let blocked = blocked.expect("owned SQLite write did not expose an in-flight commit phase");
    assert!(blocked.poll_started_at.is_none() && blocked.last_poll_duration.is_some());
    assert!(blocked.commit_started_at.unwrap() >= blocked.last_cycle_finished_at.unwrap());
    assert_eq!(initial.worker_instance, blocked.worker_instance);
}
#[tokio::test]
async fn paused_analyzer_health_uses_the_actual_owned_pause_without_creating_samples() {
    let data = tempfile::tempdir().unwrap();
    let factory = Factory::new(false);
    let core = core(data.path(), factory.clone()).await;
    let manager = core.devices.as_ref().unwrap();
    let target = Target::new("devices", "a");
    let ticket = manager.admit(target.clone()).unwrap();
    manager
        .clone()
        .start_admitted(ticket, manager.public_gate(target), true)
        .await
        .unwrap();
    let samples = manager.acquisition_health().unwrap();
    let raw = core
        .archive()
        .unwrap()
        .list_sweeps("devices", None, 1)
        .await
        .unwrap();
    core.close().await.unwrap();
    assert_eq!(
        samples.len(),
        1,
        "actual paused analyzer was missing from the cache snapshot"
    );
    assert!(samples[0].intentionally_paused);
    assert_eq!(samples[0].completed_cycles, 0);
    assert!(raw.is_empty());
    assert_eq!(factory.opens.load(Ordering::Acquire), 0);
}
