use crate::{
    ControlTask, TaskContext, TaskEffects, TaskError, TaskResult,
    book::{Book, BookLock},
    model::*,
};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    io::Write,
    path::PathBuf,
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tokio::sync::{Notify, oneshot};

#[derive(Clone)]
pub struct TaskSchedulerConfig {
    pub book_path: PathBuf,
    pub interval_seconds: f64,
    pub max_book_bytes: usize,
    pub max_tasks: usize,
    pub command_capacity: usize,
    pub request_timeout: Duration,
}
impl TaskSchedulerConfig {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            book_path: path.into(),
            interval_seconds: 1.,
            max_book_bytes: 16 * 1024 * 1024,
            max_tasks: 10_000,
            command_capacity: 32,
            request_timeout: Duration::from_secs(30),
        }
    }
    fn validate(&self) -> TaskResult<()> {
        if !(0.1..=60.).contains(&self.interval_seconds) {
            return Err(TaskError::invalid(
                "interval_seconds должен быть в диапазоне 0,1…60",
            ));
        }
        if self.book_path.file_name().is_none()
            || !(1..=16 * 1024 * 1024).contains(&self.max_book_bytes)
            || !(1..=10_000).contains(&self.max_tasks)
            || !(1..=64).contains(&self.command_capacity)
            || self.request_timeout.is_zero()
            || self.request_timeout > Duration::from_secs(300)
        {
            return Err(TaskError::resource());
        }
        Ok(())
    }
}
struct Queue {
    commands: VecDeque<Envelope>,
    bytes: usize,
    sequence: u64,
}
struct Envelope {
    work: Work,
    reply: oneshot::Sender<TaskResult<Value>>,
    bytes: usize,
    sequence: u64,
}
enum Work {
    Create(ControlTask),
    Cancel(String),
    List,
    Configure(bool),
    RunOnce(Option<DateTime<Utc>>, u64),
}
#[derive(Clone)]
struct CloseResult {
    attempt: u64,
    result: TaskResult<()>,
}
struct Shared {
    config: TaskSchedulerConfig,
    queue: Mutex<Queue>,
    wake: Condvar,
    snapshot: Mutex<Value>,
    closing: AtomicBool,
    close_attempt: AtomicU64,
    close_result: Mutex<Option<CloseResult>>,
    close_notify: Notify,
    pause_sequence: AtomicU64,
    epoch: AtomicU64,
    exited: AtomicBool,
    exit_notify: Notify,
    join: Mutex<Option<JoinHandle<()>>>,
    join_started: AtomicBool,
    joined: AtomicBool,
    join_error: Mutex<Option<TaskError>>,
    join_notify: Notify,
    runtime: tokio::runtime::Handle,
}
pub struct TaskScheduler {
    shared: Arc<Shared>,
}
impl TaskScheduler {
    pub fn open(
        config: TaskSchedulerConfig,
        effects: Arc<dyn TaskEffects>,
    ) -> TaskResult<Arc<Self>> {
        Self::with_clock(config, effects, Arc::new(Utc::now))
    }
    pub fn with_clock(
        config: TaskSchedulerConfig,
        effects: Arc<dyn TaskEffects>,
        clock: Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>,
    ) -> TaskResult<Arc<Self>> {
        config.validate()?;
        let runtime = tokio::runtime::Handle::try_current().map_err(|_| {
            TaskError::new(
                503,
                "task_scheduler_unavailable",
                "Для scheduler требуется действующий Tokio runtime",
            )
        })?;
        let lease = BookLock::acquire(&config.book_path)?;
        let book = Book::load(
            config.book_path.clone(),
            config.max_book_bytes,
            config.max_tasks,
        )?;
        let shared = Arc::new(Shared {
            config: config.clone(),
            queue: Mutex::new(Queue {
                commands: VecDeque::new(),
                bytes: 0,
                sequence: 0,
            }),
            wake: Condvar::new(),
            snapshot: Mutex::new(Value::Null),
            closing: AtomicBool::new(false),
            close_attempt: AtomicU64::new(0),
            close_result: Mutex::new(None),
            close_notify: Notify::new(),
            pause_sequence: AtomicU64::new(0),
            epoch: AtomicU64::new(0),
            exited: AtomicBool::new(false),
            exit_notify: Notify::new(),
            join: Mutex::new(None),
            join_started: AtomicBool::new(false),
            joined: AtomicBool::new(false),
            join_error: Mutex::new(None),
            join_notify: Notify::new(),
            runtime,
        });
        let mut actor = Actor {
            book,
            lease: Some(lease),
            shared: shared.clone(),
            effects,
            clock,
            enabled: false,
            epoch: 0,
            dirty: false,
            runtime: json!({"last_result":"idle","last_error":null,"last_task_name":null,"started_tasks":0,"completed_tasks":0,"aborted_tasks":0}),
            next_tick: Instant::now() + Duration::from_secs_f64(config.interval_seconds),
        };
        actor.recover()?;
        actor.publish();
        let thread_shared = shared.clone();
        let handle=thread::Builder::new().name("ori-task-scheduler".into()).spawn(move||{
            let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||actor.run()));
            if result.is_err(){*thread_shared.close_result.lock().unwrap()=Some(CloseResult{attempt:thread_shared.close_attempt.load(Ordering::Acquire),result:Err(TaskError::new(503,"task_scheduler_failed","Поток scheduler аварийно завершился; успешная остановка не подтверждена"))});thread_shared.close_notify.notify_waiters();}
            thread_shared.exited.store(true,Ordering::Release);thread_shared.exit_notify.notify_waiters();
            // A later closer may have a newer attempt than the final result.
            thread_shared.close_notify.notify_waiters();
        }).map_err(|_|TaskError::new(503,"task_scheduler_unavailable","Не удалось запустить поток scheduler"))?;
        *shared.join.lock().unwrap() = Some(handle);
        Ok(Arc::new(Self { shared }))
    }
    pub async fn status(&self) -> TaskResult<Value> {
        Ok(self.shared.snapshot.lock().unwrap().clone())
    }
    pub async fn list_tasks(&self) -> TaskResult<Value> {
        self.request(Work::List, 1).await
    }
    pub async fn create_task(&self, payload: Value) -> TaskResult<Value> {
        let task = ControlTask::create(payload)?;
        let bytes = estimate(&task.as_dict(), self.shared.config.max_book_bytes)?;
        self.request(Work::Create(task), bytes).await
    }
    pub async fn cancel_task(&self, name: &str) -> TaskResult<Value> {
        self.request(Work::Cancel(name.into()), name.len().saturating_add(1))
            .await
    }
    pub async fn configure(&self, payload: Value) -> TaskResult<Value> {
        let object = payload
            .as_object()
            .filter(|v| v.len() == 1 && v.contains_key("enabled"))
            .ok_or_else(|| TaskError::invalid("Разрешено только поле enabled"))?;
        let enabled = object["enabled"]
            .as_bool()
            .ok_or_else(|| TaskError::invalid("enabled должен быть JSON boolean"))?;
        self.request(Work::Configure(enabled), 1).await
    }
    pub async fn run_once(&self, moment: Option<DateTime<Utc>>) -> TaskResult<Value> {
        if self.shared.closing.load(Ordering::Acquire) {
            return Ok(Value::Null);
        }
        self.request(
            Work::RunOnce(moment, self.shared.epoch.load(Ordering::Acquire)),
            1,
        )
        .await
    }
    async fn request(&self, work: Work, bytes: usize) -> TaskResult<Value> {
        let (reply, receiver) = oneshot::channel();
        {
            let mut queue = self.shared.queue.lock().unwrap();
            if self.shared.closing.load(Ordering::Acquire)
                || self.shared.exited.load(Ordering::Acquire)
            {
                return Err(TaskError::closed());
            }
            if queue.commands.len() >= self.shared.config.command_capacity
                || bytes
                    > self
                        .shared
                        .config
                        .max_book_bytes
                        .saturating_sub(queue.bytes)
            {
                return Err(TaskError::queue_full());
            }
            queue.sequence = queue
                .sequence
                .checked_add(1)
                .ok_or_else(TaskError::resource)?;
            let sequence = queue.sequence;
            if matches!(work, Work::Configure(false)) {
                self.shared
                    .pause_sequence
                    .store(sequence, Ordering::Release);
            }
            queue.bytes += bytes;
            queue.commands.push_back(Envelope {
                work,
                reply,
                bytes,
                sequence,
            });
            self.shared.wake.notify_one();
        }
        tokio::time::timeout(self.shared.config.request_timeout, receiver)
            .await
            .map_err(|_| TaskError::busy())?
            .map_err(|_| TaskError::closed())?
    }
    pub async fn close(&self) -> TaskResult<()> {
        let attempt = {
            let _queue = self.shared.queue.lock().unwrap();
            self.shared.closing.store(true, Ordering::Release);
            let attempt = self.shared.close_attempt.fetch_add(1, Ordering::AcqRel) + 1;
            self.shared.wake.notify_one();
            attempt
        };
        ensure_joiner(self.shared.clone());
        let waiting = async {
            loop {
                let notified = self.shared.close_notify.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                let result = self.shared.close_result.lock().unwrap().clone();
                if let Some(result) = result
                    && (result.attempt >= attempt || self.shared.exited.load(Ordering::Acquire))
                {
                    result.result?;
                    break;
                }
                notified.await;
            }
            loop {
                let notified = self.shared.join_notify.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                if self.shared.joined.load(Ordering::Acquire) {
                    break;
                }
                notified.await;
            }
            if let Some(error) = self.shared.join_error.lock().unwrap().clone() {
                return Err(error);
            }
            Ok(())
        };
        tokio::time::timeout(self.shared.config.request_timeout, waiting)
            .await
            .map_err(|_| {
                TaskError::new(
                    503,
                    "task_shutdown_pending",
                    "Планировщик ещё останавливается; блокировка книги сохраняется",
                )
            })?
    }
}
impl Drop for TaskScheduler {
    fn drop(&mut self) {
        let _queue = self.shared.queue.lock().unwrap();
        if !self.shared.exited.load(Ordering::Acquire) {
            self.shared.closing.store(true, Ordering::Release);
            self.shared.close_attempt.fetch_add(1, Ordering::AcqRel);
            self.shared.wake.notify_one();
        }
        ensure_joiner(self.shared.clone());
    }
}
fn ensure_joiner(shared: Arc<Shared>) {
    if shared.join_started.swap(true, Ordering::AcqRel) {
        return;
    }
    let runtime = shared.runtime.clone();
    runtime.spawn(async move {
        loop {
            let notified = shared.exit_notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if shared.exited.load(Ordering::Acquire) {
                break;
            }
            notified.await;
        }
        let joining = shared.clone();
        let outcome = tokio::task::spawn_blocking(move || {
            if let Some(handle) = joining.join.lock().unwrap().take() {
                handle.join().map_err(|_| {
                    TaskError::new(
                        503,
                        "task_scheduler_failed",
                        "Не удалось подтвердить завершение потока scheduler",
                    )
                })?;
            }
            Ok::<(), TaskError>(())
        })
        .await;
        let error = match outcome {
            Ok(Ok(())) => None,
            Ok(Err(error)) => Some(error),
            Err(_) => Some(TaskError::new(
                503,
                "task_scheduler_failed",
                "Не удалось дождаться потока scheduler",
            )),
        };
        *shared.join_error.lock().unwrap() = error;
        shared.joined.store(true, Ordering::Release);
        shared.join_notify.notify_waiters();
    });
}
struct Count {
    count: usize,
    maximum: usize,
}
impl Write for Count {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > self.maximum.saturating_sub(self.count) {
            return Err(std::io::Error::other("queued task bytes"));
        }
        self.count += bytes.len();
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn estimate(value: &Value, maximum: usize) -> TaskResult<usize> {
    let mut counter = Count { count: 0, maximum };
    serde_json::to_writer(&mut counter, value).map_err(|_| TaskError::resource())?;
    Ok(counter.count)
}

struct Actor {
    book: Book,
    lease: Option<BookLock>,
    shared: Arc<Shared>,
    effects: Arc<dyn TaskEffects>,
    clock: Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>,
    enabled: bool,
    epoch: u64,
    dirty: bool,
    runtime: Value,
    next_tick: Instant,
}
impl Actor {
    fn increment(&mut self, key: &str) {
        self.runtime[key] = json!(self.runtime[key].as_u64().unwrap_or(0).saturating_add(1));
    }
    fn recover(&mut self) -> TaskResult<()> {
        let mut count = 0;
        for task in &mut self.book.tasks {
            if task.is(RUNNING) {
                task.abort(CRASH);
                count += 1;
            }
        }
        if count > 0 {
            self.runtime["aborted_tasks"] = json!(count);
            self.runtime["last_result"] = json!("interrupted_recovered");
            self.dirty = true;
            self.save()?;
        }
        Ok(())
    }
    fn save(&mut self) -> TaskResult<()> {
        if self.dirty {
            self.book.save()?;
            self.dirty = false;
        }
        Ok(())
    }
    fn status(&self) -> Value {
        json!({"enabled":self.enabled,"scheduler_alive":self.enabled,"policy_persistent":false,"restart_requires_opt_in":true,"runtime":self.runtime,
        "tasks":{"total":self.book.tasks.len(),"running":self.book.tasks.iter().filter(|v|v.is(RUNNING)).count(),"completed":self.book.tasks.iter().filter(|v|v.is(COMPLETED)).count()}})
    }
    fn publish(&self) {
        *self.shared.snapshot.lock().unwrap() = self.status();
    }
    fn interrupted(&self) -> bool {
        self.shared.closing.load(Ordering::Acquire)
            || self.shared.pause_sequence.load(Ordering::Acquire) > self.epoch
    }
    fn run(&mut self) {
        let mut last_close = 0;
        loop {
            if self.shared.closing.load(Ordering::Acquire) {
                let attempt = self.shared.close_attempt.load(Ordering::Acquire);
                if attempt > last_close {
                    last_close = attempt;
                    let outcome = self.finish_close();
                    *self.shared.close_result.lock().unwrap() = Some(CloseResult {
                        attempt,
                        result: outcome.clone(),
                    });
                    self.shared.close_notify.notify_waiters();
                    if outcome.is_ok() {
                        self.reject_pending();
                        return;
                    }
                }
            }
            let shared = self.shared.clone();
            let mut queue = shared.queue.lock().unwrap();
            if let Some(command) = queue.commands.pop_front() {
                queue.bytes -= command.bytes;
                drop(queue);
                let result = if shared.closing.load(Ordering::Acquire) {
                    Err(TaskError::closed())
                } else {
                    self.command(command.work, command.sequence)
                };
                self.publish();
                let _ = command.reply.send(result);
                continue;
            }
            if !shared.closing.load(Ordering::Acquire)
                && self.enabled
                && !self.interrupted()
                && Instant::now() >= self.next_tick
            {
                drop(queue);
                if let Err(error) = self.pass((self.clock)()) {
                    self.runtime["last_result"] = json!("scheduler_error");
                    self.runtime["last_error"] = json!(error.message);
                    self.publish();
                }
                self.next_tick =
                    Instant::now() + Duration::from_secs_f64(shared.config.interval_seconds);
                continue;
            }
            if shared.closing.load(Ordering::Acquire)
                && shared.close_attempt.load(Ordering::Acquire) > last_close
            {
                drop(queue);
                continue;
            }
            if self.enabled && !self.interrupted() {
                drop(
                    shared
                        .wake
                        .wait_timeout(
                            queue,
                            self.next_tick.saturating_duration_since(Instant::now()),
                        )
                        .unwrap(),
                );
            } else {
                drop(shared.wake.wait(queue).unwrap());
            }
        }
    }
    fn reject_pending(&self) {
        let mut queue = self.shared.queue.lock().unwrap();
        for command in queue.commands.drain(..) {
            let _ = command.reply.send(Err(TaskError::closed()));
        }
        queue.bytes = 0;
    }
    fn command(&mut self, work: Work, sequence: u64) -> TaskResult<Value> {
        match work {
            Work::List => self.book.public(),
            Work::Create(task) => {
                if self
                    .book
                    .tasks
                    .iter()
                    .any(|v| v.value("name") == task.value("name"))
                {
                    return Err(TaskError::invalid(format!(
                        "Задание уже существует: {}",
                        task.value("name").as_str().unwrap_or("")
                    )));
                }
                let result = task.public()?;
                self.book.add(task)?;
                self.dirty = true;
                self.save()?;
                Ok(result)
            }
            Work::Cancel(name) => {
                let index = self
                    .book
                    .tasks
                    .iter()
                    .position(|v| v.value("name").as_str() == Some(name.as_str()))
                    .ok_or_else(|| {
                        TaskError::new(404, "task_not_found", format!("Задача не найдена: {name}"))
                    })?;
                if !self.book.tasks[index].is(QUEUED) {
                    return Err(TaskError::invalid(
                        "Отменить можно только задачу в статусе «В очереди на выполнение»",
                    ));
                }
                self.book.tasks[index].abort(OPERATOR);
                self.increment("aborted_tasks");
                self.runtime["last_task_name"] = json!(name);
                self.runtime["last_result"] = json!("cancelled");
                self.dirty = true;
                self.save()?;
                self.book.tasks[index].public()
            }
            Work::Configure(enabled) => {
                self.enabled = enabled;
                self.runtime["last_result"] = json!(if enabled { "enabled" } else { "disabled" });
                if enabled {
                    if self.epoch == 0
                        || self.shared.pause_sequence.load(Ordering::Acquire) > self.epoch
                    {
                        self.next_tick = Instant::now()
                            + Duration::from_secs_f64(self.shared.config.interval_seconds);
                    }
                    self.epoch = sequence;
                    self.shared.epoch.store(sequence, Ordering::Release);
                }
                Ok(self.status())
            }
            Work::RunOnce(moment, epoch) => {
                if !self.enabled || epoch != self.epoch || self.interrupted() {
                    Ok(Value::Null)
                } else {
                    self.pass(moment.unwrap_or_else(|| (self.clock)()))
                }
            }
        }
    }
    fn pass(&mut self, now: DateTime<Utc>) -> TaskResult<Value> {
        if !self.enabled || self.interrupted() {
            return Ok(Value::Null);
        }
        self.runtime["last_error"] = Value::Null;
        for index in 0..self.book.tasks.len() {
            if self.interrupted() {
                break;
            }
            let action = (|| -> TaskResult<Option<bool>> {
                let task = &self.book.tasks[index];
                if task.is(RUNNING) {
                    return task.expired(now).map(|due| due.then_some(false));
                }
                if task.due(now)? {
                    task.require_files()?;
                    return Ok(Some(true));
                }
                Ok(None)
            })();
            let result = match action {
                Ok(Some(start)) => {
                    if start {
                        let queued = self.book.tasks[index].clone();
                        self.book.tasks[index].start();
                        self.dirty = true;
                        if let Err(error) = self.save() {
                            // No effect ran. Retain queued intent and dirty
                            // ownership even if rename preceded a failed fsync.
                            self.book.tasks[index] = queued;
                            self.dirty = true;
                            return Err(error);
                        }
                    }
                    let context = TaskContext {
                        epoch: self.epoch,
                        task: self.book.tasks[index].clone(),
                    };
                    let callback = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        if start {
                            self.shared.runtime.block_on(self.effects.on_start(context))
                        } else {
                            self.shared
                                .runtime
                                .block_on(self.effects.on_complete(context))
                        }
                    }));
                    let result = callback.unwrap_or_else(|_| {
                        Err(TaskError::new(
                            500,
                            "task_effect_panicked",
                            "Task callback panicked",
                        ))
                    });
                    result.and_then(|()| {
                        if start {
                            self.increment("started_tasks");
                            self.runtime["last_result"] = json!("started");
                        } else {
                            self.book.tasks[index].complete()?;
                            self.increment("completed_tasks");
                            self.runtime["last_result"] = json!("completed");
                        }
                        self.runtime["last_task_name"] =
                            self.book.tasks[index].value("name").clone();
                        self.dirty = true;
                        Ok(())
                    })
                }
                Ok(None) => Ok(()),
                Err(error) => Err(error),
            };
            if let Err(error) = result {
                self.book.tasks[index].abort(&error.message);
                self.increment("aborted_tasks");
                self.runtime["last_task_name"] = self.book.tasks[index].value("name").clone();
                self.runtime["last_result"] = json!("task_error");
                self.runtime["last_error"] = json!(error.message);
                self.dirty = true;
            }
        }
        self.save()?;
        self.publish();
        Ok(self.status())
    }
    fn finish_close(&mut self) -> TaskResult<()> {
        self.enabled = false;
        let mut aborted = 0;
        for task in &mut self.book.tasks {
            if task.is(RUNNING) {
                task.abort(OPERATOR);
                aborted += 1;
            }
        }
        if aborted > 0 {
            self.runtime["aborted_tasks"] =
                json!(self.runtime["aborted_tasks"].as_u64().unwrap_or(0) + aborted);
            self.dirty = true;
        }
        if let Err(error) = self.save() {
            self.runtime["last_result"] = json!("scheduler_error");
            self.runtime["last_error"] = json!(error.message);
            self.publish();
            return Err(error);
        }
        self.runtime["last_result"] = json!("closed");
        self.publish();
        drop(self.lease.take());
        Ok(())
    }
}
