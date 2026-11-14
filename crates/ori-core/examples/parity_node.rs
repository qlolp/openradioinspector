//! Trusted HTTP parity embedding, never a production serial/audio connector.
#[cfg(unix)]
mod fixture {
    use ori_contracts::{DeviceRole, DeviceStream};
    use ori_core::{
        Core, CoreConfig,
        devices::{BoxTransport, DeviceError, DeviceFuture, DeviceResult, TransportFactory},
    };
    use std::{
        fs,
        io::{Read, Write},
        os::{
            fd::{AsFd, AsRawFd},
            unix::fs::MetadataExt,
        },
        path::{Path, PathBuf},
        sync::{
            Arc, Mutex,
            atomic::{AtomicBool, Ordering},
        },
    };
    use tokio::{
        io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
        net::TcpListener,
        task::JoinSet,
    };

    type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
    // Literal synthetic replies from tests/tinysa_control_fixture.py, not
    // measured RF or a second controller/measurement implementation.
    const COMMANDS: &str = "info version deviceid help output mode lna attenuate rbw calc pause resume capture touch release menu text refresh caloutput sweep scan scanraw zero frequencies data trace marker trigger repeat spur ultra ext_gain level freq modulation levelchange save recall sd_list sd_read sd_delete selftest status";
    fn reply(command: &str) -> std::io::Result<Vec<u8>> {
        Ok(match command {
            "info" => b"tinySA Ultra ZS405\r\n".to_vec(),
            "version" => b"tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n".to_vec(),
            "deviceid" => b"synthetic\r\n".to_vec(),
            "help" => format!("commands: {COMMANDS}\r\n").into_bytes(),
            "zero" => b"zero {level}\r\n174dBm\r\n".to_vec(),
            "status" => b"Paused\r\n".to_vec(),
            "scan 100M 101M 3 3" => {
                b"100000000 -90 0\r\n100500000 -80 0\r\n101000000 -70 0\r\n".to_vec()
            }
            "output off" | "mode input" | "lna off" | "attenuate auto" | "rbw 10" | "calc off"
            | "pause" | "caloutput off" | "release" | "refresh off" | "trace dBm"
            | "trigger auto" | "resume" => vec![],
            _ => return Err(std::io::Error::other("unspecified parity fixture command")),
        })
    }
    #[derive(Default)]
    struct Factory(Mutex<JoinSet<std::io::Result<()>>>);
    impl Factory {
        async fn close(&self) -> Result<()> {
            let mut peers = std::mem::take(&mut *self.0.lock().map_err(|_| "peer registry")?);
            let mut failure = None;
            while let Some(result) = peers.join_next().await {
                if let Err(error) = result
                    .map_err(|error| error.to_string())
                    .and_then(|r| r.map_err(|error| error.to_string()))
                {
                    failure.get_or_insert(error);
                }
            }
            failure.map_or(Ok(()), |error| Err(error.into()))
        }
    }
    impl TransportFactory for Factory {
        fn identity(&self, stream: &DeviceStream) -> DeviceResult<String> {
            let port = stream
                .port
                .as_deref()
                .filter(|port| port.starts_with("synthetic://api-parity/"));
            if stream.role != DeviceRole::SpectrumAnalyzer
                || stream.level_unit != "dBm"
                || stream
                    .settings
                    .as_ref()
                    .and_then(|s| s.get("adapter"))
                    .and_then(serde_json::Value::as_str)
                    != Some("tinysa-serial")
            {
                return Err(DeviceError::Invalid(
                    "parity fixture requires its synthetic tinySA adapter".into(),
                ));
            }
            Ok(format!(
                "owned-memory:{}",
                port.ok_or_else(|| DeviceError::Invalid(
                    "parity fixture refuses every physical device path".into()
                ))?
            ))
        }
        fn open<'a>(&'a self, stream: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
            Box::pin(async move {
                self.identity(stream)?;
                let mut peers = self.0.lock().map_err(|_| DeviceError::Closed)?;
                while let Some(result) = peers.try_join_next() {
                    result.map_err(|e| DeviceError::Worker(e.to_string()))??;
                }
                if peers.len() >= 32 {
                    return Err(DeviceError::QueueFull);
                }
                let (client, server) = tokio::io::duplex(4096);
                peers.spawn(async move {
                    let mut io = BufReader::new(server);
                    loop {
                        // Bound commands independently of an unterminated frame.
                        let mut wire = Vec::new();
                        loop {
                            let available = io.fill_buf().await?;
                            if available.is_empty() {
                                return if wire.is_empty() {
                                    Ok(())
                                } else {
                                    Err(std::io::Error::other("partial fixture command"))
                                };
                            }
                            let n = available
                                .iter()
                                .position(|b| *b == b'\r')
                                .map_or(available.len(), |n| n + 1);
                            if wire.len() + n > 1024 {
                                return Err(std::io::Error::other("fixture command budget"));
                            }
                            wire.extend_from_slice(&available[..n]);
                            io.consume(n);
                            if wire.last() == Some(&b'\r') {
                                break;
                            }
                        }
                        let command = std::str::from_utf8(&wire[..wire.len() - 1])
                            .map_err(std::io::Error::other)?;
                        let body = reply(command)?;
                        io.write_all(command.as_bytes()).await?;
                        io.write_all(b"\r\n").await?;
                        io.write_all(&body).await?;
                        io.write_all(b"ch> ").await?;
                    }
                });
                Ok(Box::new(client) as BoxTransport)
            })
        }
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
    fn publish(path: &Path, url: &str) -> Result<Ready> {
        let parent = path.parent().ok_or("readiness parent")?;
        let mut stage = tempfile::NamedTempFile::new_in(parent)?;
        writeln!(stage, "{url}")?;
        stage.as_file().sync_all()?;
        stage.persist_noclobber(path).map_err(|error| error.error)?;
        fs::File::open(parent)?.sync_all()?;
        Ok(Ready(path.into(), fs::symlink_metadata(path)?))
    }
    fn input(
        core: &Arc<Core>,
        done: Arc<AtomicBool>,
    ) -> Result<std::thread::JoinHandle<std::io::Result<()>>> {
        let mut file = fs::File::from(std::io::stdin().as_fd().try_clone_to_owned()?);
        let weak = Arc::downgrade(core);
        Ok(std::thread::Builder::new()
            .name("parity-stdin-lifeline".into())
            .spawn(move || {
                let result = (|| {
                    while !done.load(Ordering::Acquire) {
                        let mut fd = libc::pollfd {
                            fd: file.as_raw_fd(),
                            events: libc::POLLIN,
                            revents: 0,
                        };
                        // SAFETY: this thread owns the live descriptor and pollfd.
                        let ready = unsafe { libc::poll(&mut fd, 1, 100) };
                        if ready < 0 {
                            let error = std::io::Error::last_os_error();
                            if error.kind() == std::io::ErrorKind::Interrupted {
                                continue;
                            }
                            return Err(error);
                        }
                        if ready > 0 && file.read(&mut [0])? == 0 {
                            break;
                        }
                    }
                    Ok(())
                })();
                if let Some(core) = weak.upgrade() {
                    core.request_shutdown();
                }
                result
            })?)
    }
    pub async fn run() -> Result<()> {
        let mut args = std::env::args_os().skip(1);
        let (mut data, mut ready, mut python) = (None, None, None);
        while let Some(key) = args.next() {
            let value = PathBuf::from(
                args.next()
                    .ok_or("explicit fixture option value required")?,
            );
            let slot = match key.to_str() {
                Some("--data-dir") => &mut data,
                Some("--ready-file") => &mut ready,
                Some("--analysis-python") => &mut python,
                _ => return Err("unknown parity fixture option".into()),
            };
            if slot.replace(value).is_some() {
                return Err("duplicate fixture option".into());
            }
        }
        let data = data.ok_or("--data-dir required")?;
        let ready = ready.ok_or("--ready-file required")?;
        if !data.is_absolute()
            || !ready.is_absolute()
            || python.as_ref().is_some_and(|path| !path.is_absolute())
            || fs::read_dir(&data)?.next().is_some()
        {
            return Err("fresh absolute fixture paths required".into());
        }
        let factory = Arc::new(Factory::default());
        let mut config = CoreConfig::replay(&data);
        config.devices.transport = Some(factory.clone());
        config.audio.native_enabled = false;
        config.max_archive_bytes = Some(1024 * 1024 * 1024);
        if let Some(program) = python {
            config.analysis_worker = Some(ori_runtime::worker::WorkerSpec {
                program,
                args: vec![
                    "-I".into(),
                    "-B".into(),
                    "-m".into(),
                    "radioinspector.research_rpc".into(),
                ],
                environment: Default::default(),
                deadline_ms: 30_000,
                require_hard_memory: false,
                linux_cgroup: None,
            });
        }
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let core = Core::open(config).await?;
        let done = Arc::new(AtomicBool::new(false));
        let result = async {
            let input = input(&core, done.clone())?;
            let result = async {
                let _ready = publish(&ready, &format!("http://{}", listener.local_addr()?))?;
                core.clone().serve(listener).await?;
                Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
            }
            .await;
            done.store(true, Ordering::Release);
            let joined = input.join().map_err(|_| "stdin lifeline panicked")?;
            result?;
            joined?;
            Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
        }
        .await;
        core.request_shutdown();
        let closed = core.close().await;
        let peers = factory.close().await;
        result?;
        closed?;
        peers?;
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use tokio::io::AsyncReadExt;

        fn stream(port: &str) -> DeviceStream {
            serde_json::from_value(serde_json::json!({
                "id":"lab", "session_id":"session", "device":"synthetic tinySA",
                "role":"spectrum_analyzer", "level_unit":"dBm", "port":port,
                "settings":{"adapter":"tinysa-serial"}
            }))
            .unwrap()
        }

        #[tokio::test]
        async fn factory_rejects_physical_paths_and_foreign_adapters_before_open() {
            let factory = Factory::default();
            for port in ["COM1", "/dev/ttyUSB0", "synthetic://other/lab"] {
                assert!(factory.identity(&stream(port)).is_err());
                assert!(factory.open(&stream(port)).await.is_err());
            }
            let mut foreign = stream("synthetic://api-parity/lab");
            foreign.settings = Some(
                serde_json::from_value(serde_json::json!({"adapter":"ardv1-serial"})).unwrap(),
            );
            assert!(factory.open(&foreign).await.is_err());
            assert!(factory.0.lock().unwrap().is_empty());
            factory.close().await.unwrap();
        }

        #[tokio::test]
        async fn owned_duplex_uses_original_literal_reply_and_joins_after_eof() {
            let factory = Factory::default();
            let stream = stream("synthetic://api-parity/lab");
            let mut io = factory.open(&stream).await.unwrap();
            io.write_all(b"info\r").await.unwrap();
            let expected = b"info\r\ntinySA Ultra ZS405\r\nch> ";
            let mut actual = vec![0; expected.len()];
            io.read_exact(&mut actual).await.unwrap();
            assert_eq!(actual, expected);
            drop(io);
            factory.close().await.unwrap();
            assert!(factory.0.lock().unwrap().is_empty());
        }

        #[tokio::test]
        async fn unspecified_command_cannot_gain_a_synthetic_success_reply() {
            let factory = Factory::default();
            let stream = stream("synthetic://api-parity/lab");
            let mut io = factory.open(&stream).await.unwrap();
            io.write_all(b"output on\r").await.unwrap();
            assert_eq!(io.read(&mut [0]).await.unwrap(), 0);
            drop(io);
            assert!(factory.close().await.is_err());
        }
    }
}

#[cfg(unix)]
#[tokio::main]
async fn main() {
    if let Err(error) = fixture::run().await {
        eprintln!("parity_node: {error}");
        std::process::exit(1);
    }
}
#[cfg(not(unix))]
fn main() {
    eprintln!("parity_node requires the existing Unix owned stdin-lifeline fixture backend");
    std::process::exit(2);
}
