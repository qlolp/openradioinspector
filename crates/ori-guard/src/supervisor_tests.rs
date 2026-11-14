use super::*;
use crate::test_common::Harness;
use std::{
    future::{Future, poll_fn},
    pin::Pin,
    task::Poll,
};

const FAILURE: &str = "controlled in-flight tick failure";

async fn parked_failure<'a>(
    harness: &'a Harness,
    id: &'a str,
) -> Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>> {
    let mut failure: Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>> =
        Box::pin(harness.service.fail_runtime(id, FAILURE));
    poll_fn(|cx| {
        match failure.as_mut().poll(cx) {
            Poll::Ready(result) => panic!("failure write was not held by coordinator: {result:?}"),
            Poll::Pending => {}
        }
        if harness
            .service
            .runtime_snapshot(id)
            .unwrap()
            .unwrap()
            .error
            .as_deref()
            == Some(FAILURE)
        {
            // fail_runtime publishes the volatile error after reading its
            // project snapshot, immediately before awaiting the coordinator.
            Poll::Ready(())
        } else {
            Poll::Pending
        }
    })
    .await;
    failure
}

#[tokio::test(flavor = "current_thread")]
async fn late_failure_preserves_committed_shutdown_intent_and_concurrent_metadata() {
    tokio::time::timeout(Duration::from_secs(5), async {
        let harness = Harness::new().await;
        let id = harness.prepared().await;
        harness
            .service
            .update_settings(&id, json!({"autostart":true}))
            .await
            .unwrap();
        harness.service.arm(&id, json!({})).await.unwrap();
        let coordinator = harness.archive.domain_coordinator();
        let held = coordinator.lock().await;
        let shutdown = harness.service.begin_shutdown().unwrap();
        while !harness
            .service
            .runtime_snapshot(&id)
            .unwrap()
            .unwrap()
            .stopped
        {
            tokio::task::yield_now().await;
        }
        // This current-thread task cannot resume until close_runtime's first
        // await: the coordinator, after setting stopped. It is queued first.
        // A pre-admitted tick failure can then retain a stale project while its
        // own write waits behind that actual shutdown transaction.
        let failure = parked_failure(&harness, &id).await;
        let mut current = harness.service.project(&id).await.unwrap();
        current["site"] = json!("Concurrent site +%/?");
        current["operator_note"] = json!({"opaque":"preserve current metadata"});
        harness
            .archive
            .put_monitoring("project", current)
            .await
            .unwrap();
        drop(held);
        let (failure_result, shutdown_result) = tokio::join!(failure, shutdown.completed());
        failure_result.unwrap();
        shutdown_result.unwrap();
        let saved = harness.service.project(&id).await.unwrap();
        let actual = (
            saved["resume_requested"].clone(),
            saved["site"].clone(),
            saved["operator_note"].clone(),
        );
        let stopped = harness
            .control
            .workers
            .lock()
            .unwrap()
            .iter()
            .all(|w| !w.alive);
        harness.close().await;
        assert!(stopped, "original controlled workers must actually stop");
        assert_eq!(
            actual,
            (
                json!(true),
                json!("Concurrent site +%/?"),
                json!({"opaque":"preserve current metadata"}),
            ),
            "a queued failure must not overwrite acknowledged shutdown intent or newer metadata"
        );
    })
    .await
    .unwrap();
}

#[tokio::test(flavor = "current_thread")]
async fn canonical_reload_failure_keeps_volatile_error_and_stops_owned_workers() {
    tokio::time::timeout(Duration::from_secs(5), async {
        let harness = Harness::new().await;
        let id = harness.prepared().await;
        harness.service.arm(&id, json!({})).await.unwrap();
        let coordinator = harness.archive.domain_coordinator();
        let held = coordinator.lock().await;
        let failure = parked_failure(&harness, &id).await;
        // The original project read succeeded. Only the canonical read/write
        // after coordinator admission now fails against the actual closed SQL
        // owner; cleanup must still stop the same already-owned workers.
        harness.archive.close().await.unwrap();
        drop(held);
        let failure_result = failure.await;
        let runtime = harness.service.runtime_snapshot(&id).unwrap().unwrap();
        let volatile = runtime.critical_alert.as_ref().unwrap()["volatile"].clone();
        let closed = harness.service.close().await;
        let stopped = harness
            .control
            .workers
            .lock()
            .unwrap()
            .iter()
            .all(|w| !w.alive);
        let stop_called = harness
            .control
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|call| call == &format!("stop:{}", runtime.sid));
        harness.archive.close().await.unwrap();
        drop(harness);
        assert!(failure_result.is_err());
        assert!(
            closed.is_err(),
            "SQL failure must remain visible through close"
        );
        assert!(runtime.stopped);
        assert_eq!(volatile, true);
        assert!(
            stop_called && stopped,
            "failure must still drain owned workers"
        );
    })
    .await
    .unwrap();
}
