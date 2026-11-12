use ori_core::{Core, CoreConfig};
use ori_runtime::lease::WriterLease;
use std::time::Duration;

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cancelled_archive_startup_cannot_release_the_external_writer_lease() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("sessions.sqlite3");
    let archive = ori_archive::Archive::open(ori_archive::ArchiveConfig::new(&path)).unwrap();
    archive.close().await.unwrap();
    let blocker = rusqlite::Connection::open(&path).unwrap();
    blocker.execute_batch("BEGIN IMMEDIATE").unwrap();
    drop(WriterLease::acquire(directory.path()).unwrap());
    let config = CoreConfig::replay(directory.path());
    let mut starting = Box::pin(Core::open(config));
    // Core::open acquires StartingWriter synchronously before its FIRST await,
    // the spawn_blocking Archive::open completion. Poll that SAME future once;
    // a competing probe loop could itself win the lease and make Core fail
    // Busy before it ever reaches the owned SQLite blocker.
    let pending = std::future::poll_fn(|cx| {
        std::task::Poll::Ready(std::future::Future::poll(starting.as_mut(), cx).is_pending())
    })
    .await;
    assert!(
        pending,
        "Startup unexpectedly completed through the owned SQLite blocker"
    );
    assert!(
        WriterLease::acquire(directory.path()).is_err(),
        "The first startup await must follow external writer admission"
    );
    drop(starting);
    assert!(
        WriterLease::acquire(directory.path()).is_err(),
        "cancelling the waiter released ownership while blocking archive initialization could still create a writer"
    );
    blocker.execute_batch("ROLLBACK").unwrap();
    drop(blocker);
    // The unfinished opening job is allowed to finish; no second process owner
    // may acquire its external lease meanwhile. Direct Archive here is only an
    // owned test probe of the process registry, followed by confirmed closure.
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let config = ori_archive::ArchiveConfig::new(&path);
            match tokio::task::spawn_blocking(move || ori_archive::Archive::open(config))
                .await
                .unwrap()
            {
                Ok(archive) => {
                    archive.close().await.unwrap();
                    break;
                }
                Err(ori_archive::ArchiveError::AlreadyOpen) => tokio::task::yield_now().await,
                Err(error) => panic!("Startup cleanup probe: {error}"),
            }
        }
    })
    .await
    .unwrap();
}
