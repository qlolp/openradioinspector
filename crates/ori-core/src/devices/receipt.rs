//! Cache-only retention of one existing worker for an admitted domain effect.
//! No constructor accepts worker IDs, no transport is opened and no revision is
//! rebound here. TuneOutcome remains the post-ACK revision proof.
use super::*;
use std::sync::atomic::AtomicU8;

#[derive(Default)]
struct TuneTaskState {
    closing: bool,
    failed: bool,
    tasks: tokio::task::JoinSet<()>,
}
pub(super) struct TuneTasks {
    state: Mutex<TuneTaskState>,
    completion: Completion,
    runtime: tokio::runtime::Handle,
    limit: usize,
}
impl TuneTasks {
    pub(super) fn new(queue_capacity: usize) -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(TuneTaskState::default()),
            completion: Completion::default(),
            runtime: tokio::runtime::Handle::current(),
            limit: queue_capacity + 1,
        })
    }
    pub(super) fn enqueue(
        self: &Arc<Self>,
        worker: Arc<Worker>,
        frequency: u64,
        mode: Option<String>,
        gate: ExecutionGate,
        stop_timeout: Duration,
    ) -> DeviceResult<oneshot::Receiver<DeviceResult<TuneOutcome>>> {
        let mut state = self.state.lock().map_err(|_| DeviceError::Closed)?;
        if state.closing {
            return Err(DeviceError::Closed);
        }
        while let Some(result) = state.tasks.try_join_next() {
            state.failed |= result.is_err();
        }
        if state.failed {
            return Err(DeviceError::Worker("Original tune monitor failed".into()));
        }
        if state.tasks.len() >= self.limit {
            return Err(DeviceError::QueueFull);
        }
        worker.live()?;
        gate.check(Some(&worker))?;
        let (response, received) = oneshot::channel();
        let stage = Arc::new(TuneStage::default());
        worker
            .commands
            .try_send(Command::Tune {
                frequency,
                mode,
                gate: gate.clone(),
                stage: stage.clone(),
                response,
            })
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => DeviceError::QueueFull,
                mpsc::error::TrySendError::Closed(_) => DeviceError::Closed,
            })?;
        let (send, caller) = oneshot::channel();
        // The registry owns the monitor, not the caller waiting on `caller`.
        // Queueing and registration share this short lock with close admission.
        state.tasks.spawn_on(
            async move {
                let result = worker
                    .monitor_tune(gate, received, stage, stop_timeout)
                    .await;
                let _ = send.send(result);
            },
            &self.runtime,
        );
        Ok(caller)
    }
    pub(super) fn begin_close(self: &Arc<Self>) {
        let (mut tasks, mut failed) = {
            let mut state = self.state.lock().expect("tune registry poisoned");
            if state.closing {
                return;
            }
            state.closing = true;
            (std::mem::take(&mut state.tasks), state.failed)
        };
        let owned = self.clone();
        self.runtime.spawn(async move {
            while let Some(result) = tasks.join_next().await {
                failed |= result.is_err();
            }
            owned.completion.finish(if failed {
                Err(DeviceError::Worker(
                    "Original tune monitor JOIN failed".into(),
                ))
            } else {
                Ok(())
            });
        });
    }
    pub(super) async fn wait(self: &Arc<Self>, timeout: Duration) -> DeviceResult<()> {
        self.begin_close();
        tokio::time::timeout(timeout, self.completion.wait())
            .await
            .map_err(|_| DeviceError::Worker("Original tune monitors remain active".into()))?
    }
    pub(super) fn okay(&self) -> bool {
        self.completion.okay()
    }
}

#[derive(Default)]
pub(super) struct TuneStage(AtomicU8);
impl TuneStage {
    pub(super) fn start(&self) -> bool {
        self.0
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }
    pub(super) fn discard_queued(&self) -> bool {
        self.0
            .try_update(Ordering::AcqRel, Ordering::Acquire, |state| {
                matches!(state, 0 | 1).then_some(2)
            })
            .is_ok()
    }
    pub(super) fn begin_io(&self) -> bool {
        self.0
            .compare_exchange(1, 3, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
            || self.0.load(Ordering::Acquire) == 3
    }
    pub(super) fn executing(&self) -> bool {
        self.0.load(Ordering::Acquire) == 3
    }
    pub(super) fn was_executed(&self) -> bool {
        matches!(self.0.load(Ordering::Acquire), 3 | 4)
    }
    pub(super) fn finish(&self) {
        let finished = if self.was_executed() { 4 } else { 5 };
        self.0.store(finished, Ordering::Release);
    }
}

#[derive(Clone)]
pub(crate) struct RetainedWorkerHandle {
    target: Target,
    worker: Arc<Worker>,
    gate: ExecutionGate,
}
impl RetainedWorkerHandle {
    /// Only the device owner can supply this Arc. It authorizes cleanup alone,
    /// never an operational command or a refreshed policy grant.
    pub(super) fn cleanup(worker: Arc<Worker>) -> Self {
        let target = Target::new(&worker.stream.session_id, &worker.stream.id);
        let gate = ExecutionGate::new(|| Err(DeviceError::Denied), Some(worker.instance.clone()));
        Self {
            target,
            worker,
            gate,
        }
    }
    // The actual actor remains private to the devices module. Domain consumers
    // receive only this opaque handle and its original expected execution gate.
    pub(super) fn original_worker(&self) -> Arc<Worker> {
        self.worker.clone()
    }
    pub(crate) fn target(&self) -> &Target {
        &self.target
    }
    pub(crate) fn worker_instance(&self) -> &str {
        &self.worker.instance
    }
    pub(crate) fn connection_id(&self) -> &str {
        &self.worker.instance
    }
    pub(crate) fn check(&self) -> DeviceResult<()> {
        self.worker.live()?;
        self.gate.check(Some(&self.worker))?;
        let status = self.worker.snapshot();
        if !status.alive
            || !matches!(status.state.as_str(), "ready" | "running" | "degraded")
            || status.current["restart_required"] == true
            || status.current["closed"] == true
        {
            return Err(DeviceError::Denied);
        }
        Ok(())
    }
    /// The same supplied callback and immutable expected worker; no new permit.
    pub(crate) fn execution_gate(&self) -> ExecutionGate {
        self.gate.clone()
    }
}
impl DeviceManager {
    /// Keep cleanup authority for the nonce admitted by this manager even if
    /// its operational callback is revoked immediately after the ready ACK.
    pub(super) fn retain_started_worker(
        self: &Arc<Self>,
        target: &Target,
        identity: &str,
        mut gate: ExecutionGate,
    ) -> DeviceResult<RetainedWorkerHandle> {
        let worker = self
            .registry
            .lock()
            .map_err(|_| DeviceError::Closed)?
            .workers
            .get(target)
            .filter(|worker| worker.instance == identity)
            .cloned()
            .ok_or(DeviceError::Denied)?;
        gate.expected_worker = Some(worker.instance.clone());
        Ok(RetainedWorkerHandle {
            target: target.clone(),
            worker,
            gate,
        })
    }

    /// Trusted cleanup of the captured owner, including after revocation.
    /// No lookup by target and no fresh operational authorization occurs.
    pub(crate) async fn stop_retained_worker(
        self: &Arc<Self>,
        handle: &RetainedWorkerHandle,
    ) -> DeviceResult<WorkerStatus> {
        if !Weak::ptr_eq(&handle.worker.manager, &Arc::downgrade(self)) {
            return Err(DeviceError::Denied);
        }
        let status = handle
            .worker
            .stop_and_join(self.options.stop_timeout)
            .await?;
        handle
            .worker
            .tune_tasks
            .wait(self.options.stop_timeout)
            .await?;
        Ok(status)
    }
    pub(crate) fn capture_worker(
        self: &Arc<Self>,
        target: &Target,
        mut gate: ExecutionGate,
    ) -> DeviceResult<RetainedWorkerHandle> {
        let worker = {
            let registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
            if registry.closed {
                return Err(DeviceError::Closed);
            }
            registry
                .workers
                .get(target)
                .cloned()
                .ok_or_else(|| DeviceError::Worker("Original worker is not connected".into()))?
        };
        gate.check(Some(&worker))?;
        gate.expected_worker = Some(worker.instance.clone());
        let handle = RetainedWorkerHandle {
            target: target.clone(),
            worker,
            gate,
        };
        handle.check()?;
        Ok(handle)
    }
}
impl Worker {
    async fn monitor_tune(
        self: Arc<Self>,
        gate: ExecutionGate,
        mut received: oneshot::Receiver<DeviceResult<TuneOutcome>>,
        stage: Arc<TuneStage>,
        timeout: Duration,
    ) -> DeviceResult<TuneOutcome> {
        loop {
            tokio::select! {
                result=&mut received=>{
                    let result=result.map_err(|_|DeviceError::Worker("Worker stopped before command completion".into()))?;
                    if self.live().and_then(|()|gate.check(Some(&self))).is_err(){
                        if stage.was_executed(){self.stop_and_join(timeout).await?;}
                        return Err(DeviceError::Denied);
                    }
                    if result.is_err()&&self.native.as_ref().is_some_and(|native|native.current()["restart_required"]==true){self.stop_and_join(timeout).await?;}
                    return result;
                },
                _=tokio::time::sleep(Duration::from_millis(25))=>{
                    if self.live().and_then(|()|gate.check(Some(&self))).is_err(){
                        if stage.discard_queued(){return Err(DeviceError::Denied);}
                        if stage.executing(){self.stop_and_join(timeout).await?;let _=received.await;return Err(DeviceError::Denied);}
                    }
                },
            }
        }
    }
    pub(super) async fn apply_tune_command(
        self: &Arc<Self>,
        adapter: &mut Adapter,
        frequency: u64,
        mode: Option<String>,
        gate: ExecutionGate,
        stage: Arc<TuneStage>,
        response: oneshot::Sender<DeviceResult<TuneOutcome>>,
    ) {
        if !stage.start() {
            let _ = response.send(Err(DeviceError::Denied));
            return;
        }
        self.active_io.store(true, Ordering::Release);
        let result = self
            .apply_tune(adapter, frequency, mode, gate, stage.clone())
            .await;
        self.active_io.store(false, Ordering::Release);
        stage.finish();
        let _ = response.send(result);
    }

    pub(super) async fn stop_and_join(
        self: &Arc<Self>,
        timeout: Duration,
    ) -> DeviceResult<WorkerStatus> {
        self.request_stop();
        tokio::time::timeout(timeout, self.completion.wait())
            .await
            .map_err(|_| {
                DeviceError::Worker(
                    "Original worker cleanup deadline exceeded; ownership retained".into(),
                )
            })??;
        if let Some(released) = &self.released {
            tokio::time::timeout(timeout, released.wait())
                .await
                .map_err(|_| {
                    DeviceError::Worker("Original transport drop is not confirmed".into())
                })?;
        }
        Ok(self.snapshot())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Core, CoreConfig};
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    struct Factory {
        opens: AtomicU64,
        drops: Arc<AtomicU64>,
        peer: Mutex<Vec<tokio::task::JoinHandle<()>>>,
        block: Arc<AtomicBool>,
        entered: Arc<tokio::sync::Semaphore>,
        release: Arc<tokio::sync::Semaphore>,
    }
    struct Owned {
        io: tokio::io::DuplexStream,
        drops: Arc<AtomicU64>,
    }
    impl Drop for Owned {
        fn drop(&mut self) {
            self.drops.fetch_add(1, Ordering::AcqRel);
        }
    }
    impl tokio::io::AsyncRead for Owned {
        fn poll_read(
            self: Pin<&mut Self>,
            cx: &mut std::task::Context<'_>,
            buf: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<std::io::Result<()>> {
            Pin::new(&mut self.get_mut().io).poll_read(cx, buf)
        }
    }
    impl tokio::io::AsyncWrite for Owned {
        fn poll_write(
            self: Pin<&mut Self>,
            cx: &mut std::task::Context<'_>,
            buf: &[u8],
        ) -> std::task::Poll<std::io::Result<usize>> {
            Pin::new(&mut self.get_mut().io).poll_write(cx, buf)
        }
        fn poll_flush(
            self: Pin<&mut Self>,
            cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<std::io::Result<()>> {
            Pin::new(&mut self.get_mut().io).poll_flush(cx)
        }
        fn poll_shutdown(
            self: Pin<&mut Self>,
            cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<std::io::Result<()>> {
            Pin::new(&mut self.get_mut().io).poll_shutdown(cx)
        }
    }
    impl TransportFactory for Factory {
        fn identity(&self, _: &DeviceStream) -> DeviceResult<String> {
            Ok("owned-generic-receiver".into())
        }
        fn open<'a>(&'a self, _: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
            Box::pin(async move {
                self.opens.fetch_add(1, Ordering::AcqRel);
                let (host, device) = tokio::io::duplex(512);
                let block = self.block.clone();
                let entered = self.entered.clone();
                let release = self.release.clone();
                let peer = tokio::spawn(async move {
                    let mut device = BufReader::new(device);
                    loop {
                        let mut command = vec![];
                        if device.read_until(b'\r', &mut command).await.unwrap() == 0 {
                            break;
                        }
                        let command = std::str::from_utf8(&command[..command.len() - 1]).unwrap();
                        if command == "RF0148.50000" && block.swap(false, Ordering::AcqRel) {
                            entered.add_permits(1);
                            release.acquire().await.unwrap().forget();
                        }
                        let reply = match command {
                            "VR" => "20VR2504A\r\n",
                            "RN" => "20RNOWNED\r\n",
                            "LM" => "20LM1371\r\n",
                            "RE1" | "VFA" | "MD0F0" | "EX" => "20\r\n",
                            c if c.starts_with("RF0155") => "40UNKNOWN\r\n",
                            c if c.starts_with("RF") => "20\r\n",
                            _ => panic!("unexpected owned receiver command {command}"),
                        };
                        if device.get_mut().write_all(reply.as_bytes()).await.is_err() {
                            break;
                        }
                    }
                });
                self.peer.lock().unwrap().push(peer);
                Ok(Box::new(Owned {
                    io: host,
                    drops: self.drops.clone(),
                }) as BoxTransport)
            })
        }
    }
    fn gate(epoch: Arc<AtomicU64>) -> ExecutionGate {
        let captured = epoch.load(Ordering::Acquire);
        ExecutionGate::new(
            move || {
                if epoch.load(Ordering::Acquire) == captured {
                    Ok(())
                } else {
                    Err(DeviceError::Denied)
                }
            },
            None,
        )
    }
    #[tokio::test]
    async fn retained_actual_receiver_survives_own_tunes_but_not_epoch_replacement_or_taint() {
        let dir = tempfile::tempdir().unwrap();
        let factory = Arc::new(Factory {
            opens: AtomicU64::new(0),
            drops: Arc::new(AtomicU64::new(0)),
            peer: Mutex::new(vec![]),
            block: Arc::new(AtomicBool::new(false)),
            entered: Arc::new(tokio::sync::Semaphore::new(0)),
            release: Arc::new(tokio::sync::Semaphore::new(0)),
        });
        let mut config = CoreConfig::replay(dir.path());
        config.devices.transport = Some(factory.clone());
        let core = Core::open(config).await.unwrap();
        core.archive().unwrap().create_session(json!({"id":"retained","name":"Owned retained worker","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"contract_version":1,"metadata":{},"streams":[{"id":"r","session_id":"retained","device":"Owned receiver","role":"receiver","state":"disconnected","level_unit":"s_meter_count","settings":{"adapter":"ardv1-serial","frequency_hz":145500000,"mode":"FM","period_s":0.1}},{"id":"a","session_id":"retained","device":"Explicit simulator","role":"spectrum_analyzer","state":"disconnected","level_unit":"dBm","settings":{"adapter":"tinysa-sim","start_hz":100000000,"stop_hz":101000000,"points":3,"period_s":0.1}}]})).await.unwrap();
        let manager = core.devices.as_ref().unwrap();
        let target = Target::new("retained", "r");
        let epoch = Arc::new(AtomicU64::new(1));
        assert!(
            manager
                .capture_worker(&target, gate(epoch.clone()))
                .is_err()
        );
        manager
            .start_stream("retained", "r", gate(epoch.clone()))
            .await
            .unwrap();
        let handle = manager
            .capture_worker(&target, gate(epoch.clone()))
            .unwrap();
        assert_eq!(handle.target(), &target);
        assert_eq!(handle.connection_id(), handle.worker_instance());
        let first = manager
            .tune(
                target.clone(),
                146500000,
                Some("FM".into()),
                handle.execution_gate(),
            )
            .await
            .unwrap();
        handle.check().unwrap();
        first.revalidate().unwrap();
        let second = manager
            .tune(
                target.clone(),
                147500000,
                Some("FM".into()),
                handle.execution_gate(),
            )
            .await
            .unwrap();
        handle.check().unwrap();
        assert!(first.revalidate().is_err());
        second.revalidate().unwrap();
        epoch.store(2, Ordering::Release);
        assert!(handle.check().is_err());
        let current = manager
            .capture_worker(&target, gate(epoch.clone()))
            .unwrap();
        current.check().unwrap();
        assert!(handle.check().is_err());
        factory.block.store(true, Ordering::Release);
        let owned = manager.clone();
        let retained = current.clone();
        let original_target = target.clone();
        let tune = tokio::spawn(async move {
            owned
                .tune(
                    original_target,
                    148500000,
                    Some("FM".into()),
                    retained.execution_gate(),
                )
                .await
        });
        tokio::time::timeout(Duration::from_secs(3), factory.entered.acquire())
            .await
            .unwrap()
            .unwrap()
            .forget();
        epoch.store(3, Ordering::Release);
        let stopped = manager.stop_retained_worker(&current).await.unwrap();
        assert!(!stopped.alive);
        assert_eq!(
            stopped.worker_instance.as_deref(),
            Some(current.worker_instance())
        );
        assert!(tune.await.unwrap().is_err());
        factory.release.add_permits(1);
        assert!(current.check().is_err());
        assert_eq!(factory.drops.load(Ordering::Acquire), 1);
        manager
            .start_stream("retained", "r", gate(epoch.clone()))
            .await
            .unwrap();
        let replacement = manager
            .capture_worker(&target, gate(epoch.clone()))
            .unwrap();
        assert_ne!(replacement.worker_instance(), handle.worker_instance());
        // Repeating trusted cleanup of the old Arc cannot stop a replacement.
        manager.stop_retained_worker(&current).await.unwrap();
        replacement.check().unwrap();
        assert_eq!(factory.opens.load(Ordering::Acquire), 2);
        assert_eq!(factory.drops.load(Ordering::Acquire), 1);
        assert!(
            manager
                .tune(
                    target.clone(),
                    148500000,
                    Some("FM".into()),
                    current.execution_gate()
                )
                .await
                .is_err()
        );
        assert!(
            manager
                .tune(
                    target.clone(),
                    155500000,
                    Some("FM".into()),
                    replacement.execution_gate()
                )
                .await
                .is_err()
        );
        assert!(replacement.check().is_err());
        assert!(
            manager
                .capture_worker(&target, gate(epoch.clone()))
                .is_err()
        );
        manager
            .start_stream("retained", "a", gate(epoch.clone()))
            .await
            .unwrap();
        let analyzer = manager
            .capture_worker(&Target::new("retained", "a"), gate(epoch))
            .unwrap();
        analyzer.check().unwrap();
        core.close().await.unwrap();
        assert!(analyzer.check().is_err());
        assert_eq!(
            factory.opens.load(Ordering::Acquire),
            factory.drops.load(Ordering::Acquire)
        );
        let peers = std::mem::take(&mut *factory.peer.lock().unwrap());
        for peer in peers {
            peer.await.unwrap();
        }
    }

    #[tokio::test]
    async fn aborted_tune_waiter_keeps_original_revocation_monitor_and_joins_before_replacement() {
        let dir = tempfile::tempdir().unwrap();
        let factory = Arc::new(Factory {
            opens: AtomicU64::new(0),
            drops: Arc::new(AtomicU64::new(0)),
            peer: Mutex::new(vec![]),
            block: Arc::new(AtomicBool::new(true)),
            entered: Arc::new(tokio::sync::Semaphore::new(0)),
            release: Arc::new(tokio::sync::Semaphore::new(0)),
        });
        let mut config = CoreConfig::replay(dir.path());
        config.devices.transport = Some(factory.clone());
        config.devices.io_limits.receiver_timeout = Duration::from_secs(10);
        let core = Core::open(config).await.unwrap();
        core.archive().unwrap().create_session(json!({"id":"aborted-tune","name":"Owned dropped waiter","state":"running","created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"contract_version":1,"metadata":{},"streams":[{"id":"r","session_id":"aborted-tune","device":"Owned receiver","role":"receiver","state":"disconnected","level_unit":"s_meter_count","settings":{"adapter":"ardv1-serial","frequency_hz":145500000,"mode":"FM","period_s":0.1}}]})).await.unwrap();
        let manager = core.devices.as_ref().unwrap();
        let target = Target::new("aborted-tune", "r");
        let epoch = Arc::new(AtomicU64::new(1));
        manager
            .start_stream("aborted-tune", "r", gate(epoch.clone()))
            .await
            .unwrap();
        let original = manager
            .capture_worker(&target, gate(epoch.clone()))
            .unwrap();
        let owned = manager.clone();
        let retained = original.clone();
        let selected = target.clone();
        let waiter = tokio::spawn(async move {
            owned
                .tune(
                    selected,
                    148500000,
                    Some("FM".into()),
                    retained.execution_gate(),
                )
                .await
        });
        tokio::time::timeout(Duration::from_secs(2), factory.entered.acquire())
            .await
            .unwrap()
            .unwrap()
            .forget();
        waiter.abort();
        assert!(matches!(waiter.await, Err(error) if error.is_cancelled()));
        epoch.store(2, Ordering::Release);
        // No trusted stop is called until after observing the monitor's own
        // wakeup. The native I/O timeout is ten seconds, not this one-second gate.
        let woke = tokio::time::timeout(Duration::from_secs(1), async {
            while factory.drops.load(Ordering::Acquire) == 0 || !original.worker.finished_ok() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .is_ok();
        let stopped = manager.stop_retained_worker(&original).await.unwrap();
        assert!(!stopped.alive);
        let no_stale_ack = core
            .archive()
            .unwrap()
            .events(0, 1000)
            .await
            .unwrap()
            .iter()
            .all(|event| event["event_type"] != "device.tuned");
        factory.release.add_permits(1);
        manager
            .start_stream("aborted-tune", "r", gate(epoch.clone()))
            .await
            .unwrap();
        let replacement = manager.capture_worker(&target, gate(epoch)).unwrap();
        manager.stop_retained_worker(&original).await.unwrap();
        replacement.check().unwrap();
        let replacement_untouched = factory.opens.load(Ordering::Acquire) == 2
            && factory.drops.load(Ordering::Acquire) == 1;
        core.close().await.unwrap();
        let peers = std::mem::take(&mut *factory.peer.lock().unwrap());
        for peer in peers {
            peer.await.unwrap();
        }
        assert!(
            woke,
            "dropping only the waiting caller removed original revocation cleanup"
        );
        assert!(replacement_untouched);
        assert!(
            no_stale_ack,
            "a late native ACK became visible after the original authority was revoked"
        );
        assert_eq!(
            factory.opens.load(Ordering::Acquire),
            factory.drops.load(Ordering::Acquire)
        );
    }
}
