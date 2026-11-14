use crate::{control::*, util::*};
use ori_archive::{Archive, MonitoringQuery, Mutation};
use ori_contracts::{Session, UtcTimestamp};
use rand::RngCore;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap, VecDeque},
    sync::{
        Arc, Mutex, RwLock, Weak,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::sync::{Mutex as AsyncMutex, OwnedMutexGuard};

#[derive(Debug, thiserror::Error)]
pub enum GuardError {
    #[error("{0}")]
    Invalid(String),
    #[error("'Запись наблюдения не найдена'")]
    NotFound,
    #[error("'Запись наблюдения не найдена'")]
    RecordNotFound { kind: String, id: String },
    #[error("'{0}'")]
    NotFoundContext(&'static str),
    #[error("{0}")]
    Conflict(String),
    #[error(transparent)]
    Control(#[from] ControlError),
    #[error(transparent)]
    Archive(#[from] ori_archive::ArchiveError),
    #[error(transparent)]
    Monitoring(#[from] ori_monitoring::MonitoringError),
}
pub type Result<T> = std::result::Result<T, GuardError>;
impl GuardError {
    pub fn http_status(&self) -> u16 {
        match self {
            Self::Invalid(_) | Self::Monitoring(_) => 400,
            Self::NotFound
            | Self::RecordNotFound { .. }
            | Self::NotFoundContext(_)
            | Self::Archive(ori_archive::ArchiveError::NotFound) => 404,
            Self::Archive(ori_archive::ArchiveError::Invalid(_)) => 400,
            Self::Conflict(_)
            | Self::Control(ControlError::Denied)
            | Self::Archive(ori_archive::ArchiveError::Conflict) => 409,
            Self::Control(_) => 503,
            Self::Archive(ori_archive::ArchiveError::Capacity { .. }) => 507,
            Self::Archive(_) => 503,
        }
    }
    pub fn api_code(&self) -> &'static str {
        match self {
            Self::Invalid(_) | Self::Monitoring(_) => "invalid_monitoring_request",
            Self::NotFound
            | Self::RecordNotFound { .. }
            | Self::NotFoundContext(_)
            | Self::Archive(ori_archive::ArchiveError::NotFound) => "monitoring_not_found",
            Self::Archive(ori_archive::ArchiveError::Invalid(_)) => "invalid_monitoring_request",
            Self::Conflict(_)
            | Self::Control(ControlError::Denied)
            | Self::Archive(ori_archive::ArchiveError::Conflict) => "monitoring_conflict",
            Self::Control(_) => "monitoring_unavailable",
            Self::Archive(_) => "monitoring_archive_unavailable",
        }
    }
}
pub trait Clock: Send + Sync + 'static {
    fn now(&self) -> UtcTimestamp;
    fn monotonic(&self) -> Duration;
}
pub trait IdSource: Send + Sync + 'static {
    fn next_id(&self, kind: IdKind) -> String;
}
#[derive(Clone, Copy, Debug)]
pub enum IdKind {
    Record,
    Owner,
    Session,
    Stream,
    Candidate,
    Investigation,
    Artifact,
}
struct SystemClock {
    start: Instant,
}
impl Clock for SystemClock {
    fn now(&self) -> UtcTimestamp {
        chrono::Utc::now()
            .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
            .parse()
            .expect("system UTC")
    }
    fn monotonic(&self) -> Duration {
        self.start.elapsed()
    }
}
struct RandomIds;
impl IdSource for RandomIds {
    fn next_id(&self, kind: IdKind) -> String {
        let mut b = [0u8; 16];
        rand::rngs::OsRng.fill_bytes(&mut b);
        b[6] = (b[6] & 15) | 64;
        b[8] = (b[8] & 63) | 128;
        let hex = b.iter().map(|n| format!("{n:02x}")).collect::<String>();
        match kind {
            IdKind::Record | IdKind::Owner => hex,
            _ => format!(
                "{}-{}-{}-{}-{}",
                &hex[..8],
                &hex[8..12],
                &hex[12..16],
                &hex[16..20],
                &hex[20..]
            ),
        }
    }
}
pub(crate) struct Ownership {
    pub project_id: String,
    pub token: String,
    pub epoch: u64,
    pub stopped: bool,
    pub expires: Option<Duration>,
    pub returning: bool,
    pub guard_token: Option<String>,
    pub manual: Option<Arc<crate::grants::ManualControlState>>,
}
pub struct GuardService {
    pub(crate) archive: Archive,
    pub(crate) control: Arc<dyn InstrumentControl>,
    pub(crate) clock: Arc<dyn Clock>,
    pub(crate) ids: Arc<dyn IdSource>,
    pub(crate) coordinator: Arc<AsyncMutex<()>>,
    pub(crate) closed: AtomicBool,
    pub(crate) owners: RwLock<BTreeMap<(String, String), Ownership>>,
    pub(crate) control_grants: Mutex<crate::grants::GrantCache>,
    pub(crate) control_changed: Arc<tokio::sync::Notify>,
    lifecycles: Mutex<HashMap<String, Weak<AsyncMutex<()>>>>,
    pub(crate) capacity_samples: Mutex<VecDeque<(Duration, u64)>>,
    pub(crate) runtime: Mutex<crate::runtime::RuntimeState>,
    pub(crate) jobs: Mutex<crate::runtime::Jobs>,
    pub(crate) startup: AsyncMutex<crate::supervisor::Startup>,
    pub(crate) wake: Arc<tokio::sync::Notify>,
    pub(crate) weak: Weak<Self>,
    pub(crate) close_active: Mutex<BTreeSet<String>>,
    pub(crate) sessions: Arc<ori_session::SessionService>,
    pub(crate) close_attempt: Mutex<Option<Arc<crate::supervisor::CloseAttempt>>>,
}
impl GuardService {
    pub fn new(archive: Archive, control: Arc<dyn InstrumentControl>) -> Arc<Self> {
        Self::with_sources(
            archive,
            control,
            Arc::new(SystemClock {
                start: Instant::now(),
            }),
            Arc::new(RandomIds),
        )
    }
    pub fn with_sources(
        archive: Archive,
        control: Arc<dyn InstrumentControl>,
        clock: Arc<dyn Clock>,
        ids: Arc<dyn IdSource>,
    ) -> Arc<Self> {
        let coordinator = archive.domain_coordinator();
        Arc::new_cyclic(|weak| {
            let session_clock = clock.clone();
            let session_ids = ids.clone();
            let sessions = ori_session::SessionService::with_sources(
                archive.clone(),
                Arc::new(crate::receiver::GuardEffects {
                    guard: weak.clone(),
                    control: control.clone(),
                    context: None,
                }),
                Arc::new(move || {
                    chrono::DateTime::<chrono::Utc>::from_timestamp_micros(
                        session_clock.now().unix_micros(),
                    )
                    .expect("typed UTC timestamp")
                }),
                Arc::new(move || session_ids.next_id(IdKind::Investigation)),
            );
            Self {
                archive,
                control,
                clock,
                ids,
                coordinator,
                sessions,
                closed: AtomicBool::new(false),
                owners: RwLock::new(BTreeMap::new()),
                control_grants: Mutex::new(crate::grants::GrantCache::default()),
                control_changed: Arc::new(tokio::sync::Notify::new()),
                lifecycles: Mutex::new(HashMap::new()),
                capacity_samples: Mutex::new(VecDeque::with_capacity(60)),
                runtime: Mutex::new(crate::runtime::RuntimeState {
                    projects: HashMap::new(),
                    preparing: HashMap::new(),
                }),
                jobs: Mutex::new(BTreeMap::new()),
                startup: AsyncMutex::new(crate::supervisor::Startup::default()),
                wake: Arc::new(tokio::sync::Notify::new()),
                weak: weak.clone(),
                close_active: Mutex::new(BTreeSet::new()),
                close_attempt: Mutex::new(None),
            }
        })
    }
    pub(crate) fn ensure_open(&self) -> Result<()> {
        if self.closed.load(Ordering::Acquire) {
            return Err(GuardError::Conflict(
                "Служба мониторинга закрывается".into(),
            ));
        }
        Ok(())
    }
    pub(crate) async fn lock_project(&self, id: &str) -> Result<OwnedMutexGuard<()>> {
        let mutex = {
            let mut map = self
                .lifecycles
                .lock()
                .map_err(|_| GuardError::Conflict("guard lifecycle lock failed".into()))?;
            map.retain(|_, v| v.strong_count() > 0);
            if let Some(m) = map.get(id).and_then(Weak::upgrade) {
                m
            } else {
                let m = Arc::new(AsyncMutex::new(()));
                map.insert(id.into(), Arc::downgrade(&m));
                m
            }
        };
        Ok(mutex.lock_owned().await)
    }
    pub fn authorize_control(
        &self,
        target: &ControlTarget,
        permit: Option<&ControlPermit>,
    ) -> ControlResult<()> {
        if self.closed.load(Ordering::Acquire) {
            return Err(ControlError::Denied);
        }
        let owners = self.owners.read().map_err(|_| ControlError::Denied)?;
        let Some(owner) = owners.get(&(target.session_id.clone(), target.stream_id.clone())) else {
            return if permit.is_none() {
                Ok(())
            } else {
                Err(ControlError::Denied)
            };
        };
        let p = permit.ok_or(ControlError::Denied)?;
        if owner.stopped || p.project_id != owner.project_id || p.epoch != owner.epoch {
            return Err(ControlError::Denied);
        }
        if owner.returning && owner.guard_token.as_ref() == Some(&p.token) {
            return Ok(());
        }
        if owner.returning
            || owner.expires.is_some_and(|e| self.clock.monotonic() >= e)
            || p.token != owner.token
        {
            return Err(ControlError::Denied);
        }
        Ok(())
    }
    pub(crate) fn record(&self, project: Option<&str>, fields: Value) -> Result<Value> {
        let mut m = fields
            .as_object()
            .cloned()
            .ok_or_else(|| invalid("record fields"))?;
        m.insert("id".into(), json!(self.ids.next_id(IdKind::Record)));
        m.insert("project_id".into(), json!(project));
        m.insert("created_at".into(), json!(self.clock.now().as_str()));
        Ok(Value::Object(m))
    }
    pub async fn project(&self, id: &str) -> Result<Value> {
        self.lookup_record("project", id).await
    }
    async fn lookup_record(&self, kind: &str, id: &str) -> Result<Value> {
        self.archive
            .monitoring(kind, id)
            .await?
            .ok_or_else(|| GuardError::RecordNotFound {
                kind: kind.to_owned(),
                id: id.to_owned(),
            })
    }
    pub async fn get_record(&self, kind: &str, id: &str, project: Option<&str>) -> Result<Value> {
        if let Some(project) = project {
            self.project(project).await?;
        }
        let record = self.lookup_record(kind, id).await?;
        if project.is_some_and(|p| record["project_id"].as_str() != Some(p)) {
            return Err(GuardError::NotFoundContext("Запись другого проекта"));
        }
        Ok(record)
    }
    pub(crate) async fn event_record(&self, project: &str, id: &str) -> Result<Value> {
        let event = self.lookup_record("event", id).await?;
        if event["project_id"].as_str() != Some(project) {
            return Err(GuardError::NotFoundContext("Событие другого проекта"));
        }
        Ok(event)
    }
    pub async fn create(&self, payload: Value) -> Result<Value> {
        fields(&payload, &["name", "site", "session_id"], false)?;
        let sid = text(&payload, "session_id", 200)?;
        let name = text(&payload, "name", 200)?;
        let site = text(&payload, "site", 200)?;
        let _transaction = self.coordinator.lock().await;
        self.ensure_open()?;
        let session = self
            .archive
            .get_session(&sid)
            .await?
            .ok_or(GuardError::NotFound)?;
        let typed: Session = decode(&session)?;
        let analyzers = typed
            .streams
            .iter()
            .filter(|s| s.role == ori_contracts::DeviceRole::SpectrumAnalyzer)
            .collect::<Vec<_>>();
        let receivers = typed
            .streams
            .iter()
            .filter(|s| s.role == ori_contracts::DeviceRole::Receiver)
            .collect::<Vec<_>>();
        if analyzers.len() != 1 || receivers.len() != 1 {
            return Err(invalid("Нужны ровно один анализатор и один receiver"));
        }
        let a = analyzers[0];
        let r = receivers[0];
        if a.level_unit != "dBm" || r.level_unit != "s_meter_count" {
            return Err(invalid(
                "Требуются исходные dBm и s_meter_count без преобразования",
            ));
        }
        let raw = |id: &str| {
            session["streams"]
                .as_array()
                .and_then(|v| v.iter().find(|v| v["id"].as_str() == Some(id)))
                .cloned()
                .ok_or_else(|| invalid("profile stream"))
        };
        let mut p=self.record(None,json!({"name":name,"site":site,"state":"draft","session_ids":[sid],"active_session_id":typed.id,"analyzer_stream_id":a.id,"receiver_stream_id":r.id,"baseline_id":null,"autostart":false,"parameters":{"margin_db":6.0,"impulse_db":12.0,"hysteresis_db":3.0,"confirmations":3,"window_sweeps":5,"dwell_seconds":10.0},"profile":{"analyzer":raw(&a.id)?,"receiver":raw(&r.id)?}}))?;
        p["project_id"] = p["id"].clone();
        self.archive.put_monitoring("project", p.clone()).await?;
        Ok(p)
    }
    pub async fn save_preparation(&self, id: &str, payload: Value) -> Result<Value> {
        fields(
            &payload,
            &[
                "antenna_notes",
                "receive_path_confirmed",
                "location_confirmed",
            ],
            true,
        )
        .map_err(|_| invalid("Нужны антенна, подтверждения тракта и места"))?;
        let notes = text_with_label(&payload, "antenna_notes", "Антенна/тракт", 1000)?;
        if !payload["receive_path_confirmed"].is_boolean()
            || !payload["location_confirmed"].is_boolean()
        {
            return Err(invalid("Подтверждения должны быть boolean"));
        }
        let _life = self.lock_project(id).await?;
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        if self.project_owned(id)? {
            return Err(GuardError::Conflict(
                "Остановите охрану/сбор перед изменением подготовки".into(),
            ));
        }
        let mut p = self.project(id).await?;
        let mut prep = payload;
        prep["antenna_notes"] = json!(notes);
        prep["confirmed_at"] = json!(self.clock.now().as_str());
        p["preparation"] = prep;
        self.archive.put_monitoring("project", p.clone()).await?;
        Ok(p)
    }
    pub(crate) fn project_owned(&self, id: &str) -> Result<bool> {
        Ok(self
            .owners
            .read()
            .map_err(|_| GuardError::Conflict("guard policy lock failed".into()))?
            .values()
            .any(|x| x.project_id == id))
    }
    pub(crate) fn mutation(kind: &str, value: Value) -> Mutation {
        Mutation::Monitoring {
            kind: kind.into(),
            value,
        }
    }
    pub async fn list_records(
        &self,
        kind: &str,
        project: Option<&str>,
        after: &str,
        limit: usize,
        recent: bool,
    ) -> Result<Value> {
        if let Some(id) = project {
            self.project(id).await?;
        }
        let mut items = self
            .archive
            .list_monitoring(
                kind,
                MonitoringQuery {
                    project_id: project.map(str::to_owned),
                    after: after.into(),
                    limit,
                    recent,
                    state: None,
                },
            )
            .await?;
        for item in &mut items {
            if kind == "baseline" {
                let count = item["source_refs"].as_array().map_or(0, Vec::len);
                if let Some(m) = item.as_object_mut() {
                    for k in [
                        "frequencies_hz",
                        "p50_dbm",
                        "p95_dbm",
                        "mad_db",
                        "source_refs",
                    ] {
                        m.remove(k);
                    }
                    m.insert("source_count".into(), json!(count));
                }
            } else if kind == "event" {
                if let Some(m) = item.as_object_mut() {
                    m.remove("source_refs");
                }
            } else if kind == "project" {
                let old = item.clone();
                *item = json!({});
                for k in [
                    "id",
                    "name",
                    "site",
                    "state",
                    "active_session_id",
                    "baseline_id",
                    "autostart",
                ] {
                    item[k] = old[k].clone();
                }
            }
        }
        Ok(json!({"next_cursor":items.last().and_then(|p|p["id"].as_str()),"items":items}))
    }
    pub async fn close(&self) -> Result<()> {
        self.owned_close().await
    }
    pub(crate) async fn drain_lifecycles(&self) -> Result<()> {
        let active = {
            let map = self
                .lifecycles
                .lock()
                .map_err(|_| GuardError::Conflict("guard lifecycle lock failed".into()))?;
            map.values().filter_map(Weak::upgrade).collect::<Vec<_>>()
        };
        for lock in active {
            let guard = tokio::time::timeout(Duration::from_secs(15), lock.lock_owned())
                .await
                .map_err(|_| GuardError::Conflict("Операция с прибором ещё не завершена".into()))?;
            drop(guard);
        }
        Ok(())
    }
}
