//! Trusted, hardware-free benchmark embedding. Not a production CLI or API.
#[cfg(unix)]
mod fixture {
    use ori_archive::{BackupCancellation, BackupOptions, restore_backup, verify_backup};
    use ori_core::{Core, CoreConfig};
    use ori_runtime::worker::{LinuxCgroup, WorkerSpec};
    use serde::Deserialize;
    use serde_json::{Value, json};
    use std::{
        collections::BTreeMap,
        fs,
        io::{Read, Write},
        os::fd::{AsFd, AsRawFd},
        os::unix::fs::MetadataExt,
        path::{Path, PathBuf},
        sync::{
            Arc,
            atomic::{AtomicBool, Ordering},
        },
        time::{Duration, Instant},
    };
    use tokio::{net::TcpListener, sync::mpsc};

    type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
    #[derive(Clone, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Profile {
        data: PathBuf,
        ready: PathBuf,
        bundle: PathBuf,
        receipts: PathBuf,
        analysis_python: PathBuf,
        research_cgroup: Option<PathBuf>,
        research_best_effort: bool,
        max_snapshot_bytes: u64,
        copy_timeout_ms: u64,
        total_timeout_ms: u64,
    }
    fn publish(path: &Path, bytes: &[u8]) -> Result<()> {
        if bytes.len() > 65536 {
            return Err("operator receipt exceeds 64 KiB".into());
        }
        let parent = path.parent().ok_or("operator receipt parent")?;
        let mut stage = tempfile::NamedTempFile::new_in(parent)?;
        stage.write_all(bytes)?;
        stage.as_file().sync_all()?;
        stage.persist_noclobber(path).map_err(|error| error.error)?;
        fs::File::open(parent)?.sync_all()?;
        Ok(())
    }
    fn receipt(path: &Path, value: Value) -> Result<()> {
        let mut bytes = serde_json::to_vec(&value)?;
        bytes.push(b'\n');
        publish(path, &bytes)
    }
    struct Ready(PathBuf, fs::Metadata);
    impl Drop for Ready {
        fn drop(&mut self) {
            if fs::symlink_metadata(&self.0)
                .is_ok_and(|now| now.dev() == self.1.dev() && now.ino() == self.1.ino())
            {
                let _ = fs::remove_file(&self.0);
            }
        }
    }
    fn input(
        core: &Arc<Core>,
        done: Arc<AtomicBool>,
        sender: mpsc::Sender<()>,
    ) -> Result<std::thread::JoinHandle<std::io::Result<()>>> {
        // One owned CLOEXEC duplicate, read only by this thread. A bounded poll
        // permits real JOIN even if serving fails before the parent's EOF.
        let mut file = fs::File::from(std::io::stdin().as_fd().try_clone_to_owned()?);
        let weak = Arc::downgrade(core);
        Ok(std::thread::Builder::new()
            .name("benchmark-stdin-lifeline".into())
            .spawn(move || {
                let result = (|| {
                    let mut asked = false;
                    while !done.load(Ordering::Acquire) {
                        let mut fd = libc::pollfd {
                            fd: file.as_raw_fd(),
                            events: libc::POLLIN,
                            revents: 0,
                        };
                        // SAFETY: this thread exclusively owns the live descriptor
                        // and the one-element pollfd remains valid for this call.
                        let ready = unsafe { libc::poll(&mut fd, 1, 100) };
                        if ready < 0 {
                            let error = std::io::Error::last_os_error();
                            if error.kind() == std::io::ErrorKind::Interrupted {
                                continue;
                            }
                            return Err(error);
                        }
                        if ready == 0 {
                            continue;
                        }
                        let mut byte = [0];
                        if file.read(&mut byte)? == 0 {
                            break;
                        }
                        if asked || byte[0] != b'B' {
                            return Err(std::io::Error::other(
                                "only one private backup trigger is accepted",
                            ));
                        }
                        asked = true;
                        sender
                            .blocking_send(())
                            .map_err(|_| std::io::Error::other("backup admission ended"))?;
                    }
                    Ok(())
                })();
                if let Some(core) = weak.upgrade() {
                    core.request_shutdown();
                }
                result
            })?)
    }
    async fn serve(profile: Profile) -> Result<()> {
        if [
            &profile.data,
            &profile.ready,
            &profile.bundle,
            &profile.receipts,
            &profile.analysis_python,
        ]
        .iter()
        .any(|path| !path.is_absolute())
            || profile
                .research_cgroup
                .as_ref()
                .is_some_and(|path| !path.is_absolute())
            || profile.research_best_effort == profile.research_cgroup.is_some()
            || profile.max_snapshot_bytes == 0
            || profile.max_snapshot_bytes > 16 * 1024_u64.pow(3)
            || profile.copy_timeout_ms == 0
            || profile.copy_timeout_ms > profile.total_timeout_ms
            || profile.total_timeout_ms > 600_000
        {
            return Err(
                "explicit absolute operator paths and bounded backup/research policy required"
                    .into(),
            );
        }
        if fs::read_dir(&profile.data)?.next().is_some() {
            return Err("benchmark Core requires a fresh, empty owned data directory".into());
        }
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let mut config = CoreConfig::replay(&profile.data);
        config.devices.transport = None;
        config.audio.native_enabled = false;
        config.max_archive_bytes = Some(profile.max_snapshot_bytes);
        // Exact trusted executable/argv policy from production main.rs. History
        // uses its existing production adaptation to `-I -B -m ori_research`.
        config.analysis_worker = Some(WorkerSpec {
            program: profile.analysis_python.clone(),
            args: vec![
                "-I".into(),
                "-B".into(),
                "-m".into(),
                "radioinspector.research_rpc".into(),
            ],
            environment: BTreeMap::new(),
            deadline_ms: 30_000,
            require_hard_memory: !profile.research_best_effort,
            linux_cgroup: profile.research_cgroup.clone().map(|path| LinuxCgroup {
                path,
                block_devices: Vec::new(),
            }),
        });
        let core = Core::open(config).await?;
        let result = serve_owned(core.clone(), listener, profile).await;
        core.request_shutdown();
        let closed = core.close().await;
        result?;
        closed?;
        Ok(())
    }
    async fn serve_owned(core: Arc<Core>, listener: TcpListener, profile: Profile) -> Result<()> {
        let url = format!("http://{}\n", listener.local_addr()?);
        publish(&profile.ready, url.as_bytes())?;
        let ready = Ready(profile.ready.clone(), fs::symlink_metadata(&profile.ready)?);
        let done = Arc::new(AtomicBool::new(false));
        let (sender, mut commands) = mpsc::channel(1);
        let input = input(&core, done.clone(), sender)?;
        let cancel = BackupCancellation::default();
        let mut serving = tokio::spawn(core.clone().serve(listener));
        let mut backup = None;
        let mut accepting = true;
        let served = loop {
            tokio::select! {
                outcome = &mut serving => break outcome,
                command = commands.recv(), if accepting => {
                    accepting = false;
                    if command.is_some() {
                        let profile = profile.clone();
                        let core = core.clone();
                        let token = cancel.clone();
                        backup = Some(tokio::spawn(async move {
                            let began = Instant::now();
                            let started = chrono::Utc::now().to_rfc3339();
                            receipt(&profile.receipts.join("backup-started.json"), json!({"phase":"running","started_at":started}))?;
                            let result = core.backup(&profile.bundle, BackupOptions {
                                max_snapshot_bytes: profile.max_snapshot_bytes,
                                copy_timeout: Duration::from_millis(profile.copy_timeout_ms),
                                total_timeout: Duration::from_millis(profile.total_timeout_ms), cancel: token }).await;
                            receipt(&profile.receipts.join("backup-finished.json"), json!({
                                "phase":if result.is_ok(){"completed"}else{"failed"},
                                "started_at":started,"finished_at":chrono::Utc::now().to_rfc3339(),
                                "elapsed_ms":began.elapsed().as_millis(),
                                "manifest":result.as_ref().ok(),"error":result.as_ref().err().map(ToString::to_string)}))?;
                            result?;
                            Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
                        }));
                    }
                }
            }
        };
        done.store(true, Ordering::Release);
        cancel.cancel();
        core.request_shutdown();
        let backup: Result<Result<()>> = if let Some(task) = backup {
            task.await.map_err(|error| error.into())
        } else {
            Ok(Ok(()))
        };
        let input = tokio::task::spawn_blocking(move || input.join()).await;
        let closed = core.close().await;
        drop(ready);
        served??;
        backup??;
        input?.map_err(|_| std::io::Error::other("stdin lifeline panicked"))??;
        closed?;
        Ok(())
    }
    pub async fn run() -> Result<()> {
        let args = std::env::args_os().skip(1).collect::<Vec<_>>();
        match args.first().and_then(|value| value.to_str()) {
            Some("serve") if args.len() == 2 => {
                let path = Path::new(&args[1]);
                let mut raw = Vec::new();
                fs::File::open(path)?.take(16_385).read_to_end(&mut raw)?;
                if raw.len() > 16_384 {
                    return Err("operator profile exceeds 16 KiB".into());
                }
                serve(serde_json::from_slice(&raw)?).await
            }
            Some("restore") if args.len() == 4 => {
                let (bundle, destination, report) = (
                    PathBuf::from(&args[1]),
                    PathBuf::from(&args[2]),
                    PathBuf::from(&args[3]),
                );
                tokio::task::spawn_blocking(move || {
                    let manifest = verify_backup(&bundle)?;
                    let restored = restore_backup(&bundle, &destination)?;
                    if manifest != restored { return Err("restore manifest changed".into()); }
                    receipt(&report, json!({"status":"passed","manifest":restored,
                        "new_destination":destination,"physical_carrier_independence":"unmeasured"}))
                }).await?
            }
            _ => Err(
                "usage: benchmark_background serve PROFILE | restore BUNDLE NEW_DATABASE REPORT"
                    .into(),
            ),
        }
    }
}

#[cfg(unix)]
#[tokio::main]
async fn main() {
    if let Err(error) = fixture::run().await {
        eprintln!("benchmark_background: {error}");
        std::process::exit(1);
    }
}

#[cfg(not(unix))]
fn main() {
    eprintln!("benchmark_background requires the existing Unix managed-process benchmark backend");
    std::process::exit(2);
}
