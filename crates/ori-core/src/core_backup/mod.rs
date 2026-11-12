//! Trusted embedding recovery backup; no HTTP, CLI or research-worker admission.
use crate::{Core, CoreError, Result};
use ori_archive::{ArchiveError, BackupCancellation, BackupIoBudget, BackupOptions};
use ori_runtime::{
    qos::{BackgroundQos, BackgroundWorkGuard, QosError},
    worker::Cancellation,
};
use serde_json::Value;
use std::{path::Path, sync::Arc, time::Duration};

pub(crate) async fn run(
    core: &Arc<Core>,
    destination: &Path,
    options: BackupOptions,
) -> Result<Value> {
    run_inner(core, destination, options, Hooks::default()).await
}

#[derive(Default)]
struct Hooks {
    #[cfg(test)]
    first_io: Option<tests::HeldIo>,
}

/// Also retained by the actual native SQLite command. Losing the result waiter
/// cannot release this registration while its copy/cleanup callback survives.
struct Budget {
    qos: BackgroundQos,
    cancel: Cancellation,
    original: BackupCancellation,
    _registration: BackgroundWorkGuard,
    #[cfg(test)]
    first_io: std::sync::Mutex<Option<tests::HeldIo>>,
}
impl Budget {
    fn check(&self) -> ori_archive::Result<()> {
        if self.original.is_cancelled() || self.cancel.is_cancelled() {
            self.original.cancel();
            self.cancel.cancel();
            Err(ArchiveError::Closed)
        } else {
            Ok(())
        }
    }
}
impl BackupIoBudget for Budget {
    fn account(&self, mut bytes: u64) -> ori_archive::Result<()> {
        #[cfg(test)]
        if let Some(held) = self
            .first_io
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?
            .take()
        {
            let _ = held.entered.send(());
            held.release
                .recv_timeout(Duration::from_secs(5))
                .map_err(|_| ArchiveError::WorkerStopped)?;
        }
        self.check()?;
        while bytes != 0 {
            let chunk = bytes.min(8 * 1024 * 1024);
            if let Err(error) = self.qos.account_blocking(chunk, &self.cancel) {
                return match error {
                    QosError::Pressure | QosError::Cancelled => {
                        self.original.cancel();
                        self.cancel.cancel();
                        Err(ArchiveError::Closed)
                    }
                    QosError::Busy | QosError::InvalidCharge => Err(ArchiveError::QueryBudget),
                };
            }
            self.check()?;
            bytes -= chunk;
        }
        Ok(())
    }
}

struct Waiter {
    original: BackupCancellation,
    cancel: Cancellation,
    active: bool,
}
impl Drop for Waiter {
    fn drop(&mut self) {
        if self.active {
            self.original.cancel();
            self.cancel.cancel();
        }
    }
}

async fn run_inner(
    core: &Arc<Core>,
    destination: &Path,
    options: BackupOptions,
    hooks: Hooks,
) -> Result<Value> {
    let archive = core
        .archive
        .clone()
        .ok_or(CoreError::Invalid("backup requires the replay archive"))?;
    let admitted = core
        .admission
        .enter()
        .map_err(|_| CoreError::Invalid("backup admission is closed"))?;
    let background = core
        .background
        .as_ref()
        .ok_or(CoreError::Invalid("backup background owner is unavailable"))?;
    let cancel = Cancellation::default();
    let registration = background.qos.register(cancel.clone())?;
    let original = options.cancel.clone();
    let budget = Arc::new(Budget {
        qos: background.qos.clone(),
        cancel: cancel.clone(),
        original: original.clone(),
        _registration: registration,
        #[cfg(test)]
        first_io: std::sync::Mutex::new(hooks.first_io),
    });
    #[cfg(not(test))]
    let _ = hooks;
    let destination = destination.to_owned();
    let owner = core.clone();
    let mut stopped = core.shutdown.subscribe();
    let (sent, receipt) = tokio::sync::oneshot::channel();
    let mut waiter = Waiter {
        original: original.clone(),
        cancel: cancel.clone(),
        active: true,
    };
    core.manual_runtime.spawn(async move {
        // The originating Core, writer admission and native callback budget
        // remain owned even if the embedding runtime drops its result future.
        let _owner = owner;
        let result = {
            let operation = archive.backup_with_io_budget(&destination, options, budget.clone());
            tokio::pin!(operation);
            let mut tick = tokio::time::interval(Duration::from_millis(10));
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                if original.is_cancelled() || cancel.is_cancelled() || *stopped.borrow() {
                    original.cancel();
                    cancel.cancel();
                    // Cancellation is a request, not permission to drop native
                    // work or its publication/cleanup receipt.
                    break operation.await;
                }
                tokio::select! {
                    biased;
                    result = &mut operation => break result,
                    _ = stopped.changed() => {
                        original.cancel();
                        cancel.cancel();
                        break operation.await;
                    }
                    _ = tick.tick() => {}
                }
            }
        };
        drop(budget);
        drop(admitted);
        let _ = sent.send(result.map_err(CoreError::Archive));
    });
    let result = receipt.await.map_err(|_| CoreError::Task);
    if result.is_ok() {
        waiter.active = false;
    }
    result?
}

#[cfg(test)]
mod tests;
