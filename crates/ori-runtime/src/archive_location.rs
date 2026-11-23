//! A private, offline change of the node's canonical measurement archive.
//! Publish the selection last, after a verified SQLite snapshot has been restored.
use crate::{
    Error, Result,
    lease::{
        InstanceLease, WriterLease, atomic_private, check_private, read_private, state_directory,
        validate_path,
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs, io,
    path::{Path, PathBuf},
};

const DATABASE: &str = "sessions.sqlite3";
const SELECTION: &str = "archive-location.json";
const OWNER: &str = "archive-owner.json";

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Selection {
    version: u8,
    directory: PathBuf,
    id: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Owner {
    version: u8,
    id: String,
}

fn unsafe_state(message: &str) -> Error {
    Error::Unsafe(message.into())
}

/// Call under the node writer lease when deciding which SQLite file to open.
/// A missing mount, changed marker or missing database can never create a fresh archive.
pub fn directory(node: &Path) -> Result<PathBuf> {
    let marker = state_directory(node)?.join(SELECTION);
    match fs::symlink_metadata(&marker) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(node.to_owned()),
        other => {
            other?;
        }
    }
    let selected: Selection = serde_json::from_slice(&read_private(&marker, 8192)?)?;
    validate_path(&selected.directory)?;
    if selected.version != 1
        || selected.id.len() != 64
        || !selected.id.bytes().all(|c| c.is_ascii_hexdigit())
    {
        return Err(unsafe_state("invalid archive selection"));
    }
    validate_local_volume(&selected.directory)?;
    let owner: Owner = serde_json::from_slice(&read_private(
        &selected.directory.join(".ori-runtime").join(OWNER),
        1024,
    )?)?;
    if owner.version != 1 || owner.id != selected.id {
        return Err(unsafe_state(
            "selected archive volume does not match the registered archive",
        ));
    }
    let database = selected.directory.join(DATABASE);
    validate_path(&database)?;
    if !source_exists(&database)? {
        return Err(unsafe_state("selected archive is unavailable"));
    }
    Ok(selected.directory)
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn validate_local_volume(path: &Path) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let path = std::ffi::CString::new(path.as_os_str().as_bytes())
        .map_err(|_| unsafe_state("invalid volume path"))?;
    let mut info = std::mem::MaybeUninit::<libc::statfs>::uninit();
    if unsafe { libc::statfs(path.as_ptr(), info.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    let info = unsafe { info.assume_init() };
    #[cfg(target_os = "macos")]
    let local = info.f_flags & libc::MNT_LOCAL as u32 != 0;
    // Ext, XFS, Btrfs, F2FS, ZFS, tmpfs and overlay (isolated/test nodes).
    #[cfg(target_os = "linux")]
    let local = matches!(
        info.f_type as u64,
        0xef53 | 0x58465342 | 0x9123683e | 0xf2f52010 | 0x2fc12fc1 | 0x01021994 | 0x794c7630
    );
    if !local {
        return Err(unsafe_state(
            "choose a supported local filesystem for SQLite",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn validate_local_volume(path: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{GetDriveTypeW, GetVolumePathNameW};
    let name: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    let mut volume = vec![0; 32768];
    if unsafe { GetVolumePathNameW(name.as_ptr(), volume.as_mut_ptr(), volume.len() as u32) } == 0 {
        return Err(io::Error::last_os_error().into());
    }
    // DRIVE_REMOVABLE and DRIVE_FIXED.
    if !matches!(unsafe { GetDriveTypeW(volume.as_ptr()) }, 2 | 3) {
        return Err(unsafe_state("a local archive volume is required"));
    }
    Ok(())
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn validate_local_volume(_: &Path) -> Result<()> {
    Err(unsafe_state("unsupported archive volume"))
}

fn prepare_destination(path: &Path) -> Result<()> {
    validate_path(path)?;
    let parent = path
        .parent()
        .ok_or_else(|| unsafe_state("select a new archive folder"))?;
    validate_local_volume(parent)?;
    #[cfg(windows)]
    crate::windows_node::ensure_archive_directory(path)?;
    #[cfg(not(windows))]
    crate::lease::private_directory(path)?;
    check_private(path, true)?;
    validate_local_volume(path)
}

fn archive_error(error: ori_archive::ArchiveError) -> Error {
    Error::Unsafe(format!("archive verification failed: {error}"))
}

fn source_exists(database: &Path) -> Result<bool> {
    validate_path(database)?;
    match fs::symlink_metadata(database) {
        Ok(info) if info.is_file() => {
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                if info.nlink() != 1 || info.uid() != unsafe { libc::geteuid() } {
                    return Err(unsafe_state(
                        "archive must be owned by this node and have one link",
                    ));
                }
            }
            Ok(true)
        }
        Ok(_) => Err(unsafe_state("archive must be a regular file")),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            for sidecar in [
                "sessions.sqlite3-wal",
                "sessions.sqlite3-shm",
                "sessions.sqlite3-journal",
            ] {
                match fs::symlink_metadata(database.with_file_name(sidecar)) {
                    Ok(_) => {
                        return Err(unsafe_state(
                            "SQLite sidecars remain without their archive; recover the source first",
                        ));
                    }
                    Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                    Err(e) => return Err(e.into()),
                }
            }
            Ok(false)
        }
        Err(e) => Err(e.into()),
    }
}

fn required_space(database: &Path) -> Result<u64> {
    let mut bytes = 0_u64;
    for path in [
        database.to_owned(),
        database.with_file_name("sessions.sqlite3-wal"),
    ] {
        match fs::metadata(path) {
            Ok(info) => bytes = bytes.saturating_add(info.len()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    // Temporary snapshot, verified restore, manifest and SQLite working space.
    Ok(bytes
        .saturating_mul(2)
        .saturating_add((bytes / 4).max(8 * 1024 * 1024)))
}

pub async fn select(node: &Path, destination: &Path) -> Result<Value> {
    let _supervisor = InstanceLease::acquire(node)?;
    let mut node_writer = WriterLease::acquire(node)?;
    let source = directory(node)?;
    let current = source.join(DATABASE);
    let has_source = source_exists(&current)?;
    let result =
        || json!({"database": DATABASE, "directory":destination, "storage_id":"node-archive"});
    if destination == source && has_source {
        return Ok(result());
    }
    if source != node {
        node_writer.reserve_storage(&source)?;
    }
    if destination == node || destination.starts_with(state_directory(node)?) {
        return Err(unsafe_state(
            "select an empty folder outside the node's private state",
        ));
    }
    prepare_destination(destination)?;
    let _target_writer = WriterLease::acquire(destination)?;
    for entry in fs::read_dir(destination)? {
        if entry?.file_name() != ".ori-runtime" {
            return Err(unsafe_state(
                "destination must be an empty private archive directory",
            ));
        }
    }
    if fs2::available_space(destination)? < required_space(&current)? {
        return Err(unsafe_state(
            "insufficient space for an independently verified copy",
        ));
    }
    let staging = tempfile::Builder::new()
        .prefix(".ori-relocate-")
        .tempdir_in(destination)?;
    let staged_database = staging.path().join(DATABASE);
    if has_source {
        let recovery = staging.path().join("snapshot");
        ori_archive::backup_stopped_archive(&current, &recovery).map_err(archive_error)?;
        ori_archive::restore_backup(&recovery, &staged_database).map_err(archive_error)?;
    } else {
        ori_archive::init_compact_archive(&staged_database)
            .await
            .map_err(archive_error)?;
    }
    let id = format!(
        "{:x}",
        sha2::Sha256::digest(crate::security::random_secret().as_bytes())
    );
    let selected = Selection {
        version: 1,
        directory: destination.to_owned(),
        id: id.clone(),
    };
    let target_marker = state_directory(destination)?.join(OWNER);
    if target_marker.try_exists()? {
        return Err(unsafe_state("archive directory is already registered"));
    }
    fs::hard_link(&staged_database, destination.join(DATABASE))?;
    // Release the staging link before the selection can become visible to the next core.
    fs::remove_file(&staged_database)?;
    #[cfg(unix)]
    fs::File::open(destination)?.sync_all()?;
    atomic_private(
        &target_marker,
        &serde_json::to_vec(&Owner { version: 1, id })?,
    )?;
    atomic_private(
        &state_directory(node)?.join(SELECTION),
        &serde_json::to_vec(&selected)?,
    )?;
    Ok(result())
}

use sha2::Digest;
