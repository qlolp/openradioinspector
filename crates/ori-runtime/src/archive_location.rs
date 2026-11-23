//! A private, offline change of the node's canonical measurement archive.
//! The legacy database name holds either the real SQLite archive or a private pointer.
//! Replacing that one file atomically also switches the authority after an interrupted move.
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
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
};

const DATABASE: &str = "sessions.sqlite3";
const SIDECARS: [&str; 3] = [
    "sessions.sqlite3-wal",
    "sessions.sqlite3-shm",
    "sessions.sqlite3-journal",
];
const SELECTION: &str = "archive-location.json";
const OWNER: &str = "archive-owner.json";
// SQLite rejects this header before modifying the file. The original core always opens
// this exact name; a missing file would let it create a second, empty archive.
const POINTER: &[u8] = b"ORI relocated archive v1\n";
const SELECTION_LIMIT: u64 = 8192;

#[derive(Serialize, Deserialize, PartialEq, Eq)]
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

fn sync_directory(_path: &Path) -> Result<()> {
    #[cfg(unix)]
    fs::File::open(_path)?.sync_all()?;
    Ok(())
}

fn pointer(database: &Path) -> Result<Option<Selection>> {
    validate_path(database)?;
    let meta = match fs::symlink_metadata(database) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        other => other?,
    };
    if !meta.is_file() {
        return Err(unsafe_state("archive must be a regular file"));
    }
    if meta.len() < POINTER.len() as u64 || meta.len() > SELECTION_LIMIT + POINTER.len() as u64 {
        return Ok(None);
    }
    let mut header = [0; POINTER.len()];
    fs::File::open(database)?.read_exact(&mut header)?;
    if header != POINTER {
        return Ok(None);
    }
    no_sidecars(database)?;
    let bytes = read_private(database, SELECTION_LIMIT + POINTER.len() as u64)?;
    Ok(Some(serde_json::from_slice(&bytes[POINTER.len()..])?))
}

fn put_pointer(node: &Path, selected: &Selection) -> Result<()> {
    let bytes = serde_json::to_vec(selected)?;
    if bytes.len() as u64 > SELECTION_LIMIT {
        return Err(unsafe_state("archive selection exceeds size bound"));
    }
    let mut stage = tempfile::NamedTempFile::new_in(node)?;
    stage.write_all(POINTER)?;
    stage.write_all(&bytes)?;
    stage.as_file().sync_all()?;
    stage
        .persist(node.join(DATABASE))
        .map_err(|err| err.error)?;
    sync_directory(node)
}

fn forget_marker(node: &Path) -> Result<()> {
    let state = state_directory(node)?;
    match fs::remove_file(state.join(SELECTION)) {
        Ok(()) => sync_directory(&state),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.into()),
    }
}

/// Call under the node writer lease. Also upgrade selections written by the first
/// relocation implementation, which retained a writable stale SQLite at the old name.
pub fn directory(node: &Path) -> Result<PathBuf> {
    let database = node.join(DATABASE);
    let marker = state_directory(node)?.join(SELECTION);
    let guard = pointer(&database)?;
    let selected: Selection = match fs::symlink_metadata(&marker) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => match guard {
            None => return Ok(node.to_owned()),
            Some(ref selected) => {
                validate_identity(node, selected)?;
                verify_volume(selected)?;
                return Ok(selected.directory.clone());
            }
        },
        other => {
            other?;
            serde_json::from_slice(&read_private(&marker, SELECTION_LIMIT)?)?
        }
    };
    if guard.as_ref().is_some_and(|value| value != &selected) {
        return Err(unsafe_state("archive location records disagree"));
    }
    validate_identity(node, &selected)?;
    let available = verify_volume(&selected);
    if guard.is_none() {
        // Legacy selections left a usable copy or a vacant filename at the node.
        // Seal it even when the selected disk is missing. Keep any divergent
        // records from an earlier downgrade in an independently verified bundle.
        if source_exists(&database)? {
            let backup_volume = if available.is_ok() {
                &selected.directory
            } else {
                node
            };
            retain_legacy_database(&database, backup_volume)?;
        }
        put_pointer(node, &selected)?;
    }
    available?;
    Ok(selected.directory)
}

fn retain_legacy_database(database: &Path, backup_volume: &Path) -> Result<()> {
    let state = state_directory(backup_volume)?;
    let stage = tempfile::tempdir_in(&state)?;
    let snapshot = stage.path().join("snapshot");
    let manifest =
        ori_archive::backup_stopped_archive(database, &snapshot).map_err(archive_error)?;
    ori_archive::checkpoint_stopped_archive(database).map_err(archive_error)?;
    let hash = manifest["archive"]["sha256"]
        .as_str()
        .ok_or_else(|| unsafe_state("missing verified archive digest"))?;
    let retained = state.join(format!("prior-node-{hash}"));
    validate_path(&retained)?;
    if retained.try_exists()? {
        if ori_archive::verify_backup(&retained).map_err(archive_error)? != manifest {
            return Err(unsafe_state(
                "retained node backup differs from the verified snapshot",
            ));
        }
    } else {
        fs::rename(snapshot, retained)?;
        sync_directory(&state)?;
    }
    Ok(())
}

fn validate_identity(node: &Path, selected: &Selection) -> Result<()> {
    if selected.directory == node
        || !selected.directory.is_absolute()
        || selected
            .directory
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err(unsafe_state("invalid archive directory"));
    }
    if selected.version != 1
        || selected.id.len() != 64
        || !selected.id.bytes().all(|c| c.is_ascii_hexdigit())
    {
        return Err(unsafe_state("invalid archive selection"));
    }
    Ok(())
}

fn verify_volume(selected: &Selection) -> Result<()> {
    validate_path(&selected.directory)?;
    check_private(&selected.directory, true)?;
    #[cfg(windows)]
    crate::windows_node::ensure_archive_directory(&selected.directory)?;
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
    Ok(())
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
            for name in SIDECARS {
                let path = database.with_file_name(name);
                validate_path(&path)?;
                match fs::symlink_metadata(path) {
                    Ok(info) => {
                        if !info.is_file() {
                            return Err(unsafe_state("unsafe SQLite sidecar"));
                        }
                        #[cfg(unix)]
                        {
                            use std::os::unix::fs::MetadataExt;
                            if info.nlink() != 1 || info.uid() != unsafe { libc::geteuid() } {
                                return Err(unsafe_state("unsafe SQLite sidecar ownership"));
                            }
                        }
                    }
                    Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                    Err(e) => return Err(e.into()),
                }
            }
            Ok(true)
        }
        Ok(_) => Err(unsafe_state("archive must be a regular file")),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            no_sidecars(database)?;
            Ok(false)
        }
        Err(e) => Err(e.into()),
    }
}

fn no_sidecars(database: &Path) -> Result<()> {
    for name in SIDECARS {
        match fs::symlink_metadata(database.with_file_name(name)) {
            Ok(_) => {
                return Err(unsafe_state(
                    "unexpected SQLite sidecars; recover the source first",
                ));
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
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
    let result =
        || json!({"database": DATABASE, "directory":destination, "storage_id":"node-archive"});
    if source != node {
        node_writer.reserve_storage(&source)?;
    }
    let current = source.join(DATABASE);
    let has_source = source_exists(&current)?;
    if !has_source && source != node {
        return Err(unsafe_state("selected archive is unavailable"));
    }
    if source == destination {
        return Ok(result());
    }
    if destination == node {
        // A supported rollback returns authority to the fixed name the old binary
        // opens. Its subsequent writes will also be found by the upgraded binary.
        let staging = verified_copy(&current, node, true).await?;
        forget_marker(node)?;
        // Until this atomic replacement, the pointer still selects the external
        // archive even if the process dies after removing the compatibility marker.
        fs::rename(staging.path().join(DATABASE), node.join(DATABASE))?;
        sync_directory(node)?;
        return Ok(result());
    }
    if destination.starts_with(state_directory(node)?) {
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
    let target_state = state_directory(destination)?;
    let target_marker = target_state.join(OWNER);
    for entry in fs::read_dir(&target_state)? {
        if entry?.file_name() != "writer.lock" {
            return Err(unsafe_state(
                "archive destination contains existing node state",
            ));
        }
    }
    let staging = verified_copy(&current, destination, has_source).await?;
    let id = format!(
        "{:x}",
        sha2::Sha256::digest(crate::security::random_secret().as_bytes())
    );
    let selected = Selection {
        version: 1,
        directory: destination.to_owned(),
        id: id.clone(),
    };
    if has_source && source == node {
        ori_archive::checkpoint_stopped_archive(&current).map_err(archive_error)?;
        // Keep the independently verified, restorable source on the chosen volume.
        fs::rename(
            staging.path().join("snapshot"),
            target_state.join("prior-node-backup"),
        )?;
        sync_directory(&target_state)?;
    }
    let staged_database = staging.path().join(DATABASE);
    fs::hard_link(&staged_database, destination.join(DATABASE))?;
    fs::remove_file(&staged_database)?;
    sync_directory(destination)?;
    atomic_private(
        &target_marker,
        &serde_json::to_vec(&Owner { version: 1, id })?,
    )?;
    // Earlier selection-aware builds use the private marker. Remove it while
    // the old pointer still resolves, then publish the new pointer and marker.
    forget_marker(node)?;
    put_pointer(node, &selected)?;
    atomic_private(
        &state_directory(node)?.join(SELECTION),
        &serde_json::to_vec(&selected)?,
    )?;
    Ok(result())
}

async fn verified_copy(
    current: &Path,
    destination: &Path,
    has_source: bool,
) -> Result<tempfile::TempDir> {
    if fs2::available_space(destination)? < required_space(current)? {
        return Err(unsafe_state(
            "insufficient space for an independently verified copy",
        ));
    }
    let staging = tempfile::Builder::new()
        .prefix(".ori-relocate-")
        .tempdir_in(destination)?;
    let target = staging.path().join(DATABASE);
    if has_source {
        let recovery = staging.path().join("snapshot");
        ori_archive::backup_stopped_archive(current, &recovery).map_err(archive_error)?;
        ori_archive::restore_backup(&recovery, &target).map_err(archive_error)?;
    } else {
        ori_archive::init_compact_archive(&target)
            .await
            .map_err(archive_error)?;
    }
    Ok(staging)
}

use sha2::Digest;
