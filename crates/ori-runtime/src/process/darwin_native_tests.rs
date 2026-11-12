use super::*;
use std::time::{Duration, Instant};

#[test]
fn exited_literal_true_group_is_reaped_by_the_original_child() {
    // Literal true creates no descendants. Cleanup always precedes assertions,
    // including the intentionally failing Darwin zombie-only EPERM baseline.
    let mut child = OwnedChild::spawn(&mut Command::new("/usr/bin/true")).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut observed = false;
    while Instant::now() < deadline {
        match child.exited() {
            Ok(true) => {
                observed = true;
                break;
            }
            Ok(false) => std::thread::sleep(Duration::from_millis(1)),
            Err(_) => break,
        }
    }
    let result = if observed {
        Some(child.terminate_and_reap())
    } else {
        None
    };
    let cleanup = if child.reaped {
        Ok(())
    } else {
        if !observed {
            let _ = child.child.kill();
        }
        child.child.wait().map(|_| {
            child.reaped = true;
        })
    };
    assert!(
        cleanup.is_ok(),
        "literal original Child.wait cleanup failed: {cleanup:?}"
    );
    assert!(
        observed,
        "literal leader did not reach its nonreaping exit observation"
    );
    let result = result.unwrap();
    assert!(
        result.is_ok(),
        "original group cleanup returned an error: {result:?}"
    );
}
