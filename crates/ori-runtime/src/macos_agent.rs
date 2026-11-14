//! Fixed personal-RC service configuration. Never selects a simulator or Python.
use crate::{Error, Result, service::ServiceConfig, supervisor::BackendSpec};
use std::{collections::BTreeMap, path::Path};

pub fn configuration(bundle: &Path, data: &Path, maximum: u64) -> Result<ServiceConfig> {
    if !bundle.is_absolute() || !data.is_absolute() || maximum == 0 {
        return Err(Error::Unsafe(
            "RC requires absolute bundle/data and a positive archive budget".into(),
        ));
    }
    let address = "127.0.0.1:8772".parse().expect("fixed loopback address");
    Ok(ServiceConfig {
        data_directory: data.to_owned(),
        backend: BackendSpec {
            name: "macos-rc1-live".into(),
            program: bundle.join("Contents/Helpers/ori-core"),
            args: vec![
                "--mode".into(),
                "live".into(),
                "--data-dir".into(),
                data.to_str()
                    .ok_or_else(|| Error::Unsafe("non-UTF8 RC path".into()))?
                    .into(),
                "--host".into(),
                "127.0.0.1".into(),
                "--port".into(),
                "8772".into(),
                "--allow-serial".into(),
                "--stdin-lifeline".into(),
                "--archive-max-bytes".into(),
                maximum.to_string(),
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

#[cfg(target_os = "macos")]
pub async fn run() -> Result<()> {
    let executable = std::fs::canonicalize(std::env::current_exe()?)?;
    let bundle = executable
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .filter(|path| path.extension().is_some_and(|ext| ext == "app"))
        .ok_or_else(|| {
            Error::Unsafe("macos-agent must run from the installed .app bundle".into())
        })?;
    let mut pwd = std::mem::MaybeUninit::<libc::passwd>::uninit();
    let mut buffer = vec![0u8; 16_384];
    let mut result = std::ptr::null_mut();
    let status = unsafe {
        libc::getpwuid_r(
            libc::geteuid(),
            pwd.as_mut_ptr(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut result,
        )
    };
    if status != 0 || result.is_null() {
        return Err(Error::Unsafe(
            "cannot resolve this user's home directory".into(),
        ));
    }
    let pwd = unsafe { pwd.assume_init() };
    use std::os::unix::{ffi::OsStrExt, fs::DirBuilderExt};
    let home =
        std::ffi::OsStr::from_bytes(unsafe { std::ffi::CStr::from_ptr(pwd.pw_dir) }.to_bytes());
    let data = Path::new(home).join("Library/Application Support/OpenRadioInspector/macOS-RC");
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&data)?;
    crate::lease::state_directory(&data)?;
    let mut config = configuration(bundle, &data, 64 * 1024 * 1024 * 1024)?;
    config.backend.name = bundled_backend_id(bundle)?;
    crate::service::run_personal_rc(config).await
}

#[cfg(not(target_os = "macos"))]
pub async fn run() -> Result<()> {
    Err(Error::Unsafe("SMAppService RC agent requires macOS".into()))
}

/// Identity of an immutable RC package, shared with the native client. This
/// checks only installed files; it never opens hardware or a working archive.
pub fn bundled_backend_id(bundle: &Path) -> Result<String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let root = std::fs::canonicalize(bundle)?;
    let path = root.join("Contents/Resources/rc-build-info.json");
    let metadata = std::fs::symlink_metadata(&path)?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > 65_536 {
        return Err(Error::Unsafe("invalid RC build manifest".into()));
    }
    let bytes = std::fs::read(path)?;
    let value: serde_json::Value = serde_json::from_slice(&bytes)?;
    let source = value["source_revision"].as_str().unwrap_or("");
    if value["schema_version"] != 1
        || value["release_version"] != "0.4.0-macos.rc.1"
        || value["bundle_identifier"] != "org.openradioinspector.macos.rc"
        || value["architecture"] != "arm64"
        || source.len() != 40
        || !source
            .bytes()
            .all(|v| v.is_ascii_digit() || (b'a'..=b'f').contains(&v))
    {
        return Err(Error::Unsafe("mixed or invalid RC build manifest".into()));
    }
    for name in ["ori-core", "ori-supervisor"] {
        let item = &value["components"][name];
        let relative = format!("Contents/Helpers/{name}");
        if item["component"] != name
            || item["source_revision"] != source
            || item["target_arch"] != "aarch64"
            || item["target_os"] != "macos"
            || item["path"] != relative
            || (name == "ori-core"
                && item["features"] != serde_json::json!({"serial":true,"audio":false}))
        {
            return Err(Error::Unsafe("mixed RC helper configuration".into()));
        }
        let path = root.join(relative);
        if !std::fs::symlink_metadata(&path)?.is_file() || std::fs::canonicalize(&path)? != path {
            return Err(Error::Unsafe(
                "RC helper must be a bundled regular file".into(),
            ));
        }
        let mut file = std::fs::File::open(path)?;
        let mut hash = Sha256::new();
        let mut buffer = [0u8; 65_536];
        loop {
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            hash.update(&buffer[..count]);
        }
        if item["sha256"] != format!("{:x}", hash.finalize()) {
            return Err(Error::Unsafe("RC helper integrity failed".into()));
        }
    }
    Ok(format!("macos-rc1-{:x}", Sha256::digest(bytes)))
}

pub fn verify_backend_package(backend: &BackendSpec) -> Result<()> {
    // Other embedding configurations retain their existing policy. The physical
    // macos-agent always derives this identity from its signed bundle.
    if backend.name.starts_with("macos-rc1-") && backend.name != "macos-rc1-live" {
        let bundle = backend
            .program
            .parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .ok_or_else(|| Error::Unsafe("invalid bundled backend path".into()))?;
        if bundled_backend_id(bundle)? != backend.name {
            return Err(Error::Unsafe(
                "installed RC changed; stop service before updating".into(),
            ));
        }
    }
    Ok(())
}
