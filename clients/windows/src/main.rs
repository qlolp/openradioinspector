#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]
#[cfg(any(windows, test))]
mod archive_folder;
#[cfg(not(windows))]
fn main() {
    eprintln!("The native wrapper is built on Windows. Portable bridge tests run on all hosts.");
}
#[cfg(windows)]
mod desktop {
    use ori_native_bridge::{
        ApplicationAck, BinaryStreamConfig, Bridge, StreamMessage, credentials,
    };
    use std::{
        collections::HashMap,
        sync::{Arc, Mutex},
    };
    use tauri::{Manager, WebviewUrl, WebviewWindowBuilder, ipc::Channel};
    use tokio_util::sync::CancellationToken;

    #[derive(Default)]
    struct Runtime {
        bridge: Option<Bridge>,
        stream: Option<Stream>,
        requests: HashMap<String, CancellationToken>,
        epoch: u64,
    }
    #[derive(Default)]
    struct State {
        runtime: Mutex<Runtime>,
        archive_picker: tokio::sync::Mutex<()>,
    }
    struct Stream {
        id: String,
        cancel: CancellationToken,
        ack: Arc<ApplicationAck>,
    }
    impl Runtime {
        fn stop(&mut self) {
            if let Some(stream) = self.stream.take() {
                stream.cancel.cancel();
            }
            for (_, token) in self.requests.drain() {
                token.cancel();
            }
        }
    }

    fn supervisor_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
        Ok(app
            .path()
            .resource_dir()
            .map_err(|_| "Installed runtime unavailable")?
            .join("runtime")
            .join("ori-supervisor.exe"))
    }

    impl Runtime {
        fn archive_access(&self, epoch: u64) -> Result<(), String> {
            super::archive_folder::check_connection(
                self.bridge.as_ref().map(Bridge::legacy_url),
                epoch,
                self.epoch,
            )
        }
    }

    // Native IPC receives neither a filesystem path nor a command line. The
    // short lock binds process launch to the captured origin and generation.
    async fn archive_helper(
        app: &tauri::AppHandle,
        epoch: u64,
        args: Vec<std::ffi::OsString>,
    ) -> Result<Vec<u8>, String> {
        use std::{
            os::windows::process::CommandExt,
            process::{Command, Stdio},
        };
        let helper = supervisor_path(app)?;
        let app = app.clone();
        tauri::async_runtime::spawn_blocking(move || {
            let state = app.state::<State>();
            let child = {
                let runtime = state
                    .runtime
                    .lock()
                    .map_err(|_| "Native state unavailable")?;
                runtime.archive_access(epoch)?;
                Command::new(helper)
                    .args(args)
                    .creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW)
                    .stdin(Stdio::null())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .spawn()
                    .map_err(|_| "Could not launch the installed Windows supervisor")?
            };
            let output = child
                .wait_with_output()
                .map_err(|_| "Windows supervisor request interrupted")?;
            state
                .runtime
                .lock()
                .map_err(|_| "Native state unavailable")?
                .archive_access(epoch)?;
            if !output.status.success() {
                let reason = String::from_utf8_lossy(&output.stderr);
                if reason.contains("resource is already owned") {
                    return Err(
                        "Stop the local Windows node before choosing its archive folder.".into(),
                    );
                }
                return Err(reason.trim().chars().take(1024).collect());
            }
            Ok(output.stdout)
        })
        .await
        .map_err(|_| "Windows supervisor request interrupted")?
    }

    #[tauri::command]
    async fn choose_local_archive_directory(
        app: tauri::AppHandle,
        window: tauri::WebviewWindow,
        state: tauri::State<'_, State>,
    ) -> Result<Option<String>, String> {
        use super::archive_folder;
        let _selection = state
            .archive_picker
            .try_lock()
            .map_err(|_| "Archive folder selection is already open.")?;
        let epoch = {
            let runtime = state
                .runtime
                .lock()
                .map_err(|_| "Native state unavailable")?;
            runtime.archive_access(runtime.epoch)?;
            runtime.epoch
        };
        let target = archive_helper(
            &app,
            epoch,
            vec!["windows-node".into(), "archive-target".into()],
        )
        .await?;
        let node = archive_folder::archive_target(&target)?;
        let (answer, selected) = tokio::sync::oneshot::channel();
        let owner = window.clone();
        window
            .run_on_main_thread(move || {
                let result = (|| {
                    let state = owner.state::<State>();
                    state
                        .runtime
                        .lock()
                        .map_err(|_| "Native state unavailable")?
                        .archive_access(epoch)?;
                    let hwnd = owner
                        .hwnd()
                        .map_err(|_| "Windows application window unavailable")?;
                    archive_folder::choose(hwnd).map_err(|error| {
                        format!("Windows could not open the folder chooser: {error}")
                    })
                })();
                let _ = answer.send(result);
            })
            .map_err(|_| "Windows application window unavailable")?;
        let Some(parent) = selected
            .await
            .map_err(|_| "Archive folder selection interrupted")??
        else {
            return Ok(None);
        };
        let directory = archive_folder::destination(&parent)?;
        let args = archive_folder::relocation_arguments(&node, &directory)
            .into_iter()
            .map(std::ffi::OsStr::to_os_string)
            .collect();
        let receipt = archive_helper(&app, epoch, args).await?;
        Ok(Some(archive_folder::confirm(&receipt, &directory)?))
    }

    #[derive(serde::Deserialize)]
    #[serde(rename_all = "snake_case")]
    enum LocalAction {
        Start,
        Shutdown,
        Register,
        Unregister,
    }
    #[tauri::command]
    async fn local_node(app: tauri::AppHandle, action: LocalAction) -> Result<(), String> {
        use std::{
            os::windows::process::CommandExt,
            process::{Command, Stdio},
        };
        let helper = supervisor_path(&app)?;
        let command = match action {
            LocalAction::Start => "start",
            LocalAction::Shutdown => "shutdown",
            LocalAction::Register => "register",
            LocalAction::Unregister => "unregister",
        };
        tauri::async_runtime::spawn_blocking(move || {
            let output = Command::new(helper)
                .args(["windows-node", command])
                .creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW)
                .stdin(Stdio::null())
                .output()
                .map_err(|_| "Could not launch the installed Windows supervisor".to_owned())?;
            if !output.status.success() {
                return Err(String::from_utf8_lossy(&output.stderr).trim().to_owned());
            }
            let response: serde_json::Value = serde_json::from_slice(&output.stdout)
                .map_err(|_| "Invalid reply from the Windows supervisor")?;
            if response["ok"] != true
                || (command == "start"
                    && (response["status"]["ready"] != true
                        || response["status"]["writer_owned"] != true))
            {
                return Err("Windows supervisor did not confirm the requested operation".into());
            }
            Ok(())
        })
        .await
        .map_err(|_| "Windows supervisor request interrupted")?
    }
    #[tauri::command]
    async fn connect_node(state: tauri::State<'_, State>, node: String) -> Result<(), String> {
        let validated = Bridge::new(&node, "")?;
        let token = credentials::load(&validated.credential_origin())?.unwrap_or_default();
        let bridge = Bridge::new(&node, &token)?;
        let mut runtime = state
            .inner()
            .runtime
            .lock()
            .map_err(|_| "Native state unavailable")?;
        runtime.stop();
        runtime.epoch += 1;
        runtime.bridge = Some(bridge);
        Ok(())
    }
    #[tauri::command]
    async fn pair_node(
        state: tauri::State<'_, State>,
        pairing_code: String,
        label: String,
    ) -> Result<(), String> {
        let (bridge, epoch) = {
            let runtime = state
                .inner()
                .runtime
                .lock()
                .map_err(|_| "Native state unavailable")?;
            (
                runtime.bridge.clone().ok_or("Connect a core node first")?,
                runtime.epoch,
            )
        };
        let paired = bridge.pair_native(&pairing_code, &label).await?;
        let saved = {
            let mut runtime = state
                .inner()
                .runtime
                .lock()
                .map_err(|_| "Native state unavailable")?;
            if runtime.epoch != epoch {
                Err("Node changed while pairing".to_owned())
            } else {
                credentials::save(&paired.bridge.credential_origin(), &paired.token).map(|()| {
                    runtime.stop();
                    runtime.epoch += 1;
                    runtime.bridge = Some(paired.bridge.clone());
                })
            }
        };
        if saved.is_err() {
            let _ = paired
                .bridge
                .request(
                    "/api/v1/security/credentials/current/revoke",
                    Some("{}".into()),
                    false,
                )
                .await;
        }
        saved
    }
    #[tauri::command]
    async fn revoke_pairing(state: tauri::State<'_, State>) -> Result<(), String> {
        let bridge = {
            let mut runtime = state
                .inner()
                .runtime
                .lock()
                .map_err(|_| "Native state unavailable")?;
            let bridge = runtime.bridge.clone().ok_or("Connect a core node first")?;
            credentials::remove(&bridge.credential_origin())?;
            runtime.stop();
            runtime.epoch += 1;
            runtime.bridge = Some(Bridge::new(bridge.legacy_url(), "")?);
            bridge
        };
        let raw=bridge.request("/api/v1/security/credentials/current/revoke",Some("{}".into()),false).await.map_err(|_|"Local credential removed; remote revocation failed. Ask the node operator to revoke this client.")?;
        #[derive(serde::Deserialize)]
        struct Outcome {
            error: Option<serde::de::IgnoredAny>,
        }
        let result: Outcome = serde_json::from_str(&raw)
            .map_err(|_| "Local credential removed; invalid revocation response")?;
        if result.error.is_some() {
            return Err("Local credential removed; the node rejected remote revocation".into());
        }
        Ok(())
    }
    #[tauri::command]
    async fn api_request(
        state: tauri::State<'_, State>,
        request_id: String,
        path: String,
        body: Option<String>,
        audio: bool,
    ) -> Result<String, String> {
        if request_id.len() > 100 {
            return Err("Invalid request identifier".into());
        }
        let (bridge, cancel, epoch) = {
            let mut runtime = state
                .inner()
                .runtime
                .lock()
                .map_err(|_| "Native state unavailable")?;
            if runtime.requests.len() >= 16 || runtime.requests.contains_key(&request_id) {
                return Err("Native request queue is full".into());
            }
            let bridge = runtime.bridge.clone().ok_or("Connect a core node first")?;
            let cancel = CancellationToken::new();
            runtime.requests.insert(request_id.clone(), cancel.clone());
            (bridge, cancel, runtime.epoch)
        };
        let result = tokio::select! { _ = cancel.cancelled() => Err("Request cancelled".into()), result = bridge.request(&path, body, audio) => result };
        if let Ok(mut runtime) = state.inner().runtime.lock()
            && runtime.epoch == epoch
        {
            runtime.requests.remove(&request_id);
        }
        result
    }
    #[tauri::command]
    async fn cancel_request(
        state: tauri::State<'_, State>,
        request_id: String,
    ) -> Result<(), String> {
        let runtime = state
            .inner()
            .runtime
            .lock()
            .map_err(|_| "Native state unavailable")?;
        if let Some(cancel) = runtime.requests.get(&request_id) {
            cancel.cancel();
        }
        Ok(())
    }
    #[tauri::command]
    async fn start_events(
        state: tauri::State<'_, State>,
        after: String,
        stream_id: String,
        binary: Option<BinaryStreamConfig>,
        channel: Channel<StreamMessage>,
    ) -> Result<(), String> {
        let after = after.parse::<u64>().map_err(|_| "Invalid event cursor")?;
        if let Some(config) = binary {
            config.validate()?;
        }
        if stream_id.is_empty()
            || stream_id.len() > 100
            || !stream_id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-')
        {
            return Err("Invalid stream identifier".into());
        }
        let (ack, receiver) = ApplicationAck::channel();
        let receiver = Arc::new(tokio::sync::Mutex::new(receiver));
        let (bridge, cancel) = {
            let mut runtime = state
                .inner()
                .runtime
                .lock()
                .map_err(|_| "Native state unavailable")?;
            let bridge = runtime.bridge.clone().ok_or("Connect a core node first")?;
            if let Some(old) = runtime.stream.take() {
                old.cancel.cancel();
            }
            let cancel = CancellationToken::new();
            runtime.stream = Some(Stream {
                id: stream_id,
                cancel: cancel.clone(),
                ack: ack.clone(),
            });
            (bridge, cancel)
        };
        tauri::async_runtime::spawn(async move {
            bridge
                .events_negotiated(after, binary, cancel, move |message| {
                    let ack = ack.clone();
                    let receiver = receiver.clone();
                    let channel = channel.clone();
                    async move {
                        let expected = match message.delivery_id.as_deref() {
                            Some(id) => match ack.arm(id) {
                                Ok(id) => Some(id),
                                Err(_) => return false,
                            },
                            None => None,
                        };
                        if channel.send(message).is_err() {
                            return false;
                        }
                        match expected {
                            Some(id) => receiver.lock().await.recv().await == Some(id),
                            None => true,
                        }
                    }
                })
                .await;
        });
        Ok(())
    }
    #[tauri::command]
    async fn stop_events(state: tauri::State<'_, State>, stream_id: String) -> Result<(), String> {
        let mut runtime = state
            .inner()
            .runtime
            .lock()
            .map_err(|_| "Native state unavailable")?;
        if runtime
            .stream
            .as_ref()
            .is_some_and(|stream| stream.id == stream_id)
        {
            runtime.stream.take().unwrap().cancel.cancel();
        }
        Ok(())
    }
    #[tauri::command]
    async fn ack_event(
        state: tauri::State<'_, State>,
        stream_id: String,
        delivery_id: String,
    ) -> Result<(), String> {
        let runtime = state
            .inner()
            .runtime
            .lock()
            .map_err(|_| "Native state unavailable")?;
        let stream = runtime
            .stream
            .as_ref()
            .filter(|stream| stream.id == stream_id)
            .ok_or("Stale native stream")?;
        stream.ack.acknowledge(&delivery_id)
    }
    #[tauri::command]
    async fn open_legacy(state: tauri::State<'_, State>) -> Result<(), String> {
        let url = state
            .inner()
            .runtime
            .lock()
            .map_err(|_| "Native state unavailable")?
            .bridge
            .as_ref()
            .ok_or("Connect a core node first")?
            .legacy_browser_url();
        webbrowser::open(&url)
            .map_err(|_| "Could not open the core in the default browser".to_owned())
    }
    pub fn run() {
        tauri::Builder::default()
            .manage(State::default())
            .invoke_handler(tauri::generate_handler![
                local_node,
                choose_local_archive_directory,
                connect_node,
                pair_node,
                revoke_pairing,
                api_request,
                cancel_request,
                start_events,
                stop_events,
                ack_event,
                open_legacy
            ])
            .setup(|app| {
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("OpenRadioInspector")
                    .inner_size(1440.0, 1000.0)
                    .min_inner_size(390.0, 640.0)
                    .devtools(cfg!(debug_assertions))
                    .on_navigation(|url| {
                        matches!(
                            (url.scheme(), url.host_str()),
                            ("tauri", Some("localhost")) | ("http", Some("tauri.localhost"))
                        ) || (cfg!(debug_assertions)
                            && url.scheme() == "http"
                            && url.host_str() == Some("127.0.0.1")
                            && url.port() == Some(1420))
                    })
                    .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                    .build()?;
                Ok(())
            })
            .on_window_event(|window, event| {
                if matches!(event, tauri::WindowEvent::Destroyed)
                    && let Ok(mut runtime) = window.state::<State>().inner().runtime.lock()
                {
                    runtime.stop();
                    runtime.epoch += 1;
                }
            })
            .run(tauri::generate_context!())
            .expect("Windows client could not start");
    }
}
#[cfg(windows)]
fn main() {
    if std::env::args().nth(1).as_deref() == Some("--build-info") {
        println!(
            "{}",
            serde_json::json!({"schema_version":1,"component":"openradioinspector-windows",
            "version":option_env!("ORI_INSTALLER_VERSION").unwrap_or(env!("CARGO_PKG_VERSION")),"target_arch":std::env::consts::ARCH,"target_os":"windows",
            "source_revision":option_env!("ORI_BUILD_SOURCE_SHA")})
        );
        return;
    }
    desktop::run();
}
