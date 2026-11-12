//! Shared Rust candidate. Default shadow mode opens neither canonical writer nor hardware.
mod acquisition_pressure;
pub mod admin;
mod admission;
pub use acquisition_pressure::AcquisitionPressureOptions;
mod analysis;
mod assets;
mod audio;
mod audio_http;
pub use audio::AudioOptions;
mod automation_binding;
mod background;
mod core_backup;
pub mod devices;
mod domain_http;
mod domains;
pub use domains::DomainSources;
mod error;
mod framing;
mod history;
mod history_jobs;
pub use history_jobs::HistoryOptions;
mod http;
mod instrument_control;
mod legacy;
pub use legacy::LegacyConfig;
mod manual_requests;
mod media;
mod private_worker;
mod raw_export;
mod recovery;
mod security;
pub mod task_execution;
mod tasks;
mod websocket;
pub use error::{CoreError, Result};
use ori_archive::{Archive, ArchiveConfig, EventDraft};
use ori_runtime::lease::WriterLease;
use rand::RngCore;
use serde_json::{Value, json};
use std::{
    path::{Component, Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::{
    net::TcpListener,
    sync::{Semaphore, watch},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CoreMode {
    Shadow,
    Replay,
}
impl CoreMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Shadow => "shadow",
            Self::Replay => "replay",
        }
    }
}
/// Process-only supervisor credential. Never serialized or Debug printed.
#[derive(Clone)]
pub struct RuntimeIdentity {
    pub token: String,
    pub instance_id: String,
}
impl RuntimeIdentity {
    pub fn from_environment() -> Result<Option<Self>> {
        let token = std::env::var("ORI_RUNTIME_CONTROL_TOKEN").ok();
        let instance = std::env::var("ORI_RUNTIME_INSTANCE_ID").ok();
        match (token, instance) {
            (None, None)
                if !std::env::vars_os()
                    .any(|(key, _)| key.to_string_lossy().starts_with("ORI_RUNTIME_")) =>
            {
                Ok(None)
            }
            (Some(token), Some(instance_id)) => {
                let identity = Self { token, instance_id };
                identity.validate()?;
                Ok(Some(identity))
            }
            _ => Err(CoreError::Invalid(
                "runtime token and instance must be supplied together",
            )),
        }
    }
    fn validate(&self) -> Result<()> {
        let valid = |text: &str, minimum: usize, maximum: usize| {
            text.len() >= minimum
                && text.len() <= maximum
                && text
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
        };
        if !valid(&self.token, 32, 256) || !valid(&self.instance_id, 1, 128) {
            return Err(CoreError::Invalid("invalid private runtime identity"));
        }
        Ok(())
    }
}
#[derive(Clone)]
pub struct LanSecurityConfig {
    pub directory: PathBuf,
    pub hosts: Vec<String>,
    pub origins: Vec<String>,
}
pub struct CoreConfig {
    pub data_directory: PathBuf,
    pub database: PathBuf,
    pub mode: CoreMode,
    pub identity: Option<RuntimeIdentity>,
    pub max_archive_bytes: Option<u64>,
    pub body_timeout: Duration,
    pub lan_security: Option<LanSecurityConfig>,
    /// Trusted executable/argv selected by the operator, never by an HTTP body.
    pub analysis_worker: Option<ori_runtime::worker::WorkerSpec>,
    pub devices: devices::DeviceOptions,
    pub domain_sources: Option<DomainSources>,
    /// Explicit operator TaskBook. None preserves the legacy ephemeral default.
    pub task_book: Option<PathBuf>,
    /// Optional trusted producer callbacks. Default scheduler is software only.
    pub task_execution: Option<task_execution::TaskExecutionConfig>,
    pub audio: AudioOptions,
    pub legacy: LegacyConfig,
    pub history: HistoryOptions,
    pub acquisition_pressure: AcquisitionPressureOptions,
}
impl CoreConfig {
    pub fn new(data: impl Into<PathBuf>) -> Self {
        let data = data.into();
        Self {
            database: data.join("sessions.sqlite3"),
            data_directory: data,
            mode: CoreMode::Shadow,
            identity: None,
            max_archive_bytes: None,
            body_timeout: Duration::from_secs(10),
            lan_security: None,
            analysis_worker: None,
            devices: devices::DeviceOptions::default(),
            domain_sources: None,
            task_book: None,
            task_execution: None,
            audio: AudioOptions::default(),
            legacy: LegacyConfig::default(),
            history: HistoryOptions::default(),
            acquisition_pressure: AcquisitionPressureOptions::default(),
        }
    }
    pub fn replay(data: impl Into<PathBuf>) -> Self {
        let mut result = Self::new(data);
        result.mode = CoreMode::Replay;
        result
    }
}
pub struct Core {
    pub(crate) config: CoreConfig,
    pub(crate) security: Arc<security::SecurityState>,
    pub(crate) archive: Option<Archive>,
    pub(crate) devices: Option<Arc<devices::DeviceManager>>,
    pub(crate) domains: std::sync::OnceLock<domains::Domains>,
    pub(crate) tasks: std::sync::OnceLock<Arc<tasks::Tasks>>,
    pub(crate) audio: Option<Arc<audio::Audio>>,
    pub(crate) audio_http: std::sync::OnceLock<Arc<audio_http::AudioHttp>>,
    pub(crate) legacy: std::sync::OnceLock<Arc<legacy::Legacy>>,
    pub(crate) instrument_control: std::sync::OnceLock<Arc<instrument_control::InstrumentControl>>,
    pub(crate) history_jobs: std::sync::OnceLock<Arc<history_jobs::HistoryJobs>>,
    pub(crate) background: Option<Arc<background::Background>>,
    pub(crate) automation: std::sync::OnceLock<Arc<automation_binding::AutomationBinding>>,
    pub(crate) acquisition_pressure:
        std::sync::OnceLock<Arc<acquisition_pressure::AcquisitionPressure>>,
    pub(crate) raw_exports: std::sync::OnceLock<Arc<raw_export::RawExports>>,
    pub(crate) effect_io: Arc<Semaphore>,
    pub(crate) webhook_slots: Arc<Semaphore>,
    pub(crate) webhook_client: reqwest::Client,
    writer: Mutex<Option<WriterLease>>,
    pub(crate) admission: Arc<admission::Admission>,
    pub(crate) mutation_order: Arc<tokio::sync::Mutex<()>>,
    pub(crate) requests: Arc<Semaphore>,
    manual_requests: Arc<Semaphore>,
    manual_runtime: tokio::runtime::Handle,
    pub(crate) private_requests: Arc<Semaphore>,
    pub(crate) body_bytes: Arc<Semaphore>,
    pub(crate) subscribers: Arc<Semaphore>,
    pub(crate) stream_encoding: Arc<Semaphore>,
    pub(crate) stream_bytes: Arc<Semaphore>,
    pub(crate) research_pool: ori_runtime::worker::ResearchPool,
    pub(crate) shutdown: watch::Sender<bool>,
    pub(crate) preflight_complete: AtomicBool,
    closed: AtomicBool,
    listener_ready: AtomicBool,
    // None: unstarted; Some(false): failed or cancelled; Some(true): confirmed closed.
    closure: tokio::sync::Mutex<Option<bool>>,
}
/// Before Core exists, blocking Archive initialization can outlive its async
/// caller. Cancellation/error cannot confirm that writer teardown has joined.
/// Retain the external lease unless it is transferred or closure was confirmed.
struct StartingWriter(Option<WriterLease>);
impl Drop for StartingWriter {
    fn drop(&mut self) {
        if let Some(lease) = self.0.take() {
            std::mem::forget(lease);
        }
    }
}
impl Core {
    /// Make an explicitly bounded private recovery bundle for trusted Rust
    /// embedding. Cancellation requests still await the original cleanup owner.
    pub async fn backup(
        self: &Arc<Self>,
        destination: &Path,
        options: ori_archive::BackupOptions,
    ) -> Result<Value> {
        core_backup::run(self, destination, options).await
    }

    pub async fn open(config: CoreConfig) -> Result<Arc<Self>> {
        config.devices.validate()?;
        config.acquisition_pressure.validate()?;
        let webhook_client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(3))
            .connect_timeout(Duration::from_secs(3))
            .pool_max_idle_per_host(0)
            .build()
            .map_err(|_| CoreError::Invalid("webhook TLS client could not initialize"))?;
        if config.body_timeout.is_zero() || config.body_timeout > Duration::from_secs(10) {
            return Err(CoreError::Invalid(
                "body deadline must be at most 10 seconds",
            ));
        }
        if let Some(identity) = &config.identity {
            identity.validate()?;
        }
        let security = security::SecurityState::open(config.lan_security.as_ref())?;
        let (writer, archive) = if config.mode == CoreMode::Replay {
            reject_symlinks(&config.database)?;
            if config.database.parent() != Some(config.data_directory.as_path()) {
                return Err(CoreError::Invalid(
                    "archive must be directly inside the exact owned data directory",
                ));
            }
            if config.identity.is_some()
                && config.database.file_name() != Some(std::ffi::OsStr::new("sessions.sqlite3"))
            {
                return Err(CoreError::Invalid(
                    "supervised candidate must use data_dir/sessions.sqlite3",
                ));
            }
            match std::fs::symlink_metadata(&config.database) {
                Ok(metadata) => {
                    if !metadata.is_file() {
                        return Err(CoreError::Invalid("archive must be a regular file"));
                    }
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::MetadataExt;
                        if metadata.nlink() != 1 {
                            return Err(CoreError::Invalid(
                                "hardlinked archives cannot establish one writer lease domain",
                            ));
                        }
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
            let mut lease = StartingWriter(Some(WriterLease::acquire(&config.data_directory)?));
            let mut archive_config = ArchiveConfig::new(&config.database);
            archive_config.max_database_bytes = config.max_archive_bytes;
            let archive = tokio::task::spawn_blocking(move || Archive::open(archive_config))
                .await
                .map_err(|_| CoreError::Task)??;
            if let Err(error) = recovery::recover(&archive).await {
                if archive.close().await.is_ok() {
                    lease.0.take();
                }
                return Err(error);
            }
            (Some(lease), Some(archive))
        } else {
            (None, None)
        };
        let mutation_order = archive
            .as_ref()
            .map(Archive::domain_coordinator)
            .unwrap_or_else(|| Arc::new(tokio::sync::Mutex::new(())));
        let (shutdown, _) = watch::channel(false);
        let devices = archive
            .as_ref()
            .map(|archive| devices::DeviceManager::new(archive.clone(), config.devices.clone()));
        let audio = if archive.is_some() {
            audio::Audio::open(&config.audio)?
        } else {
            None
        };
        let background = if archive.is_some() {
            Some(background::Background::open(&config.data_directory)?)
        } else {
            None
        };
        let core = Arc::new(Self {
            config,
            security,
            archive,
            devices,
            domains: std::sync::OnceLock::new(),
            tasks: std::sync::OnceLock::new(),
            audio,
            audio_http: std::sync::OnceLock::new(),
            legacy: std::sync::OnceLock::new(),
            instrument_control: std::sync::OnceLock::new(),
            history_jobs: std::sync::OnceLock::new(),
            background,
            automation: std::sync::OnceLock::new(),
            acquisition_pressure: std::sync::OnceLock::new(),
            raw_exports: std::sync::OnceLock::new(),
            effect_io: Arc::new(Semaphore::new(2)),
            webhook_slots: Arc::new(Semaphore::new(2)),
            webhook_client,
            writer: Mutex::new(writer.and_then(|mut starting| starting.0.take())),
            admission: Arc::default(),
            mutation_order,
            requests: Arc::new(Semaphore::new(16)),
            manual_requests: Arc::new(Semaphore::new(16)),
            manual_runtime: tokio::runtime::Handle::current(),
            private_requests: Arc::new(Semaphore::new(2)),
            body_bytes: Arc::new(Semaphore::new(32 * 1024 * 1024)),
            subscribers: Arc::new(Semaphore::new(8)),
            stream_encoding: Arc::new(Semaphore::new(2)),
            stream_bytes: Arc::new(Semaphore::new(32 * 1024 * 1024)),
            research_pool: ori_runtime::worker::ResearchPool::default(),
            shutdown,
            preflight_complete: AtomicBool::new(false),
            closed: AtomicBool::new(false),
            listener_ready: AtomicBool::new(false),
            closure: tokio::sync::Mutex::new(None),
        });
        if let Err(error) = legacy::initialize(&core).await {
            let _ = core.close().await;
            return Err(error);
        }
        if let Err(error) = tasks::initialize(&core) {
            let _ = core.close().await;
            return Err(error);
        }
        // Install the original transport's intent barrier before recovery can
        // start explicitly opted-in Guard actors, including startup failures.
        if let Err(error) = instrument_control::initialize(&core).await {
            let _ = core.close().await;
            return Err(error);
        }
        if let Err(error) = domains::initialize(&core).await {
            let _ = core.close().await;
            return Err(error);
        }
        if let Err(error) = history_jobs::initialize(&core).await {
            let _ = core.close().await;
            return Err(error);
        }
        if let Err(error) = raw_export::initialize(&core) {
            let _ = core.close().await;
            return Err(error);
        }
        if let Err(error) = automation_binding::initialize(&core).await {
            let _ = core.close().await;
            return Err(error);
        }
        if let Err(error) = audio_http::initialize(&core).await {
            let _ = core.close().await;
            return Err(error);
        }
        if let (Some(devices), Some(background)) = (&core.devices, &core.background) {
            let observer = match acquisition_pressure::AcquisitionPressure::start(
                devices.clone(),
                background.qos.clone(),
                core.config.acquisition_pressure.clone(),
            ) {
                Ok(observer) => observer,
                Err(error) => {
                    let _ = core.close().await;
                    return Err(error);
                }
            };
            core.acquisition_pressure
                .set(observer)
                .map_err(|_| CoreError::Invalid("acquisition observer already initialized"))?;
        }
        core.preflight_complete.store(true, Ordering::Release);
        Ok(core)
    }
    pub fn request_shutdown(&self) {
        self.admission.quiesce();
        // Capture opt-in active ownership synchronously before cancellation can
        // cause an in-flight audio/effect callback to finish on another thread.
        if let Some(domain) = self.domains.get() {
            let _ = domain.begin_shutdown();
        }
        self.security.begin_shutdown();
        self.shutdown.send_replace(true);
        if let Some(control) = self.instrument_control.get() {
            let _ = control.begin_shutdown();
        }
        if let Some(audio) = &self.audio {
            let _ = audio.begin_shutdown();
        }
        if let Some(audio_http) = self.audio_http.get() {
            let _ = audio_http.begin_shutdown();
        }
        if let Some(tasks) = self.tasks.get() {
            let _ = tasks.begin_shutdown();
        }
        if let Some(legacy) = self.legacy.get() {
            let _ = legacy.begin_shutdown();
        }
        if let Some(history) = self.history_jobs.get() {
            let _ = history.begin_shutdown();
        }
        if let Some(observer) = self.acquisition_pressure.get() {
            let _ = observer.begin_shutdown();
        } else if let Some(background) = &self.background {
            let _ = background.begin_shutdown();
        }
        if let Some(automation) = self.automation.get() {
            let _ = automation.begin_shutdown();
        }
        if let Some(exports) = self.raw_exports.get() {
            let _ = exports.begin_shutdown();
        }
        if self.domains.get().is_none()
            && let Some(devices) = &self.devices
        {
            let _ = devices.request_shutdown();
        }
    }
    pub async fn close(&self) -> Result<()> {
        let mut closure = self.closure.lock().await;
        match *closure {
            Some(true) => return Ok(()),
            Some(false) => {
                return Err(CoreError::Invalid(
                    "earlier cleanup failed or was cancelled; writer ownership remains retained until process exit",
                ));
            }
            None => {}
        }
        *closure = Some(false);
        self.request_shutdown();
        let domain_cleanup = self.drain_domains().await;
        let acquisition_cleanup = if let Some(observer) = self.acquisition_pressure.get() {
            let result = observer.close().await;
            // No late observer recovery setter can race this final pressure.
            // Start it even when the observer's actual JOIN reports failure.
            if let Some(background) = &self.background {
                let _ = background.begin_shutdown();
            }
            result
        } else {
            Ok(())
        };
        self.admission.wait().await;
        if let Some(devices) = &self.devices {
            devices.close().await?;
        }
        if let Some(tasks) = self.tasks.get() {
            tasks.close().await?;
        }
        if let Some(control) = self.instrument_control.get() {
            control.begin_shutdown().completed().await?;
        }
        if let Some(audio) = &self.audio {
            audio.close().await?;
        }
        if let Some(audio_http) = self.audio_http.get() {
            audio_http.begin_shutdown().completed().await?;
        }
        if let Some(legacy) = self.legacy.get() {
            legacy.close().await?;
        }
        if let Some(history) = self.history_jobs.get() {
            history.close().await?;
        }
        if let Some(automation) = self.automation.get() {
            automation.begin_shutdown().completed().await?;
        }
        if let Some(exports) = self.raw_exports.get() {
            exports.close().await?;
        }
        let _probes = self
            .effect_io
            .clone()
            .acquire_many_owned(2)
            .await
            .map_err(|_| CoreError::Invalid("effect I/O admission closed before drain"))?;
        domain_cleanup?;
        acquisition_cleanup?;
        // Child ownership survives a dropped HTTP waiter. Take the same global
        // reservation after consumers stop, proving any runner and IPC joined
        // before removing the ledger or releasing Archive/writer ownership.
        let _research_drained = loop {
            match self.research_pool.try_reserve() {
                Ok(permit) => break permit,
                Err(ori_runtime::Error::Busy) => {
                    tokio::time::sleep(Duration::from_millis(10)).await
                }
                Err(error) => return Err(error.into()),
            }
        };
        if let Some(background) = &self.background {
            background.close().await?;
        }
        if let Some(archive) = &self.archive {
            archive.close().await?;
        }
        self.writer
            .lock()
            .map_err(|_| CoreError::Invalid("writer state poisoned"))?
            .take();
        self.preflight_complete.store(false, Ordering::Release);
        self.closed.store(true, Ordering::Release);
        *closure = Some(true);
        Ok(())
    }
    pub fn router(self: &Arc<Self>) -> axum::Router {
        self.router_for(security::ListenerRole::Plain)
    }
    fn router_for(self: &Arc<Self>, role: security::ListenerRole) -> axum::Router {
        axum::Router::new()
            .fallback(http::handle)
            .layer(axum::Extension(role))
            .with_state(self.clone())
    }
    pub async fn serve(self: Arc<Self>, listener: TcpListener) -> Result<()> {
        if self.security.lan() {
            return Err(CoreError::Invalid(
                "LAN mode requires the actual TLS listener",
            ));
        }
        if !listener.local_addr()?.ip().is_loopback() {
            return Err(CoreError::Invalid(
                "candidate plaintext listener is loopback-only; LAN TLS adapter is not yet installed",
            ));
        }
        let handle = axum_server::Handle::new();
        let signal = self.shutdown_signal(handle.clone());
        self.listener_ready.store(true, Ordering::Release);
        let mut server = axum_server::from_tcp(listener.into_std()?)?
            .acceptor(framing::HeadGate::new(
                axum_server::accept::NoDelayAcceptor::new(),
            ))
            .handle(handle);
        server
            .http_builder()
            .http1()
            .keep_alive(true)
            .half_close(true);
        let serving = server
            .serve(
                self.router()
                    .into_make_service_with_connect_info::<std::net::SocketAddr>(),
            )
            .await;
        self.request_shutdown();
        let _ = signal.await;
        let closing = self.close().await;
        serving?;
        closing
    }
    pub fn runtime_status(&self) -> ori_runtime::supervisor::RuntimeStatus {
        let owned = self.writer.lock().is_ok_and(|lease| lease.is_some());
        let complete = self.preflight_complete.load(Ordering::Acquire);
        ori_runtime::supervisor::RuntimeStatus {
            version: 1,
            instance_id: self
                .config
                .identity
                .as_ref()
                .map(|i| i.instance_id.clone())
                .unwrap_or_default(),
            pid: std::process::id(),
            ready: complete
                && self.listener_ready.load(Ordering::Acquire)
                && !self.admission.is_quiescing()
                && !self.closed.load(Ordering::Acquire),
            preflight_complete: complete,
            guard_armed: self
                .domains
                .get()
                .is_some_and(|domain| domain.guard.any_armed()),
            writer_owned: owned,
        }
    }
    pub fn begin_pairing_window(&self, ttl: u64, attempts: u32) -> Result<String> {
        if self.admission.is_quiescing() {
            return Err(CoreError::Invalid("core is shutting down"));
        }
        self.security.begin(ttl, attempts)
    }
    pub fn server_fingerprint(&self) -> Option<&str> {
        self.security.fingerprint()
    }
    pub async fn serve_tls(self: Arc<Self>, listener: TcpListener) -> Result<()> {
        let tls = self.security.tls_config()?;
        #[cfg(unix)]
        let admin = admin::start(self.clone()).await?;
        let handle = axum_server::Handle::new();
        let signal = self.shutdown_signal(handle.clone());
        self.listener_ready.store(true, Ordering::Release);
        let mut server = axum_server::from_tcp(listener.into_std()?)?
            .acceptor(framing::HeadGate::new(
                axum_server::tls_rustls::RustlsAcceptor::new(tls)
                    .handshake_timeout(Duration::from_secs(5))
                    .acceptor(axum_server::accept::NoDelayAcceptor::new()),
            ))
            .handle(handle);
        server
            .http_builder()
            .http1()
            .keep_alive(true)
            .half_close(true);
        let serving = server
            .serve(
                self.router_for(security::ListenerRole::Tls)
                    .into_make_service_with_connect_info::<std::net::SocketAddr>(),
            )
            .await;
        self.request_shutdown();
        let _ = signal.await;
        #[cfg(unix)]
        admin.join().await?;
        let closing = self.close().await;
        serving?;
        closing
    }
    pub async fn serve_control(self: Arc<Self>, listener: TcpListener) -> Result<()> {
        if !listener.local_addr()?.ip().is_loopback() {
            return Err(CoreError::Invalid(
                "private runtime listener must be loopback",
            ));
        }
        let handle = axum_server::Handle::new();
        let signal = self.shutdown_signal(handle.clone());
        let mut server = axum_server::from_tcp(listener.into_std()?)?
            .acceptor(framing::HeadGate::new(
                axum_server::accept::NoDelayAcceptor::new(),
            ))
            .handle(handle);
        server
            .http_builder()
            .http1()
            .keep_alive(true)
            .half_close(true);
        let serving = server
            .serve(
                self.router_for(security::ListenerRole::Control)
                    .into_make_service_with_connect_info::<std::net::SocketAddr>(),
            )
            .await;
        self.request_shutdown();
        let _ = signal.await;
        let closing = self.close().await;
        serving?;
        closing
    }
    fn shutdown_signal(
        self: &Arc<Self>,
        handle: axum_server::Handle<std::net::SocketAddr>,
    ) -> tokio::task::JoinHandle<()> {
        let mut shutdown = self.shutdown.subscribe();
        let core = Arc::downgrade(self);
        tokio::spawn(async move {
            while !*shutdown.borrow() {
                if shutdown.changed().await.is_err() {
                    break;
                }
            }
            handle.graceful_shutdown(None);
            if let Some(core) = core.upgrade() {
                // HTTP body backpressure may prevent Hyper from polling an
                // SSE/export stream again. Drain admitted mutations and actual
                // effect/reader/child JOINs independently of socket delivery.
                // Only then terminate the remaining owned connections.
                let _ = core.close().await;
            }
            handle.shutdown();
        })
    }
    pub fn mode(&self) -> CoreMode {
        self.config.mode
    }
    /// Cache-only safe-prepare evidence for the current owned analyzer. None is
    /// unknown/unprepared; successful receipts never claim physical readback.
    pub fn device_preparation(
        &self,
        session: &str,
        stream: &str,
    ) -> devices::DeviceResult<Option<devices::PrepareReport>> {
        self.devices
            .as_ref()
            .ok_or(devices::DeviceError::Closed)?
            .preparation(&devices::Target::new(session, stream))
    }
    pub fn resource_capabilities(&self) -> Value {
        json!({"request_slots":16,"body_bytes":33554432,"control_request_bytes":65536,"measurement_request_bytes":16777216,"body_timeout_ms":self.config.body_timeout.as_millis(),"sse_subscribers":8,"event_subscribers_shared_sse_ws":8,"binary_frames_per_connection":1,"binary_frame_bytes_in_flight":33554432,"binary_encoders":2,"heavy_research_children":1,"device_workers":if self.mode()==CoreMode::Replay{self.config.devices.max_workers}else{0},"device_command_queue":self.config.devices.queue_capacity,"native_connector_enabled":self.mode()==CoreMode::Replay&&self.config.devices.transport.is_some(),"measured_rss_gate_passed":false})
    }
    pub(crate) fn archive(&self) -> error::ApiResult<&Archive> {
        self.archive.as_ref().ok_or_else(|| {
            error::ApiError::new(
                axum::http::StatusCode::CONFLICT,
                "shadow_read_only",
                "Shadow mode opens no live writer; use a private replay archive explicitly",
            )
        })
    }
    pub(crate) async fn session(&self, id: &str) -> error::ApiResult<Value> {
        self.archive()?.get_session(id).await?.ok_or_else(|| {
            error::ApiError::new(
                axum::http::StatusCode::NOT_FOUND,
                "session_not_found",
                format!("Сеанс не найден: {id}"),
            )
        })
    }
    pub(crate) async fn transition(&self, id: &str, start: bool) -> error::ApiResult<Value> {
        // Preserve the public missing-session error before consulting the
        // device facade. Reload below under the coordinator before commit.
        self.session(id).await?;
        if !start && let Some(devices) = &self.devices {
            devices.authorize_public_session(id).await?;
        }
        if !start && let Some(control) = self.instrument_control.get() {
            for status in self
                .devices
                .as_ref()
                .ok_or(crate::devices::DeviceError::Closed)?
                .list(Some(id))
                .await?
            {
                control
                    .close_target(&crate::devices::Target::new(
                        &status.session_id,
                        &status.stream_id,
                    ))
                    .await?;
            }
        }
        let _stopping = if !start {
            self.devices
                .as_ref()
                .map(|devices| devices.quiesce_session(id))
                .transpose()?
        } else {
            None
        };
        if !start && let Some(devices) = &self.devices {
            devices.stop_session_quiesced(id).await?;
        }
        let _order = self.mutation_order.lock().await;
        let mut value = self.session(id).await?;
        if value["state"] != if start { "draft" } else { "running" } {
            return Err(error::ApiError::invalid(
                "Переход сеанса из текущего состояния запрещён",
            ));
        }
        let now = utc_now();
        value["state"] = json!(if start { "running" } else { "stopped" });
        value[if start { "started_at" } else { "ended_at" }] = json!(now);
        let saved = if start {
            let event =
                EventDraft::now("session.started", Some(id.into()), json!({"session":value}));
            self.archive()?.save_session(value, Some(event)).await?
        } else {
            self.archive()?.stop_session(value, &now).await?
        };
        Ok(saved.items.into_iter().next().expect("saved transition"))
    }
}
impl Drop for Core {
    fn drop(&mut self) {
        if !self.closed.load(Ordering::Acquire) {
            self.request_shutdown();
            // An abandoned service cannot await intent persistence. Cancel its
            // own I/O and retain the lease; normal close uses the barrier.
            if let Some(devices) = &self.devices {
                let _ = devices.request_shutdown();
            }
        }
        // An unconfirmed archive cleanup must not release a lease while writer work may survive.
        if !self.closed.load(Ordering::Acquire)
            && let Ok(lease) = self.writer.get_mut()
            && let Some(lease) = lease.take()
        {
            std::mem::forget(lease);
        }
    }
}
fn reject_symlinks(path: &Path) -> Result<()> {
    if !path.is_absolute() || path.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err(CoreError::Invalid(
            "absolute normalized archive path required",
        ));
    }
    let mut current = PathBuf::new();
    for part in path.components() {
        current.push(part);
        if let Ok(metadata) = std::fs::symlink_metadata(&current)
            && metadata.file_type().is_symlink()
        {
            return Err(CoreError::Invalid("archive symlinks are refused"));
        }
    }
    Ok(())
}
pub(crate) fn utc_now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}
pub(crate) fn new_id() -> String {
    let mut bytes = [0u8; 16];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod close_tests {
    use super::*;
    #[tokio::test]
    async fn cancelled_cleanup_is_latched_and_cannot_release_the_writer_by_retrying() {
        let directory = tempfile::tempdir().unwrap();
        let core = Core::open(CoreConfig::replay(directory.path()))
            .await
            .unwrap();
        let admitted = core.admission.enter().unwrap();
        let closing = core.clone();
        let task = tokio::spawn(async move { closing.close().await });
        while !core.admission.is_quiescing() {
            tokio::task::yield_now().await;
        }
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        drop(admitted);
        assert!(
            core.close().await.is_err(),
            "cancelled cleanup was retried as if its previous outcome were confirmed"
        );
        assert!(WriterLease::acquire(directory.path()).is_err());
        core.archive.as_ref().unwrap().close().await.unwrap();
    }
}
