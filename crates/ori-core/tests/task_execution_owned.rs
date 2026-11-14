//! Injected executor ownership tests. These prove no native backend acceptance.
#[path = "../src/task_execution/mod.rs"]
pub mod task_execution;

use ori_tasks::{ControlTask, TaskContext, TaskEffects, TaskError, TaskFuture, TaskResult};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::Duration,
};
use task_execution::*;
use tokio::sync::Notify;

const NAME: &str = "task-é-e\u{301}";

#[derive(Default)]
struct Authority {
    revoked: AtomicBool,
    changed: Notify,
}
impl Authority {
    fn revoke(&self) {
        self.revoked.store(true, Ordering::SeqCst);
        self.changed.notify_waiters();
    }
}
impl TaskAuthority for Authority {
    fn revalidate(&self) -> TaskResult<()> {
        if self.revoked.load(Ordering::SeqCst) {
            Err(TaskError::new(
                403,
                "original_authority_revoked",
                "Original authority revoked",
            ))
        } else {
            Ok(())
        }
    }
    fn revoked(&self) -> TaskFuture<'_, ()> {
        Box::pin(async move {
            loop {
                let changed = self.changed.notified();
                tokio::pin!(changed);
                changed.as_mut().enable();
                if self.revoked.load(Ordering::SeqCst) {
                    return Ok(());
                }
                changed.await;
            }
        })
    }
}

fn completion() -> TaskCompletion {
    TaskCompletion {
        session_id: "session-e\u{301}".into(),
        stream_id: "stream-é".into(),
        level_unit: "dBm".into(),
        source_ids: vec!["source-é".into(), "source-e\u{301}".into()],
        interval: ori_contracts::from_json_slice(br#"{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.223457Z"}"#,1024).unwrap(),
    }
}
struct Probe {
    captures: AtomicUsize,
    prepared: AtomicUsize,
    starts: AtomicUsize,
    finishes: AtomicUsize,
    cancels: AtomicUsize,
    joins: AtomicUsize,
    join_calls: AtomicUsize,
    drops: AtomicUsize,
    start_blocked: AtomicBool,
    join_blocked: AtomicBool,
    join_fails: AtomicBool,
    worker_live: AtomicBool,
    entered_start: Notify,
    entered_join: Notify,
    release_start: Notify,
    release_join: Notify,
    cancelled: Notify,
    dropped: Notify,
    output: Mutex<TaskCompletion>,
    observed: Mutex<Vec<Value>>,
}
impl Default for Probe {
    fn default() -> Self {
        Self {
            captures: AtomicUsize::new(0),
            prepared: AtomicUsize::new(0),
            starts: AtomicUsize::new(0),
            finishes: AtomicUsize::new(0),
            cancels: AtomicUsize::new(0),
            joins: AtomicUsize::new(0),
            join_calls: AtomicUsize::new(0),
            drops: AtomicUsize::new(0),
            start_blocked: AtomicBool::new(false),
            join_blocked: AtomicBool::new(false),
            join_fails: AtomicBool::new(false),
            worker_live: AtomicBool::new(true),
            entered_start: Notify::new(),
            entered_join: Notify::new(),
            release_start: Notify::new(),
            release_join: Notify::new(),
            cancelled: Notify::new(),
            dropped: Notify::new(),
            output: Mutex::new(completion()),
            observed: Mutex::new(Vec::new()),
        }
    }
}
struct Callbacks(Arc<Probe>);
struct Target {
    probe: Arc<Probe>,
    original: Arc<dyn TaskAuthority>,
}
struct Run {
    probe: Arc<Probe>,
    original: Arc<dyn TaskAuthority>,
}
impl Drop for Run {
    fn drop(&mut self) {
        self.probe.drops.fetch_add(1, Ordering::SeqCst);
        self.probe.dropped.notify_one();
    }
}
impl TaskExecutionCallbacks for Callbacks {
    fn capture(
        &self,
        _binding: &TaskBinding,
        original: Arc<dyn TaskAuthority>,
    ) -> TaskResult<Arc<dyn TaskTarget>> {
        original.revalidate()?;
        self.0.captures.fetch_add(1, Ordering::SeqCst);
        Ok(Arc::new(Target {
            probe: self.0.clone(),
            original,
        }))
    }
}
impl TaskTarget for Target {
    fn revalidate(&self) -> TaskResult<()> {
        self.original.revalidate()?;
        if !self.probe.worker_live.load(Ordering::SeqCst) {
            return Err(TaskError::new(
                409,
                "original_worker_changed",
                "Original worker changed",
            ));
        }
        Ok(())
    }
    fn prepare(&self, context: TaskContext) -> TaskResult<Arc<dyn TaskRun>> {
        self.revalidate()?;
        self.probe.prepared.fetch_add(1, Ordering::SeqCst);
        self.probe
            .observed
            .lock()
            .unwrap()
            .push(context.task.as_dict());
        Ok(Arc::new(Run {
            probe: self.probe.clone(),
            original: self.original.clone(),
        }))
    }
}
impl TaskRun for Run {
    fn start(&self) -> TaskFuture<'_, ()> {
        Box::pin(async move {
            self.probe.entered_start.notify_one();
            if self.probe.start_blocked.load(Ordering::SeqCst) {
                tokio::select! { _ = self.probe.release_start.notified() => {}, _ = self.probe.cancelled.notified() => {} }
            }
            // The injected producer applies the SAME original actor gate.
            self.original.revalidate()?;
            self.probe.starts.fetch_add(1, Ordering::SeqCst);
            Ok(())
        })
    }
    fn finish(&self) -> TaskFuture<'_, TaskCompletion> {
        Box::pin(async move {
            self.original.revalidate()?;
            self.probe.finishes.fetch_add(1, Ordering::SeqCst);
            Ok(self.probe.output.lock().unwrap().clone())
        })
    }
    fn cancel(&self) {
        self.probe.cancels.fetch_add(1, Ordering::SeqCst);
        self.probe.cancelled.notify_one();
    }
    fn join(&self) -> TaskFuture<'_, ()> {
        Box::pin(async move {
            self.probe.join_calls.fetch_add(1, Ordering::SeqCst);
            self.probe.entered_join.notify_one();
            if self.probe.join_blocked.load(Ordering::SeqCst) {
                self.probe.release_join.notified().await;
            }
            if self.probe.join_fails.load(Ordering::SeqCst) {
                return Err(TaskError::new(
                    503,
                    "test_cleanup_failed",
                    "Cleanup was not confirmed",
                ));
            }
            self.probe.joins.fetch_add(1, Ordering::SeqCst);
            Ok(())
        })
    }
}
fn fixture_path(directory: &str, filename: &str) -> PathBuf {
    std::env::current_dir()
        .unwrap()
        .join("ori-task-execution-owned")
        .join(directory)
        .join(filename)
}
fn binding() -> TaskBinding {
    TaskBinding {
        task_name: NAME.into(),
        input: TaskInput::FixedFrequencyList(fixture_path("trusted", "source-é.json")),
        session_id: "session-e\u{301}".into(),
        stream_id: "stream-é".into(),
        level_unit: "dBm".into(),
    }
}
fn config(probe: Arc<Probe>) -> TaskExecutionConfig {
    TaskExecutionConfig {
        bindings: vec![binding()],
        callbacks: Arc::new(Callbacks(probe)),
        max_active_runs: 16,
        close_timeout: Duration::from_millis(100),
    }
}
fn task(status: &str) -> ControlTask {
    ControlTask::from_dict(
        json!({"name":NAME,"task_file":fixture_path("trusted", "source-é.json"),"status":status,
        "start_at":"2026-11-11T12:00:00.123456+00:00","duration_s":10.0,
        "profiles":[{"bandwidth_hz":-0.0}],"extra_info":"é-e\u{301}"}),
    )
    .unwrap()
}
fn context(epoch: u64) -> TaskContext {
    TaskContext {
        epoch,
        task: task("Выполняется"),
    }
}
async fn enabled(probe: Arc<Probe>) -> (Arc<TaskExecution>, Arc<Authority>) {
    let effects = TaskExecution::new(config(probe)).unwrap();
    let original = Arc::new(Authority::default());
    effects
        .admit_enable(
            &[task("В очереди на выполнение").as_dict()],
            original.clone(),
        )
        .unwrap();
    (effects, original)
}

#[tokio::test]
async fn host_absolute_sources_preserve_exact_binding_and_reject_unsafe_paths_before_capture() {
    let probe = Arc::new(Probe::default());
    let trusted = fixture_path("trusted", "source-é.json");
    let untrusted = fixture_path("untrusted", "other.json");
    let effects = TaskExecution::new(config(probe.clone())).unwrap();
    let accepted = effects.validate_task(&task("В очереди на выполнение").as_dict());
    let mut wrong = task("В очереди на выполнение").as_dict();
    wrong["task_file"] = json!(untrusted);
    let unbound = effects.validate_task(&wrong).unwrap_err();
    effects.close().await.unwrap();
    assert!(trusted.is_absolute() && untrusted.is_absolute());
    assert_ne!(trusted, untrusted);
    accepted.unwrap();
    assert_eq!(unbound.http_status, 400);
    assert_eq!(unbound.api_code, "task_execution_binding_invalid");
    for invalid in [
        PathBuf::from("trusted/source-é.json"),
        fixture_path("trusted", "..").join("source-é.json"),
    ] {
        let mut options = config(probe.clone());
        options.bindings[0].input = TaskInput::FixedFrequencyList(invalid);
        match TaskExecution::new(options) {
            Err(error) => {
                assert_eq!(error.http_status, 400);
                assert_eq!(error.api_code, "task_execution_binding_invalid");
            }
            Ok(effects) => {
                effects.close().await.unwrap();
                panic!("Unsafe task source binding was accepted");
            }
        }
    }
    assert_eq!(probe.captures.load(Ordering::SeqCst), 0);
    assert_eq!(probe.prepared.load(Ordering::SeqCst), 0);
    assert_eq!(probe.starts.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn exact_epoch_input_source_ids_units_and_utc_complete_only_after_join() {
    let probe = Arc::new(Probe::default());
    let (effects, _) = enabled(probe.clone()).await;
    assert!(effects.is_enabled());
    effects.on_start(context(7)).await.unwrap();
    effects.on_complete(context(7)).await.unwrap();
    assert_eq!(probe.starts.load(Ordering::SeqCst), 1);
    assert_eq!(probe.finishes.load(Ordering::SeqCst), 1);
    assert_eq!(probe.joins.load(Ordering::SeqCst), 1);
    let observed = probe.observed.lock().unwrap()[0].clone();
    assert_eq!(
        ori_contracts::to_json_vec(&observed).unwrap(),
        ori_contracts::to_json_vec(&context(7).task.as_dict()).unwrap()
    );
    assert_eq!(
        observed["profiles"][0]["bandwidth_hz"]
            .as_f64()
            .unwrap()
            .to_bits(),
        (-0.0f64).to_bits()
    );
    effects.close().await.unwrap();
    tokio::time::timeout(Duration::from_secs(2), probe.dropped.notified())
        .await
        .unwrap();
    assert_eq!(probe.drops.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn unbound_paths_and_changed_epoch_never_reach_another_start_or_finish() {
    let probe = Arc::new(Probe::default());
    let (effects, _) = enabled(probe.clone()).await;
    let mut wrong = context(7).task.as_dict();
    wrong["task_file"] = json!(fixture_path("untrusted", "other.json"));
    assert!(effects.validate_task(&wrong).is_err());
    assert!(
        effects
            .on_start(TaskContext {
                epoch: 7,
                task: ControlTask::from_dict(wrong).unwrap()
            })
            .await
            .is_err()
    );
    assert_eq!(probe.prepared.load(Ordering::SeqCst), 0);
    effects.on_start(context(7)).await.unwrap();
    let mut altered = context(7).task.as_dict();
    altered["profiles"][0]["bandwidth_hz"] = json!(0.0);
    assert!(
        effects
            .on_complete(TaskContext {
                epoch: 7,
                task: ControlTask::from_dict(altered).unwrap()
            })
            .await
            .is_err()
    );
    assert!(effects.on_complete(context(8)).await.is_err());
    assert_eq!(probe.finishes.load(Ordering::SeqCst), 0);
    effects.close().await.unwrap();
    assert_eq!(probe.joins.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn revoke_wakes_start_but_timeout_cannot_acknowledge_or_drop_owned_cleanup() {
    let probe = Arc::new(Probe::default());
    probe.start_blocked.store(true, Ordering::SeqCst);
    probe.join_blocked.store(true, Ordering::SeqCst);
    let (effects, _) = enabled(probe.clone()).await;
    let start = tokio::spawn({
        let effects = effects.clone();
        async move { effects.on_start(context(7)).await }
    });
    probe.entered_start.notified().await;
    effects.revoke();
    let close = effects.begin_drain();
    let owner = tokio::spawn({
        let close = close.clone();
        async move { close.wait_joined().await }
    });
    probe.entered_join.notified().await;
    start.abort();
    let _ = start.await;
    assert!(close.completed().await.is_err());
    assert!(
        !owner.is_finished(),
        "observer timeout completed the owner wait"
    );
    assert_eq!(probe.starts.load(Ordering::SeqCst), 0);
    assert_eq!(probe.cancels.load(Ordering::SeqCst), 1);
    assert_eq!(probe.joins.load(Ordering::SeqCst), 0);
    assert!(
        effects
            .admit_enable(&[], Arc::new(Authority::default()))
            .is_err()
    );
    probe.release_join.notify_one();
    owner.await.unwrap().unwrap();
    close.clone().completed().await.unwrap();
    close.completed().await.unwrap();
    assert_eq!(probe.joins.load(Ordering::SeqCst), 1);
    assert_eq!(probe.join_calls.load(Ordering::SeqCst), 1);
    assert_eq!(probe.captures.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn cancelled_owner_waiter_does_not_cancel_or_replace_the_join_attempt() {
    let probe = Arc::new(Probe::default());
    probe.join_blocked.store(true, Ordering::SeqCst);
    let (effects, _) = enabled(probe.clone()).await;
    effects.on_start(context(7)).await.unwrap();
    let original = effects.begin_shutdown();
    probe.entered_join.notified().await;
    let waiter = tokio::spawn({
        let original = original.clone();
        async move { original.wait_joined().await }
    });
    tokio::task::yield_now().await;
    assert!(!waiter.is_finished());
    waiter.abort();
    assert!(waiter.await.unwrap_err().is_cancelled());
    assert_eq!(probe.joins.load(Ordering::SeqCst), 0);
    assert_eq!(probe.join_calls.load(Ordering::SeqCst), 1);
    let same_attempt = effects.begin_shutdown();
    probe.release_join.notify_one();
    original.wait_joined().await.unwrap();
    same_attempt.wait_joined().await.unwrap();
    assert_eq!(probe.cancels.load(Ordering::SeqCst), 1);
    assert_eq!(probe.joins.load(Ordering::SeqCst), 1);
    assert_eq!(probe.join_calls.load(Ordering::SeqCst), 1);
    assert_eq!(probe.captures.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn original_auth_revocation_and_original_worker_change_cannot_attach_fresh_authority() {
    let probe = Arc::new(Probe::default());
    let (effects, original) = enabled(probe.clone()).await;
    effects.on_start(context(7)).await.unwrap();
    assert!(
        effects
            .admit_enable(&[], Arc::new(Authority::default()))
            .is_err()
    );
    original.revoke();
    effects.begin_drain().completed().await.unwrap();
    assert!(
        effects
            .admit_enable(
                &[task("Выполняется").as_dict()],
                Arc::new(Authority::default())
            )
            .is_err()
    );
    assert_eq!(probe.captures.load(Ordering::SeqCst), 1);
    effects
        .admit_enable(
            &[task("В очереди на выполнение").as_dict()],
            Arc::new(Authority::default()),
        )
        .unwrap();
    probe.worker_live.store(false, Ordering::SeqCst);
    assert!(effects.on_start(context(9)).await.is_err());
    assert_eq!(probe.starts.load(Ordering::SeqCst), 1);
    effects.close().await.unwrap();
}

#[tokio::test]
async fn source_completion_validation_denies_noop_or_changed_scope_and_still_joins() {
    for kind in ["empty", "unit", "session", "interval"] {
        let probe = Arc::new(Probe::default());
        match kind {
            "empty" => probe.output.lock().unwrap().source_ids.clear(),
            "unit" => probe.output.lock().unwrap().level_unit = "s_meter_count".into(),
            "session" => probe.output.lock().unwrap().session_id = "replacement-session".into(),
            _ => {
                let mut output = probe.output.lock().unwrap();
                let interval = &mut output.interval;
                std::mem::swap(&mut interval.started_at, &mut interval.ended_at);
            }
        }
        let (effects, _) = enabled(probe.clone()).await;
        effects.on_start(context(7)).await.unwrap();
        assert!(effects.on_complete(context(7)).await.is_err(), "{kind}");
        assert_eq!(probe.joins.load(Ordering::SeqCst), 1);
        effects.close().await.unwrap();
    }
}

#[tokio::test]
async fn cleanup_failure_is_sticky_and_blocks_reenable() {
    let probe = Arc::new(Probe::default());
    probe.join_fails.store(true, Ordering::SeqCst);
    let (effects, _) = enabled(probe.clone()).await;
    effects.on_start(context(7)).await.unwrap();
    assert!(effects.close().await.is_err());
    assert!(effects.close().await.is_err());
    assert!(effects.begin_shutdown().wait_joined().await.is_err());
    assert!(
        effects
            .admit_enable(&[], Arc::new(Authority::default()))
            .is_err()
    );
    assert_eq!(probe.joins.load(Ordering::SeqCst), 0);
    assert_eq!(probe.cancels.load(Ordering::SeqCst), 1);
    assert_eq!(probe.join_calls.load(Ordering::SeqCst), 1);
    drop(effects);
    tokio::task::yield_now().await;
    assert_eq!(
        probe.drops.load(Ordering::SeqCst),
        0,
        "unconfirmed native handle was released"
    );
}

#[tokio::test]
async fn revoked_after_finish_before_join_cannot_publish_success() {
    let probe = Arc::new(Probe::default());
    probe.join_blocked.store(true, Ordering::SeqCst);
    let (effects, original) = enabled(probe.clone()).await;
    effects.on_start(context(7)).await.unwrap();
    let complete = tokio::spawn({
        let effects = effects.clone();
        async move { effects.on_complete(context(7)).await }
    });
    probe.entered_join.notified().await;
    original.revoke();
    probe.release_join.notify_one();
    assert!(complete.await.unwrap().is_err());
    effects.close().await.unwrap();
}

#[tokio::test]
async fn configuration_is_bounded_and_contains_no_default_success_adapter() {
    let probe = Arc::new(Probe::default());
    let mut wrong = config(probe.clone());
    wrong.bindings.clear();
    assert!(TaskExecution::new(wrong).is_err());
    let mut wrong = config(probe.clone());
    wrong.max_active_runs = 17;
    assert!(TaskExecution::new(wrong).is_err());
    let mut wrong = config(probe.clone());
    wrong.bindings.push(binding());
    assert!(TaskExecution::new(wrong).is_err());
    let mut wrong = config(probe);
    wrong.close_timeout = Duration::from_secs(31);
    assert!(TaskExecution::new(wrong).is_err());
}

#[tokio::test]
async fn archive_roundtrip_preserves_spaced_scope_and_raw_unit_for_exact_binding() {
    let directory = tempfile::tempdir().unwrap().keep();
    eprintln!("Owned archive regression source: {}", directory.display());
    let archive = ori_archive::Archive::open(ori_archive::ArchiveConfig::new(
        directory.join("source.sqlite"),
    ))
    .unwrap();
    let session_id = " session-e\u{301} ";
    let stream_id = " stream-é ";
    let unit = " raw-unit-e\u{301} ";
    archive.create_session(json!({"id":session_id,"name":"Existing archive identities","state":"running",
        "created_at":"2026-11-11T12:00:00.123456Z","started_at":"2026-11-11T12:00:00.123456Z","ended_at":null,
        "contract_version":1,"metadata":{"é":-0.0,"e\u{301}":1.0000000000000002},
        "streams":[{"id":stream_id,"session_id":session_id,"device":"synthetic","role":"spectrum_analyzer",
                    "level_unit":unit,"settings":{}}]})).await.unwrap();
    let source = json!({"id":" source-e\u{301} ","session_id":session_id,"stream_id":stream_id,
        "level_unit":unit,"interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.223457Z"},
        "frequencies_hz":[100000001,100000003],"levels":[-0.0,1.0000000000000002],
        "settings":{"é":-0.0,"e\u{301}":1.0000000000000002}});
    archive.append_sweeps(vec![source.clone()]).await.unwrap();
    let saved_session = archive.get_session(session_id).await.unwrap().unwrap();
    let saved_source = archive
        .get_sweep(source["id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    archive.close().await.unwrap();
    assert_eq!(saved_session["id"], session_id);
    assert_eq!(saved_session["streams"][0]["id"], stream_id);
    assert_eq!(saved_session["streams"][0]["level_unit"], unit);
    let mut observed = saved_source.clone();
    let mut expected = source.clone();
    observed.sort_all_objects();
    expected.sort_all_objects();
    assert_eq!(
        ori_contracts::to_json_vec(&observed).unwrap(),
        ori_contracts::to_json_vec(&expected).unwrap()
    );

    let probe = Arc::new(Probe::default());
    let output = TaskCompletion {
        session_id: saved_source["session_id"].as_str().unwrap().into(),
        stream_id: saved_source["stream_id"].as_str().unwrap().into(),
        level_unit: saved_source["level_unit"].as_str().unwrap().into(),
        source_ids: vec![saved_source["id"].as_str().unwrap().into()],
        interval: ori_contracts::from_json_slice(
            &ori_contracts::to_json_vec(&saved_source["interval"]).unwrap(),
            1024,
        )
        .unwrap(),
    };
    *probe.output.lock().unwrap() = output;
    let mut options = config(probe.clone());
    options.bindings[0].session_id = session_id.into();
    options.bindings[0].stream_id = stream_id.into();
    options.bindings[0].level_unit = unit.into();
    let effects = TaskExecution::new(options).unwrap();
    effects
        .admit_enable(
            &[task("В очереди на выполнение").as_dict()],
            Arc::new(Authority::default()),
        )
        .unwrap();
    effects.on_start(context(7)).await.unwrap();
    effects.on_complete(context(7)).await.unwrap();
    effects.close().await.unwrap();
    let original = probe.output.lock().unwrap().clone();
    assert_eq!(
        (
            original.session_id.as_str(),
            original.stream_id.as_str(),
            original.level_unit.as_str()
        ),
        (session_id, stream_id, unit)
    );
    assert_eq!(original.source_ids, vec![" source-e\u{301} "]);
    std::fs::remove_dir_all(directory).unwrap();
}
