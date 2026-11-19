//! A listening instance reserves the name for the supervisor's entire lifetime.
//! Each exchange uses a fresh handle. Both peers verify the kernel owner and DACL.
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

#[derive(Clone, Copy)]
pub(super) enum Endpoint {
    Generic,
    Installed,
}
impl Endpoint {
    fn namespace(self) -> &'static [u8] {
        match self {
            Self::Generic => b"service-config-v1",
            Self::Installed => b"windows-operator-rc1",
        }
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
    fn name(&self, data: &Path, endpoint: Endpoint) -> Result<String> {
        let mut digest = Sha256::new();
        digest.update(endpoint.namespace());
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
fn create(name: &str, sddl: &str, first: bool) -> Result<NamedPipeServer> {
    let descriptor = descriptor(sddl)?;
    let mut attrs = attributes(&descriptor);
    Ok(unsafe {
        ServerOptions::new()
            .first_pipe_instance(first)
            .reject_remote_clients(true)
            .max_instances(2)
            .create_with_security_attributes_raw(
                name,
                (&mut attrs as *mut SECURITY_ATTRIBUTES).cast(),
            )
    }?)
}
pub(super) fn path(data: &Path, endpoint: Endpoint) -> Result<std::path::PathBuf> {
    Ok(Identity::current()?.name(data, endpoint)?.into())
}
pub(super) fn exists(data: &Path, endpoint: Endpoint) -> Result<bool> {
    use windows_sys::Win32::{Foundation::ERROR_SEM_TIMEOUT, System::Pipes::WaitNamedPipeW};
    let name: Vec<_> = path(data, endpoint)?
        .as_os_str()
        .encode_wide()
        .chain([0])
        .collect();
    if unsafe { WaitNamedPipeW(name.as_ptr(), 1) } != 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(ERROR_SEM_TIMEOUT as i32) {
        return Ok(true);
    }
    if error.kind() == io::ErrorKind::NotFound {
        return Ok(false);
    }
    Err(error.into())
}
pub(super) struct Listener {
    name: String,
    user: Identity,
    next: NamedPipeServer,
}

impl Listener {
    fn instance(name: &str, user: &Identity, first: bool) -> Result<NamedPipeServer> {
        let server = create(name, &format!("O:{0}D:P(A;;GA;;;{0})", user.text), first)?;
        user.verify(server.as_raw_handle(), false)?;
        Ok(server)
    }

    pub(super) async fn connect(&self) -> io::Result<()> {
        self.next.connect().await
    }

    pub(super) async fn advance(&mut self) -> Result<NamedPipeServer> {
        // Mio starts another overlapped read after consuming a frame, including
        // the acknowledgement. Never reconnect a handle carrying that old IO.
        // Keep the current instance alive until a fresh verified instance owns
        // the name. A retired handle may briefly count against the two-instance
        // kernel limit while its cancelled IO completes.
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            match Self::instance(&self.name, &self.user, false) {
                Ok(next) => return Ok(std::mem::replace(&mut self.next, next)),
                Err(Error::Io(error))
                    if error.raw_os_error() == Some(ERROR_PIPE_BUSY as i32)
                        && Instant::now() < deadline =>
                {
                    sleep(Duration::from_millis(10)).await;
                }
                Err(error) => return Err(error),
            }
        }
    }
}

pub(super) fn listen(data: &Path, endpoint: Endpoint) -> Result<Listener> {
    let user = Identity::current()?;
    let name = user.name(data, endpoint)?;
    let next = Listener::instance(&name, &user, true)?;
    Ok(Listener { name, user, next })
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
    // Let the client acknowledge the framed reply before closing this
    // connection. An abandoned client cannot hold it open indefinitely.
    let _ = timeout(Duration::from_secs(2), async {
        write(stream, response).await?;
        stream.read_u8().await?;
        Ok::<(), Error>(())
    })
    .await;
}

pub(super) async fn request(
    data: &Path,
    endpoint: Endpoint,
    command: AdminCommand,
) -> Result<serde_json::Value> {
    let user = Identity::current()?;
    let name = user.name(data, endpoint)?;
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
        // The full authenticated response is already in memory. The server
        // may have closed after its bounded receipt wait.
        if let Err(error) = client.write_u8(1).await
            && !super::disconnected(&error)
        {
            return Err(error.into());
        }
        Ok(response)
    })
    .await
    .map_err(|_| Error::Deadline)?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn installed_controls_do_not_start_attach_or_stop_a_generic_supervisor() {
        let temp = tempfile::tempdir().unwrap();
        let data = temp.path().join("private");
        ensure_data_directory(&data).unwrap();
        let _generic = listen(&data, Endpoint::Generic).unwrap();
        assert!(super::super::status(&data).await.unwrap().is_none());
        assert!(matches!(super::super::start(&data).await, Err(Error::Busy)));
        assert!(
            matches!(request(&data, Endpoint::Installed, AdminCommand::Shutdown).await,
            Err(Error::Io(error)) if error.kind() == io::ErrorKind::NotFound)
        );
        assert!(exists(&data, Endpoint::Generic).unwrap());
    }

    #[tokio::test]
    async fn abandoned_and_competing_status_clients_get_independent_framed_replies() {
        let dir = tempfile::tempdir().unwrap();
        let data = dir.path();
        let mut listener = listen(data, Endpoint::Installed).unwrap();
        let abandoned = ClientOptions::new().open(&listener.name).unwrap();
        listener.connect().await.unwrap();
        let mut old = listener.advance().await.unwrap();
        assert_ne!(old.as_raw_handle(), listener.next.as_raw_handle());
        assert!(listen(data, Endpoint::Installed).is_err());
        drop(abandoned);
        assert!(
            timeout(Duration::from_secs(2), read::<AdminCommand>(&mut old))
                .await
                .unwrap()
                .is_err()
        );
        drop(old);

        let service = async {
            for sequence in 0..16 {
                listener.connect().await.unwrap();
                let mut connection = listener.advance().await.unwrap();
                assert_ne!(connection.as_raw_handle(), listener.next.as_raw_handle());
                assert!(listen(data, Endpoint::Installed).is_err());
                assert!(matches!(
                    read(&mut connection).await.unwrap(),
                    AdminCommand::Status
                ));
                reply(
                    &mut connection,
                    &serde_json::json!({"ok":true, "sequence":sequence}),
                )
                .await;
            }
        };
        let callers = async {
            for pair in 0..8 {
                let (first, second) = tokio::join!(
                    request(data, Endpoint::Installed, AdminCommand::Status),
                    request(data, Endpoint::Installed, AdminCommand::Status),
                );
                let mut sequences = [
                    first.unwrap()["sequence"].as_u64().unwrap(),
                    second.unwrap()["sequence"].as_u64().unwrap(),
                ];
                sequences.sort();
                assert_eq!(sequences, [pair * 2, pair * 2 + 1]);
            }
        };
        timeout(Duration::from_secs(20), async {
            tokio::join!(service, callers)
        })
        .await
        .unwrap();
        assert!(listen(data, Endpoint::Installed).is_err());
    }

    #[tokio::test]
    async fn status_reopens_after_peer_closes_before_reply() {
        let dir = tempfile::tempdir().unwrap();
        let data = dir.path();
        let mut listener = listen(data, Endpoint::Installed).unwrap();
        let service = async {
            listener.connect().await.unwrap();
            let mut retired = listener.advance().await.unwrap();
            assert!(matches!(
                read(&mut retired).await.unwrap(),
                AdminCommand::Status
            ));
            drop(retired);
            listener.connect().await.unwrap();
            let mut connected = listener.advance().await.unwrap();
            assert!(matches!(
                read(&mut connected).await.unwrap(),
                AdminCommand::Status
            ));
            reply(
                &mut connected,
                &serde_json::json!({
                    "ok":true, "status":{"ready":true,"writer_owned":true,"preflight_complete":true}
                }),
            )
            .await;
        };
        let (_, status) = timeout(Duration::from_secs(10), async {
            tokio::join!(service, super::super::status(data))
        })
        .await
        .unwrap();
        assert_eq!(status.unwrap().unwrap()["status"]["ready"], true);
    }

    #[tokio::test]
    async fn both_ends_check_the_actual_current_sid_and_protected_single_ace() {
        let dir = tempfile::tempdir().unwrap();
        let user = Identity::current().unwrap();
        let name = user.name(dir.path(), Endpoint::Installed).unwrap();
        let server = listen(dir.path(), Endpoint::Installed).unwrap();
        assert!(listen(dir.path(), Endpoint::Installed).is_err());
        let client = ClientOptions::new().open(&name).unwrap();
        user.verify(client.as_raw_handle(), false).unwrap();
        drop(client);
        drop(server);
        let broad = create(
            &name,
            &format!("O:{0}D:P(A;;GA;;;{0})(A;;GR;;;WD)", user.text),
            true,
        )
        .unwrap();
        assert!(matches!(
            user.verify(broad.as_raw_handle(), false),
            Err(Error::Unauthorized)
        ));
        assert!(matches!(
            request(dir.path(), Endpoint::Installed, AdminCommand::Shutdown).await,
            Err(Error::Unauthorized)
        ));
    }
}
