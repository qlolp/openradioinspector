use crate::Result;
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
#[derive(Clone, Serialize, Deserialize)]
pub struct WorkerSpec {
    pub program: PathBuf,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub environment: BTreeMap<String, String>,
    pub deadline_ms: u64,
    #[serde(default = "default_hard_memory")]
    pub require_hard_memory: bool,
    #[serde(default)]
    pub linux_cgroup: Option<LinuxCgroup>,
}
fn default_hard_memory() -> bool {
    true
}
#[derive(Clone, Serialize, Deserialize)]
pub struct LinuxCgroup {
    pub path: PathBuf,
    #[serde(default)]
    pub block_devices: Vec<(u32, u32)>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EnforcementCapabilities {
    pub process_tree_termination: bool,
    pub hard_memory: bool,
    pub hard_io: bool,
    pub hard_cache_quota: bool,
    pub hard_temp_quota: bool,
    pub sampled_rss: bool,
}
#[derive(Serialize, Deserialize)]
pub struct WorkerResult {
    pub response: serde_json::Value,
    pub enforcement: EnforcementCapabilities,
}
#[derive(Default, Clone)]
pub struct Cancellation(Arc<AtomicBool>);
impl Cancellation {
    pub fn cancel(&self) {
        self.0.store(true, Ordering::SeqCst);
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }
}

use crate::{Error, lease::validate_path, process::OwnedChild};
const CONTROL_REQUEST_BYTES: usize = 65_536;
const ANALYSIS_MESSAGE_BYTES: usize = 16 * 1024 * 1024;
// Offline workers need an executable/locale/temp context, never the core's
// credentials, user startup configuration or dynamic-loader injection hooks.
const WORKER_ENVIRONMENT: &[&str] = &[
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "WINDIR",
];
use std::{
    io::{Read, Write},
    process::{Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};
use tokio::sync::Semaphore;

struct WorkerOwner {
    child: OwnedChild,
    threads: Vec<thread::JoinHandle<()>>,
    joined: bool,
}
impl WorkerOwner {
    fn spawn(command: &mut Command) -> Result<Self> {
        Ok(Self {
            child: OwnedChild::spawn(command)?,
            threads: Vec::with_capacity(3),
            joined: false,
        })
    }
    fn thread(&mut self, name: &str, work: impl FnOnce() + Send + 'static) -> Result<()> {
        self.threads
            .push(thread::Builder::new().name(name.into()).spawn(work)?);
        Ok(())
    }
    fn finish(&mut self) -> Result<std::process::ExitStatus> {
        // Unknown process cleanup retains this owner and its reservation.
        let exit = loop {
            match self.child.terminate_and_reap() {
                Ok(exit) => break exit,
                Err(_) => thread::sleep(Duration::from_millis(10)),
            }
        };
        let mut failed = false;
        for handle in self.threads.drain(..) {
            failed |= handle.join().is_err();
        }
        self.joined = true;
        if failed {
            return Err(Error::Protocol("worker IPC thread failed".into()));
        }
        Ok(exit)
    }
}
impl Drop for WorkerOwner {
    fn drop(&mut self) {
        if !self.joined {
            let _ = self.finish();
        }
    }
}
/// Share one pool between all API handlers in a service. It never queues heavy jobs.
#[derive(Clone)]
pub struct ResearchPool {
    admission: Arc<Semaphore>,
}
/// One global heavy-work reservation, including preparation of its sealed source.
/// Non-cloneable. Retained execution permits sequential reuse after the prior
/// child and all IPC threads have joined; concurrent children are never admitted.
pub struct ResearchPermit {
    _permit: tokio::sync::OwnedSemaphorePermit,
}
impl Default for ResearchPool {
    fn default() -> Self {
        Self {
            admission: Arc::new(Semaphore::new(1)),
        }
    }
}
fn limits() -> serde_json::Value {
    serde_json::json!({"duckdb_threads":2,"duckdb_memory_bytes":536870912u64,"worker_rss_bytes":805306368u64,"cache_bytes":10737418240u64,"temp_bytes":10737418240u64,"io_bytes_per_second":20971520u64,"batch_rows":1024,"batch_bytes":4194304,"max_record_bytes":8388608,"max_points_per_sweep":65536})
}
fn read_bounded(mut stream: impl Read, limit: usize) -> Result<Vec<u8>> {
    let mut result = Vec::new();
    let mut bytes = [0u8; 8192];
    loop {
        let count = stream.read(&mut bytes)?;
        if count == 0 {
            return Ok(result);
        }
        if result.len() + count > limit {
            return Err(Error::OutputLimit);
        }
        result.extend_from_slice(&bytes[..count]);
    }
}
impl ResearchPool {
    pub fn try_reserve(&self) -> Result<ResearchPermit> {
        Ok(ResearchPermit {
            _permit: self
                .admission
                .clone()
                .try_acquire_owned()
                .map_err(|_| Error::Busy)?,
        })
    }

    pub async fn run(
        &self,
        spec: WorkerSpec,
        request: Vec<u8>,
        cancel: Cancellation,
    ) -> Result<WorkerResult> {
        self.try_reserve()?.run(spec, request, cancel).await
    }
}
impl ResearchPermit {
    /// Consume the preparation reservation without reacquiring the shared pool.
    pub async fn run(
        self,
        spec: WorkerSpec,
        request: Vec<u8>,
        cancel: Cancellation,
    ) -> Result<WorkerResult> {
        let (_reservation, result) = self.run_retained(spec, request, cancel).await;
        result
    }
    /// Return the same reservation after actual child/IPC JOIN, on success or
    /// failure. A dropped waiter cancels an independently owned runner which
    /// keeps admission until cleanup finishes. Sequential reuse is permitted.
    pub async fn run_retained(
        self,
        spec: WorkerSpec,
        request: Vec<u8>,
        cancel: Cancellation,
    ) -> (Self, Result<WorkerResult>) {
        struct CancelOnDrop(Option<Cancellation>);
        impl Drop for CancelOnDrop {
            fn drop(&mut self) {
                if let Some(cancel) = &self.0 {
                    cancel.cancel();
                }
            }
        }
        let mut waiter = CancelOnDrop(Some(cancel.clone()));
        let owner = tokio::task::spawn_blocking(move || {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                self.execute(spec, request, cancel)
            }))
            .unwrap_or_else(|_| Err(Error::Protocol("owned research runner failed".into())));
            (self, result)
        });
        let outcome = owner.await.expect("owned research runner completion");
        waiter.0 = None;
        outcome
    }
    fn execute(
        &self,
        spec: WorkerSpec,
        request: Vec<u8>,
        cancel: Cancellation,
    ) -> Result<WorkerResult> {
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        if request.len() > ANALYSIS_MESSAGE_BYTES
            || !spec.program.is_absolute()
            || spec.deadline_ms == 0
            || spec.deadline_ms > 86400000
        {
            return Err(Error::Unsafe("invalid bounded worker invocation".into()));
        }
        if spec.environment.iter().any(|(key, value)| {
            !WORKER_ENVIRONMENT.contains(&key.as_str())
                || value.len() > 8192
                || value.contains('\0')
        }) {
            return Err(Error::Unsafe(
                "worker environment contains an unsupported execution setting".into(),
            ));
        }
        let original_bytes = request.len();
        let mut request: serde_json::Value = strict_json(&request)?;
        let object = request
            .as_object_mut()
            .ok_or_else(|| Error::Protocol("worker request must be an object".into()))?;
        let analysis = matches!(
            object.get("operation").and_then(|value| value.as_str()),
            Some("analysis" | "control_analysis")
        );
        let request_budget = if analysis {
            ANALYSIS_MESSAGE_BYTES
        } else {
            CONTROL_REQUEST_BYTES
        };
        let response_budget = if analysis {
            ANALYSIS_MESSAGE_BYTES
        } else {
            2 * 1024 * 1024
        };
        if original_bytes > request_budget {
            return Err(Error::Unsafe(
                "worker envelope exceeds operation budget".into(),
            ));
        }
        if object.get("version").and_then(|v| v.as_u64()) != Some(1) {
            return Err(Error::Protocol("unsupported worker request version".into()));
        }
        let request_id = object
            .get("request_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty() && s.len() <= 128)
            .ok_or_else(|| Error::Protocol("invalid worker request identity".into()))?
            .to_owned();
        let mut effective = limits();
        if let Some(overrides) = object.get("limits") {
            let overrides = overrides
                .as_object()
                .ok_or_else(|| Error::Unsafe("worker limits must be an object".into()))?;
            for (key, value) in overrides {
                let maximum = effective
                    .get(key)
                    .and_then(|v| v.as_u64())
                    .ok_or_else(|| Error::Unsafe("unknown worker limit".into()))?;
                let number = value
                    .as_u64()
                    .filter(|v| *v > 0 && *v <= maximum)
                    .ok_or_else(|| Error::Unsafe("worker limits may only be lowered".into()))?;
                effective[key] = number.into();
            }
        }
        object.insert("limits".into(), effective.clone());
        let request = ori_contracts::to_json_vec(&request)
            .map_err(|_| Error::Protocol("invalid finite worker request".into()))?;
        if request.len() > request_budget {
            return Err(Error::Unsafe(
                "worker envelope exceeds request bound".into(),
            ));
        }
        let rss_limit = effective["worker_rss_bytes"].as_u64().unwrap();
        let mut capabilities = EnforcementCapabilities {
            process_tree_termination: cfg!(unix),
            hard_memory: false,
            hard_io: false,
            hard_cache_quota: false,
            hard_temp_quota: false,
            sampled_rss: cfg!(target_os = "linux"),
        };
        if !capabilities.process_tree_termination {
            return Err(Error::Unsafe("research child requires an owned process-tree containment implementation on this platform".into()));
        }
        let mut command = Command::new(&spec.program);
        command.env_clear();
        for key in WORKER_ENVIRONMENT {
            if let Some(value) = spec
                .environment
                .get(*key)
                .map(std::ffi::OsString::from)
                .or_else(|| std::env::var_os(key))
            {
                command.env(key, value);
            }
        }
        command
            .args(&spec.args)
            .env("PYTHONNOUSERSITE", "1")
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("OMP_NUM_THREADS", "1")
            .env("OPENBLAS_NUM_THREADS", "1")
            .env("MKL_NUM_THREADS", "1")
            .env("NUMEXPR_NUM_THREADS", "1")
            .env("VECLIB_MAXIMUM_THREADS", "1")
            .env("ARROW_NUM_THREADS", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(target_os = "linux")]
        {
            use std::os::unix::process::CommandExt;
            unsafe {
                command.pre_exec(|| {
                    if libc::setpriority(libc::PRIO_PROCESS, 0, 19) < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    // Idle I/O priority is a scheduling hint, never claimed as a hard byte-rate cap.
                    let _ = libc::syscall(libc::SYS_ioprio_set, 1, 0, 3 << 13);
                    Ok(())
                });
            }
        }
        if let Some(cgroup) = spec.linux_cgroup.as_ref() {
            cgroup.configure(&mut command, rss_limit, &mut capabilities)?;
        }
        if spec.require_hard_memory && !capabilities.hard_memory {
            return Err(Error::Unsafe("hard child memory budget requires a delegated Linux cgroup; best effort must be explicitly selected".into()));
        }
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        let mut owner = WorkerOwner::spawn(&mut command)?;
        let mut stdin = owner.child.child_mut().stdin.take().unwrap();
        let stdout = owner.child.child_mut().stdout.take().unwrap();
        let stderr = owner.child.child_mut().stderr.take().unwrap();
        let (sender, receiver) = mpsc::channel();
        let (write_sender, write_receiver) = mpsc::channel();
        owner.thread("ori-research-stdin", move || {
            let result = stdin.write_all(&request);
            drop(stdin);
            let _ = write_sender.send(result);
        })?;
        let out_sender = sender.clone();
        owner.thread("ori-research-stdout", move || {
            let _ = out_sender.send((true, read_bounded(stdout, response_budget)));
        })?;
        owner.thread("ori-research-stderr", move || {
            let _ = sender.send((false, read_bounded(stderr, 65536)));
        })?;
        let deadline = Instant::now() + Duration::from_millis(spec.deadline_ms);
        let mut output = None;
        let mut failure = None;
        loop {
            for (is_stdout, result) in receiver.try_iter() {
                match result {
                    Ok(bytes) if is_stdout => output = Some(bytes),
                    Ok(_) => {}
                    Err(error) => failure = Some(error),
                }
            }
            if failure.is_some() {
                break;
            }
            if cancel.is_cancelled() {
                failure = Some(Error::Cancelled);
                break;
            }
            if Instant::now() >= deadline {
                failure = Some(Error::Deadline);
                break;
            }
            #[cfg(target_os = "linux")]
            if group_rss(owner.child.pid()) > rss_limit {
                failure = Some(Error::ResourceLimit);
                break;
            }
            if owner.child.exited()? {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        let exit = owner.finish()?;
        let write_result = write_receiver
            .recv()
            .map_err(|_| Error::Protocol("worker stdin thread failed".into()))?;
        for (is_stdout, result) in receiver.try_iter() {
            match result {
                Ok(bytes) if is_stdout => output = Some(bytes),
                Ok(_) => {}
                Err(error) => {
                    if failure.is_none() {
                        failure = Some(error)
                    }
                }
            }
        }
        if let Some(error) = failure {
            return Err(error);
        }
        write_result?;
        let response: serde_json::Value = strict_json(
            &output.ok_or_else(|| Error::Protocol("worker returned no result".into()))?,
        )?;
        if response["version"] != 1
            || response["request_id"] != request_id
            || !response["ok"].is_boolean()
            || (response["ok"] == true && !exit.success())
        {
            return Err(Error::Protocol(
                "worker response identity/exit mismatch".into(),
            ));
        }
        Ok(WorkerResult {
            response,
            enforcement: capabilities,
        })
    }
}

impl LinuxCgroup {
    fn configure(
        &self,
        command: &mut Command,
        memory: u64,
        capabilities: &mut EnforcementCapabilities,
    ) -> Result<()> {
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (command, memory, capabilities);
            return Err(Error::Unsafe(
                "Linux cgroups are unavailable on this platform".into(),
            ));
        }
        #[cfg(target_os = "linux")]
        {
            use std::{
                ffi::CString,
                os::unix::{ffi::OsStrExt, process::CommandExt},
            };
            validate_path(&self.path)?;
            if !self.path.starts_with("/sys/fs/cgroup")
                || self.path == std::path::Path::new("/sys/fs/cgroup")
            {
                return Err(Error::Unsafe(
                    "a dedicated delegated cgroup v2 leaf is required".into(),
                ));
            }
            let path = CString::new(self.path.as_os_str().as_bytes())
                .map_err(|_| Error::Unsafe("invalid cgroup path".into()))?;
            let mut stat: libc::statfs = unsafe { std::mem::zeroed() };
            if unsafe { libc::statfs(path.as_ptr(), &mut stat) } < 0
                || stat.f_type != libc::CGROUP2_SUPER_MAGIC
            {
                return Err(Error::Unsafe(
                    "cgroup path is not a cgroup v2 filesystem".into(),
                ));
            }
            if !std::fs::read_to_string(self.path.join("cgroup.procs"))?
                .trim()
                .is_empty()
            {
                return Err(Error::Busy);
            }
            for (file, value) in [
                ("memory.max", memory.to_string()),
                ("memory.swap.max", "0".into()),
                ("pids.max", "32".into()),
                ("cpu.weight", "10".into()),
                ("io.weight", "default 10".into()),
            ] {
                std::fs::write(self.path.join(file), value)?;
            }
            if !self.block_devices.is_empty() {
                let rate = 20971520u64 / (2 * self.block_devices.len() as u64);
                for (major, minor) in &self.block_devices {
                    std::fs::write(
                        self.path.join("io.max"),
                        format!("{major}:{minor} rbps={rate} wbps={rate}"),
                    )?;
                }
                capabilities.hard_io = true;
            }
            let attach =
                CString::new(self.path.join("cgroup.procs").as_os_str().as_bytes()).unwrap();
            unsafe {
                command.pre_exec(move || {
                    let fd = libc::open(
                        attach.as_ptr(),
                        libc::O_WRONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
                    );
                    if fd < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    let written = libc::write(fd, b"0".as_ptr().cast(), 1);
                    let error = std::io::Error::last_os_error();
                    libc::close(fd);
                    if written != 1 {
                        return Err(error);
                    }
                    Ok(())
                });
            }
            capabilities.hard_memory = true;
            Ok(())
        }
    }
}

#[cfg(target_os = "linux")]
fn group_rss(group: u32) -> u64 {
    let page = unsafe { libc::sysconf(libc::_SC_PAGESIZE) }.max(1) as u64;
    let mut total = 0u64;
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return 0;
    };
    for entry in entries.flatten() {
        if entry.file_name().to_string_lossy().parse::<u32>().is_err() {
            continue;
        }
        let Ok(stat) = std::fs::read_to_string(entry.path().join("stat")) else {
            continue;
        };
        let Some((_, fields)) = stat.rsplit_once(") ") else {
            continue;
        };
        let fields: Vec<_> = fields.split_whitespace().collect();
        if fields.get(2).and_then(|s| s.parse::<u32>().ok()) != Some(group) {
            continue;
        }
        if let Some(rss) = fields.get(21).and_then(|s| s.parse::<u64>().ok()) {
            total = total.saturating_add(rss.saturating_mul(page));
        }
    }
    total
}

// Use the shared parser: serde's arbitrary-precision protocol must never alias
// actual metadata objects or round large integers at either IPC boundary.
fn strict_json(bytes: &[u8]) -> Result<serde_json::Value> {
    ori_contracts::parse_json_value(bytes, ANALYSIS_MESSAGE_BYTES)
        .map_err(|_| Error::Protocol("invalid strict worker JSON".into()))
}
