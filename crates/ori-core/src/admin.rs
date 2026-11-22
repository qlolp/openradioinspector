//! Private operator IPC. Pairing-window creation is never a LAN HTTP operation.
use ori_runtime::security::CredentialScope;

pub async fn request(
    data: &std::path::Path,
    ttl: u64,
    attempts: u32,
) -> crate::Result<serde_json::Value> {
    request_scoped(data, ttl, attempts, CredentialScope::Operator).await
}

#[cfg(unix)]
mod platform {
    use super::CredentialScope;
    use crate::{Core, CoreError, Result};
    use serde_json::{Value, json};
    use std::{
        os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt},
        path::{Path, PathBuf},
        sync::Arc,
        time::Duration,
    };
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{UnixListener, UnixStream},
        task::JoinHandle,
    };
    struct SocketOwner {
        path: PathBuf,
        device: u64,
        inode: u64,
    }
    impl Drop for SocketOwner {
        fn drop(&mut self) {
            if std::fs::symlink_metadata(&self.path)
                .is_ok_and(|meta| meta.dev() == self.device && meta.ino() == self.inode)
            {
                let _ = std::fs::remove_file(&self.path);
            }
        }
    }
    pub(crate) struct AdminServer {
        task: JoinHandle<Result<()>>,
    }
    impl AdminServer {
        pub async fn join(self) -> Result<()> {
            self.task.await.map_err(|_| CoreError::Task)?
        }
    }
    pub(crate) async fn start(core: Arc<Core>) -> Result<AdminServer> {
        let state = ori_runtime::lease::state_directory(&core.config.data_directory)?;
        let path = state.join("core-admin.sock");
        match std::fs::symlink_metadata(&path) {
            Ok(meta) => {
                if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } {
                    return Err(CoreError::Invalid("foreign operator socket path"));
                }
                match UnixStream::connect(&path).await {
                    Ok(_) => {
                        return Err(CoreError::Invalid("an operator socket is already active"));
                    }
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::ConnectionRefused | std::io::ErrorKind::NotFound
                        ) =>
                    {
                        std::fs::remove_file(&path)?
                    }
                    Err(error) => return Err(error.into()),
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        let listener = UnixListener::bind(&path)?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        let metadata = std::fs::symlink_metadata(&path)?;
        let owner = SocketOwner {
            path,
            device: metadata.dev(),
            inode: metadata.ino(),
        };
        let mut stopped = core.shutdown.subscribe();
        let task = tokio::spawn(async move {
            let _owner = owner;
            loop {
                if *stopped.borrow() {
                    break;
                }
                tokio::select! {
                    _=stopped.changed()=>{},
                    incoming=listener.accept()=>{
                        let (mut stream,_)=incoming?;
                        if stream.peer_cred()?.uid()!=unsafe{libc::geteuid()}{continue;}
                        let mut request=Vec::new();let read=tokio::time::timeout(Duration::from_secs(1),(&mut stream).take(4097).read_to_end(&mut request)).await;
                        let response=if matches!(read,Ok(Ok(_)))&&request.len()<=4096 {match ori_contracts::parse_json_value(&request,4096) {
                            Ok(value)=>command(&core,value),Err(_)=>json!({"version":1,"error":"invalid_request"}),
                        }}else{json!({"version":1,"error":"invalid_request"})};
                        let bytes=ori_contracts::to_json_vec(&response).map_err(|_|CoreError::Invalid("operator response serialization"))?;
                        let _=tokio::time::timeout(Duration::from_secs(1),stream.write_all(&bytes)).await;
                    }
                }
            }
            Ok(())
        });
        Ok(AdminServer { task })
    }
    fn command(core: &Core, value: Value) -> Value {
        let allowed = ["version", "command", "ttl_seconds", "attempts", "scope"];
        if value
            .as_object()
            .is_none_or(|map| map.keys().any(|key| !allowed.contains(&key.as_str())))
            || value["version"].as_u64() != Some(1)
            || value["command"] != "pairing.start"
        {
            return json!({"version":1,"error":"invalid_request"});
        }
        let ttl = match value.get("ttl_seconds") {
            None => 300,
            Some(value) => match value.as_u64() {
                Some(ttl) => ttl,
                None => return json!({"version":1,"error":"invalid_request"}),
            },
        };
        let attempts = match value.get("attempts") {
            None => 5,
            Some(value) => match value.as_u64().and_then(|v| u32::try_from(v).ok()) {
                Some(attempts) => attempts,
                None => return json!({"version":1,"error":"invalid_request"}),
            },
        };
        let scope = match value.get("scope") {
            None => CredentialScope::Operator,
            Some(value) => match serde_json::from_value(value.clone()) {
                Ok(scope) => scope,
                Err(_) => return json!({"version":1,"error":"invalid_request"}),
            },
        };
        match core.begin_scoped_pairing_window(ttl, attempts, scope) {
            Ok(code) => {
                let mut reply = json!({"version":1,"pairing_code":code,"expires_in":ttl,"server_fingerprint_sha256":core.server_fingerprint()});
                if value.get("scope").is_some() {
                    reply["access_scope"] = json!(scope);
                }
                reply
            }
            Err(_) => json!({"version":1,"error":"pairing_unavailable"}),
        }
    }
    pub async fn request_scoped(
        data: &Path,
        ttl: u64,
        attempts: u32,
        scope: CredentialScope,
    ) -> Result<Value> {
        let path = ori_runtime::lease::state_directory(data)?.join("core-admin.sock");
        let mut stream = UnixStream::connect(path).await?;
        let mut body =
            json!({"version":1,"command":"pairing.start","ttl_seconds":ttl,"attempts":attempts});
        if scope == CredentialScope::Reader {
            body["scope"] = json!(scope);
        }
        let request = ori_contracts::to_json_vec(&body)
            .map_err(|_| CoreError::Invalid("operator request serialization"))?;
        stream.write_all(&request).await?;
        stream.shutdown().await?;
        let mut bytes = Vec::new();
        tokio::time::timeout(
            Duration::from_secs(3),
            stream.take(4097).read_to_end(&mut bytes),
        )
        .await
        .map_err(|_| CoreError::Invalid("operator request timeout"))??;
        let response = ori_contracts::parse_json_value(&bytes, 4096)
            .map_err(|_| CoreError::Invalid("operator response invalid"))?;
        if scope == CredentialScope::Reader
            && (response["version"] != 1 || response["access_scope"] != json!(scope))
        {
            return Err(CoreError::Invalid(
                "node did not confirm a reader pairing window",
            ));
        }
        Ok(response)
    }
}
#[cfg(unix)]
pub use platform::request_scoped;
#[cfg(unix)]
pub(crate) use platform::start;
#[cfg(not(unix))]
pub async fn request_scoped(
    _data: &std::path::Path,
    _ttl: u64,
    _attempts: u32,
    _scope: CredentialScope,
) -> crate::Result<serde_json::Value> {
    Err(crate::CoreError::Invalid(
        "local pairing operator IPC requires verified native platform support",
    ))
}
