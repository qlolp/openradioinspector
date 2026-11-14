use ori_core::{Core, CoreConfig, CoreError, CoreMode, LanSecurityConfig, Result, RuntimeIdentity};
use std::{
    ffi::OsString,
    fs::{self, OpenOptions},
    io::{IsTerminal, Read, Write},
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::{Path, PathBuf},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::watch,
};
struct Options {
    data: PathBuf,
    data_explicit: bool,
    database: Option<PathBuf>,
    host: IpAddr,
    port: u16,
    mode: CoreMode,
    maximum: u64,
    ready: Option<PathBuf>,
    lifeline: bool,
    check: bool,
    lan: bool,
    tls_state: Option<PathBuf>,
    hosts: Vec<String>,
    control_port: u16,
    analysis_python: Option<PathBuf>,
    research_cgroup: Option<PathBuf>,
    research_best_effort: bool,
    research_deadline_ms: u64,
    allow_serial: bool,
    allow_audio: bool,
    task_book: Option<PathBuf>,
    legacy_library: Option<PathBuf>,
}
fn default_data() -> PathBuf {
    if let Some(path) = std::env::var_os("ORI_RUNTIME_DATA_DIR") {
        return path.into();
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("."));
    if cfg!(windows) {
        std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData/Local"))
            .join("OpenRadioInspector")
    } else if cfg!(target_os = "macos") {
        home.join("Library/Application Support/OpenRadioInspector")
    } else {
        std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
            .unwrap_or_else(|| home.join(".local/share"))
            .join("openradioinspector")
    }
}
fn options() -> Result<Option<Options>> {
    let mut result = Options {
        data: default_data(),
        data_explicit: false,
        database: None,
        host: IpAddr::V4(Ipv4Addr::LOCALHOST),
        port: 8770,
        mode: CoreMode::Shadow,
        maximum: 1024 * 1024 * 1024,
        ready: None,
        lifeline: false,
        check: false,
        lan: false,
        tls_state: None,
        hosts: Vec::new(),
        control_port: 8771,
        analysis_python: None,
        research_cgroup: None,
        research_best_effort: false,
        research_deadline_ms: 30000,
        allow_serial: false,
        allow_audio: false,
        task_book: None,
        legacy_library: None,
    };
    let mut args = std::env::args_os().skip(1);
    while let Some(key) = args.next() {
        let key = key
            .to_str()
            .ok_or(CoreError::Invalid("option must be UTF-8"))?;
        if key == "--help" || key == "-h" {
            println!(
                "ori-core --mode shadow|replay|live --data-dir DIR [--database FILE] [--host IP] [--port PORT] [--lan --trusted-host NAME:PORT --tls-state DIR] [--control-port PORT] [--archive-max-bytes BYTES] [--allow-serial] [--ready-file FILE] [--stdin-lifeline] [--check-ready] [--analysis-python PYTHON (--research-cgroup DIR | --research-best-effort)] [--research-deadline-ms 30000]\nori-core init-lan --data-dir DIR --name DNS_OR_IP [--name DNS_OR_IP]\nori-core pair --data-dir DIR [--ttl-seconds 300] [--attempts 5]\nDefault shadow opens no archive writer or hardware. Replay is an explicit private-copy candidate. Live requires --data-dir; native serial requires --mode live --allow-serial. Release acceptance is incomplete. LAN identity provisioning and operator trust are explicit."
            );
            println!(
                "TaskBook uses ephemeral storage by default; --task-book FILE selects the operator's persistent book. This candidate supports planning; execution is unavailable. --allow-audio explicitly enables the compiled native audio backend; construction opens no input."
            );
            println!(
                "--legacy-library DIR restricts explicit legacy PAN selection. Its scientific worker inherits the configured analysis interpreter and uses the installed radioinspector.legacy_rpc module."
            );
            return Ok(None);
        }
        if key == "--version" {
            println!("ori-core 0.1.0 candidate");
            return Ok(None);
        }
        match key {
            "--allow-serial" => result.allow_serial = true,
            "--allow-audio" => result.allow_audio = true,
            "--task-book" => result.task_book = Some(PathBuf::from(value(&mut args)?)),
            "--legacy-library" => result.legacy_library = Some(PathBuf::from(value(&mut args)?)),
            "--stdin-lifeline" => result.lifeline = true,
            "--check-ready" => result.check = true,
            "--lan" => result.lan = true,
            "--analysis-python" => result.analysis_python = Some(PathBuf::from(value(&mut args)?)),
            "--research-cgroup" => result.research_cgroup = Some(PathBuf::from(value(&mut args)?)),
            "--research-best-effort" => result.research_best_effort = true,
            "--research-deadline-ms" => {
                result.research_deadline_ms = value(&mut args)?
                    .to_str()
                    .ok_or(CoreError::Invalid("research deadline"))?
                    .parse()
                    .map_err(|_| CoreError::Invalid("research deadline"))?
            }
            "--tls-state" => result.tls_state = Some(PathBuf::from(value(&mut args)?)),
            "--trusted-host" => result.hosts.push(
                value(&mut args)?
                    .into_string()
                    .map_err(|_| CoreError::Invalid("trusted host"))?,
            ),
            "--control-port" => {
                result.control_port = value(&mut args)?
                    .to_str()
                    .ok_or(CoreError::Invalid("control port"))?
                    .parse()
                    .map_err(|_| CoreError::Invalid("control port"))?
            }
            "--data-dir" => {
                result.data = PathBuf::from(value(&mut args)?);
                result.data_explicit = true;
            }
            "--database" => result.database = Some(PathBuf::from(value(&mut args)?)),
            "--ready-file" => result.ready = Some(PathBuf::from(value(&mut args)?)),
            "--host" => {
                result.host = value(&mut args)?
                    .to_str()
                    .ok_or(CoreError::Invalid("host"))?
                    .parse()
                    .map_err(|_| CoreError::Invalid("host"))?
            }
            "--port" => {
                result.port = value(&mut args)?
                    .to_str()
                    .ok_or(CoreError::Invalid("port"))?
                    .parse()
                    .map_err(|_| CoreError::Invalid("port"))?
            }
            "--archive-max-bytes" => {
                result.maximum = value(&mut args)?
                    .to_str()
                    .ok_or(CoreError::Invalid("archive max bytes"))?
                    .parse()
                    .map_err(|_| CoreError::Invalid("archive max bytes"))?
            }
            "--mode" => {
                result.mode = match value(&mut args)?.to_str() {
                    Some("shadow") => CoreMode::Shadow,
                    Some("replay") => CoreMode::Replay,
                    Some("live") => CoreMode::Live,
                    _ => return Err(CoreError::Invalid("mode must be shadow, replay or live")),
                }
            }
            _ => return Err(CoreError::Invalid("unknown option; see --help")),
        }
    }
    if result.maximum == 0 {
        return Err(CoreError::Invalid("archive max bytes must be positive"));
    }
    if result.mode == CoreMode::Live && !result.data_explicit {
        return Err(CoreError::Invalid(
            "live mode requires an explicit --data-dir",
        ));
    }
    if result.allow_serial && !cfg!(feature = "serial") {
        return Err(CoreError::Invalid(
            "serial support was not compiled; build the serial feature explicitly",
        ));
    }
    if result.allow_audio && !cfg!(feature = "audio") {
        return Err(CoreError::Invalid(
            "audio support was not compiled; build the audio feature explicitly",
        ));
    }
    if result.allow_serial && result.mode != CoreMode::Live {
        return Err(CoreError::Invalid("native serial requires --mode live"));
    }
    if result.research_deadline_ms == 0 || result.research_deadline_ms > 86400000 {
        return Err(CoreError::Invalid(
            "research deadline must be1..86400000 milliseconds",
        ));
    }
    if let Some(program) = &result.analysis_python {
        if !program.is_absolute() {
            return Err(CoreError::Invalid(
                "analysis Python executable must be absolute",
            ));
        }
        if !result.research_best_effort && result.research_cgroup.is_none() {
            return Err(CoreError::Invalid(
                "analysis requires a delegated research cgroup; --research-best-effort is an explicit development fallback",
            ));
        }
    } else if result.research_cgroup.is_some() || result.research_best_effort {
        return Err(CoreError::Invalid(
            "research settings require --analysis-python",
        ));
    }
    if !result.lan && !result.host.is_loopback() {
        return Err(CoreError::Invalid(
            "plaintext compatibility is loopback-only; LAN requires --lan",
        ));
    }
    if result.lan && result.hosts.is_empty() {
        return Err(CoreError::Invalid(
            "LAN requires an explicit --trusted-host authority",
        ));
    }
    if !result.lan && (!result.hosts.is_empty() || result.tls_state.is_some()) {
        return Err(CoreError::Invalid("TLS options require --lan"));
    }
    if !result.data.is_absolute() {
        result.data = std::env::current_dir()?.join(result.data);
    }
    if let Some(directory) = std::env::var_os("ORI_RUNTIME_DATA_DIR")
        && Path::new(&directory) != result.data.as_path()
    {
        return Err(CoreError::Invalid(
            "runtime data directory conflicts with CLI",
        ));
    }
    Ok(Some(result))
}
fn value(args: &mut impl Iterator<Item = OsString>) -> Result<OsString> {
    args.next()
        .ok_or(CoreError::Invalid("option value missing"))
}
struct ReadyFile {
    path: PathBuf,
    metadata: fs::Metadata,
    #[cfg(not(unix))]
    bytes: Vec<u8>,
}
impl ReadyFile {
    fn publish(path: &Path, url: &str) -> Result<Self> {
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        let temp = parent.join(format!(".ori-ready-{}", std::process::id()));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut created = false;
        let result = (|| {
            let mut file = options.open(&temp)?;
            created = true;
            let bytes = format!("{url}\n").into_bytes();
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::hard_link(&temp, path)?;
            #[cfg(unix)]
            std::fs::File::open(parent)?.sync_all()?;
            let metadata = fs::symlink_metadata(path)?;
            Ok(Self {
                path: path.into(),
                metadata,
                #[cfg(not(unix))]
                bytes,
            })
        })();
        if created {
            let _ = fs::remove_file(&temp);
        }
        result
    }
}
impl Drop for ReadyFile {
    fn drop(&mut self) {
        let owned = fs::symlink_metadata(&self.path).is_ok_and(|current| {
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                current.dev() == self.metadata.dev() && current.ino() == self.metadata.ino()
            }
            #[cfg(not(unix))]
            {
                current.created().ok() == self.metadata.created().ok()
                    && fs::read(&self.path).ok().as_deref() == Some(self.bytes.as_slice())
            }
        });
        if owned {
            let _ = fs::remove_file(&self.path);
        }
    }
}
#[tokio::main]
async fn main() {
    if let Err(error) = execute().await {
        eprintln!("ori-core: {error}");
        std::process::exit(1);
    }
}
async fn execute() -> Result<()> {
    if std::env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("--build-info")) {
        if std::env::args_os().len() != 2 {
            return Err(CoreError::Invalid("--build-info takes no other arguments"));
        }
        println!(
            "{}",
            serde_json::json!({
                "schema_version":1, "component":"ori-core", "version":env!("CARGO_PKG_VERSION"),
                "target_arch":std::env::consts::ARCH, "target_os":std::env::consts::OS,
                "source_revision":option_env!("ORI_BUILD_SOURCE_SHA"), "sqlite_version":rusqlite::version(),
                "features":{"serial":cfg!(feature="serial"),"audio":cfg!(feature="audio")}
            })
        );
        return Ok(());
    }
    if let Some(command) = std::env::args_os().nth(1)
        && matches!(command.to_str(), Some("init-lan" | "pair"))
    {
        return operator_command(command.to_str().expect("matched UTF-8")).await;
    }
    let Some(options) = options()? else {
        return Ok(());
    };
    if options.check {
        return check_ready(&options).await;
    }
    let (stop, mut stopped) = watch::channel(false);
    #[cfg(unix)]
    {
        let mut interrupt =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        let stop = stop.clone();
        tokio::spawn(async move {
            tokio::select! {_=interrupt.recv()=>{},_=terminate.recv()=>{}}
            stop.send_replace(true);
        });
    }
    #[cfg(not(unix))]
    {
        let stop = stop.clone();
        tokio::spawn(async move {
            let _ = tokio::signal::ctrl_c().await;
            stop.send_replace(true);
        });
    }
    if options.mode.writes_archive() {
        let mut builder = fs::DirBuilder::new();
        builder.recursive(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        builder.create(&options.data)?;
    }
    // Bind once, before opening the writer; port 0 is resolved from this actual listener.
    let listener = TcpListener::bind(SocketAddr::new(options.host, options.port)).await?;
    let actual_port = listener.local_addr()?.port();
    let hosts = trusted_hosts(&options, actual_port)?;
    let mut configuration = CoreConfig::new(&options.data);
    #[cfg(feature = "serial")]
    if options.allow_serial {
        configuration.devices.transport =
            Some(std::sync::Arc::new(ori_core::devices::NativeSerialFactory));
    }
    configuration.mode = options.mode;
    configuration.audio.native_enabled = options.allow_audio;
    configuration.task_book = options.task_book.clone();
    configuration.legacy.library = options.legacy_library.clone();
    configuration.identity = RuntimeIdentity::from_environment()?;
    configuration.max_archive_bytes = Some(options.maximum);
    if let Some(program) = &options.analysis_python {
        configuration.analysis_worker = Some(ori_runtime::worker::WorkerSpec {
            program: program.clone(),
            args: vec![
                "-I".into(),
                "-B".into(),
                "-m".into(),
                "radioinspector.research_rpc".into(),
            ],
            environment: std::collections::BTreeMap::new(),
            deadline_ms: options.research_deadline_ms,
            require_hard_memory: !options.research_best_effort,
            linux_cgroup: options.research_cgroup.as_ref().map(|path| {
                ori_runtime::worker::LinuxCgroup {
                    path: path.clone(),
                    block_devices: Vec::new(),
                }
            }),
        });
    }
    if options.lan {
        configuration.lan_security = Some(LanSecurityConfig {
            directory: tls_directory(&options),
            origins: hosts
                .iter()
                .map(|host| {
                    url::Url::parse(&format!("https://{host}/"))
                        .map(|url| url.origin().ascii_serialization())
                        .map_err(|_| CoreError::Invalid("trusted host"))
                })
                .collect::<Result<_>>()?,
            hosts: hosts.clone(),
        });
    }
    let control = if options.lan && configuration.identity.is_some() {
        if options.control_port == 0 {
            return Err(CoreError::Invalid(
                "supervised LAN control port must be explicit",
            ));
        }
        Some(TcpListener::bind((Ipv4Addr::LOCALHOST, options.control_port)).await?)
    } else {
        None
    };
    if let Some(database) = options.database {
        configuration.database = if database.is_absolute() {
            database
        } else {
            options.data.join(database)
        };
    }
    let core = Core::open(configuration).await?;
    if *stopped.borrow() {
        return core.close().await;
    }
    let url = if options.lan {
        format!("https://{}", hosts.first().expect("validated LAN hosts"))
    } else {
        format!("http://{}", listener.local_addr()?)
    };
    let ready = if let Some(path) = options.ready {
        match ReadyFile::publish(&path, &url) {
            Ok(ready) => Some(ready),
            Err(error) => {
                core.close().await?;
                return Err(error);
            }
        }
    } else {
        None
    };
    if options.lifeline {
        let weak = std::sync::Arc::downgrade(&core);
        std::thread::Builder::new()
            .name("owned-core-lifeline".into())
            .spawn(move || {
                let mut byte = [0u8; 1];
                let mut input = std::io::stdin().lock();
                while input.read(&mut byte).is_ok_and(|n| n != 0) {}
                if let Some(core) = weak.upgrade() {
                    core.request_shutdown();
                }
            })?;
    }
    let signal_core = core.clone();
    tokio::spawn(async move {
        loop {
            if *stopped.borrow() {
                signal_core.request_shutdown();
                break;
            }
            if stopped.changed().await.is_err() {
                break;
            }
        }
    });
    println!("OpenRadioInspector ready: {url}");
    std::io::stdout().flush()?;
    let result = if options.lan {
        if let Some(control) = control {
            let (public, private) = tokio::join!(
                core.clone().serve_tls(listener),
                core.clone().serve_control(control)
            );
            public.and(private)
        } else {
            core.serve_tls(listener).await
        }
    } else {
        core.serve(listener).await
    };
    drop(ready);
    result
}
fn tls_directory(options: &Options) -> PathBuf {
    options
        .tls_state
        .clone()
        .unwrap_or_else(|| options.data.join(".ori-runtime/security"))
}
fn trusted_hosts(options: &Options, actual_port: u16) -> Result<Vec<String>> {
    options
        .hosts
        .iter()
        .map(|host| {
            let parsed = url::Url::parse(&format!("https://{host}/"))
                .map_err(|_| CoreError::Invalid("trusted host"))?;
            if options.port == 0 && parsed.port() == Some(0) {
                let mut parsed = parsed;
                parsed
                    .set_port(Some(actual_port))
                    .map_err(|_| CoreError::Invalid("trusted host port"))?;
                Ok(parsed[url::Position::BeforeHost..url::Position::AfterPort].to_owned())
            } else {
                Ok(host.clone())
            }
        })
        .collect()
}
fn create_data(path: &Path) -> Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)?;
    Ok(())
}
async fn operator_command(command: &str) -> Result<()> {
    let mut data = default_data();
    let mut names = Vec::new();
    let mut ttl = 300;
    let mut attempts = 5;
    let mut args = std::env::args_os().skip(2);
    while let Some(key) = args.next() {
        match key.to_str() {
            Some("--data-dir") => data = value(&mut args)?.into(),
            Some("--name") if command == "init-lan" => names.push(
                value(&mut args)?
                    .into_string()
                    .map_err(|_| CoreError::Invalid("certificate name"))?,
            ),
            Some("--ttl-seconds") if command == "pair" => {
                ttl = value(&mut args)?
                    .to_str()
                    .ok_or(CoreError::Invalid("pairing duration"))?
                    .parse()
                    .map_err(|_| CoreError::Invalid("pairing duration"))?
            }
            Some("--attempts") if command == "pair" => {
                attempts = value(&mut args)?
                    .to_str()
                    .ok_or(CoreError::Invalid("pairing attempts"))?
                    .parse()
                    .map_err(|_| CoreError::Invalid("pairing attempts"))?
            }
            _ => return Err(CoreError::Invalid("unknown operator option; see --help")),
        }
    }
    if !data.is_absolute() {
        data = std::env::current_dir()?.join(data);
    }
    if command == "init-lan" {
        create_data(&data)?;
        let directory = ori_runtime::lease::state_directory(&data)?.join("security");
        let identity = ori_runtime::security::TlsIdentity::create(&directory, names)?;
        let ca = directory.join("public-ca.pem");
        let mut opts = OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut file = opts.open(&ca)?;
        file.write_all(identity.trust_anchor_pem().as_bytes())?;
        file.sync_all()?;
        println!(
            "Installation CA: {}\nServer SHA-256: {}\nInstall the CA through each client's operating-system trust settings before pairing.",
            ca.display(),
            identity.fingerprint_sha256()
        );
    } else {
        if !std::io::stdout().is_terminal() {
            return Err(CoreError::Invalid(
                "pairing codes require a local interactive terminal; use owner-only operator IPC for native tools",
            ));
        }
        if ttl == 0 || ttl > 300 || attempts == 0 || attempts > 10 {
            return Err(CoreError::Invalid(
                "pairing bounds are 1..300 seconds and 1..10 attempts",
            ));
        }
        let response = ori_core::admin::request(&data, ttl, attempts).await?;
        let code = response["pairing_code"]
            .as_str()
            .ok_or(CoreError::Invalid("pairing window unavailable"))?;
        println!(
            "Pairing code: {code}\nExpires in {ttl} seconds; one use, at most {attempts} attempts."
        );
    }
    Ok(())
}
async fn check_ready(options: &Options) -> Result<()> {
    let host = options.host;
    let port = options.port;
    let host = if host.is_unspecified() {
        IpAddr::V4(Ipv4Addr::LOCALHOST)
    } else {
        host
    };
    let hosts = trusted_hosts(options, port)?;
    let tls = if options.lan {
        let identity = ori_runtime::security::TlsIdentity::load(&tls_directory(options))?;
        let mut roots = rustls::RootCertStore::empty();
        for certificate in rustls_pemfile::certs(&mut identity.trust_anchor_pem().as_bytes()) {
            roots
                .add(certificate?)
                .map_err(|_| CoreError::Invalid("local CA invalid"))?;
        }
        let config = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .map_err(|_| CoreError::Invalid("TLS versions unavailable"))?
        .with_root_certificates(roots)
        .with_no_client_auth();
        let authority = hosts
            .first()
            .ok_or(CoreError::Invalid("trusted host missing"))?;
        let url = url::Url::parse(&format!("https://{authority}/"))
            .map_err(|_| CoreError::Invalid("trusted host"))?;
        let server_name = rustls::pki_types::ServerName::try_from(
            url.host_str()
                .ok_or(CoreError::Invalid("trusted host"))?
                .trim_matches(['[', ']'])
                .to_owned(),
        )
        .map_err(|_| CoreError::Invalid("TLS server name"))?;
        Some((
            tokio_rustls::TlsConnector::from(std::sync::Arc::new(config)),
            server_name,
            authority.clone(),
        ))
    } else {
        None
    };
    let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    while tokio::time::Instant::now() < deadline {
        let request = async {
            let socket = TcpStream::connect(SocketAddr::new(host, port)).await?;
            if let Some((connector, name, authority)) = &tls {
                let socket = connector.connect(name.clone(), socket).await?;
                readiness_request(socket, authority, "/api/v1/security/status").await
            } else {
                readiness_request(socket, &format!("{host}:{port}"), "/api/v1/health").await
            }
        };
        if let Ok(Ok(bytes)) = tokio::time::timeout(Duration::from_secs(1), request).await
            && bytes.len() <= 65536
            && let Some(split) = bytes.windows(4).position(|s| s == b"\r\n\r\n")
            && (bytes.starts_with(b"HTTP/1.1 200") || bytes.starts_with(b"HTTP/1.0 200"))
            && let Ok(value) = ori_contracts::parse_json_value(&bytes[split + 4..], 65536)
            && if options.lan {
                value["data"]["mode"] == "lan_tls"
            } else {
                value["data"]["status"] == "ok"
            }
        {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Err(CoreError::Invalid("core readiness deadline exceeded"))
}
async fn readiness_request(
    mut socket: impl tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    authority: &str,
    path: &str,
) -> std::io::Result<Vec<u8>> {
    socket
        .write_all(
            format!("GET {path} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n")
                .as_bytes(),
        )
        .await?;
    let mut bytes = Vec::new();
    socket.take(65537).read_to_end(&mut bytes).await?;
    Ok(bytes)
}
