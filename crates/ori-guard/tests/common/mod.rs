#![allow(dead_code)]
use ori_archive::{Archive, ArchiveConfig};
use ori_contracts::{JSONObject, Session, UtcTimestamp};
use ori_guard::*;
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU64, Ordering},
};

pub fn fixture() -> Value {
    ori_contracts::parse_json_value(
        include_bytes!("../../../../contracts/fixtures/guard-domain-v1.json"),
        1024 * 1024,
    )
    .unwrap()
}
pub struct Sources {
    pub now: Mutex<UtcTimestamp>,
    ids: AtomicU64,
    monotonic: AtomicU64,
}
impl Sources {
    pub fn fixed() -> Arc<Self> {
        Arc::new(Self {
            now: Mutex::new(fixture()["clock"].as_str().unwrap().parse().unwrap()),
            ids: AtomicU64::new(0),
            monotonic: AtomicU64::new(0),
        })
    }
    pub fn advance(&self, seconds: u64) {
        self.monotonic.fetch_add(seconds, Ordering::SeqCst);
    }
}
impl Clock for Sources {
    fn now(&self) -> UtcTimestamp {
        self.now.lock().unwrap().clone()
    }
    fn monotonic(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.monotonic.load(Ordering::SeqCst))
    }
}
impl IdSource for Sources {
    fn next_id(&self, kind: IdKind) -> String {
        let n = self.ids.fetch_add(1, Ordering::SeqCst) + 1;
        match kind {
            IdKind::Record | IdKind::Owner => format!("{n:032x}"),
            _ => format!("fixture-{kind:?}-{n:032x}"),
        }
    }
}
pub struct FakeControl {
    pub calls: Mutex<Vec<String>>,
    pub workers: Mutex<Vec<WorkerStatus>>,
    pub ready: Mutex<Option<bool>>,
    pub storage: Mutex<StorageProbe>,
    pub permits: Mutex<Vec<(ControlTarget, ControlPermit)>>,
    pub acknowledged: Mutex<Option<bool>>,
    pub service: Mutex<std::sync::Weak<GuardService>>,
    pub sweep_on_start: Mutex<Option<(Archive, Value)>>,
    pub fail_stop: Mutex<bool>,
    pub delivery_success: Mutex<bool>,
    pub deliveries: Mutex<Vec<WebhookRequest>>,
    pub tune_gate: Mutex<Option<Arc<TuneGate>>>,
    pub stop_gate: Mutex<Option<Arc<TuneGate>>>,
    pub stream_stop_gate: Mutex<Option<Arc<TuneGate>>>,
    pub stream_stop_acknowledged: Mutex<bool>,
    pub tune_revision: AtomicU64,
    pub audio_devices: Mutex<Option<AudioInputs>>,
    pub audio_requests: Mutex<Vec<AudioRequest>>,
    pub audio_gate: Mutex<Option<Arc<TuneGate>>>,
    pub audio_recording: Mutex<Option<RecordedAudio>>,
}
pub struct TuneGate {
    pub entered: tokio::sync::Notify,
    pub release: tokio::sync::Notify,
}
impl TuneGate {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            entered: tokio::sync::Notify::new(),
            release: tokio::sync::Notify::new(),
        })
    }
}
impl FakeControl {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            calls: Mutex::new(vec![]),
            workers: Mutex::new(vec![]),
            ready: Mutex::new(Some(true)),
            storage: Mutex::new(StorageProbe {
                filesystem_free_bytes: Some(1_000_000_000_000),
                writable: Some(true),
            }),
            permits: Mutex::new(vec![]),
            acknowledged: Mutex::new(Some(true)),
            service: Mutex::new(std::sync::Weak::new()),
            sweep_on_start: Mutex::new(None),
            fail_stop: Mutex::new(false),
            delivery_success: Mutex::new(false),
            deliveries: Mutex::new(vec![]),
            tune_gate: Mutex::new(None),
            stop_gate: Mutex::new(None),
            stream_stop_gate: Mutex::new(None),
            stream_stop_acknowledged: Mutex::new(true),
            tune_revision: AtomicU64::new(0),
            audio_devices: Mutex::new(None),
            audio_requests: Mutex::new(vec![]),
            audio_gate: Mutex::new(None),
            audio_recording: Mutex::new(None),
        })
    }
    fn worker(&self, session: &Session, stream_id: &str) -> WorkerStatus {
        WorkerStatus {
            session_id: session.id.clone(),
            stream_id: stream_id.to_owned(),
            state: "running".into(),
            alive: true,
            current: JSONObject::new(),
            extra: [("device".into(), json!("synthetic"))].into(),
            worker_instance: Some(format!("instance-{stream_id}")),
        }
    }
    fn validate(&self, target: &ControlTarget, permit: &ControlPermit) -> ControlResult<()> {
        self.service
            .lock()
            .unwrap()
            .upgrade()
            .ok_or(ControlError::Denied)?
            .authorize_control(target, Some(permit))?;
        if let Some(expected) = permit.expected_worker_instance() {
            let workers = self.workers.lock().unwrap();
            if !workers.iter().any(|w| {
                w.session_id == target.session_id
                    && w.stream_id == target.stream_id
                    && w.worker_instance.as_deref() == Some(expected)
            }) {
                return Err(ControlError::Denied);
            }
        }
        Ok(())
    }
}
impl InstrumentControl for FakeControl {
    fn preflight<'a>(&'a self, _: &'a Session) -> ControlFuture<'a, InstrumentPreflight> {
        Box::pin(async {
            Ok(InstrumentPreflight {
                ready: *self.ready.lock().unwrap(),
                checks: vec![],
            })
        })
    }
    fn list_workers<'a>(&'a self, sid: &'a str) -> ControlFuture<'a, Vec<WorkerStatus>> {
        Box::pin(async move {
            Ok(self
                .workers
                .lock()
                .unwrap()
                .iter()
                .filter(|w| w.session_id == sid)
                .cloned()
                .collect())
        })
    }
    fn start_session<'a>(
        &'a self,
        s: &'a Session,
        permit: ControlPermit,
    ) -> ControlFuture<'a, Vec<WorkerStatus>> {
        Box::pin(async move {
            let mut out = vec![];
            for stream in &s.streams {
                let target = ControlTarget {
                    session_id: s.id.clone(),
                    stream_id: stream.id.clone(),
                };
                self.validate(&target, &permit)?;
                self.permits.lock().unwrap().push((target, permit.clone()));
                out.push(self.worker(s, &stream.id));
            }
            self.calls.lock().unwrap().push(format!("start:{}", s.id));
            self.workers.lock().unwrap().extend(out.clone());
            Ok(out)
        })
    }
    fn stop_session<'a>(&'a self, sid: &'a str) -> ControlFuture<'a, Vec<WorkerStatus>> {
        Box::pin(async move {
            self.calls.lock().unwrap().push(format!("stop:{sid}"));
            let gate = self.stop_gate.lock().unwrap().clone();
            if let Some(gate) = gate {
                gate.entered.notify_one();
                gate.release.notified().await;
            }
            if *self.fail_stop.lock().unwrap() {
                return Err(ControlError::Failed("synthetic stop failure".into()));
            }
            let mut workers = self.workers.lock().unwrap();
            for w in workers.iter_mut().filter(|w| w.session_id == sid) {
                w.alive = false;
                w.state = "stopped".into();
            }
            Ok(workers
                .iter()
                .filter(|w| w.session_id == sid)
                .cloned()
                .collect())
        })
    }
    fn start_stream<'a>(
        &'a self,
        s: &'a Session,
        id: &'a str,
        p: ControlPermit,
    ) -> ControlFuture<'a, WorkerStatus> {
        Box::pin(async move {
            let target = ControlTarget {
                session_id: s.id.clone(),
                stream_id: id.into(),
            };
            self.validate(&target, &p)?;
            let w = self.worker(s, id);
            {
                let mut workers = self.workers.lock().unwrap();
                workers.retain(|x| x.session_id != s.id || x.stream_id != id);
                workers.push(w.clone());
            }
            let sample = self.sweep_on_start.lock().unwrap().take();
            if let Some((archive, mut sweep)) = sample {
                sweep["session_id"] = json!(s.id);
                sweep["stream_id"] = json!(id);
                archive
                    .append_sweeps(vec![sweep])
                    .await
                    .map_err(|e| ControlError::Failed(e.to_string()))?;
            }
            Ok(w)
        })
    }
    fn stop_stream<'a>(&'a self, t: &'a ControlTarget) -> ControlFuture<'a, WorkerStatus> {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .push(format!("stop-stream:{}", t.stream_id));
            let gate = self.stream_stop_gate.lock().unwrap().clone();
            if let Some(gate) = gate {
                gate.entered.notify_one();
                gate.release.notified().await;
            }
            let mut workers = self.workers.lock().unwrap();
            let w = workers
                .iter_mut()
                .find(|x| x.session_id == t.session_id && x.stream_id == t.stream_id)
                .ok_or(ControlError::Unavailable("worker"))?;
            if !*self.stream_stop_acknowledged.lock().unwrap() {
                return Ok(w.clone());
            }
            w.alive = false;
            w.state = "stopped".into();
            Ok(w.clone())
        })
    }
    fn tune<'a>(&'a self, r: TuneRequest) -> ControlFuture<'a, TuneReply> {
        Box::pin(async move {
            self.validate(&r.target, &r.permit)?;
            let gate = self.tune_gate.lock().unwrap().clone();
            if let Some(gate) = gate {
                gate.entered.notify_one();
                gate.release.notified().await;
                self.validate(&r.target, &r.permit)?;
            }
            self.calls.lock().unwrap().push(format!(
                "tune:{}:{}:{}",
                r.target.stream_id, r.frequency_hz, r.mode
            ));
            let w = self
                .workers
                .lock()
                .unwrap()
                .iter()
                .find(|x| x.session_id == r.target.session_id && x.stream_id == r.target.stream_id)
                .cloned()
                .ok_or(ControlError::Unavailable("worker"))?;
            Ok(TuneReply {
                acknowledged: *self.acknowledged.lock().unwrap(),
                tuned: [
                    ("frequency_hz".into(), json!(r.frequency_hz)),
                    ("mode".into(), json!(r.mode)),
                ]
                .into(),
                worker: w,
                acknowledged_at: fixture()["clock"].as_str().unwrap().parse().unwrap(),
                applied_tune_revision: self.tune_revision.fetch_add(1, Ordering::SeqCst) + 1,
            })
        })
    }
    fn storage_probe(&self) -> ControlFuture<'_, StorageProbe> {
        Box::pin(async { Ok(self.storage.lock().unwrap().clone()) })
    }
    fn audio_inputs(&self) -> ControlFuture<'_, AudioInputs> {
        Box::pin(async {
            self.audio_devices
                .lock()
                .unwrap()
                .clone()
                .ok_or(ControlError::Unavailable("audio_inputs"))
        })
    }
    fn record_audio<'a>(&'a self, request: AudioRequest) -> ControlFuture<'a, RecordedAudio> {
        Box::pin(async move {
            self.validate(&request.target, &request.permit)?;
            self.validate_tune(&request.tune_reply)?;
            self.audio_requests.lock().unwrap().push(request.clone());
            let gate = self.audio_gate.lock().unwrap().clone();
            if let Some(gate) = gate {
                gate.entered.notify_one();
                gate.release.notified().await;
            }
            self.validate(&request.target, &request.permit)?;
            self.validate_tune(&request.tune_reply)?;
            self.audio_recording
                .lock()
                .unwrap()
                .clone()
                .ok_or(ControlError::Unavailable("record_audio"))
        })
    }
    fn validate_tune(&self, reply: &TuneReply) -> ControlResult<()> {
        if reply.acknowledged != Some(true)
            || reply.applied_tune_revision != self.tune_revision.load(Ordering::SeqCst)
        {
            return Err(ControlError::Denied);
        }
        let workers = self.workers.lock().unwrap();
        let current = workers
            .iter()
            .find(|w| {
                w.session_id == reply.worker.session_id && w.stream_id == reply.worker.stream_id
            })
            .ok_or(ControlError::Denied)?;
        if !current.alive
            || current.worker_instance.is_none()
            || current.worker_instance != reply.worker.worker_instance
        {
            return Err(ControlError::Denied);
        }
        Ok(())
    }
    fn deliver_webhook<'a>(
        &'a self,
        request: WebhookRequest,
    ) -> ControlFuture<'a, DeliveryReceipt> {
        Box::pin(async move {
            self.deliveries.lock().unwrap().push(request);
            let success = *self.delivery_success.lock().unwrap();
            Ok(DeliveryReceipt {
                delivered: Some(success),
                status: Some(if success { 204 } else { 503 }),
            })
        })
    }
}
pub struct Harness {
    pub dir: tempfile::TempDir,
    pub archive: Archive,
    pub service: Arc<GuardService>,
    pub control: Arc<FakeControl>,
    pub sources: Arc<Sources>,
}
impl Harness {
    pub async fn new() -> Self {
        Self::with_session(fixture()["session"].clone()).await
    }
    pub async fn with_session(session: Value) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let archive = Archive::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
        let control = FakeControl::new();
        let sources = Sources::fixed();
        let service = GuardService::with_sources(
            archive.clone(),
            control.clone(),
            sources.clone(),
            sources.clone(),
        );
        *control.service.lock().unwrap() = Arc::downgrade(&service);
        archive.create_session(session).await.unwrap();
        Self {
            dir,
            archive,
            service,
            control,
            sources,
        }
    }
    pub async fn prepared(&self) -> String {
        let f = fixture();
        let p = self
            .service
            .create(json!({"name":"Room α","site":"Cité+%/?","session_id":f["session"]["id"]}))
            .await
            .unwrap();
        let id = p["id"].as_str().unwrap();
        self.service.save_preparation(id,json!({"antenna_notes":"RX fixture","receive_path_confirmed":true,"location_confirmed":true})).await.unwrap();
        self.service.add_allowed(id,json!({"name":"Known carrier","start_hz":100060000,"end_hz":100070000,"comment":"Original units","watch_changes":true})).await.unwrap();
        self.archive
            .append_sweeps(f["sweeps"].as_array().unwrap()[..20].to_vec())
            .await
            .unwrap();
        self.service
            .create_baseline(id, json!({"name":"Quiet","location":"Roof","confirm":true}))
            .await
            .unwrap();
        id.to_owned()
    }
    pub async fn close(self) {
        self.service.close().await.unwrap();
        self.archive.close().await.unwrap();
    }
}
