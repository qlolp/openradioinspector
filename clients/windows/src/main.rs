#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]
#[cfg(not(windows))]
fn main() {
    eprintln!("The native wrapper is built on Windows. Portable bridge tests run on all hosts.");
}
#[cfg(windows)]
mod desktop {
    use ori_native_bridge::{Bridge, StreamMessage};
    use serde_json::Value;
    use std::{collections::HashMap, sync::Mutex};
    use tauri::{Manager, WebviewUrl, WebviewWindowBuilder, ipc::Channel};
    use tokio_util::sync::CancellationToken;

    #[derive(Default)]
    struct Runtime {
        bridge: Option<Bridge>,
        stream: Option<CancellationToken>,
        requests: HashMap<String, CancellationToken>,
        epoch: u64,
    }
    #[derive(Default)]
    struct State(Mutex<Runtime>);
    impl Runtime {
        fn stop(&mut self) {
            if let Some(stream) = self.stream.take() {
                stream.cancel();
            }
            for (_, token) in self.requests.drain() {
                token.cancel();
            }
        }
    }

    #[tauri::command]
    async fn connect_node(
        state: tauri::State<'_, State>,
        node: String,
        token: String,
    ) -> Result<(), String> {
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
    async fn api_request(
        state: tauri::State<'_, State>,
        request_id: String,
        path: String,
        body: Option<Value>,
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
        after: u64,
        channel: Channel<StreamMessage>,
    ) -> Result<(), String> {
        let (bridge, cancel) = {
            let mut runtime = state
                .inner()
                .0
                .lock()
                .map_err(|_| "Native state unavailable")?;
            let bridge = runtime.bridge.clone().ok_or("Connect a core node first")?;
            if let Some(old) = runtime.stream.take() {
                old.cancel();
            }
            let cancel = CancellationToken::new();
            runtime.stream = Some(cancel.clone());
            (bridge, cancel)
        };
        tauri::async_runtime::spawn(async move {
            bridge
                .events(after, cancel, move |message| channel.send(message).is_ok())
                .await;
        });
        Ok(())
    }
    #[tauri::command]
    async fn stop_events(state: tauri::State<'_, State>) -> Result<(), String> {
        let mut runtime = state
            .inner()
            .0
            .lock()
            .map_err(|_| "Native state unavailable")?;
        if let Some(cancel) = runtime.stream.take() {
            cancel.cancel();
        }
        Ok(())
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
            .legacy_url()
            .to_owned();
        webbrowser::open(&url)
            .map_err(|_| "Could not open the core in the default browser".to_owned())
    }
    pub fn run() {
        tauri::Builder::default()
            .manage(State::default())
            .invoke_handler(tauri::generate_handler![
                connect_node,
                api_request,
                cancel_request,
                start_events,
                stop_events,
                open_legacy
            ])
            .setup(|app| {
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("OpenRadioInspector · Spectrum desk")
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
