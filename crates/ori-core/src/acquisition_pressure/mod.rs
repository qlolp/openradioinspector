//! Owned, cache-only live analyzer pressure for existing derived work grants.
use crate::{
    CoreError, Result,
    devices::{AcquisitionHealth, DeviceManager},
};
use ori_runtime::qos::BackgroundQos;
use serde::Serialize;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex, OnceLock},
    time::{Duration, Instant},
};
use tokio::sync::watch;

/// Trusted startup engineering policy. These values are not hardware acceptance.
#[derive(Clone, Debug)]
pub struct AcquisitionPressureOptions {
    pub sample_interval: Duration,
    pub initial_grace: Duration,
    pub lag_floor: Duration,
    pub lag_slack: Duration,
    pub recovery_dwell: Duration,
}
impl Default for AcquisitionPressureOptions {
    fn default() -> Self {
        Self {
            sample_interval: Duration::from_millis(250),
            initial_grace: Duration::from_secs(90),
            lag_floor: Duration::from_secs(2),
            lag_slack: Duration::from_millis(500),
            recovery_dwell: Duration::from_secs(1),
        }
    }
}
impl AcquisitionPressureOptions {
    pub(crate) fn validate(&self) -> Result<()> {
        if self.sample_interval < Duration::from_millis(10)
            || self.sample_interval > Duration::from_secs(5)
            || self.lag_floor < self.sample_interval
            || self.lag_floor > Duration::from_secs(600)
            || self.initial_grace < self.lag_floor
            || self.initial_grace > Duration::from_secs(600)
            || self.lag_slack > Duration::from_secs(60)
            || self.recovery_dwell < self.sample_interval
            || self.recovery_dwell > Duration::from_secs(600)
        {
            Err(CoreError::Invalid(
                "invalid bounded acquisition pressure timing",
            ))
        } else {
            Ok(())
        }
    }
}
#[derive(Clone, Serialize)]
pub(crate) struct AcquisitionPressureStatus {
    pub observer_running: bool,
    pub acquisition_pressure: bool,
    pub active_analyzers: usize,
    pub calibrating_analyzers: usize,
    pub delayed_analyzers: usize,
    pub last_error: Option<&'static str>,
}
pub(crate) type Completion = watch::Receiver<Option<std::result::Result<(), &'static str>>>;
pub(crate) struct AcquisitionPressure {
    status: Arc<Mutex<AcquisitionPressureStatus>>,
    stop: watch::Sender<bool>,
    task: Mutex<Option<tokio::task::JoinHandle<std::result::Result<(), &'static str>>>>,
    runtime: tokio::runtime::Handle,
    closed: OnceLock<Completion>,
}
impl AcquisitionPressure {
    pub(crate) fn start(
        devices: Arc<DeviceManager>,
        qos: BackgroundQos,
        options: AcquisitionPressureOptions,
    ) -> Result<Arc<Self>> {
        options.validate()?;
        let status = Arc::new(Mutex::new(AcquisitionPressureStatus {
            observer_running: true,
            acquisition_pressure: false,
            active_analyzers: 0,
            calibrating_analyzers: 0,
            delayed_analyzers: 0,
            last_error: None,
        }));
        let (stop, receiver) = watch::channel(false);
        let runtime = tokio::runtime::Handle::current();
        let own_status = status.clone();
        let task = runtime.spawn(async move {
            let result = observe(devices, qos, options, receiver, &own_status).await;
            let mut state = own_status.lock().expect("acquisition status poisoned");
            state.observer_running = false;
            state.last_error = result.as_ref().err().copied();
            result
        });
        Ok(Arc::new(Self {
            status,
            stop,
            task: Mutex::new(Some(task)),
            runtime,
            closed: OnceLock::new(),
        }))
    }
    /// Signal synchronously; the retained owner, rather than a close waiter,
    /// consumes the one actual loop JOIN. This does not set final shutdown QoS.
    pub(crate) fn begin_shutdown(self: &Arc<Self>) -> Completion {
        self.stop.send_replace(true);
        self.closed
            .get_or_init(|| {
                let (sent, done) = watch::channel(None);
                let own = self.clone();
                self.runtime.spawn(async move {
                    let task = own
                        .task
                        .lock()
                        .map_err(|_| "acquisition JOIN state poisoned")
                        .and_then(|mut task| task.take().ok_or("acquisition JOIN owner missing"));
                    let result = match task {
                        Ok(task) => task
                            .await
                            .unwrap_or(Err("acquisition observer task failed")),
                        Err(error) => Err(error),
                    };
                    if let Err(error) = result
                        && let Ok(mut state) = own.status.lock()
                    {
                        state.last_error = Some(error);
                    }
                    sent.send_replace(Some(result));
                });
                done
            })
            .clone()
    }
    pub(crate) async fn close(self: &Arc<Self>) -> Result<()> {
        let mut done = self.begin_shutdown();
        loop {
            if let Some(result) = *done.borrow() {
                return result.map_err(CoreError::Invalid);
            }
            done.changed()
                .await
                .map_err(|_| CoreError::Invalid("acquisition JOIN completion lost"))?;
        }
    }
    pub(crate) fn status(&self) -> AcquisitionPressureStatus {
        self.status.lock().unwrap().clone()
    }
}
impl Drop for AcquisitionPressure {
    fn drop(&mut self) {
        // A last-owner drop requests stop; it does not assert cleanup or JOIN.
        self.stop.send_replace(true);
    }
}

async fn observe(
    devices: Arc<DeviceManager>,
    qos: BackgroundQos,
    options: AcquisitionPressureOptions,
    mut stop: watch::Receiver<bool>,
    status: &Mutex<AcquisitionPressureStatus>,
) -> std::result::Result<(), &'static str> {
    let mut ticker = tokio::time::interval(options.sample_interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut policy = Policy::new(options);
    // Only transitions of OUR assertion write QoS. In particular an idle or
    // cold observer must not clear independent explicit pressure requests.
    let mut applied = false;
    loop {
        if *stop.borrow() {
            return Ok(());
        }
        tokio::select! {
            biased;
            _ = stop.changed() => return Ok(()),
            _ = ticker.tick() => {}
        }
        let rows = match devices.acquisition_health() {
            Ok(rows) => rows,
            Err(_) => {
                qos.pressure(true)
                    .await
                    .map_err(|_| "acquisition pressure drain failed")?;
                return Err("acquisition health snapshot unavailable");
            }
        };
        let decision = policy.observe(&rows, Instant::now());
        {
            let mut state = status.lock().map_err(|_| "acquisition status poisoned")?;
            state.acquisition_pressure = decision.pressure;
            state.active_analyzers = decision.active;
            state.calibrating_analyzers = decision.calibrating;
            state.delayed_analyzers = decision.delayed;
        }
        if decision.pressure != applied {
            // The true transition cancels ORIGINAL work tokens and waits for
            // their owners to release guards after actual child/thread JOINs.
            qos.pressure(decision.pressure)
                .await
                .map_err(|_| "acquisition pressure drain failed")?;
            applied = decision.pressure;
        }
    }
}

struct Decision {
    pressure: bool,
    active: usize,
    calibrating: usize,
    delayed: usize,
}
struct Policy {
    trackers: BTreeMap<String, Tracker>,
    options: AcquisitionPressureOptions,
}
impl Policy {
    fn new(options: AcquisitionPressureOptions) -> Self {
        Self {
            trackers: BTreeMap::new(),
            options,
        }
    }
    fn observe(&mut self, rows: &[AcquisitionHealth], now: Instant) -> Decision {
        self.trackers
            .retain(|id, _| rows.iter().any(|row| row.worker_instance == *id));
        let mut decision = Decision {
            pressure: false,
            active: 0,
            calibrating: 0,
            delayed: 0,
        };
        for row in rows {
            let tracker = self
                .trackers
                .entry(row.worker_instance.clone())
                .or_insert_with(|| Tracker::new(row, row.configuration_changed_at, false));
            if tracker.epoch != row.configuration_epoch
                || tracker.configured_at != row.configuration_changed_at
                || row.completed_cycles < tracker.last_cycles
            {
                *tracker = Tracker::new(row, row.configuration_changed_at, false);
            }
            if !row.active || row.intentionally_paused {
                tracker.paused = true;
                tracker.pressured = false;
                continue;
            }
            if tracker.paused {
                *tracker = Tracker::new(row, now, true);
            }
            decision.active += 1;
            let delayed = tracker.observe(row, now, &self.options);
            decision.calibrating += usize::from(tracker.samples < 3);
            decision.delayed += usize::from(delayed);
            decision.pressure |= tracker.pressured;
        }
        decision
    }
}

struct Tracker {
    epoch: u64,
    configured_at: Instant,
    anchor: Instant,
    last_cycles: u64,
    baseline: Duration,
    samples: u32,
    bad_samples: u8,
    pressured: bool,
    last_service_bad: bool,
    healthy_cycles: u8,
    healthy_since: Option<Instant>,
    paused: bool,
}
impl Tracker {
    fn new(row: &AcquisitionHealth, anchor: Instant, ignore_existing: bool) -> Self {
        let anchor = anchor.max(row.started_at);
        let can_learn =
            !ignore_existing && row.last_cycle_finished_at.is_some_and(|at| at >= anchor);
        Self {
            epoch: row.configuration_epoch,
            configured_at: row.configuration_changed_at,
            anchor,
            last_cycles: if can_learn {
                row.completed_cycles.saturating_sub(1)
            } else {
                row.completed_cycles
            },
            baseline: Duration::ZERO,
            samples: 0,
            bad_samples: 0,
            pressured: false,
            last_service_bad: false,
            healthy_cycles: 0,
            healthy_since: None,
            paused: false,
        }
    }
    fn budget(&self, options: &AcquisitionPressureOptions) -> Duration {
        if self.samples < 3 {
            options.initial_grace
        } else {
            options.lag_floor.max(
                self.baseline
                    .saturating_mul(3)
                    .saturating_add(options.lag_slack),
            )
        }
    }
    fn observe(
        &mut self,
        row: &AcquisitionHealth,
        now: Instant,
        options: &AcquisitionPressureOptions,
    ) -> bool {
        let previous_budget = self.budget(options);
        let valid_finish = row.last_cycle_finished_at.filter(|at| *at >= self.anchor);
        let fresh = row.completed_cycles > self.last_cycles && valid_finish.is_some();
        let service = row
            .last_poll_duration
            .zip(row.last_commit_duration)
            .map(|(poll, commit)| poll.saturating_add(commit));
        let good_commit = fresh && service.is_some_and(|service| service <= previous_budget);
        if fresh {
            self.last_service_bad = !good_commit;
            if let Some(service) = service.filter(|_| good_commit) {
                if self.samples < 3 {
                    self.baseline = self
                        .baseline
                        .saturating_mul(self.samples)
                        .saturating_add(service)
                        / (self.samples + 1);
                    self.samples += 1;
                } else {
                    self.baseline = self.baseline.saturating_mul(7).saturating_add(service) / 8;
                }
            }
        }
        self.last_cycles = row.completed_cycles;
        let budget = self.budget(options);
        let in_flight = row.poll_started_at.or(row.commit_started_at);
        let stalled = if let Some(since) = in_flight {
            elapsed(now, since.max(self.anchor)) > budget
        } else {
            elapsed(now, valid_finish.unwrap_or(self.anchor))
                > row.configured_interval.saturating_add(budget)
        };
        let no_first_commit = self.samples == 0
            && elapsed(now, self.anchor)
                > row
                    .configured_interval
                    .saturating_add(options.initial_grace);
        let bad = stalled || no_first_commit || self.last_service_bad;
        if bad {
            self.bad_samples = self.bad_samples.saturating_add(1).min(2);
            self.healthy_cycles = 0;
            self.healthy_since = None;
            if self.bad_samples >= 2 {
                self.pressured = true;
            }
        } else {
            self.bad_samples = 0;
            if self.pressured && good_commit {
                self.healthy_cycles = self.healthy_cycles.saturating_add(1).min(3);
                self.healthy_since.get_or_insert(now);
            }
            if self.pressured
                && self.healthy_cycles >= 3
                && self
                    .healthy_since
                    .is_some_and(|at| elapsed(now, at) >= options.recovery_dwell)
            {
                self.pressured = false;
            }
        }
        bad
    }
}
fn elapsed(now: Instant, since: Instant) -> Duration {
    now.checked_duration_since(since).unwrap_or_default()
}

#[cfg(test)]
mod owned_tests;
#[cfg(test)]
mod policy_tests;
