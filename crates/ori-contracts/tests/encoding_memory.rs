//! A full history page must not allocate another JSON tree just to validate output.
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

#[test]
fn encoding_a_hundred_full_resolution_sweeps_keeps_only_bounded_validation_scratch() {
    // Same numeric cardinality as 100 sweeps of 4096 frequencies and levels.
    let numbers: Vec<i64> = (0..100 * 4096 * 2).collect();
    let expected = serde_json::to_vec(&numbers).unwrap();
    let baseline = LIVE.load(Ordering::SeqCst);
    PEAK.store(baseline, Ordering::SeqCst);
    let encoded = ori_contracts::to_json_vec(&numbers).unwrap();
    let extra = PEAK.load(Ordering::SeqCst).saturating_sub(baseline);
    assert_eq!(encoded, expected);
    // Vec growth may retain twice the final wire size, plus fixed scratch.
    let budget = 2 * encoded.len() + 1024 * 1024;
    eprintln!(
        "wire bytes={}, extra heap bytes={extra}, budget={budget}",
        encoded.len()
    );
    assert!(
        extra <= budget,
        "encoding used {extra} extra heap bytes, budget {budget}"
    );
}
