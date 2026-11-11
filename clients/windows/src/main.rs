#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]
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
    struct State(Mutex<Runtime>);
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

    #[tauri::command]
    async fn connect_node(state: tauri::State<'_, State>, node: String) -> Result<(), String> {
        let validated = Bridge::new(&node, "")?;
        let token = credentials::load(&validated.credential_origin())?.unwrap_or_default();
        let bridge = Bridge::new(&node, &token)?;
        let mut runtime = state
            .inner()
            .0
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
                .0
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
                .0
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
                .0
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
                .0
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
        if let Ok(mut runtime) = state.inner().0.lock()
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
            .0
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
                .0
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
            .0
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
            .0
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
            .0
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
                    && let Ok(mut runtime) = window.state::<State>().inner().0.lock()
                {
                    runtime.stop();
                }
            })
            .run(tauri::generate_context!())
            .expect("Windows client could not start");
    }
}
#[cfg(windows)]
fn main() {
    desktop::run();
}
