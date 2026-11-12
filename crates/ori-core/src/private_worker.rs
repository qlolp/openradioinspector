//! Trusted RPC framing and ownership; never an HTTP-supplied capability.
use crate::Core;
use ori_runtime::{
    Error, Result,
    qos::QosError,
    worker::{Cancellation, ResearchPermit, WorkerResult, WorkerSpec},
};
use serde_json::Value;
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};

/// The original domain owner may retain heavy admission beyond child reap,
/// through its final canonical write and owned snapshot cleanup.
pub(crate) struct RetainedPermit(Mutex<Option<ResearchPermit>>);
impl RetainedPermit {
    pub(crate) fn new(permit: ResearchPermit) -> Arc<Self> {
        Arc::new(Self(Mutex::new(Some(permit))))
    }
}

pub(crate) async fn run_retaining(
    core: &Core,
    retained: Arc<RetainedPermit>,
    spec: WorkerSpec,
    request: Value,
    cancel: Cancellation,
    check: Option<Check>,
    class: IoClass,
) -> Result<WorkerResult> {
    let permit = retained
        .0
        .lock()
        .map_err(|_| Error::Protocol("retained reservation".into()))?
        .take()
        .ok_or(Error::Busy)?;
    run_owned(
        core,
        Reservation {
            permit: Some(permit),
            retained: Some(retained),
        },
        spec,
        request,
        cancel,
        check,
        class,
    )
    .await
}

struct Reservation {
    permit: Option<ResearchPermit>,
    retained: Option<Arc<RetainedPermit>>,
}
impl Reservation {
    fn restore(&mut self) -> Result<()> {
        let Some(retained) = &self.retained else {
            return Ok(());
        };
        let Some(permit) = self.permit.take() else {
            return Ok(());
        };
        match retained.0.lock() {
            Ok(mut slot) if slot.is_none() => {
                *slot = Some(permit);
                Ok(())
            }
            _ => {
                // An unconfirmed reservation handoff must never make another
                // child eligible while domain/SQL ownership may still survive.
                std::mem::forget(permit);
                Err(Error::Protocol("retained reservation handoff".into()))
            }
        }
    }
}
impl Drop for Reservation {
    fn drop(&mut self) {
        let _ = self.restore();
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum IoClass {
    MemoryOnly,
    Files,
}
pub(crate) type Check = Arc<dyn Fn() -> Result<()> + Send + Sync>;
fn protocol<T>() -> Result<T> {
    Err(Error::Protocol("private shared I/O receipt".into()))
}
fn consume_receipt(response: &mut Value, injected: bool, class: IoClass) -> Result<()> {
    if !injected || response["ok"] != true {
        return if response.get("shared_io_receipt").is_none() {
            Ok(())
        } else {
            protocol()
        };
    }
    let receipt = response
        .get("shared_io_receipt")
        .and_then(Value::as_object)
        .ok_or(Error::Protocol("missing private shared I/O receipt".into()))?;
    if receipt.len() != 4 || receipt.get("version").and_then(Value::as_u64) != Some(1) {
        return protocol();
    }
    let bytes = receipt
        .get("accounted_bytes")
        .and_then(Value::as_u64)
        .ok_or(Error::Protocol("private I/O byte counter".into()))?;
    let charges = receipt
        .get("charges")
        .and_then(Value::as_u64)
        .ok_or(Error::Protocol("private I/O charge counter".into()))?;
    let maximum = receipt
        .get("max_charge_bytes")
        .and_then(Value::as_u64)
        .ok_or(Error::Protocol("private I/O charge bound".into()))?;
    if class == IoClass::MemoryOnly && (bytes != 0 || charges != 0 || maximum != 0)
        || maximum > 65536
        || bytes == 0 && (charges != 0 || maximum != 0)
        || bytes > 0
            && (charges == 0
                || maximum == 0
                || bytes < maximum
                || charges
                    .checked_mul(maximum)
                    .is_none_or(|upper| bytes > upper))
    {
        return protocol();
    }
    response
        .as_object_mut()
        .ok_or(Error::Protocol("private worker envelope".into()))?
        .remove("shared_io_receipt");
    Ok(())
}
fn qos(error: QosError) -> Error {
    match error {
        QosError::Busy => Error::Busy,
        QosError::Pressure | QosError::Cancelled => Error::Cancelled,
        QosError::InvalidCharge => Error::ResourceLimit,
    }
}
struct Waiter {
    cancel: Cancellation,
    active: bool,
}
impl Drop for Waiter {
    fn drop(&mut self) {
        if self.active {
            self.cancel.cancel();
        }
    }
}

/// The independently owned task retains registration and the original heavy
/// reservation through encoding and actual child/IPC JOIN. Dropping this waiter
/// requests cancellation; it cannot drop the owner's guards prematurely.
pub(crate) async fn run(
    core: &Core,
    permit: ResearchPermit,
    spec: WorkerSpec,
    request: Value,
    cancel: Cancellation,
    check: Option<Check>,
    class: IoClass,
) -> Result<WorkerResult> {
    run_owned(
        core,
        Reservation {
            permit: Some(permit),
            retained: None,
        },
        spec,
        request,
        cancel,
        check,
        class,
    )
    .await
}
async fn run_owned(
    core: &Core,
    mut reservation: Reservation,
    spec: WorkerSpec,
    mut request: Value,
    cancel: Cancellation,
    check: Option<Check>,
    class: IoClass,
) -> Result<WorkerResult> {
    if *core.shutdown.borrow() || cancel.is_cancelled() {
        return Err(Error::Cancelled);
    }
    if let Some(check) = &check {
        check()?;
    }
    let background = core
        .background
        .as_ref()
        .ok_or(Error::Unsafe("background owner unavailable".into()))?;
    let guard = background.qos.register(cancel.clone()).map_err(qos)?;
    let shared = background.qos.worker_io();
    let trusted = spec.args == ["-I", "-B", "-m", "radioinspector.research_rpc"]
        || spec.args == ["-I", "-B", "-m", "radioinspector.legacy_rpc"];
    let expected = if spec.args == ["-I", "-B", "-m", "radioinspector.legacy_rpc"] {
        IoClass::Files
    } else {
        IoClass::MemoryOnly
    };
    if trusted && class != expected {
        return Err(Error::Unsafe("private worker I/O class mismatch".into()));
    }
    let object = request
        .as_object_mut()
        .ok_or(Error::Protocol("private request object".into()))?;
    if object.contains_key("shared_io") {
        return Err(Error::Unsafe(
            "private capability must originate at supervisor".into(),
        ));
    }
    let injected = trusted && shared.is_some();
    if injected {
        object.insert(
            "shared_io".into(),
            serde_json::to_value(shared.expect("checked private capability"))
                .map_err(|_| Error::Protocol("private capability encoding".into()))?,
        );
    }
    let (sent, receiver) = tokio::sync::oneshot::channel();
    let mut stopped = core.shutdown.subscribe();
    let mut waiter = Waiter {
        cancel: cancel.clone(),
        active: true,
    };
    tokio::spawn(async move {
        let _guard = guard;
        let outcome = async {
            let bytes = tokio::task::spawn_blocking(move || ori_contracts::to_json_vec(&request)).await
                .map_err(|_| Error::Protocol("request encoding JOIN".into()))?
                .map_err(|_| Error::Protocol("request encoding".into()))?;
            if bytes.len() > 16 * 1024 * 1024 { return Err(Error::Protocol("private request byte limit".into())); }
            if cancel.is_cancelled() || *stopped.borrow() { return Err(Error::Cancelled); }
            if let Some(check) = &check { check()?; }
            let permit=reservation.permit.take().ok_or(Error::Protocol("missing original reservation".into()))?;
            let run = permit.run_retained(spec, bytes, cancel.clone());
            tokio::pin!(run);
            let mut interval = tokio::time::interval(Duration::from_millis(10));
            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            let mut denied = None;
            let (returned, result) = loop {
                tokio::select! {
                    result=&mut run=>break result,
                    _=interval.tick()=> {
                        if cancel.is_cancelled() { denied=Some(Error::Cancelled); }
                        if let Some(check)=&check && let Err(error)=check() { denied=Some(error); }
                        if denied.is_some() { cancel.cancel(); break run.await; }
                    }
                    _=stopped.changed()=> { cancel.cancel(); denied=Some(Error::Cancelled); break run.await; }
                }
            };
            reservation.permit=Some(returned);
            if let Some(error) = denied { return Err(error); }
            let mut result = result?;
            if cancel.is_cancelled() || *stopped.borrow() { return Err(Error::Cancelled); }
            if let Some(check) = &check { check()?; }
            consume_receipt(&mut result.response, injected, class)?;
            Ok(result)
        }.await;
        let outcome = reservation.restore().and(outcome);
        let _ = sent.send(outcome);
    });
    let outcome = receiver
        .await
        .map_err(|_| Error::Protocol("private worker completion lost".into()));
    waiter.active = false;
    outcome?
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn success(receipt: serde_json::Value) -> serde_json::Value {
        json!({"version":1,"request_id":"synthetic","operation":"analysis","ok":true,"result":{"literal":{"$serde_json::private::Number":"1"},"large":18446744073709551616u128},"shared_io_receipt":receipt})
    }
    #[test]
    fn private_receipts_are_exact_and_consumed_only_for_the_original_injected_capability() {
        let zero = json!({"version":1,"accounted_bytes":0,"charges":0,"max_charge_bytes":0});
        let mut response = success(zero.clone());
        let original = ori_contracts::to_json_vec(&response["result"]).unwrap();
        consume_receipt(&mut response, true, IoClass::MemoryOnly).unwrap();
        assert_eq!(response.as_object().unwrap().len(), 5);
        assert_eq!(
            ori_contracts::to_json_vec(&response["result"]).unwrap(),
            original
        );
        assert!(consume_receipt(&mut success(zero.clone()), false, IoClass::MemoryOnly).is_err());
        assert!(consume_receipt(&mut response.clone(), true, IoClass::MemoryOnly).is_err());
        for receipt in [
            json!({"version":2,"accounted_bytes":0,"charges":0,"max_charge_bytes":0}),
            json!({"version":1,"accounted_bytes":0,"charges":0,"max_charge_bytes":0,"unknown":true}),
            json!({"version":1,"accounted_bytes":1,"charges":0,"max_charge_bytes":0}),
            json!({"version":1,"accounted_bytes":1,"charges":1,"max_charge_bytes":65537}),
            json!({"version":1,"accounted_bytes":1,"charges":18446744073709551615u64,"max_charge_bytes":65536}),
        ] {
            assert!(consume_receipt(&mut success(receipt), true, IoClass::Files).is_err());
        }
        let actual =
            json!({"version":1,"accounted_bytes":131072,"charges":2,"max_charge_bytes":65536});
        assert!(consume_receipt(&mut success(actual.clone()), true, IoClass::MemoryOnly).is_err());
        consume_receipt(&mut success(actual), true, IoClass::Files).unwrap();
        let mut failure = json!({"version":1,"request_id":"synthetic","operation":"analysis","ok":false,"error":{"code":"shared_io_unavailable"}});
        consume_receipt(&mut failure, true, IoClass::MemoryOnly).unwrap();
        failure["shared_io_receipt"] = zero;
        assert!(consume_receipt(&mut failure, true, IoClass::Files).is_err());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn original_domain_owner_retains_reservation_after_worker_success_or_cancellation() {
        for cancel_child in [false, true] {
            let data = tempfile::tempdir().unwrap();
            let core = crate::Core::open(crate::CoreConfig::replay(data.path()))
                .await
                .unwrap();
            let marker = data.path().join("retained-owned.pid");
            let retained = RetainedPermit::new(core.research_pool.try_reserve().unwrap());
            let spec = WorkerSpec {
                program: std::env::var_os("ORI_TEST_PYTHON")
                    .map(Into::into)
                    .unwrap_or_else(|| "/usr/bin/python3".into()),
                args: vec![
                    format!(
                        "{}/tests/private_worker_echo.py",
                        env!("CARGO_MANIFEST_DIR")
                    ),
                    marker.to_str().unwrap().into(),
                    if cancel_child { "block" } else { "success" }.into(),
                ],
                environment: std::collections::BTreeMap::new(),
                deadline_ms: 10_000,
                require_hard_memory: false,
                linux_cgroup: None,
            };
            let own = core.clone();
            let held = retained.clone();
            let cancel = Cancellation::default();
            let child_cancel = cancel.clone();
            let task = tokio::spawn(async move {
                run_retaining(
                    &own,
                    held,
                    spec,
                    json!({"version":1,"request_id":"retained","operation":"analysis"}),
                    child_cancel,
                    None,
                    IoClass::MemoryOnly,
                )
                .await
            });
            tokio::time::timeout(Duration::from_secs(3), async {
                while !marker.exists() {
                    tokio::time::sleep(Duration::from_millis(2)).await;
                }
            })
            .await
            .unwrap();
            if cancel_child {
                cancel.cancel();
            }
            let result = task.await.unwrap();
            let still_owned = matches!(core.research_pool.try_reserve(), Err(Error::Busy));
            drop(retained);
            let released = core.research_pool.try_reserve().is_ok();
            core.close().await.unwrap();
            assert_eq!(result.is_err(), cancel_child);
            assert!(
                still_owned && released,
                "child completion released a domain owner's original heavy reservation"
            );
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn abandoned_waiter_cancels_owned_child_and_pressure_waits_until_process_and_ipc_join() {
        let data = tempfile::tempdir().unwrap();
        let core = crate::Core::open(crate::CoreConfig::replay(data.path()))
            .await
            .unwrap();
        let marker = data.path().join("owned.pid");
        let spec = WorkerSpec {
            program: std::env::var_os("ORI_TEST_PYTHON")
                .map(Into::into)
                .unwrap_or_else(|| "/usr/bin/python3".into()),
            args: vec![
                format!(
                    "{}/tests/private_worker_echo.py",
                    env!("CARGO_MANIFEST_DIR")
                ),
                marker.to_str().unwrap().into(),
                "block".into(),
            ],
            environment: std::collections::BTreeMap::new(),
            deadline_ms: 10_000,
            require_hard_memory: false,
            linux_cgroup: None,
        };
        let permit = core.research_pool.try_reserve().unwrap();
        let own = core.clone();
        let task = tokio::spawn(async move {
            run(
                &own,
                permit,
                spec,
                json!({"version":1,"request_id":"owned","operation":"analysis"}),
                Cancellation::default(),
                None,
                IoClass::MemoryOnly,
            )
            .await
        });
        let pid = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if let Ok(bytes) = std::fs::read(&marker) {
                    break ori_contracts::parse_json_value(&bytes, 1024).unwrap()["pid"]
                        .as_i64()
                        .unwrap() as i32;
                }
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
        })
        .await
        .unwrap();
        task.abort();
        let cancelled = match task.await {
            Err(error) => error.is_cancelled(),
            Ok(_) => false,
        };
        core.background
            .as_ref()
            .unwrap()
            .qos
            .pressure(true)
            .await
            .unwrap();
        let registered = core
            .background
            .as_ref()
            .unwrap()
            .qos
            .status()
            .registered_work;
        let reaped = unsafe { libc::kill(pid, 0) } == -1
            && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH);
        let reserved = core.research_pool.try_reserve().is_ok();
        core.close().await.unwrap();
        assert_eq!(registered, 0);
        assert!(cancelled && reaped && reserved);
    }
}
