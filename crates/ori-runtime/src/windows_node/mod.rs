//! Per-user Windows operator node. The controller is a separate process from
//! the Tauri UI. The installed node accepts status and graceful shutdown only.
mod pipe;

use crate::{
    Error, Result,
    lease::{InstanceLease, WriterLease, read_private, state_directory},
    process::windows_job::JobChild,
    service::{AdminCommand, ServiceConfig},
    supervisor::{BackendSpec, Supervisor},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs, io,
    os::windows::process::CommandExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};
use tokio::time::{Instant, sleep, timeout};
use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;

const ADDRESS: &str = "127.0.0.1:8770";

fn data_dir() -> Result<PathBuf> {
    let root = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .ok_or_else(|| Error::Unsafe("Windows LocalAppData unavailable".into()))?;
    Ok(root.join("OpenRadioInspector-Windows-RC1"))
}

fn installation() -> Result<PathBuf> {
    std::env::current_exe()?
        .parent()
        .map(Path::to_owned)
        .ok_or_else(|| Error::Unsafe("Windows runtime installation unavailable".into()))
}

fn configuration(data: &Path, installation: &Path) -> Result<ServiceConfig> {
    // Enforce the same source and feature set selected during packaging, even
    // after a manual copy or interrupted update.
    let manifest = installation.join("rc-build-info.json");
    if fs::metadata(&manifest)?.len() > 65_536 {
        return Err(Error::OutputLimit);
    }
    let manifest: Value = serde_json::from_slice(&fs::read(manifest)?)?;
    let source = option_env!("ORI_BUILD_SOURCE_SHA").unwrap_or("");
    if source.len() != 40
        || !source.bytes().all(|b| b.is_ascii_hexdigit())
        || manifest["schema_version"] != 1
        || manifest["release_version"] != "0.4.0-rc.1"
        || manifest["target"] != "x86_64-pc-windows-msvc"
        || manifest["source_revision"] != source
    {
        return Err(Error::Unsafe("invalid Windows RC build manifest".into()));
    }
    for name in ["ori-core", "ori-supervisor"] {
        let path = installation.join(format!("{name}.exe"));
        crate::lease::validate_path(&path)?;
        let component = &manifest["components"][name];
        let mut hash = Sha256::new();
        io::copy(&mut fs::File::open(path)?, &mut hash)?;
        if component["component"] != name
            || component["source_revision"] != source
            || component["target_os"] != "windows"
            || component["target_arch"] != "x86_64"
            || component["sha256"] != format!("{:x}", hash.finalize())
            || (name == "ori-core" && component["features"] != json!({"serial":true,"audio":false}))
        {
            return Err(Error::Unsafe(
                "installed Windows RC helper integrity failed".into(),
            ));
        }
    }
    let address: std::net::SocketAddr = ADDRESS.parse().expect("fixed loopback address");
    Ok(ServiceConfig {
        data_directory: data.to_owned(),
        backend: BackendSpec {
            name: format!("windows-rc1-{source}"),
            program: installation.join("ori-core.exe"),
            args: vec![
                "--mode".into(),
                "live".into(),
                "--data-dir".into(),
                data.to_str()
                    .ok_or_else(|| Error::Unsafe("Windows archive path is not Unicode".into()))?
                    .into(),
                "--host".into(),
                "127.0.0.1".into(),
                "--port".into(),
                address.port().to_string(),
                "--allow-serial".into(),
                "--stdin-lifeline".into(),
                "--archive-max-bytes".into(),
                (64u64 * 1024 * 1024 * 1024).to_string(),
            ],
            environment: BTreeMap::new(),
            control_addr: address,
            ports: vec![address],
            startup_timeout_ms: 30_000,
            shutdown_timeout_ms: 120_000,
            supervisor_lifeline: true,
        },
    })
}

async fn initialize_archive(config: &ServiceConfig) -> Result<()> {
    let _writer = WriterLease::acquire(&config.data_directory)?;
    let database = config.data_directory.join("sessions.sqlite3");
    if database.try_exists()? {
        return Ok(());
    }
    let mut command = Command::new(&config.backend.program);
    command.arg("archive-init-compact").arg(&database);
    let child = JobChild::spawn(&command, false)?;
    let deadline = Instant::now() + Duration::from_secs(30);
    while !child.exited()? {
        if Instant::now() >= deadline {
            child.terminate_and_reap()?;
            return Err(Error::Deadline);
        }
        sleep(Duration::from_millis(25)).await;
    }
    let code = child.exit_code()?;
    child.terminate_and_reap()?;
    if code != 0 || !database.is_file() {
        return Err(Error::Unsafe(
            "fresh compact archive initialization failed".into(),
        ));
    }
    Ok(())
}

pub(crate) async fn serve(mut config: ServiceConfig, installed: bool) -> Result<()> {
    let mut supervisor = Supervisor::new(&config.data_directory)?;
    let mut server = pipe::listen(&config.data_directory)?;
    if installed {
        initialize_archive(&config).await?;
    } else {
        let selection = state_directory(&config.data_directory)?.join("selection.json");
        if selection.try_exists()? {
            config.backend = serde_json::from_slice(&read_private(&selection, 1024 * 1024)?)?;
        }
    }
    let name = config.backend.name.clone();
    supervisor.start(config.backend).await?;
    let interrupt = async {
        if let Ok(mut signal) = tokio::signal::windows::ctrl_c() {
            signal.recv().await;
        } else {
            std::future::pending::<()>().await;
        }
    };
    tokio::pin!(interrupt);
    let mut health = tokio::time::interval(Duration::from_secs(1));
    loop {
        tokio::select! {
            _ = &mut interrupt => return supervisor.shutdown().await,
            _ = health.tick() => {
                if !supervisor.backend_running()? {
                    return Err(Error::Protocol("owned Windows backend exited".into()));
                }
                continue;
            }
            connected = server.connect() => {
                if let Err(error) = connected {
                    if disconnected(&error) {
                        let _ = server.disconnect();
                        continue;
                    }
                    return Err(error.into());
                }
            }
        }
        let request = timeout(
            Duration::from_secs(2),
            pipe::read::<AdminCommand>(&mut server),
        )
        .await;
        let mut stopped = false;
        let response = match request {
            Ok(Ok(AdminCommand::Status)) => match supervisor.status().await {
                Ok(status) => json!({"ok":true, "status":status, "backend_id":name}),
                Err(_) => json!({"ok":false, "error":"status_unavailable"}),
            },
            Ok(Ok(AdminCommand::Shutdown)) => match supervisor.shutdown().await {
                Ok(()) => {
                    stopped = true;
                    json!({"ok":true})
                }
                Err(_) => json!({"ok":false, "error":"shutdown_unconfirmed"}),
            },
            Ok(Ok(_)) => json!({"ok":false, "error":"windows_node_supports_status_and_shutdown"}),
            _ => json!({"ok":false, "error":"invalid_request"}),
        };
        pipe::reply(&mut server, &response).await;
        if let Err(error) = server.disconnect()
            && !disconnected(&error)
        {
            return Err(error.into());
        }
        if stopped {
            return Ok(());
        }
    }
}

fn disconnected(error: &io::Error) -> bool {
    use windows_sys::Win32::Foundation::{
        ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED,
    };
    error.raw_os_error().is_some_and(|code| {
        matches!(
            code as u32,
            ERROR_BROKEN_PIPE | ERROR_NO_DATA | ERROR_PIPE_NOT_CONNECTED
        )
    })
}

pub(crate) fn admin_pipe_path(data: &Path) -> Result<PathBuf> {
    pipe::path(data)
}

pub(crate) async fn admin(data: &Path, action: AdminCommand) -> Result<Value> {
    pipe::request(data, action).await
}

async fn status(data: &Path) -> Result<Option<Value>> {
    match admin(data, AdminCommand::Status).await {
        Ok(value)
            if value["ok"] == true
                && value["status"]["ready"] == true
                && value["status"]["writer_owned"] == true =>
        {
            Ok(Some(value))
        }
        Ok(_) => Err(Error::Protocol("owned Windows node is not ready".into())),
        Err(Error::Io(error)) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(other) => Err(other),
    }
}

async fn start(data: &Path) -> Result<Value> {
    pipe::ensure_data_directory(data)?;
    if let Some(value) = status(data).await? {
        return Ok(value);
    }
    let mut child = Command::new(std::env::current_exe()?)
        .args(["windows-node", "run"])
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(70);
    loop {
        if let Some(value) = status(data).await? {
            return Ok(value);
        }
        if child.try_wait()?.is_some() {
            // A concurrent launch can own the lease while still binding its pipe.
            match InstanceLease::acquire(data) {
                Err(Error::Busy) => {}
                Ok(_) => return Err(Error::Protocol("Windows supervisor exited before readiness (check the installed helpers, archive and loopback port)".into())),
                Err(other) => return Err(other),
            }
        }
        if Instant::now() >= deadline {
            return Err(Error::Deadline);
        }
        sleep(Duration::from_millis(50)).await;
    }
}

fn assert_stopped(data: &Path) -> Result<()> {
    if data.try_exists()? {
        let _instance = InstanceLease::acquire(data)?;
        let _writer = WriterLease::acquire(data)?;
    }
    Ok(())
}

fn logon(action: &str) -> Result<Value> {
    let system = std::env::var_os("SystemRoot")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .ok_or_else(|| Error::Unsafe("Windows directory unavailable".into()))?;
    let result = Command::new(system.join(r"System32\WindowsPowerShell\v1.0\powershell.exe"))
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-File",
        ])
        .arg(installation()?.join("register-user-task.ps1"))
        .arg("-Action")
        .arg(action)
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::null())
        .output()?;
    if !result.status.success() {
        return Err(Error::Protocol(format!(
            "Windows Task Scheduler {action} failed; run the installed register-user-task.ps1 for details"
        )));
    }
    Ok(json!({"ok":true}))
}

pub async fn command(action: &str) -> Result<()> {
    let data = data_dir()?;
    let value = match action {
        "run" => {
            pipe::ensure_data_directory(&data)?;
            return serve(configuration(&data, &installation()?)?, true).await;
        }
        "start" => start(&data).await?,
        "status" => status(&data)
            .await?
            .unwrap_or(json!({"ok":true, "service_state":"stopped"})),
        "shutdown" => {
            let response = admin(&data, AdminCommand::Shutdown).await?;
            if response["ok"] != true {
                return Err(Error::Protocol("Windows core shutdown unconfirmed".into()));
            }
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                match assert_stopped(&data) {
                    Err(Error::Busy) if Instant::now() < deadline => {
                        sleep(Duration::from_millis(25)).await
                    }
                    other => {
                        other?;
                        break;
                    }
                }
            }
            response
        }
        "check-stopped" => {
            assert_stopped(&data)?;
            json!({"ok":true})
        }
        "register" => logon("Register")?,
        "unregister" => logon("Unregister")?,
        _ => {
            return Err(Error::Unsafe(
                "windows-node run|start|status|shutdown|register|unregister|check-stopped".into(),
            ));
        }
    };
    println!("{}", serde_json::to_string(&value)?);
    Ok(())
}
