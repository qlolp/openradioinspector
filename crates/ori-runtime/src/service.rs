//! Foreground service runner; native managers own restart policy, never backend upgrades.
use crate::{
    Error, Result,
    lease::{read_private, state_directory},
    supervisor::{BackendSpec, Supervisor},
};
#[cfg(unix)]
use crate::{
    lease::atomic_private,
    supervisor::{CutoverOptions, CutoverOutcome},
};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServiceConfig {
    pub data_directory: PathBuf,
    pub backend: BackendSpec,
}
#[derive(Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub enum AdminCommand {
    Status,
    Start,
    Shutdown,
    Cutover {
        candidate: BackendSpec,
        #[serde(default)]
        acknowledge_coverage_gap: bool,
    },
}

struct Termination {
    #[cfg(unix)]
    interrupt: tokio::signal::unix::Signal,
    #[cfg(unix)]
    terminate: tokio::signal::unix::Signal,
}
impl Termination {
    fn new() -> Result<Self> {
        Ok(Self {
            #[cfg(unix)]
            interrupt: tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?,
            #[cfg(unix)]
            terminate: tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?,
        })
    }
    async fn recv(&mut self) -> Result<()> {
        #[cfg(unix)]
        tokio::select! {_=self.interrupt.recv()=>{},_=self.terminate.recv()=>{}}
        #[cfg(not(unix))]
        tokio::signal::ctrl_c().await?;
        Ok(())
    }
}

pub async fn run(config: ServiceConfig) -> Result<()> {
    run_with_policy(config, false).await
}

/// Trusted embedding entry point; physical macOS selection is the separate
/// macos-agent CLI. An enabled LaunchAgent starts idle until an operator starts
/// its bundled backend over the private socket.
pub async fn run_personal_rc(config: ServiceConfig) -> Result<()> {
    run_with_policy(config, true).await
}

async fn run_with_policy(config: ServiceConfig, macos_rc: bool) -> Result<()> {
    let mut termination = Termination::new()?;
    let mut supervisor = Supervisor::new(&config.data_directory)?;
    let state = state_directory(&config.data_directory)?;
    let selection = state.join("selection.json");
    if macos_rc && selection.exists() {
        return Err(Error::Unsafe("RC agent requires its bundled Rust backend; persisted cutover selection must be reviewed explicitly".into()));
    }
    let backend = if selection.exists() {
        serde_json::from_slice(&read_private(&selection, 1024 * 1024)?)?
    } else {
        config.backend
    };
    #[cfg(unix)]
    let mut selected = backend.clone();
    if !macos_rc {
        tokio::select! {
            result=supervisor.start(backend)=>{result?;},
            result=termination.recv()=>{result?;supervisor.shutdown().await?;return Ok(());},
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
        use tokio::{
            io::{AsyncReadExt, AsyncWriteExt},
            net::UnixListener,
            time::{Duration, timeout},
        };
        let socket_path = admin_socket_path(&config.data_directory)?;
        if socket_path.exists() {
            let metadata = std::fs::symlink_metadata(&socket_path)?;
            if !metadata.file_type().is_socket() || metadata.uid() != unsafe { libc::geteuid() } {
                return Err(Error::Unsafe(
                    "refusing to replace foreign admin socket".into(),
                ));
            }
            std::fs::remove_file(&socket_path)?;
        }
        let listener = UnixListener::bind(&socket_path)?;
        std::fs::set_permissions(&socket_path, std::fs::Permissions::from_mode(0o600))?;
        struct Cleanup(PathBuf);
        impl Drop for Cleanup {
            fn drop(&mut self) {
                let _ = std::fs::remove_file(&self.0);
            }
        }
        let _cleanup = Cleanup(socket_path);
        let signal = termination.recv();
        tokio::pin!(signal);
        let mut health = tokio::time::interval(Duration::from_secs(1));
        let mut continuity = crate::power::ContinuityFence::system()?;
        let mut idle_sleep = crate::power::IdleSleepAssertion::default();
        let mut paused = macos_rc;
        let mut pause_reason = "operator_start_required";
        loop {
            tokio::select! {
                result=&mut signal=>{result?;supervisor.shutdown().await?;return Ok(());},
                _=health.tick()=>{
                    if paused { continue; }
                    if !supervisor.backend_running()? {return Err(Error::Protocol("owned backend exited".into()));}
                    if macos_rc {
                        if continuity.check_system().is_err() {
                            supervisor.record_sleep_gap()?;
                            supervisor.shutdown().await?;
                            idle_sleep.set_armed(false)?;
                            paused = true;
                            pause_reason = "system_sleep_preflight_required";
                            continue;
                        }
                        if idle_sleep.set_armed(supervisor.status().await?.guard_armed).is_err() {
                            supervisor.shutdown().await?;
                            paused = true;
                            pause_reason = "idle_sleep_protection_failed";
                        }
                    }
                },
                accepted=listener.accept()=>{
                    let (mut stream,_)=accepted?;
                    if stream.peer_cred()?.uid()!=unsafe {libc::geteuid()} {continue;}
                    let mut bytes=Vec::new();
                    let read=timeout(Duration::from_secs(1),(&mut stream).take(1024*1024+1).read_to_end(&mut bytes)).await;
                    let response=if !matches!(read,Ok(Ok(_))) || bytes.len()>1024*1024 {serde_json::json!({"ok":false,"error":"invalid_request"})} else {
                        match serde_json::from_slice::<AdminCommand>(&bytes) {
                            Ok(AdminCommand::Status) if paused=>serde_json::json!({"ok":true,"service_state":"stopped","reason":pause_reason}),
                            Ok(AdminCommand::Status)=>match supervisor.status().await {Ok(status)=>serde_json::json!({"ok":true,"status":status}),Err(_)=>serde_json::json!({"ok":false,"error":"status_unavailable"})},
                            Ok(AdminCommand::Start) if macos_rc=>{
                                let starting=paused;
                                let result=if starting {supervisor.start(selected.clone()).await} else {supervisor.status().await};
                                match result {
                                    Ok(status)=>{if starting {continuity=crate::power::ContinuityFence::system()?;}paused=false;serde_json::json!({"ok":true,"status":status})},
                                    Err(_)=>serde_json::json!({"ok":false,"error":"bundled_core_start_failed"}),
                                }
                            },
                            Ok(AdminCommand::Start)=>serde_json::json!({"ok":false,"error":"start_requires_personal_rc_agent"}),
                            Ok(AdminCommand::Shutdown)=>{
                                let result=supervisor.shutdown().await;
                                let response=serde_json::json!({"ok":result.is_ok()});
                                let _=timeout(Duration::from_secs(1),stream.write_all(&serde_json::to_vec(&response)?)).await;
                                result?;return Ok(());
                            },
                            Ok(AdminCommand::Cutover{candidate,acknowledge_coverage_gap})=>{
                                if macos_rc {
                                    let response=serde_json::json!({"ok":false,"error":"rc_update_requires_stopped_service"});
                                    let _=timeout(Duration::from_secs(1),stream.write_all(&serde_json::to_vec(&response)?)).await;
                                    continue;
                                }
                                match supervisor.cutover(candidate.clone(),CutoverOptions{automatic:false,acknowledge_coverage_gap}).await {
                                    Ok(CutoverOutcome::Activated)=>{
                                        // Preserve a successful explicit selection across service restarts.
                                        // A failed persistence is visible; no stale archive backup is restored.
                                        match atomic_private(&selection,&serde_json::to_vec(&candidate)?) {
                                            Ok(())=>{selected=candidate;serde_json::json!({"ok":true,"outcome":"activated"})},
                                            Err(_)=>match supervisor.rollback(selected.clone()).await {
                                                Ok(_)=>serde_json::json!({"ok":false,"error":"selection_persistence_failed","outcome":"rolled_back"}),
                                                Err(_)=>serde_json::json!({"ok":false,"error":"selection_persistence_failed","outcome":"rollback_failed"}),
                                            },
                                        }
                                    },
                                    Ok(CutoverOutcome::RolledBack{..})=>serde_json::json!({"ok":false,"outcome":"rolled_back"}),
                                    Err(_)=>serde_json::json!({"ok":false,"error":"cutover_refused_or_failed"}),
                                }
                            },
                            Err(_)=>serde_json::json!({"ok":false,"error":"invalid_request"}),
                        }
                    };
                    let _=timeout(Duration::from_secs(1),stream.write_all(&serde_json::to_vec(&response)?)).await;
                }
            }
        }
    }
    #[cfg(not(unix))]
    {
        termination.recv().await?;
        supervisor.shutdown().await
    }
}

pub async fn admin(data: &Path, command: AdminCommand) -> Result<serde_json::Value> {
    #[cfg(unix)]
    {
        use tokio::{
            io::{AsyncReadExt, AsyncWriteExt},
            net::UnixStream,
            time::{Duration, timeout},
        };
        let path = admin_socket_path(data)?;
        let mut stream = UnixStream::connect(path).await?;
        stream.write_all(&serde_json::to_vec(&command)?).await?;
        stream.shutdown().await?;
        let mut response = Vec::new();
        // Cutover deadlines belong to each backend. The operator may wait; no backend kill-by-port.
        timeout(
            Duration::from_secs(300),
            stream.take(1024 * 1024 + 1).read_to_end(&mut response),
        )
        .await
        .map_err(|_| Error::Deadline)??;
        if response.len() > 1024 * 1024 {
            return Err(Error::OutputLimit);
        }
        Ok(serde_json::from_slice(&response)?)
    }
    #[cfg(not(unix))]
    {
        let _ = (data, command);
        Err(Error::Unsafe("native Windows clients must embed the supervisor and call its lifecycle API; Unix admin sockets are unavailable".into()))
    }
}

/// Darwin's sockaddr_un is short. Keep leases by the archive, and place only
/// its owner-only admin socket in the OS-provided per-user temporary directory.
pub fn admin_socket_path(data: &Path) -> Result<PathBuf> {
    let state = state_directory(data)?;
    #[cfg(target_os = "macos")]
    {
        use sha2::{Digest, Sha256};
        use std::os::unix::ffi::OsStrExt;
        let mut bytes = vec![0u8; 4096];
        let count = unsafe {
            libc::confstr(
                libc::_CS_DARWIN_USER_TEMP_DIR,
                bytes.as_mut_ptr().cast(),
                bytes.len(),
            )
        };
        if count <= 1 || count > bytes.len() {
            return Err(Error::Unsafe(
                "private macOS runtime directory unavailable".into(),
            ));
        }
        let root =
            std::fs::canonicalize(Path::new(std::ffi::OsStr::from_bytes(&bytes[..count - 1])))?;
        crate::lease::check_private(&root, true)?;
        let digest = Sha256::digest(std::fs::canonicalize(data)?.as_os_str().as_bytes());
        let directory = root.join(format!(
            "ori-{:016x}",
            &digest[..8]
                .iter()
                .fold(0u64, |n, b| (n << 8) | u64::from(*b))
        ));
        crate::lease::private_directory(&directory)?;
        let path = directory.join("s");
        if path.as_os_str().as_bytes().len() >= 104 {
            return Err(Error::Unsafe(
                "macOS runtime socket path exceeds platform bound".into(),
            ));
        }
        let _ = state;
        Ok(path)
    }
    #[cfg(not(target_os = "macos"))]
    Ok(state.join("supervisor.sock"))
}
