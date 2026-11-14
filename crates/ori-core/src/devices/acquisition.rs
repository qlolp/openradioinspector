//! Monotonic, cache-only observations of actual analyzer poll/commit progress.
use super::*;
use std::time::Instant;

#[derive(Clone, Debug)]
pub(crate) struct AcquisitionHealth {
    pub worker_instance: String,
    pub configured_interval: Duration,
    pub configuration_epoch: u64,
    pub configuration_changed_at: Instant,
    pub active: bool,
    pub intentionally_paused: bool,
    pub started_at: Instant,
    pub poll_started_at: Option<Instant>,
    pub last_poll_duration: Option<Duration>,
    pub commit_started_at: Option<Instant>,
    pub last_commit_duration: Option<Duration>,
    pub last_cycle_finished_at: Option<Instant>,
    pub completed_cycles: u64,
}
#[derive(Clone)]
pub(super) struct Phases {
    ready: bool,
    started_at: Instant,
    poll_started_at: Option<Instant>,
    last_poll_duration: Option<Duration>,
    commit_started_at: Option<Instant>,
    last_commit_duration: Option<Duration>,
    last_cycle_finished_at: Option<Instant>,
    completed_cycles: u64,
}
impl Default for Phases {
    fn default() -> Self {
        Self {
            ready: false,
            started_at: Instant::now(),
            poll_started_at: None,
            last_poll_duration: None,
            commit_started_at: None,
            last_commit_duration: None,
            last_cycle_finished_at: None,
            completed_cycles: 0,
        }
    }
}
impl Worker {
    pub(super) fn acquisition_ready(&self) {
        let mut phase = self.acquisition.lock().expect("acquisition phase poisoned");
        phase.ready = true;
        phase.started_at = Instant::now();
    }
    pub(super) fn acquisition_poll_started(&self) {
        self.acquisition
            .lock()
            .expect("acquisition phase poisoned")
            .poll_started_at = Some(Instant::now());
    }
    pub(super) fn acquisition_poll_finished(&self) {
        let mut phase = self.acquisition.lock().expect("acquisition phase poisoned");
        phase.last_poll_duration = phase.poll_started_at.take().map(|at| at.elapsed());
    }
    pub(super) fn acquisition_commit_started(&self) {
        self.acquisition
            .lock()
            .expect("acquisition phase poisoned")
            .commit_started_at = Some(Instant::now());
    }
    pub(super) fn acquisition_commit_finished(&self, success: bool) {
        let mut phase = self.acquisition.lock().expect("acquisition phase poisoned");
        phase.last_commit_duration = phase.commit_started_at.take().map(|at| at.elapsed());
        if success {
            phase.completed_cycles = phase.completed_cycles.saturating_add(1);
            phase.last_cycle_finished_at = Some(Instant::now());
        }
    }
}
impl DeviceManager {
    pub(crate) fn acquisition_health(&self) -> DeviceResult<Vec<AcquisitionHealth>> {
        // Release the registry before touching any individual worker cache.
        let workers = self
            .registry
            .lock()
            .map_err(|_| DeviceError::Closed)?
            .workers
            .values()
            .cloned()
            .collect::<Vec<_>>();
        let mut samples = Vec::new();
        for worker in workers {
            if worker.stream.role != DeviceRole::SpectrumAnalyzer {
                continue;
            }
            let status = worker.status.lock().map_err(|_| DeviceError::Closed)?;
            if !status.alive {
                continue;
            }
            let running = status.state != "error" && !worker.stopping.load(Ordering::Acquire);
            drop(status);
            let phase = worker
                .acquisition
                .lock()
                .map_err(|_| DeviceError::Closed)?
                .clone();
            let (epoch, changed) = worker.control.configuration();
            samples.push(AcquisitionHealth {
                worker_instance:worker.instance.clone(),configured_interval:worker.period,
                configuration_epoch:epoch,configuration_changed_at:if epoch==0{phase.started_at}else{changed},
                active:running&&phase.ready,
                intentionally_paused:worker.control.paused() || matches!(&worker.native,Some(Native::Analyzer(actor)) if actor.state().control_mode==ori_instruments::ControlMode::Lab),
                started_at:phase.started_at,poll_started_at:phase.poll_started_at,last_poll_duration:phase.last_poll_duration,
                commit_started_at:phase.commit_started_at,last_commit_duration:phase.last_commit_duration,last_cycle_finished_at:phase.last_cycle_finished_at,completed_cycles:phase.completed_cycles,
            });
        }
        Ok(samples)
    }
}
