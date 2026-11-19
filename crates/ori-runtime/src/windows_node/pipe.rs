//! The single server instance reserves its name for the supervisor's entire
//! lifetime. Both peers verify the live kernel object's owner and DACL.
use crate::{Error, Result, service::AdminCommand};
use serde::{Serialize, de::DeserializeOwned};
use sha2::{Digest, Sha256};
use std::{
    ffi::c_void,
    io,
    os::windows::{
        ffi::OsStrExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
    },
    path::Path,
    ptr::{addr_of_mut, null_mut},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::windows::named_pipe::{ClientOptions, NamedPipeServer, ServerOptions},
    time::{Duration, Instant, sleep, timeout},
};
use windows_sys::Win32::{
    Foundation::{ERROR_PIPE_BUSY, ERROR_SUCCESS, GENERIC_ALL, HANDLE, LocalFree},
    Security::{Authorization::*, *},
    Storage::FileSystem::{
        CreateDirectoryW, FILE_ALL_ACCESS, FILE_FLAG_BACKUP_SEMANTICS, READ_CONTROL,
    },
    System::{
        SystemServices::ACCESS_ALLOWED_ACE_TYPE,
        Threading::{GetCurrentProcess, OpenProcessToken},
    },
};

struct LocalMemory(*mut c_void);
impl Drop for LocalMemory {
    fn drop(&mut self) {
        unsafe { LocalFree(self.0) };
    }
}
fn check(value: i32) -> io::Result<()> {
    if value == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

struct Identity {
    // TOKEN_USER and its pointer to the contained SID live in this aligned buffer.
    user: Vec<usize>,
    text: String,
}
impl Identity {
    fn current() -> Result<Self> {
        let mut token = null_mut();
        check(unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) })?;
        let token = unsafe { OwnedHandle::from_raw_handle(token) };
        let mut length = 0;
        unsafe {
            GetTokenInformation(token.as_raw_handle(), TokenUser, null_mut(), 0, &mut length)
        };
        if length == 0 {
            return Err(io::Error::last_os_error().into());
        }
        let mut user = vec![0usize; (length as usize).div_ceil(size_of::<usize>())];
        check(unsafe {
            GetTokenInformation(
                token.as_raw_handle(),
                TokenUser,
                user.as_mut_ptr().cast(),
                length,
                &mut length,
            )
        })?;
        let sid = unsafe { (*user.as_ptr().cast::<TOKEN_USER>()).User.Sid };
        let mut string = null_mut();
        check(unsafe { ConvertSidToStringSidW(sid, &mut string) })?;
        let _string = LocalMemory(string.cast());
        let length = (0..256)
            .find(|i| unsafe { *string.add(*i) == 0 })
            .ok_or_else(|| Error::Unsafe("invalid Windows user SID".into()))?;
        let text = String::from_utf16(unsafe { std::slice::from_raw_parts(string, length) })
            .map_err(|_| Error::Unsafe("invalid Windows user SID".into()))?;
        Ok(Self { user, text })
    }
    fn sid(&self) -> PSID {
        unsafe { (*self.user.as_ptr().cast::<TOKEN_USER>()).User.Sid }
    }
    fn name(&self, data: &Path) -> Result<String> {
        let mut digest = Sha256::new();
        digest.update(self.text.as_bytes());
        for unit in std::fs::canonicalize(data)?.as_os_str().encode_wide() {
            digest.update(unit.to_le_bytes());
        }
        Ok(format!(
            r"\\.\pipe\OpenRadioInspector-{:x}",
            digest.finalize()
        ))
    }
    fn verify(&self, handle: HANDLE, directory: bool) -> Result<()> {
        let (mut owner, mut acl, mut descriptor) = (null_mut(), null_mut(), null_mut());
        let status = unsafe {
            GetSecurityInfo(
                handle,
                if directory {
                    SE_FILE_OBJECT
                } else {
                    SE_KERNEL_OBJECT
                },
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                null_mut(),
                &mut acl,
                null_mut(),
                &mut descriptor,
            )
        };
        if status != ERROR_SUCCESS {
            return Err(io::Error::from_raw_os_error(status as i32).into());
        }
        let _descriptor = LocalMemory(descriptor);
        let mut control = 0;
        let mut revision = 0;
        check(unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) })?;
        if owner.is_null()
            || acl.is_null()
            || control & SE_DACL_PROTECTED == 0
            || unsafe { EqualSid(owner, self.sid()) == 0 || (*acl).AceCount != 1 }
        {
            return Err(Error::Unauthorized);
        }
        let mut entry = null_mut();
        check(unsafe { GetAce(acl, 0, &mut entry) })?;
        // The OS returns a valid ACL. Check the type before accessing its SID.
        let header = unsafe { &*entry.cast::<ACE_HEADER>() };
        if u32::from(header.AceType) != ACCESS_ALLOWED_ACE_TYPE
            || header.AceFlags
                != if directory {
                    (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE) as u8
                } else {
                    0
                }
        {
            return Err(Error::Unauthorized);
        }
        let allowed = entry.cast::<ACCESS_ALLOWED_ACE>();
        if !matches!(unsafe { (*allowed).Mask }, GENERIC_ALL | FILE_ALL_ACCESS)
            || unsafe { EqualSid(addr_of_mut!((*allowed).SidStart).cast(), self.sid()) == 0 }
        {
            return Err(Error::Unauthorized);
        }
        Ok(())
    }
}

fn descriptor(sddl: &str) -> Result<LocalMemory> {
    let text: Vec<_> = sddl.encode_utf16().chain([0]).collect();
    let mut descriptor = null_mut();
    check(unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            text.as_ptr(),
            SDDL_REVISION_1,
            &mut descriptor,
            null_mut(),
        )
    })?;
    Ok(LocalMemory(descriptor))
}
fn attributes(descriptor: &LocalMemory) -> SECURITY_ATTRIBUTES {
    SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.0,
        bInheritHandle: 0,
    }
}
fn create(name: &str, sddl: &str) -> Result<NamedPipeServer> {
    let descriptor = descriptor(sddl)?;
    let mut attrs = attributes(&descriptor);
    Ok(unsafe {
        ServerOptions::new()
            .first_pipe_instance(true)
            .reject_remote_clients(true)
            .max_instances(1)
            .create_with_security_attributes_raw(
                name,
                (&mut attrs as *mut SECURITY_ATTRIBUTES).cast(),
            )
    }?)
}
pub(super) fn path(data: &Path) -> Result<std::path::PathBuf> {
    Ok(Identity::current()?.name(data)?.into())
}
pub(super) fn listen(data: &Path) -> Result<NamedPipeServer> {
    let user = Identity::current()?;
    let pipe = create(
        &user.name(data)?,
        &format!("O:{0}D:P(A;;GA;;;{0})", user.text),
    )?;
    user.verify(pipe.as_raw_handle(), false)?;
    Ok(pipe)
}

pub(super) fn ensure_data_directory(path: &Path) -> Result<()> {
    use std::os::windows::fs::OpenOptionsExt;
    crate::lease::validate_path(path)?;
    let user = Identity::current()?;
    let descriptor = descriptor(&format!("O:{0}D:P(A;OICI;FA;;;{0})", user.text))?;
    let attrs = attributes(&descriptor);
    let name: Vec<_> = path.as_os_str().encode_wide().chain([0]).collect();
    if unsafe { CreateDirectoryW(name.as_ptr(), &attrs) } == 0 {
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::AlreadyExists {
            return Err(error.into());
        }
    }
    crate::lease::validate_path(path)?;
    let directory = std::fs::OpenOptions::new()
        .access_mode(READ_CONTROL)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS)
        .open(path)?;
    if !directory.metadata()?.is_dir() {
        return Err(Error::Unsafe("private archive directory required".into()));
    }
    user.verify(directory.as_raw_handle(), true)
}

const MAX_FRAME: usize = 1024 * 1024;
pub(super) async fn read<T: DeserializeOwned>(stream: &mut (impl AsyncRead + Unpin)) -> Result<T> {
    let len = stream.read_u32().await? as usize;
    if len == 0 || len > MAX_FRAME {
        return Err(Error::OutputLimit);
    }
    let mut frame = vec![0; len];
    stream.read_exact(&mut frame).await?;
    Ok(serde_json::from_slice(&frame)?)
}
async fn write(stream: &mut (impl AsyncWrite + Unpin), value: &impl Serialize) -> Result<()> {
    let frame = serde_json::to_vec(value)?;
    if frame.len() > MAX_FRAME {
        return Err(Error::OutputLimit);
    }
    stream.write_u32(frame.len() as u32).await?;
    stream.write_all(&frame).await?;
    Ok(())
}
pub(super) async fn reply(stream: &mut NamedPipeServer, response: &serde_json::Value) {
    // DisconnectNamedPipe discards unread bytes. Let the client acknowledge
    // receipt before recycling the single instance. A broken client is bounded.
    let _ = timeout(Duration::from_secs(2), async {
        write(stream, response).await?;
        stream.read_u8().await?;
        Ok::<(), Error>(())
    })
    .await;
}

pub(super) async fn request(data: &Path, command: AdminCommand) -> Result<serde_json::Value> {
    let user = Identity::current()?;
    let name = user.name(data)?;
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut client = loop {
        match ClientOptions::new().open(&name) {
            Ok(client) => break client,
            Err(error)
                if error.raw_os_error() == Some(ERROR_PIPE_BUSY as i32)
                    && Instant::now() < deadline =>
            {
                sleep(Duration::from_millis(25)).await;
            }
            Err(error) => return Err(error.into()),
        }
    };
    user.verify(client.as_raw_handle(), false)?;
    timeout(Duration::from_secs(150), async {
        write(&mut client, &command).await?;
        let response = read(&mut client).await?;
        client.write_u8(1).await?;
        Ok(response)
    })
    .await
    .map_err(|_| Error::Deadline)?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn both_ends_check_the_actual_current_sid_and_protected_single_ace() {
        let dir = tempfile::tempdir().unwrap();
        let user = Identity::current().unwrap();
        let name = user.name(dir.path()).unwrap();
        let server = listen(dir.path()).unwrap();
        assert!(listen(dir.path()).is_err());
        let client = ClientOptions::new().open(&name).unwrap();
        user.verify(client.as_raw_handle(), false).unwrap();
        drop(client);
        drop(server);
        let broad = create(
            &name,
            &format!("O:{0}D:P(A;;GA;;;{0})(A;;GR;;;WD)", user.text),
        )
        .unwrap();
        assert!(matches!(
            user.verify(broad.as_raw_handle(), false),
            Err(Error::Unauthorized)
        ));
        assert!(matches!(
            request(dir.path(), AdminCommand::Shutdown).await,
            Err(Error::Unauthorized)
        ));
    }
}
