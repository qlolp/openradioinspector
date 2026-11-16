//! Explicit, private host diagnostics. No HTTP control and no automatic hardware access.
#[cfg(test)]
#[path = "acceptance/budget_tests.rs"]
mod budget_tests;
mod wire;
use crate::{Core, CoreError, Result};
use ori_archive::{BackupOptions, CommitObserver};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

const MAX_TRACE: u64 = 256 * 1024 * 1024;
const MAX_SNAPSHOT: u64 = 16 * 1024_u64.pow(3);
const BACKUP_RATE: u64 = 20 * 1024 * 1024;

fn backup_budget(snapshot_bytes: u64) -> Result<BackupOptions> {
    if snapshot_bytes == 0 || snapshot_bytes > MAX_SNAPSHOT {
        return Err(CoreError::Invalid("acceptance snapshot size outside quota"));
    }
    // Six logical full-file passes: copy read/write, two integrity scans,
    // and two SHA256 scans. Include 50% headroom and bounded fixed overhead.
    let copy = (3 * snapshot_bytes).div_ceil(BACKUP_RATE) + 60;
    let total = (9 * snapshot_bytes).div_ceil(BACKUP_RATE) + 120;
    Ok(BackupOptions {
        max_snapshot_bytes: MAX_SNAPSHOT,
        copy_timeout: Duration::from_secs(copy.max(300)),
        total_timeout: Duration::from_secs(total.max(600)),
        cancel: ori_archive::BackupCancellation::default(),
    })
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Profile {
    version: u32,
    run_id: String,
    output_dir: PathBuf,
    data_dir: PathBuf,
    expected_core_sha256: String,
    #[serde(default = "default_phase")]
    phase: String,
}
fn default_phase() -> String {
    "load".into()
}
#[derive(Debug)]
pub struct Acceptance {
    run_id: String,
    phase: String,
    output: PathBuf,
    sha256: String,
    trace: Mutex<(File, u64)>,
    failed: AtomicBool,
    wire: Option<Arc<wire::WireRing>>,
}

pub fn executable_digest() -> Result<String> {
    let mut file = File::open(std::env::current_exe()?)?;
    let mut hash = Sha256::new();
    let mut block = [0u8; 65536];
    loop {
        let read = file.read(&mut block)?;
        if read == 0 {
            break;
        }
        hash.update(&block[..read]);
    }
    Ok(hash
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}
#[cfg(target_os = "macos")]
pub fn monotonic_ns() -> u64 {
    #[repr(C)]
    struct Base {
        numer: u32,
        denom: u32,
    }
    unsafe extern "C" {
        fn mach_absolute_time() -> u64;
        fn mach_timebase_info(info: *mut Base) -> i32;
    }
    static BASE: std::sync::OnceLock<(u32, u32)> = std::sync::OnceLock::new();
    let &(numer, denom) = BASE.get_or_init(|| {
        let mut value = Base { numer: 0, denom: 0 };
        // SAFETY: the OS fills this live, correctly sized POD timebase.
        if unsafe { mach_timebase_info(&mut value) } == 0 && value.denom != 0 {
            (value.numer, value.denom)
        } else {
            (0, 1)
        }
    });
    // SAFETY: mach_absolute_time has no arguments or ownership requirements.
    ((unsafe { mach_absolute_time() } as u128) * u128::from(numer) / u128::from(denom))
        .min(u128::from(u64::MAX)) as u64
}
#[cfg(all(unix, not(target_os = "macos")))]
pub fn monotonic_ns() -> u64 {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: clock_gettime receives a valid initialized timespec pointer.
    if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut value) } != 0 {
        return 0;
    }
    (value.tv_sec as u64)
        .saturating_mul(1_000_000_000)
        .saturating_add(value.tv_nsec as u64)
}
#[cfg(not(unix))]
pub fn monotonic_ns() -> u64 {
    0
}

#[cfg(unix)]
fn private(path: &Path, directory: bool) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    let value = fs::symlink_metadata(path)?;
    // SAFETY: geteuid has no arguments and returns the process identity.
    if value.file_type().is_symlink()
        || value.is_dir() != directory
        || (!directory && !value.is_file())
        || value.uid() != unsafe { libc::geteuid() }
        || value.mode() & 0o077 != 0
    {
        return Err(CoreError::Invalid(
            "acceptance paths must be private, owned and not linked",
        ));
    }
    Ok(())
}
#[cfg(not(unix))]
fn private(_path: &Path, _directory: bool) -> Result<()> {
    Err(CoreError::Invalid("Mac acceptance requires a Unix host"))
}

impl Acceptance {
    pub fn open(path: &Path, data: &Path) -> Result<Self> {
        private(path, false)?;
        if fs::metadata(path)?.len() > 65536 {
            return Err(CoreError::Invalid("acceptance profile exceeds 64 KiB"));
        }
        let value = ori_archive::parse_json_value(&fs::read(path)?, 65536)?;
        let profile: Profile = serde_json::from_value(value)
            .map_err(|_| CoreError::Invalid("invalid acceptance profile fields"))?;
        if !matches!(profile.phase.as_str(), "load" | "hardware")
            || profile.version != 1
            || profile.run_id.is_empty()
            || profile.run_id.len() > 64
            || !profile
                .run_id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
            || !profile.output_dir.is_absolute()
            || !profile.data_dir.is_absolute()
            || profile.data_dir.canonicalize()? != data.canonicalize()?
        {
            return Err(CoreError::Invalid("invalid acceptance run/profile scope"));
        }
        private(&profile.output_dir, true)?;
        private(&profile.data_dir, true)?;
        let sha256 = executable_digest()?;
        if profile.expected_core_sha256 != sha256 {
            return Err(CoreError::Invalid("acceptance executable hash mismatch"));
        }
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let trace_path = profile.output_dir.join(if profile.phase == "hardware" {
            format!("core-{}.jsonl", std::process::id())
        } else {
            "core.jsonl".into()
        });
        let wire = if profile.phase == "hardware" {
            Some(Arc::new(wire::WireRing::new(
                &profile.output_dir,
                32 * 1024 * 1024,
            )?))
        } else {
            None
        };
        let value = Self {
            run_id: profile.run_id,
            phase: profile.phase,
            output: profile.output_dir,
            sha256,
            trace: Mutex::new((options.open(trace_path)?, 0)),
            failed: AtomicBool::new(false),
            wire,
        };
        value.record(json!({"stage":"core_identity","mode":"explicit_acceptance","clock":if cfg!(target_os="macos"){"mach_absolute_time"}else{"clock_monotonic"}}));
        Ok(value)
    }
    pub fn hardware(&self) -> bool {
        self.phase == "hardware"
    }
    pub fn failed(&self) -> bool {
        self.failed.load(Ordering::Acquire) || self.wire.as_ref().is_some_and(|wire| wire.failed())
    }
    pub fn transport(
        &self,
        inner: Arc<dyn crate::devices::TransportFactory>,
    ) -> Arc<dyn crate::devices::TransportFactory> {
        match &self.wire {
            Some(ring) => Arc::new(wire::LoggedFactory {
                inner,
                ring: ring.clone(),
            }),
            None => inner,
        }
    }
    pub fn record(&self, mut row: Value) {
        row["v"] = json!(1);
        row["run_id"] = json!(self.run_id);
        row["component_sha256"] = json!(self.sha256);
        if row.get("at_ns").is_none() {
            row["at_ns"] = json!(monotonic_ns());
        }
        let result = (|| -> std::io::Result<()> {
            let mut bytes = serde_json::to_vec(&row)?;
            bytes.push(b'\n');
            if bytes.len() > 65536 {
                return Err(std::io::Error::other("acceptance line exceeds bound"));
            }
            let mut state = self
                .trace
                .lock()
                .map_err(|_| std::io::Error::other("acceptance trace lock"))?;
            if state.1 + bytes.len() as u64 > MAX_TRACE {
                return Err(std::io::Error::other("acceptance trace quota"));
            }
            state.0.write_all(&bytes)?;
            state.1 += bytes.len() as u64;
            Ok(())
        })();
        if result.is_err() {
            self.failed.store(true, Ordering::Release);
        }
    }
    pub fn receipt(&self, name: &str, value: &Value) -> Result<()> {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let stage = self.output.join(format!(".{name}.pending"));
        let target = self.output.join(name);
        if target.exists() {
            return Err(CoreError::Invalid("acceptance receipt already exists"));
        }
        let mut file = options.open(&stage)?;
        file.write_all(
            &serde_json::to_vec(value)
                .map_err(|_| CoreError::Invalid("invalid acceptance receipt"))?,
        )?;
        file.sync_all()?;
        fs::rename(stage, target)?;
        Ok(())
    }
    pub async fn supervise(self: Arc<Self>, core: Arc<Core>) -> Result<()> {
        let mut shutdown = core.shutdown.subscribe();
        let mut requested = [false, false];
        let mut frozen = false;
        loop {
            if *shutdown.borrow() {
                break;
            }
            if !frozen && self.output.join("wire-freeze.request").try_exists()? {
                let request = self.output.join("wire-freeze.request");
                private(&request, false)?;
                if fs::metadata(&request)?.len() > 128
                    || fs::read_to_string(&request)?.trim() != self.run_id
                {
                    return Err(CoreError::Invalid("wire freeze run mismatch"));
                }
                if let Some(wire) = &self.wire {
                    wire.freeze();
                }
                frozen = true;
                self.receipt(
                    "wire-frozen.json",
                    &json!({"at_ns":monotonic_ns(),"failed":self.failed()}),
                )?;
            }
            for (index, name) in ["backup", "final-backup"].iter().enumerate() {
                let request = self.output.join(format!("{name}.request"));
                if requested[index] || !request.try_exists()? {
                    continue;
                }
                requested[index] = true;
                private(&request, false)?;
                if fs::metadata(&request)?.len() > 128
                    || fs::read_to_string(&request)?.trim() != self.run_id
                {
                    return Err(CoreError::Invalid("acceptance backup request run mismatch"));
                }
                let capacity = core
                    .archive
                    .as_ref()
                    .ok_or(CoreError::Invalid("acceptance backup requires archive"))?
                    .capacity()
                    .await?;
                let size = capacity["database_bytes"]
                    .as_u64()
                    .ok_or(CoreError::Invalid("acceptance archive size unavailable"))?;
                let options = backup_budget(size)?;
                let budget = json!({"snapshot_bytes":size,"rate_bytes_per_second":BACKUP_RATE,
                    "copy_timeout_seconds":options.copy_timeout.as_secs(),
                    "total_timeout_seconds":options.total_timeout.as_secs()});
                self.receipt(
                    &format!("{name}-started.json"),
                    &json!({"phase":"running","at_ns":monotonic_ns(),"budget":budget}),
                )?;
                let result = core.backup(&self.output.join(name), options).await;
                self.receipt(&format!("{name}-finished.json"),&match &result {
                    Ok(manifest)=>json!({"phase":"completed","manifest":manifest,"at_ns":monotonic_ns(),"budget":budget}),
                    Err(error)=>json!({"phase":"failed","error":error.to_string(),"at_ns":monotonic_ns(),"budget":budget})})?;
                if result.is_err() {
                    self.failed.store(true, Ordering::Release);
                }
            }
            tokio::select! {_=shutdown.changed()=>{},_=tokio::time::sleep(Duration::from_millis(50))=>{}}
        }
        if let Some(wire) = &self.wire {
            wire.freeze();
        }
        self.record(json!({"stage":"core.finished","successful":!self.failed()}));
        let state = self.trace.lock().map_err(|_| CoreError::Task)?;
        state.0.sync_all()?;
        if self.failed() {
            Err(CoreError::Invalid("acceptance diagnostics incomplete"))
        } else {
            Ok(())
        }
    }
}
impl CommitObserver for Acceptance {
    fn committed(&self, _items: &[Value], events: &[Value]) {
        let at_ns = monotonic_ns();
        for event in events {
            let (kind, key) = match event["event_type"].as_str() {
                Some("sweep.received") => ("sweep", "sweep"),
                Some("observation.received") => ("observation", "observation"),
                _ => continue,
            };
            self.record(json!({"stage":"committed","kind":kind,"at_ns":at_ns,
                "sequence":event["sequence"],"source_id":event["payload"][key]["id"],"session_id":event["session_id"]}));
        }
    }
}

/// Recovery helper used by the engineering kit, never by HTTP or an armed session.
pub fn restore(bundle: &Path, destination: &Path, report: &Path) -> Result<()> {
    private(bundle, true)?;
    private(
        destination
            .parent()
            .ok_or(CoreError::Invalid("restore parent"))?,
        true,
    )?;
    private(
        report
            .parent()
            .ok_or(CoreError::Invalid("restore report parent"))?,
        true,
    )?;
    if destination.exists() || report.exists() {
        return Err(CoreError::Invalid("restore requires new destinations"));
    }
    let manifest = ori_archive::verify_backup(bundle)?;
    let restored = ori_archive::restore_backup(bundle, destination)?;
    if manifest != restored {
        return Err(CoreError::Invalid("restore manifest changed"));
    }
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(report)?;
    file.write_all(
        &serde_json::to_vec(&json!({"phase":"completed","manifest":restored}))
            .map_err(|_| CoreError::Invalid("restore receipt encoding"))?,
    )?;
    file.sync_all()?;
    Ok(())
}
