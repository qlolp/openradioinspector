use ori_tasks::{
    NoopTaskEffects, TaskContext, TaskEffects, TaskFuture, TaskScheduler, TaskSchedulerConfig,
};
use serde_json::{Value, json};
use std::{
    future::Future,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    task::Poll,
    time::Duration,
};
use tokio::sync::Notify;

async fn confirm_pending<F: Future>(mut future: Pin<&mut F>) {
    std::future::poll_fn(|context| {
        assert!(
            future.as_mut().poll(context).is_pending(),
            "blocked effect must retain the operation"
        );
        Poll::Ready(())
    })
    .await;
}

fn payload(path: &std::path::Path, name: &str) -> Value {
    json!({"name":name,"task_file":path,"start_at":"2026-11-11T12:00:00Z","duration_s":10})
}
struct Blocked {
    entered: Notify,
    release: Notify,
    calls: AtomicUsize,
}
impl TaskEffects for Blocked {
    fn on_start(&self, _context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.entered.notify_one();
            self.release.notified().await;
            Ok(())
        })
    }
    fn on_complete(&self, _context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}

#[tokio::test]
async fn cancelled_close_waiter_does_not_release_book_while_original_callback_is_live() {
    let dir = tempfile::tempdir().unwrap();
    let book = dir.path().join("tasks.json");
    let input = dir.path().join("input");
    std::fs::write(&input, "{} ").unwrap();
    let effects = Arc::new(Blocked {
        entered: Notify::new(),
        release: Notify::new(),
        calls: AtomicUsize::new(0),
    });
    let mut config = TaskSchedulerConfig::new(&book);
    config.interval_seconds = 60.;
    config.request_timeout = Duration::from_secs(3);
    let service = TaskScheduler::open(config.clone(), effects.clone()).unwrap();
    service.create_task(payload(&input, "first")).await.unwrap();
    service
        .create_task(payload(&input, "never-start-after-close"))
        .await
        .unwrap();
    service.configure(json!({"enabled":true})).await.unwrap();
    let run = service.clone();
    let pass = tokio::spawn(async move {
        run.run_once(Some("2026-11-11T12:00:00Z".parse().unwrap()))
            .await
    });
    tokio::time::timeout(Duration::from_secs(2), effects.entered.notified())
        .await
        .unwrap();
    let mut close = Box::pin(service.close());
    confirm_pending(close.as_mut()).await;
    drop(close);
    assert!(
        TaskScheduler::open(config.clone(), Arc::new(NoopTaskEffects)).is_err(),
        "still owns book during callback"
    );
    effects.release.notify_one();
    pass.await.unwrap().unwrap();
    service.close().await.unwrap();
    assert_eq!(effects.calls.load(Ordering::SeqCst), 1);
    let saved: Value = serde_json::from_slice(&std::fs::read(&book).unwrap()).unwrap();
    assert_eq!(saved["tasks"][0]["status"], "Снято с выполнения");
    assert_eq!(
        saved["tasks"][0]["outcome"],
        "принудительно завершено оператором"
    );
    assert_eq!(saved["tasks"][1]["status"], "В очереди на выполнение");
    let next = TaskScheduler::open(config, Arc::new(NoopTaskEffects)).unwrap();
    assert_eq!(next.status().await.unwrap()["enabled"], false);
    next.close().await.unwrap();
    assert!(
        service
            .create_task(payload(&input, "old-owner"))
            .await
            .is_err()
    );
}

#[tokio::test]
async fn cancelled_run_waiter_keeps_owned_effect_and_disable_waits_before_resume() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let effects = Arc::new(Blocked {
        entered: Notify::new(),
        release: Notify::new(),
        calls: AtomicUsize::new(0),
    });
    let mut config = TaskSchedulerConfig::new(dir.path().join("tasks.json"));
    config.interval_seconds = 60.;
    let service = TaskScheduler::open(config, effects.clone()).unwrap();
    service.create_task(payload(&input, "task")).await.unwrap();
    service.configure(json!({"enabled":true})).await.unwrap();
    let run = service.clone();
    let pass = tokio::spawn(async move {
        run.run_once(Some("2026-11-11T12:00:00Z".parse().unwrap()))
            .await
    });
    tokio::time::timeout(Duration::from_secs(2), effects.entered.notified())
        .await
        .unwrap();
    pass.abort();
    let mut disabled = Box::pin(service.configure(json!({"enabled":false})));
    confirm_pending(disabled.as_mut()).await;
    effects.release.notify_one();
    disabled.await.unwrap();
    assert_eq!(
        service.list_tasks().await.unwrap()[0]["status"],
        "Выполняется",
        "disable preserves running task"
    );
    assert_eq!(effects.calls.load(Ordering::SeqCst), 1);
    service.configure(json!({"enabled":true})).await.unwrap();
    service
        .run_once(Some("2026-11-11T12:00:20Z".parse().unwrap()))
        .await
        .unwrap();
    assert_eq!(
        service.list_tasks().await.unwrap()[0]["status"],
        "Выполнено"
    );
    assert_eq!(effects.calls.load(Ordering::SeqCst), 1);
    service.close().await.unwrap();
}

#[tokio::test]
async fn explicit_book_byte_and_task_budgets_never_discard_existing_tasks() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.json");
    let raw=br#"{"format":"openradioinspector-task-book","version":1,"tasks":[{"name":"first"},{"name":"second"}]}"#;
    std::fs::write(&path, raw).unwrap();
    let mut config = TaskSchedulerConfig::new(&path);
    config.max_tasks = 1;
    let error = TaskScheduler::open(config, Arc::new(NoopTaskEffects))
        .err()
        .unwrap();
    assert_eq!(error.api_code, "task_book_resource_limit");
    assert_eq!(std::fs::read(&path).unwrap(), raw);
    let mut config = TaskSchedulerConfig::new(&path);
    config.max_book_bytes = 8;
    let error = TaskScheduler::open(config, Arc::new(NoopTaskEffects))
        .err()
        .unwrap();
    assert_eq!(error.api_code, "task_book_resource_limit");
    assert_eq!(std::fs::read(&path).unwrap(), raw);
}

#[cfg(unix)]
#[tokio::test]
async fn failed_close_retains_lock_and_retries_dirty_save_without_truncating_book() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.json");
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let mut config = TaskSchedulerConfig::new(&path);
    config.interval_seconds = 60.;
    let service = TaskScheduler::open(config.clone(), Arc::new(NoopTaskEffects)).unwrap();
    service.create_task(payload(&input, "task")).await.unwrap();
    service.configure(json!({"enabled":true})).await.unwrap();
    service
        .run_once(Some("2026-11-11T12:00:00Z".parse().unwrap()))
        .await
        .unwrap();
    let before = std::fs::read(&path).unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o777)).unwrap();
    let failed = service.close().await.unwrap_err();
    assert_eq!(failed.api_code, "task_book_io");
    assert_eq!(std::fs::read(&path).unwrap(), before);
    assert!(TaskScheduler::open(config.clone(), Arc::new(NoopTaskEffects)).is_err());
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    service.close().await.unwrap();
    let value: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    assert_eq!(value["tasks"][0]["status"], "Снято с выполнения");
    let next = TaskScheduler::open(config, Arc::new(NoopTaskEffects)).unwrap();
    next.close().await.unwrap();
}

#[cfg(unix)]
#[tokio::test]
async fn atomic_save_preserves_permissions_and_never_follows_predictable_temporary_symlink() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.json");
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let victim = dir.path().join("victim");
    std::fs::write(&victim, "unchanged").unwrap();
    symlink(&victim, dir.path().join("tasks.json.tmp")).unwrap();
    let service =
        TaskScheduler::open(TaskSchedulerConfig::new(&path), Arc::new(NoopTaskEffects)).unwrap();
    service.create_task(payload(&input, "task")).await.unwrap();
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(std::fs::read_to_string(&victim).unwrap(), "unchanged");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o400)).unwrap();
    service
        .create_task(payload(&input, "second"))
        .await
        .unwrap();
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o400
    );
    assert_eq!(
        std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.file_name().to_string_lossy().starts_with(".tasks.json."))
            .count(),
        0
    );
    service.close().await.unwrap();
}

#[cfg(unix)]
#[tokio::test]
async fn pre_callback_save_failure_never_runs_start_or_completes_unstarted_work() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.json");
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let effects = Arc::new(Blocked {
        entered: Notify::new(),
        release: Notify::new(),
        calls: AtomicUsize::new(0),
    });
    let mut config = TaskSchedulerConfig::new(&path);
    config.interval_seconds = 60.;
    let service = TaskScheduler::open(config, effects.clone()).unwrap();
    service
        .create_task(payload(&input, "not-started"))
        .await
        .unwrap();
    service.configure(json!({"enabled":true})).await.unwrap();
    let before = std::fs::read(&path).unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o777)).unwrap();
    let error = service
        .run_once(Some("2026-11-11T12:00:00Z".parse().unwrap()))
        .await
        .unwrap_err();
    assert_eq!(error.api_code, "task_book_io");
    assert_eq!(effects.calls.load(Ordering::SeqCst), 0);
    assert_eq!(std::fs::read(&path).unwrap(), before);
    let state = service.list_tasks().await.unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(
        state[0]["status"], "В очереди на выполнение",
        "failed preflight must not create a completable running task"
    );
    let run = service.clone();
    let pass = tokio::spawn(async move {
        run.run_once(Some("2026-11-11T12:00:00Z".parse().unwrap()))
            .await
    });
    tokio::time::timeout(Duration::from_secs(2), effects.entered.notified())
        .await
        .unwrap();
    effects.release.notify_one();
    pass.await.unwrap().unwrap();
    assert_eq!(effects.calls.load(Ordering::SeqCst), 1);
    service.close().await.unwrap();
}

#[tokio::test]
async fn full_mailbox_rejects_extra_work_without_discarding_an_admitted_request() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let effects = Arc::new(Blocked {
        entered: Notify::new(),
        release: Notify::new(),
        calls: AtomicUsize::new(0),
    });
    let mut config = TaskSchedulerConfig::new(dir.path().join("tasks.json"));
    config.interval_seconds = 60.;
    config.command_capacity = 1;
    let service = TaskScheduler::open(config, effects.clone()).unwrap();
    service
        .create_task(payload(&input, "active"))
        .await
        .unwrap();
    service.configure(json!({"enabled":true})).await.unwrap();
    let run = service.clone();
    let pass = tokio::spawn(async move {
        run.run_once(Some("2026-11-11T12:00:00Z".parse().unwrap()))
            .await
    });
    tokio::time::timeout(Duration::from_secs(2), effects.entered.notified())
        .await
        .unwrap();
    let mut admitted = Box::pin(service.create_task(payload(&input, "admitted")));
    confirm_pending(admitted.as_mut()).await;
    let error = service
        .create_task(payload(&input, "rejected"))
        .await
        .unwrap_err();
    assert_eq!(error.api_code, "task_scheduler_queue_full");
    effects.release.notify_one();
    pass.await.unwrap().unwrap();
    admitted.await.unwrap();
    let items = service.list_tasks().await.unwrap();
    assert_eq!(items.as_array().unwrap().len(), 2);
    assert_eq!(items[1]["name"], "admitted");
    service.close().await.unwrap();
}

#[tokio::test]
async fn old_queued_pass_cannot_complete_work_across_disable_and_reenable() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let effects = Arc::new(Blocked {
        entered: Notify::new(),
        release: Notify::new(),
        calls: AtomicUsize::new(0),
    });
    let mut config = TaskSchedulerConfig::new(dir.path().join("tasks.json"));
    config.interval_seconds = 60.;
    let service = TaskScheduler::open(config, effects.clone()).unwrap();
    service.create_task(payload(&input, "task")).await.unwrap();
    service.configure(json!({"enabled":true})).await.unwrap();
    let run = service.clone();
    let active = tokio::spawn(async move {
        run.run_once(Some("2026-11-11T12:00:00Z".parse().unwrap()))
            .await
    });
    tokio::time::timeout(Duration::from_secs(2), effects.entered.notified())
        .await
        .unwrap();
    let mut old = Box::pin(service.run_once(Some("2026-11-11T12:00:20Z".parse().unwrap())));
    confirm_pending(old.as_mut()).await;
    let mut disable = Box::pin(service.configure(json!({"enabled":false})));
    confirm_pending(disable.as_mut()).await;
    let mut resume = Box::pin(service.configure(json!({"enabled":true})));
    confirm_pending(resume.as_mut()).await;
    effects.release.notify_one();
    active.await.unwrap().unwrap();
    assert!(old.await.unwrap().is_null());
    assert_eq!(disable.await.unwrap()["enabled"], false);
    assert_eq!(resume.await.unwrap()["enabled"], true);
    assert_eq!(
        service.list_tasks().await.unwrap()[0]["status"],
        "Выполняется"
    );
    assert_eq!(effects.calls.load(Ordering::SeqCst), 1);
    service
        .run_once(Some("2026-11-11T12:00:20Z".parse().unwrap()))
        .await
        .unwrap();
    assert_eq!(
        service.list_tasks().await.unwrap()[0]["status"],
        "Выполнено"
    );
    service.close().await.unwrap();
}

struct Background {
    started: Notify,
    completed: Notify,
}
impl TaskEffects for Background {
    fn on_start(&self, _context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async move {
            self.started.notify_one();
            Ok(())
        })
    }
    fn on_complete(&self, _context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async move {
            self.completed.notify_one();
            Ok(())
        })
    }
}
#[tokio::test]
async fn real_background_actor_uses_injected_utc_and_all_close_waiters_join() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let base = "2026-11-11T12:00:00Z"
        .parse::<chrono::DateTime<chrono::Utc>>()
        .unwrap();
    let now = Arc::new(std::sync::Mutex::new(base));
    let clock = now.clone();
    let effects = Arc::new(Background {
        started: Notify::new(),
        completed: Notify::new(),
    });
    let mut config = TaskSchedulerConfig::new(dir.path().join("tasks.json"));
    config.interval_seconds = 0.1;
    let service = TaskScheduler::with_clock(
        config,
        effects.clone(),
        Arc::new(move || *clock.lock().unwrap()),
    )
    .unwrap();
    service
        .create_task(payload(&input, "background"))
        .await
        .unwrap();
    service.configure(json!({"enabled":true})).await.unwrap();
    tokio::time::timeout(Duration::from_secs(2), effects.started.notified())
        .await
        .unwrap();
    *now.lock().unwrap() = base + chrono::TimeDelta::seconds(10);
    tokio::time::timeout(Duration::from_secs(2), effects.completed.notified())
        .await
        .unwrap();
    let tasks = service.list_tasks().await.unwrap();
    assert_eq!(tasks[0]["status"], "Выполнено");
    assert_eq!(tasks[0]["completed_cycles"], 1);
    let (a, b, c) = tokio::join!(service.close(), service.close(), service.close());
    a.unwrap();
    b.unwrap();
    c.unwrap();
    assert_eq!(service.status().await.unwrap()["scheduler_alive"], false);
}
