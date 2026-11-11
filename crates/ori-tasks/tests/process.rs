use ori_tasks::{
    NoopTaskEffects, TaskContext, TaskEffects, TaskFuture, TaskScheduler, TaskSchedulerConfig,
};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Child, Command, Stdio},
    sync::Arc,
    time::Duration,
};

fn python() -> Command {
    let mut command =
        Command::new(std::env::var_os("ORI_TASKS_TEST_PYTHON").unwrap_or_else(|| "python3".into()));
    command
        .env(
            "PYTHONPATH",
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
        )
        .env("PYTHONUTF8", "1");
    command
}
struct OwnedChild(Child);
impl Drop for OwnedChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn ready(child: &mut Child) {
    let output = child.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    std::thread::spawn(move || {
        for line in BufReader::new(output).lines().take(30) {
            if line.as_deref().is_ok_and(|line| line.ends_with("READY")) {
                let _ = tx.send(());
                return;
            }
        }
    });
    rx.recv_timeout(Duration::from_secs(5))
        .expect("owned child READY before deadline");
}
fn payload(input: &Path) -> Value {
    json!({"name":"original/е\u{301}","task_file":input,"start_at":"2026-11-11T12:00:00Z","duration_s":10})
}

#[tokio::test]
async fn real_python_process_obeys_rust_book_lock_and_can_reopen_after_durable_close() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.json");
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let service =
        TaskScheduler::open(TaskSchedulerConfig::new(&path), Arc::new(NoopTaskEffects)).unwrap();
    service.create_task(payload(&input)).await.unwrap();
    let before = std::fs::read(&path).unwrap();
    let probe = r#"import sys
from radioinspector.tasks import TaskBook
from radioinspector.task_scheduler import TaskScheduler
try:
 s=TaskScheduler(TaskBook(sys.argv[1]))
except RuntimeError as error:
 assert 'другим scheduler' in str(error)
 print('LOCKED')
else:
 s.close()
 raise SystemExit('second owner acquired live book')
"#;
    let output = python().args(["-c", probe]).arg(&path).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(String::from_utf8_lossy(&output.stdout).trim(), "LOCKED");
    assert_eq!(std::fs::read(&path).unwrap(), before);
    service.close().await.unwrap();
    let output=python().args(["-c","import sys; from radioinspector.tasks import TaskBook; from radioinspector.task_scheduler import TaskScheduler; s=TaskScheduler(TaskBook(sys.argv[1])); assert not s.status()['enabled']; s.close(); print('REOPENED')"]).arg(&path).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(String::from_utf8_lossy(&output.stdout).trim(), "REOPENED");
}

#[tokio::test]
async fn python_owner_crash_releases_os_lock_and_running_work_is_aborted_without_replay() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.json");
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let script = r#"import sys
from datetime import datetime, timezone
from radioinspector.tasks import TaskBook
from radioinspector.task_scheduler import TaskScheduler
at=datetime(2026,11,11,12,tzinfo=timezone.utc)
s=TaskScheduler(TaskBook(sys.argv[1]),clock=lambda:at,interval_seconds=60)
s.create_task(dict(name='crashed',task_file=sys.argv[2],start_at=at.isoformat(),duration_s=10))
s.configure(True);s.run_once(at)
print('READY',flush=True)
sys.stdin.readline()
s.close()
"#;
    let mut child = OwnedChild(
        python()
            .args(["-u", "-c", script])
            .arg(&path)
            .arg(&input)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap(),
    );
    ready(&mut child.0);
    let before = std::fs::read(&path).unwrap();
    assert!(
        TaskScheduler::open(TaskSchedulerConfig::new(&path), Arc::new(NoopTaskEffects)).is_err()
    );
    assert_eq!(std::fs::read(&path).unwrap(), before);
    child.0.kill().unwrap();
    child.0.wait().unwrap();
    let service =
        TaskScheduler::open(TaskSchedulerConfig::new(&path), Arc::new(NoopTaskEffects)).unwrap();
    let state = service.status().await.unwrap();
    assert_eq!(state["enabled"], false);
    assert_eq!(state["runtime"]["aborted_tasks"], 1);
    assert_eq!(state["runtime"]["last_result"], "interrupted_recovered");
    let tasks = service.list_tasks().await.unwrap();
    assert_eq!(tasks[0]["outcome"], "аварийное завершение приложения");
    assert_eq!(tasks[0]["status"], "Снято с выполнения");
    assert!(service.run_once(None).await.unwrap().is_null());
    service.close().await.unwrap();
}

struct CrashBoundary;
impl TaskEffects for CrashBoundary {
    fn on_start(&self, _context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async {
            println!("READY");
            std::io::stdout().flush().unwrap();
            std::future::pending().await
        })
    }
    fn on_complete(&self, _context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}
async fn child_callback_boundary(path: std::path::PathBuf) {
    let input = path.with_file_name("input");
    let mut config = TaskSchedulerConfig::new(&path);
    config.interval_seconds = 60.;
    let service = TaskScheduler::open(config, Arc::new(CrashBoundary)).unwrap();
    service.create_task(payload(&input)).await.unwrap();
    service.configure(json!({"enabled":true})).await.unwrap();
    service
        .run_once(Some("2026-11-11T12:00:00Z".parse().unwrap()))
        .await
        .unwrap();
}

#[tokio::test]
async fn rust_callback_crash_has_a_valid_running_book_and_recovery_does_not_repeat_start() {
    if let Some(path) = std::env::var_os("ORI_TASKS_CHILD_BOOK") {
        child_callback_boundary(path.into()).await;
        panic!("owned child must remain at the callback boundary until killed");
    }
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.json");
    std::fs::write(dir.path().join("input"), "{}").unwrap();
    let mut child = OwnedChild(
        Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "rust_callback_crash_has_a_valid_running_book_and_recovery_does_not_repeat_start",
                "--nocapture",
            ])
            .env("ORI_TASKS_CHILD_BOOK", &path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    ready(&mut child.0);
    let running: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    assert_eq!(
        running["tasks"][0]["status"], "Выполняется",
        "RUNNING is durable before callback"
    );
    child.0.kill().unwrap();
    child.0.wait().unwrap();
    let service =
        TaskScheduler::open(TaskSchedulerConfig::new(&path), Arc::new(NoopTaskEffects)).unwrap();
    assert_eq!(service.status().await.unwrap()["enabled"], false);
    assert_eq!(
        service.list_tasks().await.unwrap()[0]["outcome"],
        "аварийное завершение приложения"
    );
    assert!(service.run_once(None).await.unwrap().is_null());
    service.close().await.unwrap();
}
