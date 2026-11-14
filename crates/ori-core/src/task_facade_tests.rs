//! An injected owned file producer; this fixture never produces measurements.
use super::*;
use crate::task_execution::{
    TaskBinding, TaskCompletion, TaskExecutionCallbacks, TaskExecutionConfig, TaskInput, TaskRun,
    TaskTarget,
};
use std::sync::{Condvar, atomic::AtomicUsize};
use tokio::sync::Semaphore;

struct Producer {
    captures: AtomicUsize,
    joins: AtomicUsize,
    started: Semaphore,
    joining: Semaphore,
    state: Mutex<(bool, bool)>,
    changed: Condvar,
}
impl Default for Producer {
    fn default() -> Self {
        Self {
            captures: AtomicUsize::new(0),
            joins: AtomicUsize::new(0),
            started: Semaphore::new(0),
            joining: Semaphore::new(0),
            state: Mutex::new((false, false)),
            changed: Condvar::new(),
        }
    }
}
struct Callbacks {
    producer: Arc<Producer>,
}
struct Target {
    producer: Arc<Producer>,
    original: Arc<dyn TaskAuthority>,
    path: std::path::PathBuf,
}
struct Run {
    producer: Arc<Producer>,
    path: std::path::PathBuf,
    job: Mutex<Option<tokio::task::JoinHandle<std::io::Result<()>>>>,
}
impl TaskExecutionCallbacks for Callbacks {
    fn capture(
        &self,
        binding: &TaskBinding,
        original: Arc<dyn TaskAuthority>,
    ) -> TaskResult<Arc<dyn TaskTarget>> {
        original.revalidate()?;
        self.producer.captures.fetch_add(1, Ordering::AcqRel);
        let TaskInput::FixedFrequencyList(path) = &binding.input else {
            panic!("fixture input kind")
        };
        Ok(Arc::new(Target {
            producer: self.producer.clone(),
            original,
            path: path.with_extension("producer"),
        }))
    }
}
impl TaskTarget for Target {
    fn revalidate(&self) -> TaskResult<()> {
        self.original.revalidate()
    }
    fn prepare(&self, _: ori_tasks::TaskContext) -> TaskResult<Arc<dyn TaskRun>> {
        self.revalidate()?;
        Ok(Arc::new(Run {
            producer: self.producer.clone(),
            path: self.path.clone(),
            job: Mutex::new(None),
        }))
    }
}
impl TaskRun for Run {
    fn start(&self) -> ori_tasks::TaskFuture<'_, ()> {
        Box::pin(async move {
            let producer = self.producer.clone();
            let path = self.path.clone();
            let task = tokio::task::spawn_blocking(move || {
                let file = std::fs::File::create(path)?;
                producer.started.add_permits(1);
                let mut state = producer.state.lock().unwrap();
                while !state.0 {
                    state = producer.changed.wait(state).unwrap();
                }
                producer.joining.add_permits(1);
                while !state.1 {
                    state = producer.changed.wait(state).unwrap();
                }
                drop(file);
                Ok(())
            });
            *self.job.lock().unwrap() = Some(task);
            Ok(())
        })
    }
    fn finish(&self) -> ori_tasks::TaskFuture<'_, TaskCompletion> {
        Box::pin(async {
            Err(ori_tasks::TaskError::new(
                409,
                "synthetic_no_measurement",
                "Synthetic producer never confirms a measurement",
            ))
        })
    }
    fn cancel(&self) {
        self.producer.state.lock().unwrap().0 = true;
        self.producer.changed.notify_all();
    }
    fn join(&self) -> ori_tasks::TaskFuture<'_, ()> {
        let job = self.job.lock().unwrap().take();
        Box::pin(async move {
            if let Some(job) = job {
                job.await.unwrap().unwrap();
                self.producer.joins.fetch_add(1, Ordering::AcqRel);
            }
            Ok(())
        })
    }
}
#[tokio::test]
async fn optional_task_facade_retains_disable_after_waiter_drop_until_real_file_thread_join() {
    let data = tempfile::tempdir().unwrap();
    let input = data.path().join("synthetic.task");
    std::fs::write(&input, b"not a native measurement recipe").unwrap();
    let producer = Arc::new(Producer::default());
    let mut config = crate::CoreConfig::replay(data.path());
    config.task_execution = Some(TaskExecutionConfig {
        bindings: vec![TaskBinding {
            task_name: "owned".into(),
            input: TaskInput::FixedFrequencyList(input.clone()),
            session_id: "tasks".into(),
            stream_id: "receiver".into(),
            level_unit: "s_meter_count".into(),
        }],
        callbacks: Arc::new(Callbacks {
            producer: producer.clone(),
        }),
        max_active_runs: 1,
        close_timeout: std::time::Duration::from_millis(10),
    });
    let core = Core::open(config).await.unwrap();
    let tasks = core.tasks.get().unwrap().clone();
    let auth = core
        .security
        .check(
            &axum::http::Method::POST,
            "/api/v1/task-scheduler",
            &axum::http::HeaderMap::new(),
            crate::security::ListenerRole::Plain,
        )
        .await
        .unwrap()
        .receipt();
    let start = chrono::Utc::now() + chrono::Duration::hours(1);
    tasks.scheduler.create_task(json!({"name":"owned","start_at":start.to_rfc3339_opts(chrono::SecondsFormat::Micros,true),"duration_s":60,"task_file":input})).await.unwrap();
    let status = tasks
        .configure(json!({"enabled":true}), auth.clone())
        .await
        .unwrap();
    tasks.scheduler.run_once(Some(start)).await.unwrap();
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        producer.started.acquire(),
    )
    .await
    .unwrap()
    .unwrap()
    .forget();
    let facade = tasks.clone();
    let waiter =
        tokio::spawn(async move { facade.configure(json!({"enabled":false}), auth).await });
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        producer.joining.acquire(),
    )
    .await
    .unwrap()
    .unwrap()
    .forget();
    waiter.abort();
    assert!(waiter.await.unwrap_err().is_cancelled());
    let attempt = tasks.execution.as_ref().unwrap().begin_drain();
    let observer_pending = attempt.completed().await.is_err();
    let original = core.clone();
    let closing = tokio::spawn(async move { original.close().await });
    let held = ori_runtime::lease::WriterLease::acquire(data.path()).is_err();
    producer.state.lock().unwrap().1 = true;
    producer.changed.notify_all();
    tokio::time::timeout(std::time::Duration::from_secs(3), closing)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let released = ori_runtime::lease::WriterLease::acquire(data.path()).is_ok();
    assert_eq!(status["enabled"], true);
    assert_eq!(status["execution_available"], true);
    assert!(observer_pending && held && released);
    assert_eq!(producer.captures.load(Ordering::Acquire), 1);
    assert_eq!(producer.joins.load(Ordering::Acquire), 1);
}
