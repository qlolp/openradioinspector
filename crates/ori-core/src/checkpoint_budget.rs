//! The same cooperative background ledger for actual native checkpoint I/O.
use crate::background::Background;
use ori_archive::{ArchiveError, CheckpointBudget, CheckpointIoPermit};
use ori_runtime::{
    qos::{BackgroundWorkGuard, QosError},
    worker::Cancellation,
};
use std::sync::Arc;

pub(crate) struct Budget(Arc<Background>);
impl Budget {
    pub fn new(background: Arc<Background>) -> Arc<Self> {
        Arc::new(Self(background))
    }
}
struct Permit {
    background: Arc<Background>,
    cancel: Cancellation,
    _registration: BackgroundWorkGuard,
}
impl CheckpointBudget for Budget {
    fn try_begin(&self) -> ori_archive::Result<Option<Arc<dyn CheckpointIoPermit>>> {
        let cancel = Cancellation::default();
        let registration = match self.0.qos.register(cancel.clone()) {
            Ok(guard) => guard,
            Err(QosError::Pressure | QosError::Busy) => return Ok(None),
            Err(_) => return Err(ArchiveError::WorkerStopped),
        };
        Ok(Some(Arc::new(Permit {
            background: self.0.clone(),
            cancel,
            _registration: registration,
        })))
    }
}
impl CheckpointIoPermit for Permit {
    fn account(&self, bytes: u64) -> ori_archive::Result<()> {
        if bytes > 64 * 1024 {
            return Err(ArchiveError::QueryBudget);
        }
        self.background
            .qos
            .account_blocking(bytes, &self.cancel)
            .map_err(|error| match error {
                QosError::Cancelled | QosError::Pressure => ArchiveError::Closed,
                _ => ArchiveError::WorkerStopped,
            })
    }
    fn cancel(&self) {
        self.cancel.cancel();
    }
}

#[cfg(test)]
mod tests {
    use crate::{Core, CoreConfig};
    use ori_archive::CheckpointBudget;

    #[tokio::test]
    async fn acquisition_pressure_defers_checkpoint_admission_without_blocking_ingestion() {
        let root = tempfile::tempdir().unwrap();
        let core = Core::open(CoreConfig::replay(root.path())).await.unwrap();
        core.background
            .as_ref()
            .unwrap()
            .qos
            .pressure(true)
            .await
            .unwrap();
        let archive = core.archive.as_ref().unwrap();
        let result = archive.checkpoint().await;
        let committed = archive
            .create_session(serde_json::json!({
                "id":"during-checkpoint-pressure", "name":"Synthetic only", "streams":[],
                "created_at":"2026-11-11T00:00:00Z"
            }))
            .await;
        core.close().await.unwrap();
        assert!(committed.is_ok());
        assert!(matches!(
            result,
            Err(ori_archive::ArchiveError::QueryBudget)
        ));
    }

    #[tokio::test]
    async fn original_registration_survives_cancel_until_its_actual_owner_drops() {
        let root = tempfile::tempdir().unwrap();
        let background = super::Background::open(root.path()).unwrap();
        let budget = super::Budget::new(background.clone());
        let permit = budget.try_begin().unwrap().unwrap();
        assert_eq!(background.qos.status().registered_work, 1);
        let qos = background.qos.clone();
        let pressure = tokio::spawn(async move { qos.pressure(true).await });
        while !background.qos.status().acquisition_pressure {
            tokio::task::yield_now().await;
        }
        assert!(matches!(
            permit.account(4096),
            Err(ori_archive::ArchiveError::Closed)
        ));
        assert!(!pressure.is_finished());
        assert_eq!(background.qos.status().registered_work, 1);
        drop(permit);
        pressure.await.unwrap().unwrap();
        assert!(budget.try_begin().unwrap().is_none());
        background.close().await.unwrap();
    }
}
