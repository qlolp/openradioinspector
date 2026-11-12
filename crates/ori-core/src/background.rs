//! One Core-owned cooperative timeline, retained through derivative cleanup.
use crate::{CoreError, Result};
use ori_runtime::qos::BackgroundQos;
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex, OnceLock},
};
use tokio::sync::watch;

type Completion = watch::Receiver<Option<std::result::Result<(), &'static str>>>;
pub(crate) struct Background {
    pub qos: BackgroundQos,
    pub root: PathBuf,
    stage: Mutex<Option<tempfile::TempDir>>,
    runtime: tokio::runtime::Handle,
    pressure: OnceLock<Completion>,
    closed: OnceLock<Completion>,
}
impl Background {
    pub fn open(data: &Path) -> Result<Arc<Self>> {
        let stage = tempfile::Builder::new()
            .prefix(".ori-background-")
            .tempdir_in(data)?;
        let root = stage.path().canonicalize()?;
        #[cfg(unix)]
        let qos = BackgroundQos::with_shared_io(root.join("io"))?;
        #[cfg(not(unix))]
        let qos = BackgroundQos::default();
        Ok(Arc::new(Self {
            qos,
            root,
            stage: Mutex::new(Some(stage)),
            runtime: tokio::runtime::Handle::current(),
            pressure: OnceLock::new(),
            closed: OnceLock::new(),
        }))
    }
    pub fn begin_shutdown(self: &Arc<Self>) -> Completion {
        self.pressure
            .get_or_init(|| {
                let (sent, receiver) = watch::channel(None);
                let own = self.clone();
                self.runtime.spawn(async move {
                    let outcome = own
                        .qos
                        .pressure(true)
                        .await
                        .map_err(|_| "background pressure drain failed");
                    sent.send_replace(Some(outcome));
                });
                receiver
            })
            .clone()
    }
    /// Called after all consumers have closed. The owned task survives a
    /// cancelled waiter; the ledger and roots remain until their real JOIN.
    pub async fn close(self: &Arc<Self>) -> Result<()> {
        let mut receiver = self
            .closed
            .get_or_init(|| {
                let (sent, receiver) = watch::channel(None);
                let own = self.clone();
                self.runtime.spawn(async move {
                    let outcome = async {
                        wait(own.begin_shutdown()).await?;
                        own.qos
                            .close_shared_io()
                            .await
                            .map_err(|_| "background ledger cleanup failed")?;
                        let stage = own
                            .stage
                            .lock()
                            .map_err(|_| "background storage state poisoned")?
                            .take();
                        if let Some(stage) = stage {
                            tokio::task::spawn_blocking(move || stage.close())
                                .await
                                .map_err(|_| "background cleanup JOIN failed")?
                                .map_err(|_| "background directory cleanup failed")?;
                        }
                        Ok(())
                    }
                    .await;
                    sent.send_replace(Some(outcome));
                });
                receiver
            })
            .clone();
        loop {
            if let Some(outcome) = *receiver.borrow() {
                return outcome.map_err(CoreError::Invalid);
            }
            receiver
                .changed()
                .await
                .map_err(|_| CoreError::Invalid("background cleanup completion lost"))?;
        }
    }
}
async fn wait(mut receiver: Completion) -> std::result::Result<(), &'static str> {
    loop {
        if let Some(outcome) = *receiver.borrow() {
            return outcome;
        }
        receiver
            .changed()
            .await
            .map_err(|_| "background pressure completion lost")?;
    }
}
impl Drop for Background {
    fn drop(&mut self) {
        if let Ok(stage) = self.stage.get_mut()
            && let Some(stage) = stage.take()
        {
            // An abandoned lifecycle has not proved that file users JOINED.
            std::mem::forget(stage);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ori_runtime::worker::Cancellation;

    #[tokio::test]
    async fn cancelled_close_waiter_retains_roots_until_the_registered_owner_joins() {
        let data = tempfile::tempdir().unwrap();
        let background = Background::open(data.path()).unwrap();
        let root = background.root.clone();
        let cancellation = Cancellation::default();
        let guard = background.qos.register(cancellation.clone()).unwrap();
        let (release, blocked) = std::sync::mpsc::channel();
        let (entered, ready) = tokio::sync::oneshot::channel();
        let owner = tokio::spawn(async move {
            tokio::task::spawn_blocking(move || {
                entered.send(()).unwrap();
                blocked.recv().unwrap();
            })
            .await
            .unwrap();
            drop(guard);
        });
        ready.await.unwrap();
        let held = background.clone();
        let close = tokio::spawn(async move { held.close().await });
        while !cancellation.is_cancelled() {
            tokio::task::yield_now().await;
        }
        let pending = !close.is_finished();
        close.abort();
        let cancelled = close.await.unwrap_err().is_cancelled();
        let retained = root.is_dir();
        release.send(()).unwrap();
        owner.await.unwrap();
        background.close().await.unwrap();
        assert!(pending && cancelled && retained);
        assert!(!root.exists());
    }
}
