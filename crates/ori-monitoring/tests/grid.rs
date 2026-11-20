use ori_monitoring::ScanPlan;
use serde_json::json;

fn plan(points: u32, step: u64) -> ScanPlan {
    serde_json::from_value(
        json!({"name":"Survey","bands":[{"id":"rx","name":"RX","start_hz":100000,
        "stop_hz":100000+u64::from(points-1)*step,"points":points,"step_hz":step,
        "rbw_hz":30000,"max_revisit_seconds":30}]}),
    )
    .unwrap()
}

#[test]
fn inclusive_grid_covers_every_actual_point_once_for_full_survey_and_tail_sizes() {
    for points in [2, 289, 290, 291, 292, 579, 580, 581, 582, 129991] {
        let p = plan(points, 10000);
        p.validate_receive_plan(800_000_000).unwrap();
        let b = &p.bands[0];
        let mut actual = vec![];
        for i in 0..b.window_count() {
            let w = b.window(i).unwrap();
            assert!((2..=290).contains(&w.points));
            assert_eq!(w.stop_hz, w.start_hz + 10_000 * u64::from(w.points - 1));
            actual.extend((0..w.points).map(|n| w.start_hz + u64::from(n) * 10_000));
        }
        assert!(b.window(b.window_count()).is_none());
        assert_eq!(
            actual,
            (0..points)
                .map(|n| 100_000 + u64::from(n) * 10_000)
                .collect::<Vec<_>>()
        );
        if points == 129991 {
            assert_eq!(
                (b.window_count(), actual.last()),
                (449, Some(&1_300_000_000))
            );
        }
        if points == 291 {
            assert_eq!(
                (b.window(0).unwrap().points, b.window(1).unwrap().points),
                (289, 2)
            );
        }
    }
}

#[test]
fn grid_is_explicit_and_preserves_legacy_and_profile_limits() {
    for (points, step) in [(129992, 10000), (2, 9999), (2, 10001)] {
        assert!(
            plan(points, step)
                .validate_receive_plan(800_000_000)
                .is_err()
        );
    }
    let mut p = plan(129991, 10_000);
    p.bands[0].points -= 1;
    assert!(p.validate_receive_plan(800_000_000).is_err());
    p.bands[0].step_hz = None;
    p.bands[0].points = 290;
    assert!(p.validate_receive_plan(800_000_000).is_err());
    p.bands[0].stop_hz = 800_000_000;
    assert!(p.validate_receive_plan(800_000_000).is_ok());
    p.bands[0].points = 291;
    assert!(p.validate_receive_plan(800_000_000).is_err());
}
