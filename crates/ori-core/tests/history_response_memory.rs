//! A raw history response must move its numeric page, not clone a second tree.
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

struct MeasuredAllocator;
static LIVE: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);

fn allocated(bytes: usize) {
    let live = LIVE.fetch_add(bytes, Ordering::SeqCst) + bytes;
    PEAK.fetch_max(live, Ordering::SeqCst);
}

// This test binary has one test. Count requested heap bytes, independently of
// OS RSS, allocator arenas and timing; the real Mac gate still measures RSS.
unsafe impl GlobalAlloc for MeasuredAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let pointer = unsafe { System.alloc(layout) };
        if !pointer.is_null() {
            allocated(layout.size());
        }
        pointer
    }
    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        LIVE.fetch_sub(layout.size(), Ordering::SeqCst);
        unsafe { System.dealloc(pointer, layout) };
    }
    unsafe fn realloc(&self, pointer: *mut u8, old: Layout, new: usize) -> *mut u8 {
        let result = unsafe { System.realloc(pointer, old, new) };
        if !result.is_null() {
            if new >= old.size() {
                allocated(new - old.size());
            } else {
                LIVE.fetch_sub(old.size() - new, Ordering::SeqCst);
            }
        }
        result
    }
}

#[global_allocator]
static ALLOCATOR: MeasuredAllocator = MeasuredAllocator;

#[tokio::test(flavor = "current_thread")]
async fn raw_history_http_response_does_not_clone_the_full_numeric_page() {
    use ori_archive::{Archive, ArchiveConfig};
    use ori_core::{Core, CoreConfig};
    use serde_json::{Value, json};
    use std::{sync::Arc, time::Duration};
    use tokio::net::TcpListener;

    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap();
    let mut session = fixture["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    let mut originals = Vec::new();
    for index in 0..100 {
        let mut sweep = fixture["sweep"].clone();
        sweep["id"] = json!(format!("memory-sweep-{index:03}"));
        sweep["interval"] = json!({"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.223456Z"});
        sweep["frequencies_hz"] = json!(
            (0..4096)
                .map(|i| 100_000_000_i64 + i * 1000)
                .collect::<Vec<_>>()
        );
        sweep["levels"] = json!(
            (0..4096)
                .map(|i| if i == 4095 {
                    -0.0
                } else {
                    -100.0 + (i % 41) as f64 * 0.125
                })
                .collect::<Vec<_>>()
        );
        originals.push(sweep);
    }
    for chunk in originals.chunks(10) {
        archive.append_sweeps(chunk.to_vec()).await.unwrap();
    }
    archive.close().await.unwrap();
    drop(archive);
    let core = Core::open(CoreConfig::replay(directory.path()))
        .await
        .unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!(
        "http://{}/api/v1/sessions/{}/sweeps?limit=100",
        listener.local_addr().unwrap(),
        fixture["session"]["id"].as_str().unwrap()
    );
    let server = {
        let core = Arc::clone(&core);
        tokio::spawn(async move { core.serve(listener).await })
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(60))
        .build()
        .unwrap();
    let baseline = LIVE.load(Ordering::SeqCst);
    PEAK.store(baseline, Ordering::SeqCst);
    let response = client.get(url).send().await.unwrap();
    assert_eq!(response.status(), 200);
    let bytes = response.bytes().await.unwrap();
    let extra = PEAK.load(Ordering::SeqCst).saturating_sub(baseline);
    core.request_shutdown();
    server.await.unwrap().unwrap();
    let decoded: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(decoded["data"], Value::Array(originals));
    // Exactly one 819200-number tree, <=16 bytes of numeric spelling per
    // sample, two <=8MiB wire buffers and fixed metadata. Value's ABI size
    // depends on serde_json/preserve_order feature unification.
    // A second numeric tree for json! does not fit this allowance.
    let budget = 100 * 4096 * 2 * (std::mem::size_of::<Value>() + 16) + 18 * 1024 * 1024;
    eprintln!(
        "raw page wire={} extra heap={extra} budget={budget}",
        bytes.len()
    );
    assert!(
        extra <= budget,
        "raw page response allocated {extra} extra bytes, budget {budget}"
    );
}
