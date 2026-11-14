use ori_control::{BoundExecution, ControlError, OwnerAuthority, OwnerReceipt, Target};
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};

struct OriginalAuthority {
    target: Target,
    worker: String,
    connection: String,
    epoch: Arc<AtomicU64>,
    original_epoch: u64,
}
impl OwnerAuthority for OriginalAuthority {
    fn target(&self) -> &Target {
        &self.target
    }
    fn worker_instance(&self) -> &str {
        &self.worker
    }
    fn connection_id(&self) -> &str {
        &self.connection
    }
    fn revalidate(&self) -> ori_control::Result<()> {
        if self.epoch.load(Ordering::SeqCst) == self.original_epoch {
            Ok(())
        } else {
            Err(ControlError::Denied)
        }
    }
}
fn owner(epoch: Arc<AtomicU64>) -> OwnerReceipt {
    OwnerReceipt::from_backend(Arc::new(OriginalAuthority {
        target: Target {
            session_id: "owned-session".into(),
            stream_id: "owned-stream".into(),
        },
        worker: "original-worker".into(),
        connection: "original-connection".into(),
        original_epoch: epoch.load(Ordering::SeqCst),
        epoch,
    }))
    .unwrap()
}
#[test]
fn a_receipt_retains_original_identity_and_never_becomes_current_after_another_epoch() {
    let epoch = Arc::new(AtomicU64::new(1));
    let receipt = owner(epoch.clone());
    assert_eq!(receipt.worker_instance(), "original-worker");
    assert_eq!(receipt.connection_id(), "original-connection");
    epoch.store(2, Ordering::SeqCst);
    assert!(matches!(receipt.revalidate(), Err(ControlError::Denied)));
    let current = owner(epoch.clone());
    assert!(current.revalidate().is_ok());
    assert!(matches!(receipt.revalidate(), Err(ControlError::Denied)));
}
#[test]
fn omitted_revision_binds_once_at_serial_execution_start_and_explicit_stale_fails() {
    let receipt = owner(Arc::new(AtomicU64::new(1)));
    let first = BoundExecution::bind(receipt.clone(), None, 4).unwrap();
    let second = BoundExecution::bind(receipt.clone(), None, 5).unwrap();
    assert_eq!(first.control_revision(), 4);
    assert_eq!(second.control_revision(), 5);
    assert!(matches!(
        BoundExecution::bind(receipt, Some(4), 5),
        Err(ControlError::Conflict(_))
    ));
    assert_eq!(first.control_revision(), 4);
}
#[test]
fn revocation_while_queued_prevents_even_revision_binding() {
    let epoch = Arc::new(AtomicU64::new(9));
    let receipt = owner(epoch.clone());
    epoch.store(10, Ordering::SeqCst);
    assert!(matches!(
        BoundExecution::bind(receipt, None, 100),
        Err(ControlError::Denied)
    ));
}

#[test]
fn weak_admission_identity_distinguishes_same_worker_credentials_without_retaining_them() {
    let first_epoch = Arc::new(AtomicU64::new(1));
    let second_epoch = Arc::new(AtomicU64::new(1));
    let first = owner(first_epoch.clone());
    let second = owner(second_epoch.clone());
    let key = first.downgrade();
    let clone = first.clone();
    assert!(first.same_admission(&clone));
    assert!(key.matches(&clone));
    assert!(!first.same_admission(&second));
    assert!(!key.matches(&second));
    second_epoch.store(2, Ordering::SeqCst);
    assert!(second.revalidate().is_err());
    assert!(key.upgrade().unwrap().revalidate().is_ok());
    first_epoch.store(2, Ordering::SeqCst);
    assert!(key.upgrade().unwrap().revalidate().is_err());
    drop(first);
    assert!(key.upgrade().is_some());
    drop(clone);
    assert!(key.upgrade().is_none());
}

#[test]
fn backend_error_mapping_uses_actual_taint_and_redacts_transport_or_policy_text() {
    use ori_instruments::InstrumentError;
    assert!(matches!(
        ori_control::classify_adapter_error(InstrumentError::PolicyDenied, false),
        ControlError::Denied
    ));
    assert!(matches!(
        ori_control::classify_adapter_error(InstrumentError::PolicyDenied, true),
        ControlError::Indeterminate(_)
    ));
    assert!(matches!(
        ori_control::classify_adapter_error(
            InstrumentError::UnsupportedCommand("owned fixture".into()),
            false
        ),
        ControlError::Unavailable(_)
    ));
    let error = ori_control::classify_adapter_error(
        InstrumentError::Io(std::io::Error::other("PRIVATE credential must not appear")),
        true,
    );
    assert!(!error.to_string().contains("PRIVATE"));
    let opaque = ControlError::Backend("PRIVATE backend detail".into());
    assert!(!format!("{opaque:?}: {opaque}").contains("PRIVATE"));
}
