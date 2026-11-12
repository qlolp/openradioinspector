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
    let mut termination = Termination::new()?;
    let mut supervisor = Supervisor::new(&config.data_directory)?;
    let state = state_directory(&config.data_directory)?;
    let selection = state.join("selection.json");
    let backend = if selection.exists() {
        serde_json::from_slice(&read_private(&selection, 1024 * 1024)?)?
    } else {
        config.backend
    };
    #[cfg(unix)]
    let mut selected = backend.clone();
    tokio::select! {
        result=supervisor.start(backend)=>{result?;},
        result=termination.recv()=>{result?;supervisor.shutdown().await?;return Ok(());},
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
        use tokio::{
            io::{AsyncReadExt, AsyncWriteExt},
            net::UnixListener,
            time::{Duration, timeout},
        };
        let socket_path = state.join("supervisor.sock");
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
        loop {
            tokio::select! {
                result=&mut signal=>{result?;supervisor.shutdown().await?;return Ok(());},
                _=health.tick()=>{if !supervisor.backend_running()? {return Err(Error::Protocol("owned backend exited".into()));}},
                accepted=listener.accept()=>{
                    let (mut stream,_)=accepted?;
                    if stream.peer_cred()?.uid()!=unsafe {libc::geteuid()} {continue;}
                    let mut bytes=Vec::new();
                    let read=timeout(Duration::from_secs(1),(&mut stream).take(1024*1024+1).read_to_end(&mut bytes)).await;
                    let response=if !matches!(read,Ok(Ok(_))) || bytes.len()>1024*1024 {serde_json::json!({"ok":false,"error":"invalid_request"})} else {
                        match serde_json::from_slice::<AdminCommand>(&bytes) {
                            Ok(AdminCommand::Status)=>match supervisor.status().await {Ok(status)=>serde_json::json!({"ok":true,"status":status}),Err(_)=>serde_json::json!({"ok":false,"error":"status_unavailable"})},
                            Ok(AdminCommand::Shutdown)=>{
                                let result=supervisor.shutdown().await;
                                let response=serde_json::json!({"ok":result.is_ok()});
                                let _=timeout(Duration::from_secs(1),stream.write_all(&serde_json::to_vec(&response)?)).await;
                                result?;return Ok(());
                            },
                            Ok(AdminCommand::Cutover{candidate,acknowledge_coverage_gap})=>{
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
        let path = state_directory(data)?.join("supervisor.sock");
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
