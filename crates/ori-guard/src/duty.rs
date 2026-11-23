//! A duty plan records operator intent separately from actual guard ownership.
use crate::{GuardError, GuardService, Result, runtime::StopMode, util::invalid};
use jiff::{Timestamp, tz::TimeZone};
use ori_contracts::UtcTimestamp;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Mutex,
    time::Duration,
};

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum DutyTrigger {
    Timer,
    Restart,
    Wake,
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "mode", rename_all = "snake_case", deny_unknown_fields)]
enum Schedule {
    Continuous,
    Until {
        until_at: UtcTimestamp,
    },
    Weekly {
        timezone: String,
        weekdays: Vec<u8>,
        start_local: String,
        end_local: String,
    },
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Policy {
    schedule: Schedule,
    #[serde(default)]
    resume_after_restart: bool,
}
fn minute(s: &str) -> Result<u16> {
    let bytes = s.as_bytes();
    if bytes.len() != 5
        || bytes[2] != b':'
        || [bytes[0], bytes[1], bytes[3], bytes[4]]
            .iter()
            .any(|b| !b.is_ascii_digit())
    {
        return Err(invalid("Локальное время должно иметь формат ЧЧ:ММ"));
    }
    let h = u16::from((bytes[0] - b'0') * 10 + bytes[1] - b'0');
    let m = u16::from((bytes[3] - b'0') * 10 + bytes[4] - b'0');
    if h > 23 || m > 59 {
        return Err(invalid("Локальное время вне суток"));
    }
    Ok(h * 60 + m)
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Window {
    Open { from: i64, until: i64 },
    Closed,
    Expired,
}
impl Window {
    fn is_open(self) -> bool {
        matches!(self, Self::Open { .. })
    }
}
impl Schedule {
    fn window(&self, now: &UtcTimestamp) -> Result<Window> {
        match self {
            Self::Continuous => Ok(Window::Open {
                from: i64::MIN,
                until: i64::MAX,
            }),
            Self::Until { until_at } => Ok(if now.unix_micros() < until_at.unix_micros() {
                Window::Open {
                    from: i64::MIN,
                    until: until_at.unix_micros(),
                }
            } else {
                Window::Expired
            }),
            Self::Weekly {
                timezone,
                weekdays,
                start_local,
                end_local,
            } => {
                let unique = weekdays.iter().collect::<BTreeSet<_>>();
                if weekdays.is_empty()
                    || weekdays.len() > 7
                    || unique.len() != weekdays.len()
                    || weekdays.iter().any(|d| !(1..=7).contains(d))
                {
                    return Err(invalid("Нужны уникальные дни недели 1..7"));
                }
                let zone = TimeZone::get(timezone)
                    .map_err(|_| invalid("Неизвестный часовой пояс IANA"))?;
                let instant = Timestamp::from_microsecond(now.unix_micros())
                    .map_err(|_| invalid("Время вне диапазона"))?;
                let today = instant.to_zoned(zone.clone()).date();
                let start = minute(start_local)?;
                let end = minute(end_local)?;
                if start == end {
                    return Err(invalid("Начало и конец смены должны различаться"));
                }
                let mut active = Window::Closed;
                for day in [today.yesterday().ok(), Some(today)].into_iter().flatten() {
                    if !weekdays.contains(&(day.weekday().to_monday_one_offset() as u8)) {
                        continue;
                    }
                    let finish = if start < end {
                        day
                    } else {
                        day.tomorrow()
                            .map_err(|_| invalid("Дата смены вне диапазона"))?
                    };
                    // Resolve once per civil day: a repeated hour belongs to the same shift.
                    // Compatible starts use the first offset in a fold, later ends use the last.
                    let from = zone
                        .to_ambiguous_timestamp(day.at(
                            (start / 60) as i8,
                            (start % 60) as i8,
                            0,
                            0,
                        ))
                        .compatible()
                        .map_err(|_| invalid("Начало смены вне диапазона"))?;
                    let to = zone
                        .to_ambiguous_timestamp(finish.at((end / 60) as i8, (end % 60) as i8, 0, 0))
                        .later()
                        .map_err(|_| invalid("Конец смены вне диапазона"))?;
                    if (from..to).contains(&instant) {
                        // All matching intervals contain this instant. Keep the union of overlapping intervals.
                        active = match active {
                            Window::Open { from: a, until: b } => Window::Open {
                                from: a.min(from.as_microsecond()),
                                until: b.max(to.as_microsecond()),
                            },
                            _ => Window::Open {
                                from: from.as_microsecond(),
                                until: to.as_microsecond(),
                            },
                        };
                    }
                }
                Ok(active)
            }
        }
    }
}
#[derive(Default)]
pub(crate) struct DutyRuntime {
    windows: Mutex<BTreeMap<String, Window>>,
    sampled: Mutex<Option<(i64, Duration)>>,
    pending_wakes: Mutex<BTreeSet<String>>,
    stops: Mutex<BTreeMap<String, usize>>,
    channel_projects: Mutex<BTreeSet<(String, &'static str)>>,
}
pub(crate) struct StopIntent<'a> {
    duty: &'a DutyRuntime,
    project: &'a str,
}
impl Drop for StopIntent<'_> {
    fn drop(&mut self) {
        if let Ok(mut stops) = self.duty.stops.lock()
            && let Some(count) = stops.get_mut(self.project)
        {
            *count -= 1;
            if *count == 0 {
                stops.remove(self.project);
            }
        }
    }
}
impl DutyRuntime {
    fn cancel_pending(&self, id: &str) -> bool {
        self.stops
            .lock()
            .map_or(true, |stops| stops.contains_key(id))
    }
    pub(crate) fn stopping<'a>(&'a self, project: &'a str) -> Result<StopIntent<'a>> {
        let mut stops = self.stops.lock().map_err(|_| invalid("duty intent"))?;
        *stops.entry(project.to_owned()).or_default() += 1;
        Ok(StopIntent {
            duty: self,
            project,
        })
    }
    pub(crate) fn channels(&self, project: &str, value: &Value) -> Result<()> {
        let mut entries = self
            .channel_projects
            .lock()
            .map_err(|_| invalid("duty channels"))?;
        for ch in ori_archive::DutyChannel::ALL {
            let key = (project.to_owned(), ch.name());
            if value[ch.name()]["enabled"] == true {
                entries.insert(key);
            } else {
                entries.remove(&key);
            }
        }
        Ok(())
    }
    pub(crate) fn channel_active(&self, ch: ori_archive::DutyChannel) -> Result<bool> {
        Ok(self
            .channel_projects
            .lock()
            .map_err(|_| invalid("duty channels"))?
            .iter()
            .any(|entry| entry.1 == ch.name()))
    }
    fn track(&self, id: &str, window: Option<Window>) -> Result<()> {
        let mut windows = self.windows.lock().map_err(|_| invalid("duty registry"))?;
        if let Some(window) = window {
            windows.insert(id.to_owned(), window);
        } else {
            windows.remove(id);
            self.pending_wakes
                .lock()
                .map_err(|_| invalid("duty clock"))?
                .remove(id);
        }
        Ok(())
    }
    /// Recheck after an unobserved interval, including suspend on clocks that count sleep.
    fn due(&self, wall: i64, mono: Duration) -> Result<Option<DutyTrigger>> {
        let mut stored = self.sampled.lock().map_err(|_| invalid("duty clock"))?;
        let trigger = match *stored {
            None => DutyTrigger::Timer,
            Some((before, at)) => {
                let wall_delta = wall.saturating_sub(before);
                let elapsed = mono.saturating_sub(at);
                let micros = i64::try_from(elapsed.as_micros()).unwrap_or(i64::MAX);
                if wall_delta.abs_diff(micros) > 5_000_000 || elapsed > Duration::from_secs(30) {
                    DutyTrigger::Wake
                } else if elapsed < Duration::from_secs(1) {
                    return Ok(None);
                } else {
                    DutyTrigger::Timer
                }
            }
        };
        *stored = Some((wall, mono));
        Ok(Some(trigger))
    }
}
fn held(project: &Value) -> bool {
    project["manual_primary_hold"] == true || project["manual_primary_hold"].is_object()
}
fn off() -> Value {
    json!({"enabled":false,"schedule":{"mode":"continuous"},"resume_after_restart":false,"status":"disarmed","last_error":null})
}
impl GuardService {
    /// The serial writer checks the currently registered civil interval before dispatch.
    /// A pending reply keeps its own bounded deadline and may finish after the shift.
    pub fn duty_acquisition_allowed(&self, id: &str) -> bool {
        let window = match self.duty_runtime.windows.lock() {
            Ok(windows) => windows.get(id).copied(),
            Err(_) => return false,
        };
        // Cancellation latches stop intent before removing the registered interval.
        if self.duty_runtime.cancel_pending(id) {
            return false;
        }
        match window {
            None
            | Some(Window::Open {
                from: i64::MIN,
                until: i64::MAX,
            }) => true,
            Some(Window::Open { from, until }) => (from..until).contains(&self.clock.now_micros()),
            _ => false,
        }
    }
    pub(crate) fn require_duty_window(&self, project: &Value) -> Result<()> {
        if project["id"]
            .as_str()
            .is_some_and(|id| self.duty_runtime.cancel_pending(id))
        {
            return Err(GuardError::Conflict("Отмена дежурства".into()));
        }
        if project["duty"]["enabled"] == true {
            let schedule: Schedule = serde_json::from_value(project["duty"]["schedule"].clone())
                .map_err(|_| invalid("Сохранённое расписание дежурства"))?;
            if !schedule.window(&self.clock.now())?.is_open() {
                return Err(GuardError::Conflict("Окно дежурства закрыто".into()));
            }
        }
        Ok(())
    }
    pub async fn duty(&self, id: &str) -> Result<Value> {
        let p = self.project(id).await?;
        let mut state = p
            .get("duty")
            .filter(|v| v.is_object())
            .cloned()
            .unwrap_or_else(off);
        if state["enabled"] == true && state["status"] == "armed" {
            match p["state"].as_str() {
                Some("degraded") => {
                    state["status"] = json!("degraded");
                    state["last_error"] = json!("Часть приборов недоступна");
                }
                Some("error" | "interrupted") => {
                    state["status"] = json!("coverage_lost");
                    state["last_error"] = json!("Покрытие прервано");
                }
                _ => {}
            }
        }
        if state["enabled"] == true && held(&p) {
            state["status"] = json!("manual_hold");
        }
        state
            .as_object_mut()
            .expect("duty object")
            .remove("receiver_run_id");
        state["deliveries"] = json!(self.archive.duty_delivery_journal(id, 32).await?);
        Ok(state)
    }
    async fn store_duty(&self, id: &str, state: &Value, window: Option<Window>) -> Result<()> {
        let _transaction = self.coordinator.lock().await;
        self.ensure_open()?;
        let mut p = self.project(id).await?;
        p["duty"] = state.clone();
        // A duty policy supersedes the legacy unconditional restart switch.
        p["autostart"] = json!(false);
        self.archive.put_monitoring("project", p).await?;
        self.duty_runtime.track(id, window)
    }
    pub async fn arm_duty(&self, id: &str, payload: Value) -> Result<Value> {
        let policy: Policy = serde_json::from_value(payload)
            .map_err(|_| invalid("Неверное расписание дежурства"))?;
        let window = policy.schedule.window(&self.clock.now())?;
        if window == Window::Expired {
            return Err(invalid("Окончание дежурства должно быть в будущем"));
        }
        let _operation = self.lock_project(&format!("duty/{id}")).await?;
        let mut state =
            serde_json::to_value(policy).map_err(|_| invalid("Расписание дежурства"))?;
        state["enabled"] = json!(true);
        state["status"] = json!("starting");
        state["last_error"] = Value::Null;
        self.store_duty(id, &state, Some(window)).await?;
        self.advance_duty(id, state, DutyTrigger::Timer).await?;
        self.duty(id).await
    }
    pub async fn reconcile_duty(&self, id: &str, trigger: DutyTrigger) -> Result<()> {
        let _operation = self.lock_project(&format!("duty/{id}")).await?;
        let tracked = self
            .duty_runtime
            .windows
            .lock()
            .map_err(|_| invalid("duty registry"))?
            .contains_key(id);
        if trigger == DutyTrigger::Timer && !tracked {
            return Ok(());
        }
        if trigger == DutyTrigger::Wake
            && tracked
            && let Some(runtime) = self.runtime_snapshot(id)?
        {
            // Latch revocation before archive I/O, including when a removable store vanished during sleep.
            self.disarm_runtime(id, StopMode::Immediate).await?;
            self.notify_error(
                &runtime.cached_project,
                "Разрыв покрытия после сна",
                "sleep_gap",
            )
            .await?;
        }
        let p = self.project(id).await?;
        if p["duty"]["enabled"] == true {
            self.advance_duty(id, p["duty"].clone(), trigger).await?;
        }
        Ok(())
    }
    async fn advance_duty(&self, id: &str, mut state: Value, trigger: DutyTrigger) -> Result<()> {
        let prior = state.clone();
        let schedule: Schedule = serde_json::from_value(state["schedule"].clone())
            .map_err(|_| invalid("Сохранённое расписание дежурства"))?;
        let mut window = schedule.window(&self.clock.now())?;
        self.duty_runtime.track(id, Some(window))?;
        let interrupted = trigger != DutyTrigger::Timer;
        let running = self.runtime_snapshot(id)?.is_some();
        if running && (!window.is_open() || interrupted) {
            self.disarm_runtime(
                id,
                if interrupted {
                    StopMode::Immediate
                } else {
                    StopMode::DutyWindowEnd
                },
            )
            .await?;
        }
        if !window.is_open() || (interrupted && state["resume_after_restart"] != true) {
            let _life = self.lock_project(id).await?;
            let collecting = self
                .runtime
                .lock()
                .map_err(|_| invalid("guard runtime lock"))?
                .preparing
                .contains_key(id);
            if collecting && let Err(error) = self.stop_preparation_locked(id).await {
                state["status"] = json!("stop_failed");
                state["last_error"] =
                    json!("Не удалось подтвердить остановку подготовительного приёма");
                self.store_duty(id, &state, Some(window)).await?;
                return Err(error);
            }
        }
        state["last_error"] = Value::Null;
        if window == Window::Expired {
            state["enabled"] = json!(false);
            state["status"] = json!("expired");
        } else if interrupted && state["resume_after_restart"] != true {
            state["enabled"] = json!(false);
            state["status"] = json!("interrupted");
        } else if window == Window::Closed {
            state["status"] = json!("waiting");
        } else if !interrupted
            && (prior["status"] == "preflight_failed" || running && prior["status"] == "armed")
        {
            return Ok(());
        } else {
            if held(&self.project(id).await?) {
                state["status"] = json!("manual_hold");
            } else {
                let resume_receiver = interrupted
                    && (prior["status"] == "armed" || prior["receiver_run_id"].is_string());
                let attempt = self.arm_project(id, resume_receiver).await;
                window = schedule.window(&self.clock.now())?;
                match (window, attempt) {
                    (Window::Closed, _) => state["status"] = json!("waiting"),
                    (Window::Expired, _) => {
                        state["status"] = json!("expired");
                        state["enabled"] = json!(false);
                    }
                    (Window::Open { .. }, Ok(armed)) => {
                        state["status"] = json!("armed");
                        if armed["primary_source_kind"] == "ardv1" {
                            state["receiver_run_id"] = armed["active_session_id"].clone();
                        }
                    }
                    (Window::Open { .. }, Err(error)) => {
                        self.ensure_open()?;
                        state["status"] = json!("preflight_failed");
                        state["last_error"] = match error {
                            GuardError::RfExplorerMaxHold
                            | GuardError::RfExplorerDetectorUnconfirmed => json!(error.to_string()),
                            _ => json!("Проверка прибора, эталона или архива не пройдена"),
                        };
                        let project = self.project(id).await?;
                        self.notify_error(&project, "Не удалось начать дежурство", "restart_error")
                            .await?;
                    }
                }
                if state["status"] != "armed" && self.runtime_snapshot(id)?.is_some() {
                    self.disarm_runtime(
                        id,
                        if window.is_open() {
                            StopMode::Immediate
                        } else {
                            StopMode::DutyWindowEnd
                        },
                    )
                    .await?;
                }
            }
        }
        if state["status"] == "waiting" || state["enabled"] != true {
            state
                .as_object_mut()
                .expect("duty object")
                .remove("receiver_run_id");
        }
        if state != prior {
            let enabled_window = (state["enabled"] == true).then_some(window);
            self.store_duty(id, &state, enabled_window).await?;
        }
        Ok(())
    }
    pub(crate) async fn cancel_duty(&self, id: &str) -> Result<()> {
        self.duty_runtime.track(id, None)?;
        let p = self.project(id).await?;
        if let Some(mut state) = p.get("duty").cloned() {
            state["enabled"] = json!(false);
            state["status"] = json!("disarmed");
            state
                .as_object_mut()
                .expect("duty object")
                .remove("receiver_run_id");
            state["last_error"] = Value::Null;
            self.store_duty(id, &state, None).await?;
        }
        Ok(())
    }
    pub(crate) async fn duty_tick(&self) -> Result<()> {
        let Some(trigger) = self
            .duty_runtime
            .due(self.clock.now().unix_micros(), self.clock.monotonic())?
        else {
            return Ok(());
        };
        let ids = self
            .duty_runtime
            .windows
            .lock()
            .map_err(|_| GuardError::Conflict("duty registry".into()))?
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        if trigger == DutyTrigger::Wake {
            self.duty_runtime
                .pending_wakes
                .lock()
                .map_err(|_| invalid("duty clock"))?
                .extend(ids.iter().cloned());
        }
        let mut outcome = Ok(());
        for id in ids {
            let woke = self
                .duty_runtime
                .pending_wakes
                .lock()
                .map_err(|_| invalid("duty clock"))?
                .contains(&id);
            match self
                .reconcile_duty(
                    &id,
                    if woke {
                        DutyTrigger::Wake
                    } else {
                        DutyTrigger::Timer
                    },
                )
                .await
            {
                Ok(()) if woke => {
                    self.duty_runtime
                        .pending_wakes
                        .lock()
                        .map_err(|_| invalid("duty clock"))?
                        .remove(&id);
                }
                Err(error) => outcome = Err(error),
                _ => {}
            }
        }
        outcome
    }
}
