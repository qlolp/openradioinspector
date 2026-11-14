use ori_runtime::{qos::BackgroundQos, worker::Cancellation};
use std::time::Duration;

#[tokio::test]
async fn pressure_cancels_registered_work_and_waits_for_owned_join_guard() {
    let qos = BackgroundQos::default();
    let cancel = Cancellation::default();
    let guard = qos.register(cancel.clone()).unwrap();
    let barrier = {
        let qos = qos.clone();
        tokio::spawn(async move { qos.pressure(true).await })
    };
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert!(cancel.is_cancelled());
    assert!(
        !barrier.is_finished(),
        "cancel signal alone is not child completion"
    );
    assert!(qos.register(Cancellation::default()).is_err());
    drop(guard);
    barrier.await.unwrap().unwrap();
    qos.pressure(false).await.unwrap();
    assert!(qos.register(Cancellation::default()).is_ok());
}

#[tokio::test]
async fn logical_budget_is_bounded_and_never_claims_physical_io() {
    let qos = BackgroundQos::default();
    let cancel = Cancellation::default();
    qos.account(65536, &cancel).await.unwrap();
    let status = qos.status();
    assert_eq!(status.logical_bytes, 65536);
    assert_eq!(status.logical_bytes_per_second, 20 * 1024 * 1024);
    assert!(!status.physical_io_enforced);
    cancel.cancel();
    assert!(qos.account(1, &cancel).await.is_err());
}

#[tokio::test]
async fn repeated_pressure_requests_share_the_same_drain_barrier() {
    let qos = BackgroundQos::default();
    let cancel = Cancellation::default();
    let guard = qos.register(cancel.clone()).unwrap();
    let first = {
        let qos = qos.clone();
        tokio::spawn(async move { qos.pressure(true).await })
    };
    while !cancel.is_cancelled() {
        tokio::task::yield_now().await;
    }
    let second = {
        let qos = qos.clone();
        tokio::spawn(async move { qos.pressure(true).await })
    };
    tokio::task::yield_now().await;
    drop(guard);
    first.await.unwrap().unwrap();
    second.await.unwrap().unwrap();
}
