use crate::Result;
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    net::SocketAddr,
    path::{Path, PathBuf},
};
#[derive(Clone, Serialize, Deserialize)]
pub struct BackendSpec {
    pub name: String,
    pub program: PathBuf,
    pub args: Vec<String>,
    #[serde(default)]
    pub environment: BTreeMap<String, String>,
    pub control_addr: SocketAddr,
    pub ports: Vec<SocketAddr>,
    pub startup_timeout_ms: u64,
    pub shutdown_timeout_ms: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RuntimeStatus {
    pub version: u32,
    pub instance_id: String,
    pub pid: u32,
    pub ready: bool,
    pub preflight_complete: bool,
    pub guard_armed: bool,
    pub writer_owned: bool,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ShutdownReason {
    Shutdown,
    Cutover,
    Rollback,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct ShutdownRequest {
    pub version: u32,
    pub preserve_resume_requested: bool,
    pub reason: ShutdownReason,
    #[serde(default)]
    pub acknowledge_coverage_gap: bool,
}
pub struct CutoverOptions {
    pub automatic: bool,
    pub acknowledge_coverage_gap: bool,
}
pub enum CutoverOutcome {
    Activated,
    RolledBack { candidate_error: String },
}

use crate::{
    Error,
    lease::{InstanceLease, WriterLease, open_private, state_directory},
    process::OwnedChild,
};
use std::{
    fs::File,
    io::Write,
    net::TcpListener,
    process::{Command, Stdio},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    time::{Instant, sleep, timeout},
};

struct Backend {
    spec: BackendSpec,
    child: OwnedChild,
    token: String,
    instance_id: String,
}
pub struct Supervisor {
    data: PathBuf,
    _instance: InstanceLease,
    active: Option<Backend>,
    journal: File,
}
impl Supervisor {
    pub fn new(data: &Path) -> Result<Self> {
        let instance = InstanceLease::acquire(data)?;
        let mut journal = open_private(&state_directory(data)?.join("coverage.ndjson"))?;
        use std::io::{Seek, SeekFrom};
        journal.seek(SeekFrom::End(0))?;
        Ok(Self {
            data: data.to_owned(),
            _instance: instance,
            active: None,
            journal,
        })
    }
    fn record(&mut self, event: &str, backend: &str) -> Result<()> {
        let unix_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| Error::Unsafe("clock before epoch".into()))?
            .as_millis();
        serde_json::to_writer(
            &mut self.journal,
            &serde_json::json!({"version":1,"event":event,"backend":backend,"unix_ms":unix_ms}),
        )?;
        self.journal.write_all(b"\n")?;
        self.journal.sync_all()?;
        Ok(())
    }
    pub(crate) fn record_sleep_gap(&mut self) -> Result<()> {
        let name = self
            .active
            .as_ref()
            .ok_or_else(|| Error::Protocol("no owned backend".into()))?
            .spec
            .name
            .clone();
        self.record("system_sleep_coverage_gap", &name)
    }
    fn validate(spec: &BackendSpec) -> Result<()> {
        if !spec.program.is_absolute()
            || !spec.control_addr.ip().is_loopback()
            || spec.control_addr.port() == 0
            || spec.ports.is_empty()
            || !spec.ports.contains(&spec.control_addr)
            || spec.startup_timeout_ms == 0
            || spec.shutdown_timeout_ms == 0
            || spec.name.is_empty()
            || spec.name.len() > 128
        {
            return Err(Error::Unsafe("backend requires absolute executable, explicit ports, loopback control and bounded deadlines".into()));
        }
        if spec
            .environment
            .keys()
            .any(|key| key.starts_with("ORI_RUNTIME_"))
        {
            return Err(Error::Unsafe("reserved runtime environment key".into()));
        }
        Ok(())
    }
    fn released(&self, spec: &BackendSpec) -> Result<()> {
        let _writer = WriterLease::acquire(&self.data)?;
        // Bind the configured listener addresses; connect-only checks miss bound, non-listening sockets.
        let mut reservations = Vec::new();
        for address in &spec.ports {
            reservations.push(TcpListener::bind(address).map_err(|_| {
                Error::Unsafe("backend port is occupied; orderly legacy handover required".into())
            })?);
        }
        Ok(())
    }
    async fn wait_release(&self, spec: &BackendSpec) -> Result<()> {
        let deadline = Instant::now() + Duration::from_millis(spec.shutdown_timeout_ms);
        loop {
            if self.released(spec).is_ok() {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(Error::Unsafe(
                    "writer lease or listener remains owned after process exit; handover required"
                        .into(),
                ));
            }
            sleep(Duration::from_millis(20)).await;
        }
    }
    pub async fn start(&mut self, spec: BackendSpec) -> Result<RuntimeStatus> {
        if self.active.is_some() {
            return Err(Error::Busy);
        }
        Self::validate(&spec)?;
        self.released(&spec)?;
        let token = crate::security::random_secret();
        let instance_id = crate::security::random_secret();
        let mut command = Command::new(&spec.program);
        command
            .args(&spec.args)
            .envs(&spec.environment)
            .env("ORI_RUNTIME_CONTROL_TOKEN", &token)
            .env("ORI_RUNTIME_INSTANCE_ID", &instance_id)
            .env("ORI_RUNTIME_DATA_DIR", &self.data)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let child = OwnedChild::spawn(&mut command)?;
        self.active = Some(Backend {
            spec: spec.clone(),
            child,
            token,
            instance_id,
        });
        let deadline = Instant::now() + Duration::from_millis(spec.startup_timeout_ms);
        loop {
            let child = self.active.as_mut().unwrap();
            if child.child.exited()? {
                self.force_stop().await?;
                return Err(Error::Protocol("candidate exited before readiness".into()));
            }
            if let Ok(status) = self.status().await
                && status.ready
                && status.preflight_complete
                && status.writer_owned
                && matches!(WriterLease::acquire(&self.data), Err(Error::Busy))
            {
                return Ok(status);
            }
            if Instant::now() >= deadline {
                self.force_stop().await?;
                return Err(Error::Protocol(
                    "candidate readiness deadline exceeded".into(),
                ));
            }
            sleep(Duration::from_millis(20)).await;
        }
    }
    pub fn backend_running(&mut self) -> Result<bool> {
        match self.active.as_mut() {
            Some(backend) => Ok(!backend.child.exited()?),
            None => Ok(false),
        }
    }
    pub async fn status(&mut self) -> Result<RuntimeStatus> {
        let backend = self
            .active
            .as_ref()
            .ok_or_else(|| Error::Protocol("no active backend".into()))?;
        let (code, body) = request(backend, "GET", "/internal/runtime/v1/status", None).await?;
        if code != 200 {
            return Err(Error::Protocol("control status was refused".into()));
        }
        let status: RuntimeStatus = serde_json::from_slice(&body)?;
        if status.version != 1
            || status.instance_id != backend.instance_id
            || status.pid != backend.child.pid()
        {
            return Err(Error::Protocol("backend identity mismatch".into()));
        }
        Ok(status)
    }
    async fn stop(&mut self, reason: ShutdownReason, acknowledge_coverage_gap: bool) -> Result<()> {
        let Some(backend) = self.active.as_ref() else {
            return Ok(());
        };
        let body = serde_json::to_vec(&ShutdownRequest {
            version: 1,
            preserve_resume_requested: true,
            reason,
            acknowledge_coverage_gap,
        })?;
        let (code, _) = request(
            backend,
            "POST",
            "/internal/runtime/v1/shutdown",
            Some(&body),
        )
        .await?;
        if code != 202 {
            return Err(Error::Protocol("backend refused graceful shutdown".into()));
        }
        let deadline = Instant::now() + Duration::from_millis(backend.spec.shutdown_timeout_ms);
        loop {
            let backend = self.active.as_mut().unwrap();
            if backend.child.exited()? {
                backend.child.terminate_and_reap()?;
                let spec = backend.spec.clone();
                self.wait_release(&spec).await?;
                self.active = None;
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(Error::Protocol(
                    "graceful shutdown timed out; candidate was not started".into(),
                ));
            }
            sleep(Duration::from_millis(20)).await;
        }
    }
    async fn force_stop(&mut self) -> Result<()> {
        if let Some(backend) = self.active.as_mut() {
            backend.child.terminate_and_reap()?;
            let spec = backend.spec.clone();
            self.wait_release(&spec).await?;
            self.active = None;
        }
        Ok(())
    }
    pub async fn cutover(
        &mut self,
        candidate: BackendSpec,
        options: CutoverOptions,
    ) -> Result<CutoverOutcome> {
        Self::validate(&candidate)?;
        let status = self.status().await?;
        if status.guard_armed && (options.automatic || !options.acknowledge_coverage_gap) {
            return Err(Error::Unsafe(
                "armed guard requires explicit cutover and acknowledged coverage interruption"
                    .into(),
            ));
        }
        let previous = self.active.as_ref().unwrap().spec.clone();
        self.record("coverage_interruption_started", &previous.name)?;
        // Core atomically rechecks armed state before accepting this request.
        self.stop(
            ShutdownReason::Cutover,
            !options.automatic && options.acknowledge_coverage_gap,
        )
        .await?;
        match self.start(candidate.clone()).await {
            Ok(_) => {
                self.record("coverage_interruption_ended", &candidate.name)?;
                Ok(CutoverOutcome::Activated)
            }
            Err(candidate_error) => {
                self.force_stop().await?;
                // No archive restore: previous backend sees the latest committed data.
                self.start(previous.clone()).await?;
                self.record("coverage_interruption_ended", &previous.name)?;
                Ok(CutoverOutcome::RolledBack {
                    candidate_error: candidate_error.to_string(),
                })
            }
        }
    }
    /// Restore an explicitly retained backend specification after a cutover transaction fails.
    /// The candidate is stopped first; committed data is never restored from a stale copy.
    pub async fn rollback(&mut self, previous: BackendSpec) -> Result<RuntimeStatus> {
        Self::validate(&previous)?;
        self.stop(ShutdownReason::Rollback, true).await?;
        self.start(previous).await
    }
    pub async fn shutdown(&mut self) -> Result<()> {
        self.stop(ShutdownReason::Shutdown, false).await
    }
}

async fn request(
    backend: &Backend,
    method: &str,
    path: &str,
    body: Option<&[u8]>,
) -> Result<(u16, Vec<u8>)> {
    let address = backend.spec.control_addr;
    let body = body.unwrap_or_default();
    timeout(Duration::from_millis(500),async {
        let mut socket=TcpStream::connect(address).await?;
        let headers=format!("{method} {path} HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",backend.token,body.len());
        socket.write_all(headers.as_bytes()).await?;socket.write_all(body).await?;
        let mut data=Vec::new();let mut chunk=[0u8;4096];
        loop {let n=socket.read(&mut chunk).await?;if n==0 {break;}data.extend_from_slice(&chunk[..n]);if data.len()>32768 {return Err(Error::Protocol("control response exceeds bound".into()));}}
        let split=data.windows(4).position(|w|w==b"\r\n\r\n").ok_or_else(||Error::Protocol("malformed control response".into()))?;
        let head=std::str::from_utf8(&data[..split]).map_err(|_|Error::Protocol("non-UTF8 control headers".into()))?;
        let code=head.lines().next().and_then(|s|s.split_whitespace().nth(1)).and_then(|s|s.parse::<u16>().ok()).ok_or_else(||Error::Protocol("missing control status".into()))?;
        Ok((code,data[split+4..].to_vec()))
    }).await.map_err(|_|Error::Protocol("control request timed out".into()))?
}
