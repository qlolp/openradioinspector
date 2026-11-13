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
                "--archive-max-bytes".into(),
                maximum.to_string(),
            ],
            environment: BTreeMap::new(),
            control_addr: address,
            ports: vec![address],
            startup_timeout_ms: 30_000,
            shutdown_timeout_ms: 120_000,
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
    let config = configuration(bundle, &data, 64 * 1024 * 1024 * 1024)?;
    crate::service::run_personal_rc(config).await
}

#[cfg(not(target_os = "macos"))]
pub async fn run() -> Result<()> {
    Err(Error::Unsafe("SMAppService RC agent requires macOS".into()))
}
