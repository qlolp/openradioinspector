//! Public control routes through the SAME owned Core executor and real SQLite.
//! Every byte stream is private synthetic memory; no port/audio is enumerated.
use base64::{Engine, engine::general_purpose::STANDARD};
use ori_contracts::{DeviceRole, DeviceStream, encode_path_segment, parse_json_value, to_json_vec};
use ori_core::{
    Core, CoreConfig,
    devices::{BoxTransport, DeviceFuture, DeviceResult, TransportFactory},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    task::{Context, Poll},
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader, ReadBuf},
    net::TcpListener,
};
const CLIENT: &str = "owned-control-client-0123456789";
fn oracle() -> Value {
    parse_json_value(
        include_bytes!("../../ori-control/tests/fixtures/python_control.json"),
        8 * 1024 * 1024,
    )
    .unwrap()
}
fn bytes(value: &Value) -> Vec<u8> {
    STANDARD.decode(value.as_str().unwrap()).unwrap()
}
struct OwnedIo {
    inner: tokio::io::DuplexStream,
    drops: Arc<AtomicUsize>,
}
impl Drop for OwnedIo {
    fn drop(&mut self) {
        self.drops.fetch_add(1, Ordering::SeqCst);
    }
}
impl AsyncRead for OwnedIo {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_read(cx, buf)
    }
}
impl AsyncWrite for OwnedIo {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.get_mut().inner).poll_write(cx, bytes)
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_flush(cx)
    }
    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_shutdown(cx)
    }
}
struct Factory {
    opens: AtomicUsize,
    drops: Arc<AtomicUsize>,
    commands: Arc<Mutex<Vec<String>>>,
    block_command: Arc<Mutex<Option<String>>>,
    entered: Arc<tokio::sync::Semaphore>,
    release: Arc<tokio::sync::Semaphore>,
    peers: Mutex<Vec<tokio::task::JoinHandle<()>>>,
}
impl Factory {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            opens: AtomicUsize::new(0),
            drops: Arc::new(AtomicUsize::new(0)),
            commands: Arc::new(Mutex::new(vec![])),
            block_command: Arc::new(Mutex::new(None)),
            entered: Arc::new(tokio::sync::Semaphore::new(0)),
            release: Arc::new(tokio::sync::Semaphore::new(0)),
            peers: Mutex::new(vec![]),
        })
    }
}
impl TransportFactory for Factory {
    fn identity(&self, stream: &DeviceStream) -> DeviceResult<String> {
        Ok(format!(
            "owned-memory:{}",
            stream.port.as_deref().unwrap_or(&stream.id)
        ))
    }
    fn open<'a>(&'a self, _stream: &'a DeviceStream) -> DeviceFuture<'a, BoxTransport> {
        Box::pin(async move {
            let receiver = _stream.role == DeviceRole::Receiver;
            self.opens.fetch_add(1, Ordering::SeqCst);
            let (client, server) = tokio::io::duplex(4096);
            let log = self.commands.clone();
            let blocked = self.block_command.clone();
            let entered = self.entered.clone();
            let release = self.release.clone();
            let peer = tokio::spawn(async move {
                let mut io = BufReader::new(server);
                let mut responses = BTreeMap::<String, Vec<u8>>::new();
                for wire in [
                    "output off",
                    "mode input",
                    "lna off",
                    "attenuate auto",
                    "rbw 10",
                    "calc off",
                    "pause",
                    "caloutput off",
                    "release",
                    "repeat 2",
                    "repeat 3",
                    "ultra off",
                    "sweep 100000000 101000000 3",
                    "trigger auto",
                    "trace dBm",
                    "refresh off",
                ] {
                    responses.insert(wire.into(), vec![]);
                }
                responses.insert("info".into(), b"tinySA Ultra ZS405\r\n".to_vec());
                responses.insert(
                    "version".into(),
                    b"tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n".to_vec(),
                );
                responses.insert("deviceid".into(), b"OWNED-SYNTHETIC-ONLY\r\n".to_vec());
                responses.insert("help".into(),b"info version deviceid help output mode lna attenuate rbw calc pause resume capture touch release menu text refresh caloutput sweep scan scanraw zero trace marker trigger repeat spur ultra ext_gain level freq modulation levelchange save recall sd_list sd_read sd_delete selftest status sweeptime\r\n".to_vec());
                responses.insert(
                    "help".into(),
                    (oracle()["cases"][0]["result"]["commands"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|v| v.as_str().unwrap())
                        .collect::<Vec<_>>()
                        .join(" ")
                        + "\r\n")
                        .into_bytes(),
                );
                responses.insert("zero".into(), b"zero {level}\r\n174dBm\r\n".to_vec());
                responses.insert("rbw".into(), b"rbw {auto|...}\r\n10000Hz\r\n".to_vec());
                responses.insert("capture".into(), b"\xf8\0".repeat(480 * 320));
                responses.insert("sd_list".into(), b"TRACE.CSV\r\n".to_vec());
                let raw = std::iter::once(b'{')
                    .chain([-90., -80., -70.].into_iter().flat_map(|level| {
                        let value = ((174. + level) * 32.) as u16;
                        [b'x', value.to_le_bytes()[0], value.to_le_bytes()[1]]
                    }))
                    .chain(*b"}")
                    .collect::<Vec<_>>();
                responses.insert("scanraw 100000000 101000000 3 0".into(), raw.clone());
                responses.insert("scanraw 100000000 100000000 3 0".into(), raw);
                responses.insert(
                    "scan 100M 101M 3 3".into(),
                    b"100000000 -90\r\n100500000 -80\r\n101000000 -70\r\n".to_vec(),
                );
                let sd = b"owned-original\0SD\xffch> bytes";
                let mut body = (sd.len() as u32).to_le_bytes().to_vec();
                body.extend_from_slice(sd);
                responses.insert("sd_read TRACE.CSV".into(), body);
                // Accepted command text and response payloads come from the
                // committed actual-Python Controller/UltraWire oracle.
                for case in oracle()["cases"].as_array().unwrap() {
                    for command in case["commands"].as_array().unwrap() {
                        responses
                            .entry(command.as_str().unwrap().into())
                            .or_default();
                    }
                    match case["operation"].as_str().unwrap() {
                        "sd.list" => {
                            responses.insert(
                                "sd_list".into(),
                                format!(
                                    "{}\r\n",
                                    case["result"]["directory_text"].as_str().unwrap()
                                )
                                .into_bytes(),
                            );
                        }
                        "sd.read" => {
                            let payload = bytes(&case["result"]["artifact"]["data_base64"]);
                            let mut body = (payload.len() as u32).to_le_bytes().to_vec();
                            body.extend(payload);
                            responses.insert("sd_read TRACE.CSV".into(), body);
                        }
                        "sd.delete" => {
                            responses.insert(
                                "sd_delete TRACE.CSV".into(),
                                b"delete: TRACE.CSV OK\r\n".to_vec(),
                            );
                        }
                        "acquire.raw" | "acquire.zero" => {
                            let raw = parse_json_value(
                                &bytes(&case["result"]["artifact"]["data_base64"]),
                                65536,
                            )
                            .unwrap();
                            let command = case["commands"]
                                .as_array()
                                .unwrap()
                                .last()
                                .unwrap()
                                .as_str()
                                .unwrap();
                            responses.insert(command.into(), bytes(&raw["wire_base64"]));
                        }
                        "trace" if case["parameters"].get("action").is_none() => {
                            let raw = parse_json_value(
                                &bytes(&case["result"]["artifact"]["data_base64"]),
                                65536,
                            )
                            .unwrap();
                            responses.insert(
                                "trace 1 value".into(),
                                format!("{}\r\n", raw["device_response"].as_str().unwrap())
                                    .into_bytes(),
                            );
                        }
                        "marker" if case["parameters"].get("action").is_none() => {
                            responses.insert(
                                "marker 1".into(),
                                format!(
                                    "{}\r\n",
                                    case["result"]["device_response"].as_str().unwrap()
                                )
                                .into_bytes(),
                            );
                        }
                        _ => {}
                    }
                }
                responses.entry("repeat 4".into()).or_default();
                let mut receiver_mode = "00".to_owned();
                loop {
                    let mut wire = Vec::new();
                    match io.read_until(b'\r', &mut wire).await {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {}
                    }
                    let command = String::from_utf8(wire[..wire.len() - 1].to_vec()).unwrap();
                    log.lock().unwrap().push(command.clone());
                    let should_block = blocked.lock().unwrap().as_deref() == Some(command.as_str());
                    if should_block {
                        entered.add_permits(1);
                        tokio::select! {p=release.acquire()=>p.unwrap().forget(),eof=io.fill_buf()=>{assert!(eof.unwrap().is_empty());break;}}
                    }
                    if receiver {
                        let reply = match command.as_str() {
                            "VR" => "20VR2504A\r\n".to_owned(),
                            "RN" => "20RNOWNED-SYNTHETIC\r\n".into(),
                            "LM" => "20LM1371\r\n".into(),
                            "MD" => format!("20MD0{receiver_mode}\r\n"),
                            "RE1" | "VFA" | "EX" => "20\r\n".into(),
                            c if c.starts_with("RF") => "20\r\n".into(),
                            c if c.starts_with("MD0") && c.len() == 5 => {
                                receiver_mode = c[3..].into();
                                "20\r\n".into()
                            }
                            _ => panic!("outside verified owned AR-DV1 registry: {command}"),
                        };
                        if io.get_mut().write_all(reply.as_bytes()).await.is_err() {
                            break;
                        }
                        continue;
                    }
                    let body = responses
                        .get(&command)
                        .unwrap_or_else(|| panic!("unexpected owned control command: {command}"));
                    let mut packet = format!("{command}\r\n").into_bytes();
                    packet.extend_from_slice(body);
                    packet.extend_from_slice(b"ch> ");
                    if io.get_mut().write_all(&packet).await.is_err() {
                        break;
                    }
                }
            });
            self.peers.lock().unwrap().push(peer);
            Ok(Box::new(OwnedIo {
                inner: client,
                drops: self.drops.clone(),
            }) as BoxTransport)
        })
    }
}
struct Server {
    _directory: tempfile::TempDir,
    core: Arc<Core>,
    url: String,
    task: tokio::task::JoinHandle<ori_core::Result<()>>,
    factory: Arc<Factory>,
    client: reqwest::Client,
    bearer: Option<String>,
}
struct PcmBackend;
struct PcmStream {
    sink: ori_audio::PcmSink,
}
impl ori_audio::AudioBackend for PcmBackend {
    fn inputs(&self) -> ori_audio::AudioResult<Vec<ori_audio::AudioInput>> {
        Ok(vec![ori_audio::AudioInput {
            index: 0,
            name: "owned-memory-input".into(),
            formats: vec![ori_audio::AudioFormatRange {
                channels: 1,
                encoding: ori_audio::SampleEncoding::I16,
                min_sample_rate_hz: 8000,
                max_sample_rate_hz: 8000,
            }],
        }])
    }
    fn open(
        &self,
        input: &ori_audio::InputSelection,
        rate: u32,
        sink: ori_audio::PcmSink,
    ) -> ori_audio::AudioResult<Box<dyn ori_audio::AudioStream>> {
        assert_eq!(
            (input.index, input.name.as_str(), rate),
            (0, "owned-memory-input", 8000)
        );
        Ok(Box::new(PcmStream { sink }))
    }
}
impl ori_audio::AudioStream for PcmStream {
    fn start(&mut self) -> ori_audio::AudioResult<()> {
        let wav = bytes(&oracle()["wav_base64"]);
        assert_eq!(wav.len(), 1644);
        assert_eq!(
            self.sink.push_pcm16(
                &wav[44..],
                chrono::Utc::now()
                    .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
                    .parse()
                    .unwrap()
            ),
            ori_audio::PushOutcome::Accepted
        );
        Ok(())
    }
    fn stop(&mut self) -> ori_audio::AudioResult<()> {
        Ok(())
    }
}
impl Server {
    async fn new() -> Self {
        Self::configured(false).await
    }
    async fn configured(features: bool) -> Self {
        Self::configured_mode(features, false).await
    }
    async fn configured_mode(features: bool, lan: bool) -> Self {
        Self::configured_transport(features, lan, true).await
    }
    async fn configured_transport(features: bool, lan: bool, enabled: bool) -> Self {
        let directory = tempfile::tempdir().unwrap();
        let factory = Factory::new();
        let mut config = CoreConfig::replay(directory.path());
        if enabled {
            config.devices.transport = Some(factory.clone());
        }
        if features {
            config.analysis_worker = Some(ori_runtime::worker::WorkerSpec {
                program: std::env::var_os("ORI_RESEARCH_PYTHON")
                    .or_else(|| std::env::var_os("ORI_TEST_PYTHON"))
                    .map(Into::into)
                    .unwrap_or_else(|| "/usr/bin/python3".into()),
                args: vec![
                    "-I".into(),
                    "-B".into(),
                    "-m".into(),
                    "radioinspector.research_rpc".into(),
                ],
                // The supervisor itself fixes BLAS/OMP to one. Its trusted
                // override allowlist intentionally rejects these env keys.
                environment: BTreeMap::new(),
                deadline_ms: 5000,
                require_hard_memory: false,
                linux_cgroup: None,
            });
            config.audio.backend = Some(Arc::new(PcmBackend));
        }
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let authority = listener.local_addr().unwrap().to_string();
        let url = format!("{}://{authority}", if lan { "https" } else { "http" });
        let client = if lan {
            let security = directory.path().join("security");
            std::fs::create_dir(&security).unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&security, std::fs::Permissions::from_mode(0o700))
                    .unwrap();
            }
            let identity =
                ori_runtime::security::TlsIdentity::create(&security, vec!["127.0.0.1".into()])
                    .unwrap();
            config.lan_security = Some(ori_core::LanSecurityConfig {
                directory: security,
                hosts: vec![authority],
                origins: vec![url.clone()],
            });
            reqwest::Client::builder()
                .tls_certs_only([reqwest::Certificate::from_pem(
                    identity.trust_anchor_pem().as_bytes(),
                )
                .unwrap()])
                .build()
                .unwrap()
        } else {
            reqwest::Client::new()
        };
        let core = Core::open(config).await.unwrap();
        let own = core.clone();
        let task = tokio::spawn(async move {
            if lan {
                own.serve_tls(listener).await
            } else {
                own.serve(listener).await
            }
        });
        let mut server = Self {
            _directory: directory,
            core,
            url,
            task,
            factory,
            client,
            bearer: None,
        };
        if lan {
            server.bearer = Some(server.pair().await);
        }
        server
    }
    async fn pair(&self) -> String {
        let code = self.core.begin_pairing_window(300, 10).unwrap();
        let response=self.client.post(format!("{}/api/v1/security/pair",self.url)).header("Origin",&self.url).json(&json!({"pairing_code":code,"label":"owned synthetic credential","client":"native"})).send().await.unwrap();
        assert_eq!(response.status(), 200);
        parse_json_value(&response.bytes().await.unwrap(), 4096).unwrap()["data"]["token"]
            .as_str()
            .unwrap()
            .into()
    }
    async fn request(
        &self,
        method: &str,
        path: &str,
        payload: Option<Value>,
        client: Option<&str>,
    ) -> (u16, Value) {
        self.request_as(method, path, payload, client, self.bearer.as_deref())
            .await
    }
    async fn request_as(
        &self,
        method: &str,
        path: &str,
        payload: Option<Value>,
        client: Option<&str>,
        bearer: Option<&str>,
    ) -> (u16, Value) {
        let mut request = self
            .client
            .request(method.parse().unwrap(), format!("{}{path}", self.url));
        if let Some(bearer) = bearer {
            request = request.bearer_auth(bearer);
        }
        if let Some(client) = client {
            request = request.header("X-Instrument-Client", client);
        }
        if let Some(payload) = payload {
            request = request
                .header("Content-Type", "application/json")
                .body(to_json_vec(&payload).unwrap());
        }
        let response = request.send().await.unwrap();
        let status = response.status().as_u16();
        let bytes = response.bytes().await.unwrap();
        (status, parse_json_value(&bytes, 16 * 1024 * 1024).unwrap())
    }
    async fn setup(&self) {
        let session = json!({"id":"control-fixture","name":"Owned synthetic controller","streams":[{"id":"source","device":"Owned tinySA memory fixture","role":"spectrum_analyzer","level_unit":"dBm","port":"owned-memory-only","settings":{"adapter":"tinysa-serial","start_hz":100000000,"stop_hz":101000000,"points":3,"rbw_hz":10000,"period_s":0.1,"monitoring_instrument_context":"inspect"}}]});
        assert_eq!(
            self.request("POST", "/api/v1/sessions", Some(session), None)
                .await
                .0,
            201
        );
        assert_eq!(
            self.request(
                "POST",
                "/api/v1/sessions/control-fixture/start",
                Some(json!({})),
                None
            )
            .await
            .0,
            200
        );
    }
    async fn submit(&self, payload: Value) -> Value {
        let operation = payload["operation"].clone();
        let (status, value) = self
            .request(
                "POST",
                "/api/v1/sessions/control-fixture/streams/source/instrument/operations",
                Some(payload),
                Some(CLIENT),
            )
            .await;
        if status != 202 {
            let (_, workers) = self
                .request(
                    "GET",
                    "/api/v1/sessions/control-fixture/workers",
                    None,
                    None,
                )
                .await;
            let wire = self.factory.commands.lock().unwrap().clone();
            panic!(
                "owned {operation} admission {status}: {value}; worker={workers}; synthetic wire tail={:?}",
                &wire[wire.len().saturating_sub(20)..]
            );
        }
        assert!(value["data"].get("result").is_none());
        value["data"].clone()
    }
    async fn done(&self, id: &str, client: Option<&str>) -> Value {
        tokio::time::timeout(Duration::from_secs(5),async{loop{let(status,value)=self.request("GET",&format!("/api/v1/sessions/control-fixture/streams/source/instrument/operations/{id}"),None,client).await;assert_eq!(status,200,"{value}");if matches!(value["data"]["state"].as_str(),Some("succeeded"|"failed"|"unknown"|"cancelled")){return value["data"].clone();}tokio::time::sleep(Duration::from_millis(5)).await;}}).await.unwrap()
    }
    async fn stop(self) {
        self.core.request_shutdown();
        tokio::time::timeout(Duration::from_secs(8), self.task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        let peers = std::mem::take(&mut *self.factory.peers.lock().unwrap());
        for peer in peers {
            tokio::time::timeout(Duration::from_secs(3), peer)
                .await
                .unwrap()
                .unwrap();
        }
        assert_eq!(
            self.factory.opens.load(Ordering::SeqCst),
            self.factory.drops.load(Ordering::SeqCst)
        );
    }
}

#[tokio::test]
async fn standalone_without_transport_keeps_hardware_unavailable_and_cold_validation_honest() {
    let server = Server::configured_transport(false, false, false).await;
    server.setup().await;
    let (status, caps) = server
        .request("GET", "/api/v1/capabilities", None, None)
        .await;
    assert_eq!(status, 200, "{caps}");
    assert_eq!(caps["data"]["features"]["hardware_control"], true);
    assert_eq!(
        caps["data"]["features"]["tinysa_ultra_instrument_control"],
        true
    );
    assert_eq!(
        caps["data"]["features"]["tinysa_ultra_async_operations"],
        true
    );
    assert_eq!(
        caps["data"]["features"]["tinysa_ultra_hardware_validated"],
        false
    );
    assert_eq!(caps["data"]["features"]["explicit_serial_adapters"], false);
    assert_eq!(caps["data"]["candidate"]["native_connector_enabled"], false);
    let base = "/api/v1/sessions/control-fixture/streams/source";
    let (status, value) = server
        .request(
            "GET",
            &format!("{base}/instrument/capabilities"),
            None,
            None,
        )
        .await;
    assert_eq!(status, 200, "{value}");
    assert_eq!(value["data"]["status"]["profile_verified"], false);
    let (status, value) = server
        .request("POST", &format!("{base}/start"), Some(json!({})), None)
        .await;
    assert_eq!(status, 400, "{value}");
    assert_eq!(value["error"]["code"], "unsupported_adapter");
    let (status, value) = server
        .request(
            "POST",
            &format!("{base}/instrument/operations"),
            Some(json!({"operation":"lease.acquire","parameters":{"acknowledge_rf":true}})),
            Some(CLIENT),
        )
        .await;
    assert_eq!(status, 409, "{value}");
    assert_eq!(value["error"]["code"], "instrument_conflict");
    assert!(
        value["error"]["message"]
            .as_str()
            .unwrap()
            .contains("owned_instrument_not_connected")
    );
    let (status, value) = server
        .request(
            "GET",
            "/api/v1/sessions/missing/streams/source/instrument/artifacts",
            None,
            None,
        )
        .await;
    assert_eq!(status, 404);
    assert_eq!(value["error"]["code"], "instrument_not_found");
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), 0);
    server.stop().await;
}

#[tokio::test]
async fn cached_control_metadata_then_explicit_connect_lab_and_saved_png_use_one_actual_worker() {
    let server = Server::new().await;
    server.setup().await;
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), 0);
    let (status, caps) = server
        .request(
            "GET",
            "/api/v1/sessions/control-fixture/streams/source/instrument/capabilities",
            None,
            None,
        )
        .await;
    assert_eq!(status, 200, "{caps}");
    assert_eq!(caps["data"]["status"]["profile_verified"], false);
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), 0);
    let (status, connected) = server
        .request(
            "POST",
            "/api/v1/sessions/control-fixture/streams/source/start",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{connected}");
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), 1);
    let lease = server
        .submit(json!({"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}))
        .await;
    let lease = server
        .done(lease["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    assert_eq!(lease["state"], "succeeded", "{lease}");
    let secret = lease["result"]["lease_id"].as_str().unwrap();
    let (_, private) = server
        .request(
            "GET",
            &format!(
                "/api/v1/sessions/control-fixture/streams/source/instrument/operations/{}",
                lease["operation_id"].as_str().unwrap()
            ),
            None,
            Some("other-owned-client-0123456789"),
        )
        .await;
    assert!(private["data"]["result"].get("lease_id").is_none());
    let saved = server
        .submit(json!({"operation":"capture","parameters":{"save":true},"lease_id":secret}))
        .await;
    let saved = server
        .done(saved["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    assert_eq!(saved["state"], "succeeded", "{saved}");
    let summary = &saved["result"]["artifact"];
    let response = reqwest::get(format!(
        "{}/api/v1/sessions/control-fixture/streams/source/instrument/artifacts/{}/content",
        server.url,
        summary["id"].as_str().unwrap()
    ))
    .await
    .unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers()["content-type"], "image/png");
    assert!(response.bytes().await.unwrap().starts_with(b"\x89PNG"));
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), 1);
    let release = server
        .submit(json!({"operation":"lease.release","lease_id":secret}))
        .await;
    let release = server
        .done(release["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    assert_eq!(release["state"], "succeeded", "{release}");
    assert_eq!(release["result"]["mode"], "lab");
    assert_eq!(release["result"]["lease_active"], false);
    server.stop().await;
}

fn normalize(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for key in [
                "connection_id",
                "segment_id",
                "started_at",
                "ended_at",
                "occurred_at",
                "created_at",
                "lease_seconds",
                "restart_required",
                "source_sha256",
                "source_format",
                "width",
                "height",
                "acquisition_paused",
                "acquisition_pause_reason",
            ] {
                map.remove(key);
            }
            if map.contains_key("lease_id") {
                map.insert("lease_id".into(), json!("<private synthetic lease>"));
            }
            for value in map.values_mut() {
                normalize(value);
            }
        }
        Value::Array(items) => {
            for value in items {
                normalize(value);
            }
        }
        Value::Number(number) => {
            if let Some(f) = number.as_f64()
                && f.fract() == 0.
                && f >= i64::MIN as f64
                && f <= i64::MAX as f64
            {
                *value = json!(f as i64);
            }
        }
        _ => {}
    }
}
#[tokio::test]
async fn all_29_public_operations_use_real_owned_transport_audio_worker_and_canonical_artifacts() {
    let server = Server::configured(true).await;
    server.setup().await;
    let (status, body) = server
        .request(
            "POST",
            "/api/v1/sessions/control-fixture/streams/source/start",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{body}");
    let mut secret = None;
    let mut kinds = std::collections::BTreeSet::new();
    let fixture = oracle();
    for case in fixture["cases"].as_array().unwrap() {
        let operation = case["operation"].as_str().unwrap();
        kinds.insert(operation);
        let mut request = json!({"operation":operation,"parameters":case["parameters"]});
        if let Some(secret) = &secret {
            request["lease_id"] = json!(secret);
        }
        let from = server.factory.commands.lock().unwrap().len();
        let queued = server.submit(request).await;
        let view = server
            .done(queued["operation_id"].as_str().unwrap(), Some(CLIENT))
            .await;
        assert_eq!(view["state"], "succeeded", "{operation}: {view}");
        let mut actual = view["result"].clone();
        if let Some(token) = actual["lease_id"].as_str() {
            secret = Some(token.to_owned());
        }
        if matches!(operation, "lease.release" | "monitor.resume") {
            secret = None;
        }
        let mut wire = server.factory.commands.lock().unwrap()[from..].to_vec();
        // Monitoring alone may produce its ordinary committed scan before the
        // lab pause/after explicit resume. Lab command order must be exact.
        wire.retain(|s| s != "scan 100M 101M 3 3");
        assert_eq!(
            json!(wire),
            case["commands"],
            "actual public wire order {operation}"
        );
        let mut expected = case["result"].clone();
        if let Some(summary) = actual.get("artifact").cloned() {
            let response = reqwest::get(format!(
                "{}/api/v1/sessions/control-fixture/streams/source/instrument/artifacts/{}/content",
                server.url,
                summary["id"].as_str().unwrap()
            ))
            .await
            .unwrap();
            assert_eq!(response.status(), 200);
            let raw = response.bytes().await.unwrap();
            let original = bytes(&expected["artifact"]["data_base64"]);
            if matches!(operation, "sd.read" | "audio.capture") {
                assert_eq!(
                    raw.as_ref(),
                    original,
                    "original binary payload {operation}"
                );
            } else {
                let mut a = parse_json_value(&raw, 8 * 1024 * 1024).unwrap();
                let mut b = parse_json_value(&original, 8 * 1024 * 1024).unwrap();
                normalize(&mut a);
                normalize(&mut b);
                assert_eq!(a, b, "artifact source/science {operation}");
            }
            for key in ["kind", "mime_type", "filename"] {
                assert_eq!(summary[key], expected["artifact"][key], "{operation} {key}");
            }
            actual.as_object_mut().unwrap().remove("artifact");
            expected.as_object_mut().unwrap().remove("artifact");
        }
        if operation == "capture" {
            let preview = actual.as_object_mut().unwrap().remove("preview").unwrap();
            assert!(bytes(&preview["data_base64"]).starts_with(b"\x89PNG"));
            expected.as_object_mut().unwrap().remove("artifact");
        }
        normalize(&mut actual);
        normalize(&mut expected);
        assert_eq!(actual, expected, "original public result {operation}");
    }
    assert_eq!(kinds.len(), 29);
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), 1);
    let (_, artifacts) = server
        .request(
            "GET",
            "/api/v1/sessions/control-fixture/streams/source/instrument/artifacts",
            None,
            None,
        )
        .await;
    assert!(artifacts["data"]["items"].as_array().unwrap().len() >= 9);
    let (_, events) = server
        .request(
            "GET",
            "/api/v1/events/history?after=0&limit=1000",
            None,
            None,
        )
        .await;
    let succeeded = events["data"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["event_type"] == "instrument.operation.succeeded")
        .count();
    assert_eq!(succeeded, fixture["cases"].as_array().unwrap().len());
    let pauses = events["data"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|event| event["event_type"] == "device.acquisition.paused")
        .collect::<Vec<_>>();
    assert_eq!(
        pauses.len(),
        1,
        "valid processed trace must pause once, not retry/close or enter Sweep history"
    );
    assert_eq!(pauses[0]["payload"]["source_coverage"], "paused");
    let (_, workers) = server
        .request(
            "GET",
            "/api/v1/sessions/control-fixture/workers",
            None,
            None,
        )
        .await;
    assert_eq!(workers["data"][0]["alive"], true);
    assert_eq!(workers["data"][0]["state"], "degraded");
    assert_eq!(workers["data"][0]["current"]["source_coverage"], "paused");
    let source = &pauses[0]["payload"]["artifact"];
    let raw = server
        .client
        .get(format!(
            "{}/api/v1/sessions/control-fixture/streams/source/instrument/artifacts/{}/content",
            server.url,
            source["id"].as_str().unwrap()
        ))
        .send()
        .await
        .unwrap()
        .bytes()
        .await
        .unwrap();
    let retained = parse_json_value(&raw, 8 * 1024 * 1024).unwrap();
    assert_eq!(retained["kind"], "processed_trace");
    assert_eq!(retained["levels"], json!([-90., -80., -70.]));
    server.stop().await;
}

#[tokio::test]
async fn artifact_missing_session_and_receiver_scope_preserve_original_error_precedence() {
    let server = Server::new().await;
    for suffix in ["", "/original", "/original/content"] {
        let (status, error) = server
            .request(
                "GET",
                &format!(
                    "/api/v1/sessions/missing-%C3%A9/streams/source/instrument/artifacts{suffix}"
                ),
                None,
                None,
            )
            .await;
        assert_eq!(status, 404);
        assert_eq!(error["error"]["code"], "instrument_not_found");
        assert_eq!(error["error"]["message"], "'Сеанс не найден: missing-é'");
    }
    let(status,value)=server.request("POST","/api/v1/sessions",Some(json!({"id":"receiver-artifacts","name":"Owned receiver","streams":[{"id":"r","device":"memory","role":"receiver","level_unit":"s_meter_count","settings":{"adapter":"ardv1-sim"}}]})),None).await;
    assert_eq!(status, 201, "{value}");
    let (status, list) = server
        .request(
            "GET",
            "/api/v1/sessions/receiver-artifacts/streams/r/instrument/artifacts",
            None,
            None,
        )
        .await;
    assert_eq!(status, 200, "{list}");
    assert_eq!(list["data"]["items"], json!([]));
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), 0);
    for (query, message) in [
        ("limit=0", "Artifact limit must be 1..100"),
        ("offset=-1", "Artifact offset must be 0..10000000"),
    ] {
        let (status, error) = server
            .request(
                "GET",
                &format!(
                    "/api/v1/sessions/receiver-artifacts/streams/r/instrument/artifacts?{query}"
                ),
                None,
                None,
            )
            .await;
        assert_eq!(status, 400);
        assert_eq!(error["error"]["code"], "instrument_invalid");
        assert_eq!(error["error"]["message"], message);
    }
    let (status, error) = server
        .request(
            "GET",
            "/api/v1/sessions/missing-%E2%80%8B/streams/source/instrument/artifacts",
            None,
            None,
        )
        .await;
    assert_eq!(status, 404);
    assert_eq!(
        error["error"]["message"],
        "'Сеанс не найден: missing-\\u200b'"
    );
    let oracle = parse_json_value(
        include_bytes!("fixtures/control-artifact-query-v1.json"),
        65536,
    )
    .unwrap();
    for case in oracle["cases"].as_array().unwrap() {
        let query = url::form_urlencoded::Serializer::new(String::new())
            .append_pair(
                case["key"].as_str().unwrap(),
                case["value"].as_str().unwrap(),
            )
            .finish();
        let (status, body) = server
            .request(
                "GET",
                &format!(
                    "/api/v1/sessions/receiver-artifacts/streams/r/instrument/artifacts?{query}"
                ),
                None,
                None,
            )
            .await;
        assert_eq!(
            status,
            case["status"].as_u64().unwrap() as u16,
            "{case}: {body}"
        );
        if let Some(error) = case.get("error") {
            assert_eq!(
                &body["error"], error,
                "original Python artifact number/error {case}"
            );
        }
    }
    server.stop().await;
}

#[tokio::test]
async fn rejected_second_clients_revocation_cannot_replace_original_queued_authority() {
    let server = Server::configured_mode(false, true).await;
    server.setup().await;
    let (status, body) = server
        .request(
            "POST",
            "/api/v1/sessions/control-fixture/streams/source/start",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{body}");
    *server.factory.block_command.lock().unwrap() = Some("release".into());
    let queued = server
        .submit(json!({"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}))
        .await;
    tokio::time::timeout(Duration::from_secs(3), server.factory.entered.acquire())
        .await
        .unwrap()
        .unwrap()
        .forget();
    let second = server.pair().await;
    let (status, _) = server
        .request_as(
            "POST",
            "/api/v1/sessions/control-fixture/streams/source/instrument/operations",
            Some(json!({"operation":"invalid-second-client-operation"})),
            Some("owned-second-client-0123456789"),
            Some(&second),
        )
        .await;
    assert_eq!(status, 400);
    let (status, _) = server
        .request_as(
            "POST",
            "/api/v1/security/credentials/current/revoke",
            Some(json!({})),
            None,
            Some(&second),
        )
        .await;
    assert_eq!(status, 200);
    *server.factory.block_command.lock().unwrap() = None;
    server.factory.release.add_permits(1);
    let view = server
        .done(queued["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    server.stop().await;
    assert_eq!(
        view["state"], "succeeded",
        "original first credential/grant must remain the callback authority: {view}"
    );
}

#[tokio::test]
async fn original_credential_revocation_cancels_silent_io_and_never_replays_on_new_credential() {
    let server = Server::configured_mode(false, true).await;
    server.setup().await;
    let (status, body) = server
        .request(
            "POST",
            "/api/v1/sessions/control-fixture/streams/source/start",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{body}");
    let acquired = server
        .submit(json!({"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}))
        .await;
    let acquired = server
        .done(acquired["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    let secret = acquired["result"]["lease_id"].as_str().unwrap();
    let second = server.pair().await;
    *server.factory.block_command.lock().unwrap() = Some("sd_read TRACE.CSV".into());
    let queued = server
        .submit(json!({"operation":"sd.read","lease_id":secret,"request_id":"original-silent-sd"}))
        .await;
    tokio::time::timeout(Duration::from_secs(3), server.factory.entered.acquire())
        .await
        .unwrap()
        .unwrap()
        .forget();
    let (status, _) = server
        .request(
            "POST",
            "/api/v1/security/credentials/current/revoke",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200);
    let id = queued["operation_id"].as_str().unwrap();
    let view = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let (status, value) = server
                .request_as(
                    "GET",
                    &format!(
                        "/api/v1/sessions/control-fixture/streams/source/instrument/operations/{id}"
                    ),
                    None,
                    Some(CLIENT),
                    Some(&second),
                )
                .await;
            assert_eq!(status, 200, "{value}");
            if matches!(
                value["data"]["state"].as_str(),
                Some("unknown" | "failed" | "cancelled")
            ) {
                break value["data"].clone();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    assert!(view["result"].is_null());
    let (status, _) = server
        .request_as(
            "POST",
            "/api/v1/sessions/control-fixture/streams/source/instrument/operations",
            Some(
                json!({"operation":"sd.read","lease_id":secret,"request_id":"original-silent-sd"}),
            ),
            Some(CLIENT),
            Some(&second),
        )
        .await;
    assert!(matches!(status, 409 | 503));
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), 1);
    assert_eq!(server.factory.drops.load(Ordering::SeqCst), 1);
    assert_eq!(
        server
            .factory
            .commands
            .lock()
            .unwrap()
            .iter()
            .filter(|command| *command == "sd_read TRACE.CSV")
            .count(),
        1
    );
    server.stop().await;
}

#[tokio::test]
async fn queued_defaults_bind_once_explicit_stale_fails_before_io_and_idempotency_never_replays() {
    let server = Server::new().await;
    server.setup().await;
    let (status, body) = server
        .request(
            "POST",
            "/api/v1/sessions/control-fixture/streams/source/start",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{body}");
    let (status, error) = server
        .request(
            "POST",
            "/api/v1/sessions/control-fixture/streams/source/instrument/operations",
            Some(json!({"operation":"raw","parameters":{"command":"output on"}})),
            Some(CLIENT),
        )
        .await;
    assert_eq!(status, 400);
    assert_eq!(
        error["error"]["message"],
        "Unknown typed instrument operation"
    );
    let denied = server
        .submit(json!({"operation":"setting","parameters":{"name":"output_enabled","value":true}}))
        .await;
    let denied = server
        .done(denied["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    assert_eq!(denied["state"], "failed");
    assert_eq!(
        denied["error"],
        "Требуется действующая эксклюзивная lab-аренда"
    );
    assert!(
        !server
            .factory
            .commands
            .lock()
            .unwrap()
            .iter()
            .any(|command| command == "output on" || command == "mode output")
    );
    let acquired = server
        .submit(json!({"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}))
        .await;
    let acquired = server
        .done(acquired["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    let secret = acquired["result"]["lease_id"].as_str().unwrap();
    let revision = acquired["result"]["status"]["revision"].as_u64().unwrap();
    *server.factory.block_command.lock().unwrap() = Some("sd_read TRACE.CSV".into());
    let sd = server
        .submit(json!({"operation":"sd.read","lease_id":secret}))
        .await;
    tokio::time::timeout(Duration::from_secs(3), server.factory.entered.acquire())
        .await
        .unwrap()
        .unwrap()
        .forget();
    let payload = json!({"operation":"setting","parameters":{"name":"repeat","value":3},"lease_id":secret,"request_id":"owned-queued-repeat-A"});
    let first = server.submit(payload.clone()).await;
    let duplicate = server.submit(payload).await;
    assert_eq!(first["operation_id"], duplicate["operation_id"]);
    let second=server.submit(json!({"operation":"setting","parameters":{"name":"repeat","value":4},"lease_id":secret,"request_id":"owned-queued-repeat-B"})).await;
    let stale=server.submit(json!({"operation":"setting","parameters":{"name":"repeat","value":5},"lease_id":secret,"expected_revision":revision})).await;
    let(status,error)=server.request("POST","/api/v1/sessions/control-fixture/streams/source/instrument/operations",Some(json!({"operation":"setting","parameters":{"name":"repeat","value":4},"lease_id":secret,"request_id":"owned-queued-repeat-A"})),Some(CLIENT)).await;
    assert_eq!(status, 409);
    assert_eq!(
        error["error"]["message"],
        "request_id identifies another operation"
    );
    let(status,_)=server.request("POST","/api/v1/sessions/control-fixture/streams/source/instrument/operations",Some(json!({"operation":"setting","parameters":{"name":"repeat","value":6},"lease_id":secret})),Some(CLIENT)).await;
    assert_eq!(
        status, 409,
        "full normal queue must explicitly reject before any wire effect"
    );
    *server.factory.block_command.lock().unwrap() = None;
    server.factory.release.add_permits(1);
    let sd = server
        .done(sd["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    assert_eq!(sd["state"], "succeeded");
    let first = server
        .done(first["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    let second = server
        .done(second["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    let stale = server
        .done(stale["operation_id"].as_str().unwrap(), Some(CLIENT))
        .await;
    assert_eq!(first["state"], "succeeded", "{first}");
    assert_eq!(second["state"], "succeeded", "{second}");
    assert_eq!(stale["state"], "failed", "{stale}");
    assert_eq!(first["result"]["revision"], revision + 1);
    assert_eq!(second["result"]["revision"], revision + 2);
    let wire = server.factory.commands.lock().unwrap().clone();
    assert_eq!(wire.iter().filter(|s| *s == "repeat 3").count(), 1);
    assert_eq!(wire.iter().filter(|s| *s == "repeat 4").count(), 1);
    assert!(!wire.iter().any(|s| s == "repeat 5" || s == "repeat 6"));
    server.stop().await;
}

async fn done_at(server: &Server, base: &str, id: &str) -> Value {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let (status, value) = server
                .request(
                    "GET",
                    &format!("{base}/instrument/operations/{id}"),
                    None,
                    Some(CLIENT),
                )
                .await;
            assert_eq!(status, 200, "{value}");
            if matches!(
                value["data"]["state"].as_str(),
                Some("succeeded" | "failed" | "unknown" | "cancelled")
            ) {
                return value["data"].clone();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap()
}
#[tokio::test]
async fn explicit_guard_analyzer_handoff_binds_once_and_keeps_actual_acquisition_paused() {
    let server = Server::new().await;
    let session = json!({"id":"manual-control","name":"Owned serial Guard handoff","streams":[
        {"id":"a","device":"Owned tinySA","role":"spectrum_analyzer","level_unit":"dBm","port":"owned-analyzer-memory","settings":{"adapter":"tinysa-serial","start_hz":100000000,"stop_hz":101000000,"points":3,"rbw_hz":10000,"period_s":0.01,"monitoring_instrument_context":"inspect"}},
        {"id":"r","device":"Owned AR-DV1","role":"receiver","level_unit":"s_meter_count","port":"owned-receiver-memory","settings":{"adapter":"ardv1-serial","frequency_hz":145500000,"mode":"FM","period_s":0.05}}
    ]});
    let (status, value) = server
        .request("POST", "/api/v1/sessions", Some(session), None)
        .await;
    assert_eq!(status, 201, "{value}");
    let (status, value) = server
        .request(
            "POST",
            "/api/v1/sessions/manual-control/start",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{value}");
    let (status, value) = server
        .request(
            "POST",
            "/api/v1/sessions/manual-control/streams/a/start",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{value}");
    let original = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let (_, value) = server
                .request(
                    "GET",
                    "/api/v1/sessions/manual-control/sweeps?limit=1",
                    None,
                    None,
                )
                .await;
            if let Some(item) = value["data"].as_array().and_then(|v| v.first()) {
                break item.clone();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    let (status, value) = server
        .request(
            "POST",
            "/api/v1/sessions/manual-control/streams/a/stop",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{value}");
    for index in 0..20 {
        let mut sample = original.clone();
        sample["id"] = json!(format!("manual-baseline-{index:02}"));
        let at = chrono::Utc::now() - chrono::Duration::seconds(80 - index * 4);
        sample["interval"] = json!({"started_at":at.to_rfc3339_opts(chrono::SecondsFormat::Micros,true),"ended_at":(at+chrono::Duration::milliseconds(1)).to_rfc3339_opts(chrono::SecondsFormat::Micros,true)});
        let (status, value) = server
            .request(
                "POST",
                "/api/v1/sessions/manual-control/sweeps",
                Some(sample),
                None,
            )
            .await;
        assert_eq!(status, 201, "{value}");
    }
    let(status,project)=server.request("POST","/api/v1/monitoring-projects",Some(json!({"name":"Explicit manual fixture","site":"Owned memory only","session_id":"manual-control"})),None).await;
    assert_eq!(status, 201, "{project}");
    let project = encode_path_segment(project["data"]["id"].as_str().unwrap());
    let project = format!("/api/v1/monitoring-projects/{project}");
    for (path, payload, status) in [
        (
            "preparation",
            json!({"antenna_notes":"Owned memory fixture","receive_path_confirmed":true,"location_confirmed":true}),
            200,
        ),
        (
            "baselines",
            json!({"name":"Owned baseline","location":"Memory","confirm":true}),
            201,
        ),
        ("arm", json!({}), 200),
    ] {
        let (actual, value) = server
            .request("POST", &format!("{project}/{path}"), Some(payload), None)
            .await;
        assert_eq!(actual, status, "{path}: {value}");
    }
    let (_, armed) = server.request("GET", &project, None, None).await;
    let sid = encode_path_segment(armed["data"]["active_session_id"].as_str().unwrap());
    let stream = armed["data"]["analyzer_stream_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let base = format!(
        "/api/v1/sessions/{sid}/streams/{}",
        encode_path_segment(&stream)
    );
    let (status, manual) = server
        .request(
            "POST",
            &format!("{project}/manual/acquire"),
            Some(json!({"role":"analyzer"})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{manual}");
    let token = manual["data"]["token"].as_str().unwrap().to_owned();
    let opens = server.factory.opens.load(Ordering::SeqCst);
    let (status, _) = server
        .request("POST", &format!("{base}/start"), Some(json!({})), None)
        .await;
    assert_eq!(status, 409);
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), opens);
    let (status, connected) = server
        .request(
            "POST",
            &format!("{base}/start"),
            Some(json!({"control_owner":token})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{connected}");
    assert_eq!(connected["data"]["current"]["acquisition_paused"], true);
    assert_eq!(connected["data"]["measurements"], 0);
    let(status,queued)=server.request("POST",&format!("{base}/instrument/operations"),Some(json!({"operation":"lease.acquire","parameters":{"acknowledge_rf":true},"control_owner":token})),Some(CLIENT)).await;
    assert_eq!(status, 202, "{queued}");
    let acquired = done_at(
        &server,
        &base,
        queued["data"]["operation_id"].as_str().unwrap(),
    )
    .await;
    assert_eq!(acquired["state"], "succeeded", "{acquired}");
    let secret = acquired["result"]["lease_id"].as_str().unwrap();
    let (status, queued) = server
        .request(
            "POST",
            &format!("{base}/instrument/operations"),
            Some(json!({"operation":"monitor.resume","lease_id":secret,"control_owner":token})),
            Some(CLIENT),
        )
        .await;
    assert_eq!(status, 202, "{queued}");
    let resumed = done_at(
        &server,
        &base,
        queued["data"]["operation_id"].as_str().unwrap(),
    )
    .await;
    assert_eq!(resumed["state"], "succeeded", "{resumed}");
    tokio::time::sleep(Duration::from_millis(40)).await;
    let (_, workers) = server
        .request(
            "GET",
            &format!("/api/v1/sessions/{sid}/workers"),
            None,
            None,
        )
        .await;
    let analyzer = workers["data"]
        .as_array()
        .unwrap()
        .iter()
        .find(|w| w["stream_id"] == stream)
        .unwrap();
    assert_eq!(analyzer["measurements"], 0);
    assert_eq!(analyzer["current"]["acquisition_paused"], true);
    let (_, paused) = server.request("GET", &project, None, None).await;
    assert_eq!(paused["data"]["coverage"], "paused");
    let (status, stopped) = server
        .request(
            "POST",
            &format!("{base}/stop"),
            Some(json!({"control_owner":token})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{stopped}");
    let opens = server.factory.opens.load(Ordering::SeqCst);
    // The independently owned receiver continues LM/MD polling. This invariant
    // is specifically that the rejected analyzer re-open sends no analyzer I/O.
    let analyzer_count = |wire: &[String]| {
        wire.iter()
            .filter(|command| {
                command
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_lowercase)
            })
            .count()
    };
    let count = analyzer_count(&server.factory.commands.lock().unwrap());
    let (status, _) = server
        .request(
            "POST",
            &format!("{base}/start"),
            Some(json!({"control_owner":token})),
            None,
        )
        .await;
    assert_eq!(status, 409);
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), opens);
    assert_eq!(
        analyzer_count(&server.factory.commands.lock().unwrap()),
        count,
        "once-bound token must deny before open/prepare I/O"
    );
    let (status, returned) = server
        .request(
            "POST",
            &format!("{project}/manual/return"),
            Some(json!({"token":token})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{returned}");
    tokio::time::timeout(Duration::from_secs(4), async {
        loop {
            let (_, p) = server.request("GET", &project, None, None).await;
            if p["data"]["coverage"] != "restoring" {
                assert_ne!(p["data"]["coverage"], "paused", "{p}");
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let (status, value) = server
        .request("POST", &format!("{project}/disarm"), Some(json!({})), None)
        .await;
    assert_eq!(status, 200, "{value}");
    server.stop().await;
}

#[tokio::test]
async fn actual_lan_receiver_silent_tune_revocation_wakes_original_and_joins_before_error() {
    let server = Server::configured_mode(false, true).await;
    let(status,value)=server.request("POST","/api/v1/sessions",Some(json!({"id":"silent-receiver","name":"Owned silent receiver","streams":[{"id":"r","device":"Owned AR-DV1","role":"receiver","level_unit":"s_meter_count","port":"owned-receiver","settings":{"adapter":"ardv1-serial","frequency_hz":145500000,"mode":"FM","period_s":0.05}}]})),None).await;
    assert_eq!(status, 201, "{value}");
    let (status, value) = server
        .request(
            "POST",
            "/api/v1/sessions/silent-receiver/start",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200, "{value}");
    let base = "/api/v1/sessions/silent-receiver/streams/r";
    let (status, value) = server
        .request("POST", &format!("{base}/start"), Some(json!({})), None)
        .await;
    assert_eq!(status, 200, "{value}");
    let reader = server.pair().await;
    *server.factory.block_command.lock().unwrap() = Some("RF0146.50000".into());
    let client = server.client.clone();
    let url = format!("{}{base}/tune", server.url);
    let token = server.bearer.clone().unwrap();
    let tuning = tokio::spawn(async move {
        client
            .post(url)
            .bearer_auth(token)
            .json(&json!({"frequency_hz":146500000,"mode":"FM"}))
            .send()
            .await
            .unwrap()
    });
    tokio::time::timeout(Duration::from_secs(3), server.factory.entered.acquire())
        .await
        .unwrap()
        .unwrap()
        .forget();
    let (status, _) = server
        .request(
            "POST",
            "/api/v1/security/credentials/current/revoke",
            Some(json!({})),
            None,
        )
        .await;
    assert_eq!(status, 200);
    let response = tokio::time::timeout(Duration::from_secs(3), tuning)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(response.status(), 409);
    assert_eq!(
        server.factory.drops.load(Ordering::SeqCst),
        1,
        "HTTP denial must follow actual original native drop"
    );
    let (status, workers) = server
        .request_as(
            "GET",
            "/api/v1/sessions/silent-receiver/workers",
            None,
            None,
            Some(&reader),
        )
        .await;
    assert_eq!(status, 200);
    assert_eq!(workers["data"][0]["alive"], false);
    let (_, events) = server
        .request_as(
            "GET",
            "/api/v1/events/history?limit=1000",
            None,
            None,
            Some(&reader),
        )
        .await;
    assert_eq!(
        events["data"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|e| e["event_type"] == "device.tuned")
            .count(),
        0
    );
    *server.factory.block_command.lock().unwrap() = None;
    let (status, worker) = server
        .request_as(
            "POST",
            &format!("{base}/start"),
            Some(json!({})),
            None,
            Some(&reader),
        )
        .await;
    assert_eq!(status, 200, "{worker}");
    assert_eq!(worker["data"]["alive"], true);
    assert_eq!(server.factory.opens.load(Ordering::SeqCst), 2);
    assert_eq!(server.factory.drops.load(Ordering::SeqCst), 1);
    server.stop().await;
}
