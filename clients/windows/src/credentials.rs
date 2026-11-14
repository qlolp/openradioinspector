//! Per-user Windows Credential Manager. No token is written to application files/preferences.
use std::{ptr, slice};
use windows_sys::Win32::{
    Foundation::{ERROR_NOT_FOUND, GetLastError},
    Security::Credentials::{
        CRED_MAX_CREDENTIAL_BLOB_SIZE, CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC, CREDENTIALW,
        CredDeleteW, CredFree, CredReadW, CredWriteW,
    },
};
fn target(origin: &str) -> Result<Vec<u16>, String> {
    let bridge = crate::Bridge::new(origin, "")?;
    if bridge.credential_origin() != origin {
        return Err("Invalid credential origin".into());
    }
    Ok(format!("OpenRadioInspector/core/{origin}")
        .encode_utf16()
        .chain(Some(0))
        .collect())
}
struct Allocation(*mut CREDENTIALW);
impl Drop for Allocation {
    fn drop(&mut self) {
        unsafe {
            CredFree(self.0.cast());
        }
    }
}
pub fn load(origin: &str) -> Result<Option<String>, String> {
    let name = target(origin)?;
    let mut record = ptr::null_mut();
    // CredReadW allocates this buffer; Allocation releases it after bounded copying.
    if unsafe { CredReadW(name.as_ptr(), CRED_TYPE_GENERIC, 0, &mut record) } == 0 {
        return if unsafe { GetLastError() } == ERROR_NOT_FOUND {
            Ok(None)
        } else {
            Err("Windows Credential Manager could not read this node credential".into())
        };
    }
    if record.is_null() {
        return Err("Windows Credential Manager returned no record".into());
    }
    let allocation = Allocation(record);
    let value = unsafe { &*allocation.0 };
    if value.CredentialBlobSize == 0
        || value.CredentialBlobSize > CRED_MAX_CREDENTIAL_BLOB_SIZE
        || value.CredentialBlob.is_null()
    {
        return Err("Stored node credential is invalid".into());
    }
    let bytes =
        unsafe { slice::from_raw_parts(value.CredentialBlob, value.CredentialBlobSize as usize) };
    String::from_utf8(bytes.to_vec())
        .map(Some)
        .map_err(|_| "Stored node credential is invalid".into())
}
pub fn save(origin: &str, token: &str) -> Result<(), String> {
    if token.is_empty() || token.len() > CRED_MAX_CREDENTIAL_BLOB_SIZE as usize {
        return Err("Credential exceeds Windows secure-storage limit".into());
    }
    let mut name = target(origin)?;
    let mut username = "OpenRadioInspector"
        .encode_utf16()
        .chain(Some(0))
        .collect::<Vec<_>>();
    let record = CREDENTIALW {
        Type: CRED_TYPE_GENERIC,
        TargetName: name.as_mut_ptr(),
        CredentialBlobSize: token.len() as u32,
        CredentialBlob: token.as_ptr().cast_mut(),
        Persist: CRED_PERSIST_LOCAL_MACHINE,
        UserName: username.as_mut_ptr(),
        ..Default::default()
    };
    // Windows copies both UTF-16 fields and the blob before returning. All buffers live through the call.
    if unsafe { CredWriteW(&record, 0) } == 0 {
        return Err(
            "Windows Credential Manager could not securely store this node credential".into(),
        );
    }
    Ok(())
}
pub fn remove(origin: &str) -> Result<(), String> {
    let name = target(origin)?;
    if unsafe { CredDeleteW(name.as_ptr(), CRED_TYPE_GENERIC, 0) } == 0
        && unsafe { GetLastError() } != ERROR_NOT_FOUND
    {
        return Err("Windows Credential Manager could not remove this node credential".into());
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn windows_credential_manager_roundtrips_and_removes_an_owned_synthetic_credential() {
        let origin = format!("https://synthetic-ori-{}.invalid:8770", std::process::id());
        struct Cleanup(String);
        impl Drop for Cleanup {
            fn drop(&mut self) {
                let _ = remove(&self.0);
            }
        }
        let _cleanup = Cleanup(origin.clone());
        save(&origin, "synthetic-test-token-only").unwrap();
        assert_eq!(
            load(&origin).unwrap().as_deref(),
            Some("synthetic-test-token-only")
        );
        remove(&origin).unwrap();
        assert!(load(&origin).unwrap().is_none());
    }
}
