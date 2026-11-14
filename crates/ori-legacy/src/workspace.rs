use crate::{
    sql::{PanOwner, Snapshot},
    *,
};
use ori_contracts::{parse_json_value, to_json_vec};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::sync::{Notify, mpsc, oneshot, watch};

type Bytes = Arc<[u8]>;
struct Cache {
    full: Bytes,
    stream: Bytes,
    signals: Bytes,
    working: Bytes,
    version: u64,
    database: Option<String>,
}
fn encoded(value: &Value, limit: usize) -> Result<Bytes> {
    let bytes = to_json_vec(value)?;
    if bytes.len() > limit {
        return Err(LegacyError::ResourceLimit(
            "Legacy result budget exceeded".into(),
        ));
    }
    Ok(bytes.into())
}
impl Cache {
    fn empty(limits: &LegacyLimits) -> Result<Self> {
        Self::new(
            json!({"version":0,"database":null,"signals":[],"threshold":null,"panoramas":0,"current":null,"max":null,"min":null,"average":null,"waterfall":[],"points":[],"settings":null}),
            None,
            0,
            limits,
        )
    }
    fn new(
        mut state: Value,
        database: Option<&str>,
        version: u64,
        limits: &LegacyLimits,
    ) -> Result<Self> {
        if state.get("database").and_then(Value::as_str) != database
            || state.get("version").and_then(Value::as_u64) != Some(version)
            || !state.get("signals").is_some_and(Value::is_array)
            || !state
                .as_object()
                .is_some_and(|value| value.contains_key("threshold"))
        {
            return Err(LegacyError::Protocol("state identity/version"));
        }
        let full = encoded(&state, limits.max_result_bytes)?;
        let signals = encoded(
            &json!({"signals":state["signals"]}),
            limits.max_result_bytes,
        )?;
        let working = encoded(
            &json!({"threshold":state["threshold"],"signals":state["signals"]}),
            limits.max_result_bytes,
        )?;
        if database.is_some() {
            state
                .as_object_mut()
                .ok_or(LegacyError::Protocol("state object"))?
                .remove("waterfall");
        }
        let stream = encoded(&state, limits.max_result_bytes)?;
        if full.len() + signals.len() + working.len() + stream.len() > limits.max_cache_bytes {
            return Err(LegacyError::ResourceLimit(
                "Legacy working-state budget exceeded".into(),
            ));
        }
        Ok(Self {
            full,
            stream,
            signals,
            working,
            version,
            database: database.map(str::to_owned),
        })
    }
}
enum Command {
    Load(PathBuf),
    Threshold(Value),
    Query(LegacyQuery),
}
enum Answer {
    Value(Value),
    Json(Bytes),
}
struct Envelope {
    command: Command,
    reply: oneshot::Sender<Result<Answer>>,
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum CloseState {
    Open,
    Closed,
    Failed,
}
struct Control {
    closing: AtomicBool,
    active: Mutex<Option<LegacyCancellation>>,
    wake: Notify,
    closed: watch::Sender<CloseState>,
    joined: watch::Sender<CloseState>,
    leases: Mutex<Vec<Arc<std::fs::File>>>,
}
struct Inner {
    sender: mpsc::Sender<Envelope>,
    control: Arc<Control>,
    version: watch::Receiver<u64>,
    close_timeout: std::time::Duration,
}
impl Drop for Inner {
    fn drop(&mut self) {
        self.control.closing.store(true, Ordering::SeqCst);
        if let Ok(active) = self.control.active.lock()
            && let Some(active) = active.as_ref()
        {
            active.cancel();
        }
        self.control.wake.notify_one();
    }
}
#[derive(Clone)]
pub struct LegacyWorkspace {
    inner: Arc<Inner>,
}
impl LegacyWorkspace {
    pub async fn new(options: LegacyOptions, science: Arc<dyn LegacyScience>) -> Result<Self> {
        options.validate()?;
        let cache = Cache::empty(&options.limits)?;
        let (sender, receiver) = mpsc::channel(options.queue_capacity);
        let (version_tx, version) = watch::channel(0);
        let (closed, _) = watch::channel(CloseState::Open);
        let (joined, _) = watch::channel(CloseState::Open);
        let control = Arc::new(Control {
            closing: AtomicBool::new(false),
            active: Mutex::new(None),
            wake: Notify::new(),
            closed,
            joined,
            leases: Mutex::new(Vec::new()),
        });
        let state = State {
            options: options.clone(),
            science,
            owner: None,
            retired: Vec::new(),
            data_version: 0,
            cache,
            keywords: None,
            poisoned: false,
            control: control.clone(),
            version_tx,
        };
        let join = tokio::spawn(state.run(receiver));
        let runtime = tokio::runtime::Handle::current();
        let keeper = control.clone();
        // This keeper survives a cancelled close future, actor panic or dropped
        // public handle. Only a confirmed actor JOIN releases remaining leases.
        std::thread::spawn(move || match runtime.block_on(join) {
            Ok(()) => {
                if let Ok(mut leases) = keeper.leases.lock() {
                    leases.clear();
                }
                keeper.joined.send_replace(CloseState::Closed);
            }
            Err(_) => {
                keeper.joined.send_replace(CloseState::Failed);
                std::mem::forget(keeper);
            }
        });
        Ok(Self {
            inner: Arc::new(Inner {
                sender,
                control,
                version,
                close_timeout: options.close_timeout,
            }),
        })
    }
    pub fn version(&self) -> u64 {
        *self.inner.version.borrow()
    }
    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.inner.version.clone()
    }
    async fn request(&self, command: Command) -> Result<Answer> {
        if self.inner.control.closing.load(Ordering::SeqCst) {
            return Err(LegacyError::Closed);
        }
        let (reply, result) = oneshot::channel();
        self.inner
            .sender
            .try_send(Envelope { command, reply })
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => LegacyError::Busy,
                mpsc::error::TrySendError::Closed(_) => LegacyError::Closed,
            })?;
        result.await.map_err(|_| LegacyError::CleanupPending)?
    }
    pub async fn load_database(&self, path: &Path) -> Result<Value> {
        if path.as_os_str().len() > 4096 {
            return Err(LegacyError::Request(
                "Укажите существующую базу панорам .pan".into(),
            ));
        }
        match self.request(Command::Load(path.to_owned())).await? {
            Answer::Value(value) => Ok(value),
            _ => Err(LegacyError::Protocol("load result")),
        }
    }
    pub async fn threshold(&self, payload: Value) -> Result<Value> {
        validate_threshold_control(&payload)?;
        match self.request(Command::Threshold(payload)).await? {
            Answer::Value(value) => Ok(value),
            _ => Err(LegacyError::Protocol("threshold result")),
        }
    }
    pub async fn query_json(&self, query: LegacyQuery) -> Result<Bytes> {
        match self.request(Command::Query(query)).await? {
            Answer::Json(value) => Ok(value),
            _ => Err(LegacyError::Protocol("query result")),
        }
    }
    async fn query(&self, query: LegacyQuery) -> Result<Value> {
        Ok(parse_json_value(
            &self.query_json(query).await?,
            16 * 1024 * 1024,
        )?)
    }
    pub async fn snapshot(&self, include_waterfall: bool) -> Result<Value> {
        self.query(LegacyQuery::State { include_waterfall }).await
    }
    pub async fn signals(&self) -> Result<Value> {
        self.query(LegacyQuery::Signals).await
    }
    pub async fn coverage(&self, rows: u32, cols: u32) -> Result<Value> {
        self.query(LegacyQuery::Coverage { rows, cols }).await
    }
    pub async fn occupancy(&self, threshold_db: Option<f64>) -> Result<Value> {
        self.query(LegacyQuery::Occupancy { threshold_db }).await
    }
    pub async fn efficiency(&self, threshold_db: Option<f64>) -> Result<Value> {
        self.query(LegacyQuery::Efficiency { threshold_db }).await
    }
    pub async fn measurements(
        &self,
        start_hz: f64,
        stop_hz: f64,
        norm_hz: Option<f64>,
    ) -> Result<Value> {
        self.query(LegacyQuery::Measurements {
            start_hz,
            stop_hz,
            norm_hz,
        })
        .await
    }
    pub async fn keywords(&self) -> Result<Value> {
        self.query(LegacyQuery::Keywords).await
    }
    pub async fn close(&self) -> Result<()> {
        self.inner.control.closing.store(true, Ordering::SeqCst);
        if let Some(active) = self
            .inner
            .control
            .active
            .lock()
            .map_err(|_| LegacyError::CleanupPending)?
            .as_ref()
        {
            active.cancel();
        }
        let mut done = self.inner.control.joined.subscribe();
        let mut cleanup = self.inner.control.closed.subscribe();
        if *cleanup.borrow() == CloseState::Failed {
            self.inner.control.closed.send_replace(CloseState::Open);
        }
        self.inner.control.wake.notify_one();
        tokio::time::timeout(self.inner.close_timeout, async {
            loop {
                match *done.borrow_and_update() {
                    CloseState::Closed => break,
                    CloseState::Failed => return Err(LegacyError::CleanupPending),
                    CloseState::Open => {}
                }
                if *cleanup.borrow_and_update()==CloseState::Failed {return Err(LegacyError::CleanupPending);}
                tokio::select! {value=done.changed()=>value.map_err(|_|LegacyError::CleanupPending)?,value=cleanup.changed()=>value.map_err(|_|LegacyError::CleanupPending)?};
            }
            Ok(())
        })
        .await
        .map_err(|_| LegacyError::CleanupPending)?
    }
}

struct State {
    options: LegacyOptions,
    science: Arc<dyn LegacyScience>,
    owner: Option<PanOwner>,
    retired: Vec<PanOwner>,
    data_version: i64,
    cache: Cache,
    keywords: Option<Bytes>,
    poisoned: bool,
    control: Arc<Control>,
    version_tx: watch::Sender<u64>,
}
impl State {
    fn retain_owner(&self, owner: &PanOwner) -> Result<()> {
        self.control
            .leases
            .lock()
            .map_err(|_| LegacyError::CleanupPending)?
            .push(owner.lease());
        Ok(())
    }
    fn release_owner(&self, owner: &PanOwner) -> Result<()> {
        let lease = owner.lease();
        self.control
            .leases
            .lock()
            .map_err(|_| LegacyError::CleanupPending)?
            .retain(|value| !Arc::ptr_eq(value, &lease));
        Ok(())
    }
    async fn run(mut self, mut receiver: mpsc::Receiver<Envelope>) {
        loop {
            if self.control.closing.load(Ordering::SeqCst) {
                break;
            }
            let request = tokio::select! {value=receiver.recv()=>value,_=self.control.wake.notified()=>continue};
            let Some(request) = request else {
                self.control.closing.store(true, Ordering::SeqCst);
                break;
            };
            let cancel = LegacyCancellation::default();
            if let Ok(mut active) = self.control.active.lock() {
                *active = Some(cancel.clone());
            }
            if self.control.closing.load(Ordering::SeqCst) {
                cancel.cancel();
            }
            let result = if self.poisoned {
                Err(LegacyError::CleanupPending)
            } else {
                match request.command {
                    Command::Load(path) => self.load(path, cancel).await.map(Answer::Value),
                    Command::Threshold(payload) => {
                        self.threshold(payload, cancel).await.map(Answer::Value)
                    }
                    Command::Query(query) => self.query(query, cancel).await.map(Answer::Json),
                }
            };
            if let Ok(mut active) = self.control.active.lock() {
                *active = None;
            }
            let _ = request.reply.send(result);
        }
        while let Ok(request) = receiver.try_recv() {
            let _ = request.reply.send(Err(LegacyError::Closed));
        }
        loop {
            let mut failed = false;
            if let Some(owner) = self.owner.as_ref() {
                if owner.close().await.is_err() {
                    failed = true;
                } else {
                    self.owner = None;
                }
            }
            let mut retained = Vec::new();
            for owner in self.retired.drain(..) {
                if owner.close().await.is_err() {
                    failed = true;
                    retained.push(owner);
                }
            }
            self.retired = retained;
            if !failed {
                self.control.closed.send_replace(CloseState::Closed);
                return;
            }
            self.control.closed.send_replace(CloseState::Failed);
            // Keep every unconfirmed file lease and SQL owner alive. A retrying
            // close wakes this loop; dropping HTTP state never forces unlock.
            self.control.wake.notified().await;
            self.control.closed.send_replace(CloseState::Open);
        }
    }
    fn next_version(&self) -> Result<u64> {
        self.cache
            .version
            .checked_add(1)
            .ok_or_else(|| LegacyError::ResourceLimit("Legacy version exhausted".into()))
    }
    fn check_cancel(&self, cancel: &LegacyCancellation) -> Result<()> {
        if cancel.is_cancelled() || self.control.closing.load(Ordering::SeqCst) {
            Err(LegacyError::Cancelled)
        } else {
            Ok(())
        }
    }
    async fn invoke(
        &self,
        permit: Box<dyn LegacySciencePermit>,
        action: &str,
        parameters: Value,
        database: Option<&str>,
        snapshot: Option<&Snapshot>,
        cancel: LegacyCancellation,
    ) -> Result<Value> {
        self.check_cancel(&cancel)?;
        let id = uuid::Uuid::new_v4().simple().to_string();
        let working = if database.is_some() {
            parse_json_value(&self.cache.working, self.options.limits.max_result_bytes)?
        } else {
            json!({"threshold":null,"signals":[]})
        };
        let mut request = json!({"version":1,"request_id":id,"operation":"analysis","method":"legacy_workspace","action":action,"database":database,"source_version":self.cache.version,"state":working,"parameters":parameters,"legacy_limits":self.options.limits.rpc()});
        if let Some(snapshot) = snapshot {
            request["snapshot_directory"] = json!(snapshot.directory.path().to_str().ok_or_else(
                || LegacyError::Request("Private snapshot path must be UTF-8".into())
            )?);
            request["snapshot_nonce"] = json!(snapshot.nonce);
        }
        if to_json_vec(&request)?.len() > self.options.limits.max_result_bytes {
            return Err(LegacyError::ResourceLimit(
                "Legacy request budget exceeded".into(),
            ));
        }
        let response = permit.execute(request, cancel).await?;
        if to_json_vec(&response)?.len() > self.options.limits.max_result_bytes {
            return Err(LegacyError::ResourceLimit(
                "Legacy response budget exceeded".into(),
            ));
        }
        let envelope = response
            .as_object()
            .ok_or(LegacyError::Protocol("envelope object"))?;
        if envelope.len() != 5
            || response["version"].as_u64() != Some(1)
            || response["request_id"].as_str() != Some(&id)
            || response["operation"] != "analysis"
        {
            return Err(LegacyError::Protocol("response identity"));
        }
        match response["ok"].as_bool() {
            Some(true) => {
                let result = response
                    .get("result")
                    .and_then(Value::as_object)
                    .ok_or(LegacyError::Protocol("result object"))?;
                if !result.contains_key("reply")
                    || result
                        .keys()
                        .any(|key| !["reply", "state"].contains(&key.as_str()))
                {
                    return Err(LegacyError::Protocol("result fields"));
                }
                Ok(Value::Object(result.clone()))
            }
            Some(false) => {
                let error = response
                    .get("error")
                    .ok_or(LegacyError::Protocol("error object"))?;
                let status = error["http_status"]
                    .as_u64()
                    .filter(|v| (400..=599).contains(v))
                    .ok_or(LegacyError::Protocol("error status"))?;
                Err(LegacyError::Science {
                    status: status as u16,
                    code: error["code"]
                        .as_str()
                        .ok_or(LegacyError::Protocol("error code"))?
                        .into(),
                    message: error["api_message"]
                        .as_str()
                        .ok_or(LegacyError::Protocol("error message"))?
                        .into(),
                })
            }
            None => Err(LegacyError::Protocol("ok boolean")),
        }
    }
    async fn load(&mut self, path: PathBuf, cancel: LegacyCancellation) -> Result<Value> {
        let path = resolve(&path, self.options.library.as_deref())?;
        let display = path
            .to_str()
            .ok_or_else(|| LegacyError::Request("Database path must be UTF-8".into()))?
            .to_owned();
        let version = self.next_version()?;
        let permit = self.science.try_reserve()?;
        let io_budget = permit.io_budget();
        let mut candidate = if self.owner.as_ref().is_some_and(|owner| owner.path == path) {
            None
        } else {
            let owner = PanOwner::open(path, self.options.limits.max_database_bytes).await?;
            self.retain_owner(&owner)?;
            Some(owner)
        };
        let result = async {
            let owner = candidate
                .as_ref()
                .or(self.owner.as_ref())
                .ok_or(LegacyError::Protocol("load owner"))?;
            let snapshot = owner
                .snapshot(&self.options, None, true, cancel.clone(), io_budget.clone())
                .await?;
            let result = self
                .invoke(
                    permit,
                    "load",
                    json!({}),
                    Some(&display),
                    Some(&snapshot),
                    cancel.clone(),
                )
                .await?;
            let cache = Cache::new(
                result["state"].clone(),
                Some(&display),
                version,
                &self.options.limits,
            )?;
            if result["reply"] != json!({"ok":true,"version":version}) {
                return Err(LegacyError::Protocol("load reply"));
            }
            self.check_cancel(&cancel)?;
            owner.check(snapshot.data_version).await?;
            Ok((cache, snapshot.data_version, result["reply"].clone()))
        }
        .await;
        let (cache, data_version, reply) = match result {
            Ok(value) => value,
            Err(error) => {
                if let Some(owner) = candidate.take() {
                    if owner.close().await.is_err() {
                        self.retired.push(owner);
                        self.poisoned = true;
                        return Err(LegacyError::CleanupPending);
                    }
                    self.release_owner(&owner)?;
                }
                return Err(error);
            }
        };
        if candidate.is_some() {
            if let Some(old) = self.owner.as_ref() {
                if old.close().await.is_err() {
                    if let Some(owner) = candidate.take() {
                        self.retired.push(owner);
                    }
                    self.poisoned = true;
                    return Err(LegacyError::CleanupPending);
                }
                self.release_owner(old)?;
            }
            self.owner = candidate;
        }
        self.data_version = data_version;
        self.cache = cache;
        self.version_tx.send_replace(version);
        Ok(reply)
    }
    async fn threshold(&mut self, payload: Value, cancel: LegacyCancellation) -> Result<Value> {
        if self.owner.is_none() {
            return Err(LegacyError::Request("нет активной линии порога".into()));
        }
        let version = self.next_version()?;
        let permit = self.science.try_reserve()?;
        let io_budget = permit.io_budget();
        let owner = self.owner.as_ref().ok_or(LegacyError::Closed)?;
        let snapshot = owner
            .snapshot(
                &self.options,
                Some(self.data_version),
                false,
                cancel.clone(),
                io_budget.clone(),
            )
            .await?;
        let result = self
            .invoke(
                permit,
                "threshold",
                payload,
                self.cache.database.as_deref(),
                Some(&snapshot),
                cancel.clone(),
            )
            .await?;
        let cache = Cache::new(
            result["state"].clone(),
            self.cache.database.as_deref(),
            version,
            &self.options.limits,
        )?;
        if result["reply"]["ok"] != true
            || result["reply"]["threshold"] != result["state"]["threshold"]
        {
            return Err(LegacyError::Protocol("threshold reply"));
        }
        let (frequencies, levels) = threshold_blobs(
            &result["reply"]["threshold"],
            self.options.limits.max_record_bytes,
        )?;
        self.check_cancel(&cancel)?;
        self.data_version = owner
            .threshold(
                snapshot.data_version,
                frequencies,
                levels,
                cancel,
                io_budget,
                self.options.sql_timeout,
            )
            .await?;
        // Commit won the cancellation race: always publish its committed state.
        self.cache = cache;
        self.version_tx.send_replace(version);
        Ok(result["reply"].clone())
    }
    async fn query(&mut self, query: LegacyQuery, cancel: LegacyCancellation) -> Result<Bytes> {
        let (action, parameters) = query.rpc()?;
        if !matches!(query, LegacyQuery::Keywords)
            && let Some(owner) = self.owner.as_ref()
        {
            owner.check(self.data_version).await?;
        }
        match query {
            LegacyQuery::State { include_waterfall } => {
                return Ok(if include_waterfall {
                    self.cache.full.clone()
                } else {
                    self.cache.stream.clone()
                });
            }
            LegacyQuery::Signals => return Ok(self.cache.signals.clone()),
            LegacyQuery::Keywords => {
                if let Some(value) = self.keywords.as_ref() {
                    return Ok(value.clone());
                }
            }
            _ => {}
        }
        if self.owner.is_none() && !matches!(query, LegacyQuery::Keywords) {
            return encoded(&json!({}), self.options.limits.max_result_bytes);
        }
        let permit = self.science.try_reserve()?;
        let io_budget = permit.io_budget();
        let snapshot = if matches!(query, LegacyQuery::Keywords) {
            None
        } else {
            Some(
                self.owner
                    .as_ref()
                    .ok_or(LegacyError::Closed)?
                    .snapshot(
                        &self.options,
                        Some(self.data_version),
                        false,
                        cancel.clone(),
                        io_budget.clone(),
                    )
                    .await?,
            )
        };
        let database = if snapshot.is_some() {
            self.cache.database.as_deref()
        } else {
            None
        };
        let result = self
            .invoke(
                permit,
                action,
                parameters,
                database,
                snapshot.as_ref(),
                cancel.clone(),
            )
            .await?;
        self.check_cancel(&cancel)?;
        if let Some(snapshot) = snapshot.as_ref() {
            self.owner
                .as_ref()
                .ok_or(LegacyError::Closed)?
                .check(snapshot.data_version)
                .await?;
        }
        if result.as_object().is_none_or(|value| value.len() != 1) {
            return Err(LegacyError::Protocol("query result fields"));
        }
        let body = encoded(&result["reply"], self.options.limits.max_result_bytes)?;
        if matches!(query, LegacyQuery::Keywords) {
            self.keywords = Some(body.clone());
        }
        Ok(body)
    }
}

fn validate_threshold_control(payload: &Value) -> Result<()> {
    if !payload.is_object() {
        return Err(LegacyError::Request("JSON body must be an object".into()));
    }
    if to_json_vec(payload)?.len() > 65_536 {
        return Err(LegacyError::ResourceLimit("Request body too large".into()));
    }
    Ok(())
}

fn resolve(path: &Path, library: Option<&Path>) -> Result<PathBuf> {
    let invalid = || LegacyError::Request("Укажите существующую базу панорам .pan".into());
    if path.as_os_str().len() > 4096 {
        return Err(invalid());
    }
    let candidate = path.canonicalize().map_err(|_| invalid())?;
    if !candidate.is_file()
        || candidate
            .extension()
            .and_then(|v| v.to_str())
            .is_none_or(|v| !v.eq_ignore_ascii_case("pan"))
    {
        return Err(invalid());
    }
    if let Some(library) = library {
        let root = library
            .canonicalize()
            .map_err(|_| LegacyError::Request("База находится вне настроенного каталога".into()))?;
        if !candidate.starts_with(root) {
            return Err(LegacyError::Request(
                "База находится вне настроенного каталога".into(),
            ));
        }
    }
    Ok(candidate)
}
fn threshold_blobs(value: &Value, limit: u64) -> Result<(Vec<u8>, Vec<u8>)> {
    if value["index"].as_u64() != Some(0) || value["unit"] != "dBm" {
        return Err(LegacyError::Protocol("threshold identity"));
    }
    let frequencies = value["frequencies"]
        .as_array()
        .ok_or(LegacyError::Protocol("threshold frequencies"))?;
    let levels = value["levels"]
        .as_array()
        .ok_or(LegacyError::Protocol("threshold levels"))?;
    if frequencies.is_empty()
        || frequencies.len() != levels.len()
        || frequencies.len() as u64 > limit / 8
    {
        return Err(LegacyError::ResourceLimit(
            "Legacy threshold budget exceeded".into(),
        ));
    }
    let mut freq = Vec::with_capacity(frequencies.len() * 8);
    let mut level = Vec::with_capacity(levels.len() * 8);
    let mut previous = None;
    for (frequency, value) in frequencies.iter().zip(levels) {
        let f = frequency
            .as_f64()
            .filter(|v| v.is_finite())
            .ok_or(LegacyError::Protocol("finite threshold frequency"))?;
        let v = value
            .as_f64()
            .filter(|v| v.is_finite())
            .ok_or(LegacyError::Protocol("finite threshold level"))?;
        if previous.is_some_and(|p| f <= p) {
            return Err(LegacyError::Protocol("increasing threshold grid"));
        }
        previous = Some(f);
        freq.extend_from_slice(&f.to_le_bytes());
        level.extend_from_slice(&v.to_le_bytes());
    }
    Ok((freq, level))
}
