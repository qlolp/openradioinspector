use super::backup_budget;

#[test]
fn measured_full_load_snapshot_gets_enough_copy_and_verification_time() {
    let options = backup_budget(2_636_263_424).unwrap();
    assert_eq!(options.copy_timeout.as_secs(), 438);
    assert_eq!(options.total_timeout.as_secs(), 1252);
}

#[test]
fn tiny_archive_keeps_bounded_minimum_budgets() {
    let options = backup_budget(4096).unwrap();
    assert_eq!(options.copy_timeout.as_secs(), 300);
    assert_eq!(options.total_timeout.as_secs(), 600);
}

#[test]
fn invalid_or_over_quota_size_is_refused_before_backup_admission() {
    for size in [0, 16 * 1024_u64.pow(3) + 1, u64::MAX] {
        assert!(backup_budget(size).is_err());
    }
    let largest = backup_budget(16 * 1024_u64.pow(3)).unwrap();
    assert_eq!(largest.copy_timeout.as_secs(), 2518);
    assert_eq!(largest.total_timeout.as_secs(), 7493);
}
