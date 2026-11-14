//! Original reservation/file-I/O hook composition, using owned temporary state.
use super::*;
use std::sync::Arc;
fn spec(marker: &std::path::Path) -> WorkerSpec {
    WorkerSpec {
        program: std::env::var_os("ORI_TEST_PYTHON")
            .map(Into::into)
            .unwrap_or_else(|| "/usr/bin/python3".into()),
        args: vec![
            format!(
                "{}/tests/private_worker_echo.py",
                env!("CARGO_MANIFEST_DIR")
            ),
            marker.to_str().unwrap().into(),
            "success".into(),
        ],
        environment: std::collections::BTreeMap::new(),
        deadline_ms: 10_000,
        require_hard_memory: false,
        linux_cgroup: None,
    }
}

fn worker_outcome_matches_platform(
    windows: bool,
    response: &ori_legacy::Result<serde_json::Value>,
    marker_exists: bool,
) -> bool {
    if windows {
        matches!(
            response,
            Err(LegacyError::Science { status: 503, code, message })
                if code == "legacy_worker_unavailable"
                    && message == "Legacy scientific worker is unavailable"
        ) && !marker_exists
    } else {
        response.is_ok() && marker_exists
    }
}

#[test]
fn worker_outcome_oracle_requires_platform_result_and_launch_evidence() {
    let success = || Ok(serde_json::json!({"ok": true}));
    let science = |status, code: &str, message: &str| {
        Err(LegacyError::Science {
            status,
            code: code.into(),
            message: message.into(),
        })
    };
    let unavailable = || {
        science(
            503,
            "legacy_worker_unavailable",
            "Legacy scientific worker is unavailable",
        )
    };
    for (name, windows, response, marker_exists, expected) in [
        ("Windows child success", true, success(), true, false),
        (
            "Windows success without launch",
            true,
            success(),
            false,
            false,
        ),
        (
            "Windows containment denial",
            true,
            unavailable(),
            false,
            true,
        ),
        (
            "Windows denial after launch",
            true,
            unavailable(),
            true,
            false,
        ),
        (
            "Windows wrong status",
            true,
            science(
                502,
                "legacy_worker_unavailable",
                "Legacy scientific worker is unavailable",
            ),
            false,
            false,
        ),
        (
            "Windows wrong code",
            true,
            science(
                503,
                "legacy_timeout",
                "Legacy scientific worker is unavailable",
            ),
            false,
            false,
        ),
        (
            "Windows wrong message",
            true,
            science(503, "legacy_worker_unavailable", "different failure"),
            false,
            false,
        ),
        ("Windows busy", true, Err(LegacyError::Busy), false, false),
        (
            "Windows closed",
            true,
            Err(LegacyError::Closed),
            false,
            false,
        ),
        (
            "Windows cancelled",
            true,
            Err(LegacyError::Cancelled),
            false,
            false,
        ),
        (
            "Windows resource limit",
            true,
            Err(LegacyError::ResourceLimit("different failure".into())),
            false,
            false,
        ),
        (
            "Windows protocol failure",
            true,
            Err(LegacyError::Protocol("worker envelope")),
            false,
            false,
        ),
        ("Unix child success", false, success(), true, true),
        (
            "Unix success without launch",
            false,
            success(),
            false,
            false,
        ),
        (
            "Unix containment denial",
            false,
            unavailable(),
            false,
            false,
        ),
        ("Unix busy", false, Err(LegacyError::Busy), false, false),
    ] {
        assert_eq!(
            worker_outcome_matches_platform(windows, &response, marker_exists),
            expected,
            "{name}: response={response:?}, marker_exists={marker_exists}"
        );
    }
}

#[tokio::test]
async fn legacy_original_io_hook_retains_heavy_admission_after_the_permit_owner_drops() {
    let data = tempfile::tempdir().unwrap();
    let mut config = crate::CoreConfig::replay(data.path());
    config.legacy.worker = Some(spec(&data.path().join("unused.pid")));
    let core = Core::open(config).await.unwrap();
    let permit = Science(Arc::downgrade(&core)).try_reserve().unwrap();
    let budget = permit.io_budget();
    let supplied = budget.is_some();
    drop(permit);
    let held = matches!(
        core.research_pool.try_reserve(),
        Err(ori_runtime::Error::Busy)
    );
    drop(budget);
    let released = core.research_pool.try_reserve().is_ok();
    core.close().await.unwrap();
    assert!(
        supplied && held && released,
        "Legacy preparation/post-write has no original shared admission budget"
    );
}
#[tokio::test]
async fn legacy_file_charges_share_qos_and_post_worker_owner_holds_the_original_reservation() {
    let data = tempfile::tempdir().unwrap();
    let marker = data.path().join("owned.pid");
    let mut config = crate::CoreConfig::replay(data.path());
    config.legacy.worker = Some(spec(&marker));
    let core = Core::open(config).await.unwrap();
    let permit = Science(Arc::downgrade(&core)).try_reserve().unwrap();
    let Some(budget) = permit.io_budget() else {
        drop(permit);
        core.close().await.unwrap();
        panic!("Legacy file accounting hook is missing");
    };
    let original = Arc::downgrade(&budget);
    let same_budget = permit
        .io_budget()
        .is_some_and(|again| Arc::ptr_eq(&budget, &again));
    let before = core.background.as_ref().unwrap().qos.status().logical_bytes;
    let owned = budget.clone();
    tokio::task::spawn_blocking(move || owned.account(1024, &LegacyCancellation::default()))
        .await
        .unwrap()
        .unwrap();
    let after = core.background.as_ref().unwrap().qos.status().logical_bytes;
    let response = permit
        .execute(
            serde_json::json!({"version":1,"request_id":"legacy-original","operation":"analysis"}),
            LegacyCancellation::default(),
        )
        .await;
    let held = matches!(
        core.research_pool.try_reserve(),
        Err(ori_runtime::Error::Busy)
    );
    let registered = core
        .background
        .as_ref()
        .unwrap()
        .qos
        .status()
        .registered_work;
    let remaining_budget_owners = original.strong_count();
    drop(budget);
    let original_released = original.upgrade().is_none();
    let released = core.research_pool.try_reserve().is_ok();
    let final_registered = core
        .background
        .as_ref()
        .unwrap()
        .qos
        .status()
        .registered_work;
    core.close().await.unwrap();
    let marker_exists = marker.try_exists().unwrap();
    assert_eq!(after - before, 1024);
    assert_eq!(registered, 1);
    assert!(
        same_budget,
        "Legacy supplied a different original I/O budget Arc"
    );
    assert_eq!(remaining_budget_owners, 1);
    assert!(
        held,
        "Legacy worker completion released the original reservation"
    );
    assert!(
        original_released,
        "The final original I/O budget Arc survived drop"
    );
    assert!(
        released,
        "Dropping the original I/O budget retained admission"
    );
    assert_eq!(final_registered, 0);
    // Runtime refuses Windows research children before constructing Command:
    // owned process-tree containment is not implemented on that platform.
    assert!(
        worker_outcome_matches_platform(cfg!(windows), &response, marker_exists),
        "Unexpected Legacy worker result: response={response:?}, marker_exists={marker_exists}"
    );
}
