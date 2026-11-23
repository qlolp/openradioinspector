//! The local desktop IPC accepts no paths. Directory selection and verification
//! stay in the native process and its installed supervisor.
use std::{
    ffi::OsStr,
    path::{Path, PathBuf},
};

pub(super) fn check_connection(
    origin: Option<&str>,
    opened_at: u64,
    current: u64,
) -> Result<(), String> {
    if opened_at != current || origin.is_some_and(|origin| origin != "http://127.0.0.1:8770/") {
        return Err("Archive folders can only be chosen for the local Windows node.".into());
    }
    Ok(())
}

fn local_directory(path: &Path) -> Result<(), String> {
    if !path.is_absolute() || path.to_str().is_none() {
        return Err("Choose a folder on a local drive.".into());
    }
    #[cfg(windows)]
    if !matches!(path.components().next(), Some(std::path::Component::Prefix(prefix))
        if matches!(prefix.kind(), std::path::Prefix::Disk(_) | std::path::Prefix::VerbatimDisk(_)))
    {
        return Err("Choose a folder on a local drive.".into());
    }
    Ok(())
}

pub(super) fn destination(parent: &Path) -> Result<PathBuf, String> {
    local_directory(parent)?;
    Ok(parent.join("OpenRadioInspector Archive"))
}

pub(super) fn archive_target(output: &[u8]) -> Result<PathBuf, String> {
    let invalid =
        || "Windows supervisor did not confirm the stopped local archive target.".to_owned();
    let reply: serde_json::Value = serde_json::from_slice(output).map_err(|_| invalid())?;
    if reply["ok"] != true {
        return Err(invalid());
    }
    let node = PathBuf::from(reply["data_directory"].as_str().ok_or_else(invalid)?);
    local_directory(&node)?;
    Ok(node)
}

pub(super) fn relocation_arguments<'a>(node: &'a Path, selected: &'a Path) -> [&'a OsStr; 5] {
    [
        OsStr::new("archive-location"),
        OsStr::new("--data-dir"),
        node.as_os_str(),
        OsStr::new("--directory"),
        selected.as_os_str(),
    ]
}

pub(super) fn confirm(output: &[u8], selected: &Path) -> Result<String, String> {
    let invalid =
        || "Windows supervisor did not confirm the selected archive directory.".to_owned();
    let reply: serde_json::Value = serde_json::from_slice(output).map_err(|_| invalid())?;
    let path = selected.to_str().ok_or_else(invalid)?;
    if reply["database"] != "sessions.sqlite3"
        || reply["storage_id"] != "node-archive"
        || reply["directory"] != path
    {
        return Err(invalid());
    }
    Ok(path.to_owned())
}

#[cfg(windows)]
pub(super) fn choose(
    owner: windows::Win32::Foundation::HWND,
) -> windows::core::Result<Option<PathBuf>> {
    use std::{ffi::OsString, os::windows::ffi::OsStringExt};
    use windows::{
        Win32::{
            Foundation::{E_POINTER, ERROR_CANCELLED},
            System::Com::{
                CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, CoCreateInstance, CoInitializeEx,
                CoTaskMemFree, CoUninitialize,
            },
            UI::Shell::{
                FOS_FORCEFILESYSTEM, FOS_PATHMUSTEXIST, FOS_PICKFOLDERS, FileOpenDialog,
                IFileOpenDialog, SIGDN_FILESYSPATH,
            },
        },
        core::HRESULT,
    };

    // Called on the owning window's UI thread. Balance both S_OK and S_FALSE.
    unsafe {
        CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok()?;
    }
    struct Apartment;
    impl Drop for Apartment {
        fn drop(&mut self) {
            unsafe { CoUninitialize() }
        }
    }
    let _apartment = Apartment;
    let dialog: IFileOpenDialog =
        unsafe { CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER)? };
    unsafe {
        dialog.SetOptions(
            dialog.GetOptions()? | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_PICKFOLDERS,
        )?;
        if let Err(error) = dialog.Show(Some(owner)) {
            if error.code() == HRESULT::from_win32(ERROR_CANCELLED.0) {
                return Ok(None);
            }
            return Err(error);
        }
        let result = dialog.GetResult()?;
        let name = result.GetDisplayName(SIGDN_FILESYSPATH)?;
        if name.is_null() {
            return Err(E_POINTER.into());
        }
        let path = OsString::from_wide(name.as_wide());
        CoTaskMemFree(Some(name.as_ptr().cast()));
        Ok(Some(PathBuf::from(path)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::Path;

    #[test]
    fn selection_requires_the_same_installed_local_connection_until_dispatch() {
        for origin in [None, Some("http://127.0.0.1:8770/")] {
            assert!(check_connection(origin, 8, 8).is_ok());
            assert!(check_connection(origin, 8, 9).is_err());
        }
        for origin in [
            "https://radio.local:8770/",
            "http://127.0.0.1:9999/",
            "https://127.0.0.1:8770/",
        ] {
            assert!(check_connection(Some(origin), 8, 8).is_err());
        }
    }

    #[cfg(windows)]
    #[test]
    fn rejects_network_shares_before_supervisor_dispatch() {
        assert!(destination(Path::new(r"\\remote-node\archive")).is_err());
        assert!(destination(Path::new(r"\\?\UNC\remote-node\archive")).is_err());
    }

    #[test]
    fn verified_folder_choice_passes_separate_path_arguments_and_checks_the_supervisor_receipt() {
        let root = std::env::temp_dir();
        let parent = root.join("Диск A & B");
        let chosen = destination(&parent).unwrap();
        assert_eq!(chosen, parent.join("OpenRadioInspector Archive"));
        assert!(destination(Path::new("relative")).is_err());
        let node = root.join("installed node");
        let stopped = json!({"ok":true,"data_directory":node}).to_string();
        assert_eq!(archive_target(stopped.as_bytes()).unwrap(), node);
        assert!(archive_target(br#"{"ok":true,"data_directory":"relative"}"#).is_err());
        assert!(archive_target(br#"{"ok":false}"#).is_err());
        let args = relocation_arguments(&node, &chosen);
        assert_eq!(
            args,
            [
                "archive-location".as_ref(),
                "--data-dir".as_ref(),
                node.as_os_str(),
                "--directory".as_ref(),
                chosen.as_os_str()
            ]
        );
        let mut receipt =
            json!({"directory":chosen,"database":"sessions.sqlite3","storage_id":"node-archive"});
        assert_eq!(
            confirm(receipt.to_string().as_bytes(), &chosen).unwrap(),
            chosen.to_str().unwrap()
        );
        receipt["directory"] = json!(parent);
        assert!(confirm(receipt.to_string().as_bytes(), &chosen).is_err());
        receipt["directory"] = json!(chosen);
        receipt["storage_id"] = json!("other");
        assert!(confirm(receipt.to_string().as_bytes(), &chosen).is_err());
    }
}
