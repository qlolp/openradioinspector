//! One supervisor owns heavy task handles through completion, including when
//! request/close futures disappear. Commands never accept SQL or filesystem paths.
use crate::job::{JobWork, Record, progress, run};
use crate::{
    ExportReader, ExportTable, HistoryConfig, HistoryError, HistoryRequest, JobOutput, JobView,
    Operation, Result, ViewportPage, ViewportQuery,
    storage::{Roots, SealedSnapshot},
};
use ori_archive::Archive;
use ori_runtime::{
    qos::{BackgroundQos, BackgroundWorkGuard},
    worker::{Cancellation, ResearchPermit, ResearchPool, WorkerSpec},
};
use rand::RngCore;
use std::{
    collections::VecDeque,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::{
    sync::{Notify, mpsc, oneshot},
    task::JoinHandle,
};

type Reply<T> = oneshot::Sender<Result<T>>;
enum Command {
    Submit(
        HistoryRequest,
        Option<(Arc<SealedSnapshot>, Option<String>)>,
        Reply<String>,
    ),
    Replay(String, Option<String>, Reply<String>),
    Status(String, Reply<JobView>),
    Jobs(Reply<Vec<JobView>>),
    Result(String, Reply<Arc<JobOutput>>),
    Cancel(String, Reply<()>),
    Viewport(ViewportQuery, Cancellation, Reply<ViewportPage>),
    Export(String, ExportTable, Reply<ExportReader>),
}
struct Completion {
    closing: AtomicBool,
    finished: Mutex<Option<Result<()>>>,
    wake: Notify,
    done: Notify,
}
impl Completion {
    fn finish(&self, result: Result<()>) {
        if let Ok(mut finished) = self.finished.lock() {
            *finished = Some(result);
        }
        self.done.notify_waiters();
    }
}
#[derive(Clone)]
pub struct HistoryService {
    sender: mpsc::Sender<Command>,
    completion: Arc<Completion>,
    qos: BackgroundQos,
}
struct Running {
    id: Option<String>,
    progress: Option<Arc<Mutex<JobView>>>,
    cancel: Cancellation,
    join: JoinHandle<TaskResult>,
    _guard: BackgroundWorkGuard,
    viewport: Option<Reply<ViewportPage>>,
}
enum TaskResult {
    Job(Record),
    Viewport(Result<ViewportPage>),
}
struct Supervisor {
    archive: Archive,
    pool: ResearchPool,
    worker: WorkerSpec,
    config: HistoryConfig,
    roots: Arc<Roots>,
    nonce: String,
    receiver: mpsc::Receiver<Command>,
    completion: Arc<Completion>,
    running: Option<Running>,
    completed: VecDeque<Record>,
    retained_bytes: usize,
}
fn nonce() -> String {
    let mut bytes = [0; 16];
    rand::thread_rng().fill_bytes(&mut bytes);
    bytes.iter().map(|v| format!("{v:02x}")).collect()
}
fn job_id(id: &str) -> Result<()> {
    if id.len() != 32
        || !id
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(HistoryError::Invalid("history job identifier"));
    }
    Ok(())
}
impl HistoryService {
    pub async fn new(
        archive: Archive,
        pool: ResearchPool,
        worker: WorkerSpec,
        config: HistoryConfig,
    ) -> Result<Self> {
        config.validate()?;
        if !worker.program.is_absolute()
            || !worker.program.is_file()
            || worker.args != ["-I", "-m", "ori_research"]
            || worker.deadline_ms == 0
            || worker.deadline_ms > 86_400_000
            || worker.require_hard_memory && worker.linux_cgroup.is_none()
        {
            return Err(HistoryError::WorkerUnavailable);
        }
        // The existing runtime currently refuses uncontained Windows children.
        // Refuse before creating a private snapshot, preserving explicit legacy fallback.
        if !cfg!(unix) {
            return Err(HistoryError::WorkerUnavailable);
        }
        let roots = {
            let config = config.clone();
            Arc::new(
                tokio::task::spawn_blocking(move || Roots::open(&config))
                    .await
                    .map_err(|_| HistoryError::Internal)??,
            )
        };
        let (sender, receiver) = mpsc::channel(16);
        let completion = Arc::new(Completion {
            closing: AtomicBool::new(false),
            finished: Mutex::new(None),
            wake: Notify::new(),
            done: Notify::new(),
        });
        let service = Self {
            sender,
            completion: completion.clone(),
            qos: config.qos.clone(),
        };
        let supervisor = Supervisor {
            archive,
            pool,
            worker,
            config,
            roots,
            nonce: nonce(),
            receiver,
            completion,
            running: None,
            completed: VecDeque::new(),
            retained_bytes: 0,
        };
        // The supervisor, rather than an HTTP caller, retains every heavy JoinHandle.
        tokio::spawn(supervisor.run());
        Ok(service)
    }
    pub fn qos(&self) -> BackgroundQos {
        self.qos.clone()
    }
    async fn call<T>(&self, make: impl FnOnce(Reply<T>) -> Command) -> Result<T> {
        if self.completion.closing.load(Ordering::Acquire) {
            return Err(HistoryError::Closed);
        }
        let (reply, answer) = oneshot::channel();
        self.sender
            .try_send(make(reply))
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => HistoryError::Busy,
                mpsc::error::TrySendError::Closed(_) => HistoryError::Closed,
            })?;
        answer.await.map_err(|_| HistoryError::Closed)?
    }
    pub async fn submit(&self, request: HistoryRequest) -> Result<String> {
        request.validate()?;
        self.call(|reply| Command::Submit(request, None, reply))
            .await
    }
    pub async fn replay(&self, id: &str, cursor: Option<String>) -> Result<String> {
        job_id(id)?;
        if cursor
            .as_deref()
            .is_some_and(|v| v.is_empty() || v.len() > 4096 || v.chars().any(char::is_control))
        {
            return Err(HistoryError::Invalid("history page cursor"));
        }
        self.call(|reply| Command::Replay(id.into(), cursor, reply))
            .await
    }
    pub async fn status(&self, id: &str) -> Result<JobView> {
        job_id(id)?;
        self.call(|reply| Command::Status(id.into(), reply)).await
    }
    pub async fn jobs(&self) -> Result<Vec<JobView>> {
        self.call(Command::Jobs).await
    }
    pub async fn result(&self, id: &str) -> Result<Arc<JobOutput>> {
        job_id(id)?;
        self.call(|reply| Command::Result(id.into(), reply)).await
    }
    pub async fn cancel(&self, id: &str) -> Result<()> {
        job_id(id)?;
        self.call(|reply| Command::Cancel(id.into(), reply)).await
    }
    pub async fn export_part(&self, id: &str, table: ExportTable) -> Result<ExportReader> {
        job_id(id)?;
        self.call(|reply| Command::Export(id.into(), table, reply))
            .await
    }
    pub async fn viewport(&self, query: ViewportQuery) -> Result<ViewportPage> {
        query.validate()?;
        struct CancelOnDrop(Cancellation);
        impl Drop for CancelOnDrop {
            fn drop(&mut self) {
                self.0.cancel();
            }
        }
        let cancel = CancelOnDrop(Cancellation::default());
        self.call(|reply| Command::Viewport(query, cancel.0.clone(), reply))
            .await
    }
    pub async fn close(&self) -> Result<()> {
        self.completion.closing.store(true, Ordering::Release);
        self.completion.wake.notify_one();
        loop {
            let done = self.completion.done.notified();
            if let Some(result) = self
                .completion
                .finished
                .lock()
                .map_err(|_| HistoryError::Internal)?
                .clone()
            {
                return result;
            }
            done.await;
        }
    }
}
impl Supervisor {
    fn admission(&self, cancel: Cancellation) -> Result<(ResearchPermit, BackgroundWorkGuard)> {
        if self.roots.cleanup_failed() {
            return Err(HistoryError::Internal);
        }
        if self.running.is_some() || self.roots.reader_count()? > 0 {
            return Err(HistoryError::Busy);
        }
        let permit = self.pool.try_reserve()?;
        let guard = self.config.qos.register(cancel)?;
        Ok((permit, guard))
    }
    fn submit(
        &mut self,
        request: HistoryRequest,
        replay: Option<(Arc<SealedSnapshot>, Option<String>)>,
        reply: Reply<String>,
    ) {
        if reply.is_closed() {
            return;
        }
        let cancel = Cancellation::default();
        let admitted = self.admission(cancel.clone());
        let (permit, guard) = match admitted {
            Ok(v) => v,
            Err(error) => {
                let _ = reply.send(Err(error));
                return;
            }
        };
        let id = nonce();
        let view = Arc::new(Mutex::new(JobView::new(id.clone(), request.operation)));
        let archive = self.archive.clone();
        let roots = self.roots.clone();
        let config = self.config.clone();
        let worker = self.worker.clone();
        let current = view.clone();
        let token = cancel.clone();
        let work = JobWork {
            archive,
            roots,
            config,
            worker,
            request,
            replay,
            permit: Some(permit),
            cancel: token,
            current,
        };
        let join = tokio::spawn(async move { TaskResult::Job(run(work).await) });
        self.running = Some(Running {
            id: Some(id.clone()),
            progress: Some(view),
            cancel,
            join,
            _guard: guard,
            viewport: None,
        });
        if reply.send(Ok(id)).is_err()
            && let Some(running) = &self.running
        {
            running.cancel.cancel();
        }
    }
    fn handle(&mut self, command: Command) {
        match command {
            Command::Submit(request, replay, reply) => self.submit(request, replay, reply),
            Command::Replay(id, cursor, reply) => {
                let source = self
                    .completed
                    .iter()
                    .find(|v| v.view.id == id)
                    .and_then(|r| r.snapshot.clone().map(|s| (r.request.clone(), s)));
                match source {
                    Some((request, snapshot))
                        if request.operation != Operation::Export || cursor.is_none() =>
                    {
                        self.submit(request, Some((snapshot, cursor)), reply)
                    }
                    Some(_) => {
                        let _ = reply.send(Err(HistoryError::Invalid("export has no page cursor")));
                    }
                    None => {
                        let _ = reply.send(Err(HistoryError::NotFound));
                    }
                }
            }
            Command::Status(id, reply) => {
                let result = if let Some(running) = self
                    .running
                    .as_ref()
                    .filter(|r| r.id.as_deref() == Some(id.as_str()))
                {
                    running
                        .progress
                        .as_ref()
                        .ok_or(HistoryError::NotFound)
                        .and_then(progress)
                } else {
                    self.completed
                        .iter()
                        .find(|r| r.view.id == id)
                        .map(|r| r.view.clone())
                        .ok_or(HistoryError::NotFound)
                };
                let _ = reply.send(result);
            }
            Command::Jobs(reply) => {
                let mut jobs = self
                    .completed
                    .iter()
                    .map(|r| r.view.clone())
                    .collect::<Vec<_>>();
                if let Some(view) = self
                    .running
                    .as_ref()
                    .and_then(|r| r.progress.as_ref())
                    .and_then(|v| progress(v).ok())
                {
                    jobs.push(view);
                }
                let _ = reply.send(Ok(jobs));
            }
            Command::Result(id, reply) => {
                let _ = reply.send(
                    self.completed
                        .iter()
                        .find(|r| r.view.id == id)
                        .and_then(|r| r.output.clone())
                        .ok_or(HistoryError::NotFound),
                );
            }
            Command::Export(id, table, reply) => {
                let result = if self.running.is_some() {
                    Err(HistoryError::Busy)
                } else {
                    self.completed
                        .iter()
                        .find(|r| r.view.id == id && r.request.operation == Operation::Export)
                        .and_then(|r| r.snapshot.clone().zip(r.output.clone()))
                        .ok_or(HistoryError::NotFound)
                        .and_then(|(source, output)| {
                            crate::export::open(
                                self.roots.clone(),
                                source,
                                &output,
                                table,
                                self.config.qos.clone(),
                                self.pool.clone(),
                            )
                        })
                };
                let _ = reply.send(result);
            }
            Command::Cancel(id, reply) => {
                let result = if let Some(running) = self
                    .running
                    .as_ref()
                    .filter(|r| r.id.as_deref() == Some(id.as_str()))
                {
                    running.cancel.cancel();
                    Ok(())
                } else if self.completed.iter().any(|r| r.view.id == id) {
                    Ok(())
                } else {
                    Err(HistoryError::NotFound)
                };
                let _ = reply.send(result);
            }
            Command::Viewport(query, cancel, reply) => {
                if reply.is_closed() {
                    return;
                }
                let (permit, guard) = match self.admission(cancel.clone()) {
                    Ok(v) => v,
                    Err(error) => {
                        let _ = reply.send(Err(error));
                        return;
                    }
                };
                let archive = self.archive.clone();
                let roots = self.roots.clone();
                let nonce = self.nonce.clone();
                let qos = self.config.qos.clone();
                let token = cancel.clone();
                let join = tokio::spawn(async move {
                    let result =
                        crate::viewport::build(archive, roots, nonce, query, qos, token).await;
                    drop(permit);
                    TaskResult::Viewport(result)
                });
                self.running = Some(Running {
                    id: None,
                    progress: None,
                    cancel,
                    join,
                    _guard: guard,
                    viewport: Some(reply),
                });
            }
        }
    }
    async fn finish_task(
        &mut self,
        outcome: std::result::Result<TaskResult, tokio::task::JoinError>,
    ) -> Result<()> {
        let running = self.running.take().ok_or(HistoryError::Internal)?;
        let mut completed_permit = None;
        match outcome {
            Ok(TaskResult::Viewport(page)) => {
                if let Some(reply) = running.viewport {
                    let _ = reply.send(page);
                }
            }
            Ok(TaskResult::Job(mut record)) => {
                completed_permit = record.permit.take();
                self.retained_bytes = self.retained_bytes.saturating_add(record.bytes);
                self.completed.push_back(record);
                let mut retired = Vec::new();
                while self.completed.len() > self.config.completed_limit
                    || self.retained_bytes > self.config.retained_result_bytes
                {
                    if let Some(record) = self.completed.pop_front() {
                        self.retained_bytes = self.retained_bytes.saturating_sub(record.bytes);
                        retired.push(record);
                    }
                }
                // Keep cleanup owned and JOINED; only disposable snapshots are
                // dropped, after every worker using their Arc has exited.
                if !retired.is_empty() {
                    tokio::task::spawn_blocking(move || drop(retired))
                        .await
                        .map_err(|_| HistoryError::Internal)?;
                }
            }
            Err(_) => return Err(HistoryError::Internal),
        }
        // The QoS guard is held in `running` through actual task JOIN/cleanup.
        if self.roots.cleanup_failed() {
            // An unknown source cleanup cannot acknowledge a free global
            // launch slot. Keep it quarantined until the core is restarted.
            std::mem::forget(completed_permit);
            return Err(HistoryError::Internal);
        }
        drop(completed_permit);
        Ok(())
    }
    async fn run(mut self) {
        struct FinishOnDrop(Arc<Completion>);
        impl Drop for FinishOnDrop {
            fn drop(&mut self) {
                if self.0.finished.lock().is_ok_and(|v| v.is_none()) {
                    self.0.finish(Err(HistoryError::Internal));
                }
            }
        }
        let _finish = FinishOnDrop(self.completion.clone());
        let mut failure = None;
        loop {
            if self.completion.closing.load(Ordering::Acquire) {
                if let Some(running) = self.running.as_mut() {
                    running.cancel.cancel();
                    let outcome = (&mut running.join).await;
                    if let Err(error) = self.finish_task(outcome).await {
                        failure = Some(error);
                    }
                }
                break;
            }
            enum Event {
                Command(Option<Command>),
                Done(std::result::Result<TaskResult, tokio::task::JoinError>),
                Wake,
            }
            let event = if let Some(running) = self.running.as_mut() {
                tokio::select! { result=&mut running.join=>Event::Done(result), command=self.receiver.recv()=>Event::Command(command), _=self.completion.wake.notified()=>Event::Wake }
            } else {
                tokio::select! {command=self.receiver.recv()=>Event::Command(command),_=self.completion.wake.notified()=>Event::Wake}
            };
            match event {
                Event::Command(Some(command))
                    if !self.completion.closing.load(Ordering::Acquire) =>
                {
                    self.handle(command)
                }
                Event::Command(Some(_)) => {}
                Event::Command(None) => {
                    self.completion.closing.store(true, Ordering::Release);
                }
                Event::Wake => {}
                Event::Done(outcome) => {
                    if let Err(error) = self.finish_task(outcome).await {
                        failure = Some(error);
                        self.completion.closing.store(true, Ordering::Release);
                    }
                }
            }
        }
        let completed = std::mem::take(&mut self.completed);
        let roots = self.roots.clone();
        match tokio::task::spawn_blocking(move || roots.close_readers()).await {
            Ok(Ok(())) => {}
            _ => failure = Some(HistoryError::Internal),
        }
        if tokio::task::spawn_blocking(move || drop(completed))
            .await
            .is_err()
        {
            failure = Some(HistoryError::Internal);
        }
        if self.roots.cleanup_failed() {
            failure = Some(HistoryError::Internal);
        }
        // No task can still read an owned snapshot when successful close is acknowledged.
        drop(self.roots);
        self.completion.finish(failure.map_or(Ok(()), Err));
    }
}
