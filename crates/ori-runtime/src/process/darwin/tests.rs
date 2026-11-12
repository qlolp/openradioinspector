use super::*;
use std::{cell::RefCell, collections::VecDeque, rc::Rc};

const LEADER: Member = Member {
    pid: 24029,
    pgid: 24029,
    uid: 501,
    birth_seconds: 1700000000,
    birth_microseconds: 17,
    status: ZOMBIE,
};
const CHILD: Member = Member {
    pid: 24101,
    birth_microseconds: 18,
    ..LEADER
};
const BASELINE: Baseline = Baseline {
    pid: 24029,
    uid: 501,
    birth_seconds: 1700000000,
    birth_microseconds: 17,
};

#[derive(Clone)]
struct Snapshot {
    ids: Vec<i32>,
    bytes: i32,
    members: Vec<Member>,
    info_bytes: i32,
    denied: bool,
    reply_override: Option<Member>,
}
impl Snapshot {
    fn valid() -> Self {
        Self {
            ids: vec![24029, 24101],
            bytes: 8,
            members: vec![LEADER, CHILD],
            info_bytes: 136,
            denied: false,
            reply_override: None,
        }
    }
}
struct FakeKernel {
    pending: VecDeque<Snapshot>,
    current: Option<Snapshot>,
    events: Rc<RefCell<Vec<String>>>,
}
impl Probe for FakeKernel {
    fn group(&mut self, pgid: i32) -> io::Result<Vec<i32>> {
        assert_eq!(pgid, 24029);
        self.events.borrow_mut().push("list".into());
        let snapshot = self.pending.pop_front().expect("bounded two snapshots");
        let mut buffer = snapshot.ids.clone();
        buffer.resize(MAX_GROUP_MEMBERS, 0);
        let result = checked_group_ids(&buffer, snapshot.bytes);
        self.current = Some(snapshot);
        result
    }
    fn member(&mut self, pid: i32) -> io::Result<Member> {
        self.events.borrow_mut().push(format!("info:{pid}"));
        let snapshot = self.current.as_ref().unwrap();
        checked_info_size(snapshot.info_bytes, BSD_INFO_BYTES)?;
        if snapshot.denied {
            return Err(io::Error::from(io::ErrorKind::PermissionDenied));
        }
        if let Some(member) = snapshot.reply_override {
            return Ok(member);
        }
        snapshot
            .members
            .iter()
            .find(|m| m.pid == pid)
            .copied()
            .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
    }
}
fn kernel(first: Snapshot, second: Snapshot) -> FakeKernel {
    FakeKernel {
        pending: VecDeque::from([first, second]),
        current: None,
        events: Rc::default(),
    }
}
fn deny(first: Snapshot, second: Snapshot) {
    let mut probe = kernel(first, second);
    let result: io::Result<Option<()>> =
        reap_after_zombie_proof(Some(BASELINE), true, &mut probe, || {
            panic!("unknown group must not reap leader")
        });
    assert!(matches!(result, Ok(None)));
}

#[test]
fn complete_two_birth_and_state_snapshots_precede_single_original_wait() {
    let mut probe = kernel(Snapshot::valid(), Snapshot::valid());
    let events = probe.events.clone();
    let result = reap_after_zombie_proof(Some(BASELINE), true, &mut probe, || {
        events.borrow_mut().push("original.wait".into());
        Ok(17_u8)
    });
    assert_eq!(result.unwrap(), Some(17));
    assert_eq!(
        *events.borrow(),
        [
            "list",
            "info:24029",
            "info:24101",
            "list",
            "info:24029",
            "info:24101",
            "original.wait"
        ]
    );
}

#[test]
fn wait_error_is_propagated_after_proof_without_retry_or_success() {
    let mut probe = kernel(Snapshot::valid(), Snapshot::valid());
    let events = probe.events.clone();
    let result = reap_after_zombie_proof(Some(BASELINE), true, &mut probe, || {
        events.borrow_mut().push("original.wait".into());
        Err::<u8, _>(io::Error::from_raw_os_error(10))
    });
    assert_eq!(result.unwrap_err().raw_os_error(), Some(10));
    assert_eq!(
        events
            .borrow()
            .iter()
            .filter(|s| s.as_str() == "original.wait")
            .count(),
        1
    );
}

#[test]
fn live_foreign_wrong_group_and_invalid_birth_are_denied() {
    for changed in [
        Member { status: 2, ..CHILD },
        Member { uid: 502, ..CHILD },
        Member {
            pgid: 24030,
            ..CHILD
        },
        Member {
            birth_seconds: 0,
            ..CHILD
        },
        Member {
            birth_microseconds: 1000000,
            ..CHILD
        },
    ] {
        let mut snapshot = Snapshot::valid();
        snapshot.members[1] = changed;
        deny(snapshot, Snapshot::valid());
    }
}

#[test]
fn leader_birth_reuse_and_missing_leader_are_denied() {
    let mut reused = Snapshot::valid();
    reused.members[0].birth_microseconds += 1;
    deny(reused, Snapshot::valid());
    let mut absent = Snapshot::valid();
    absent.ids = vec![24101];
    absent.bytes = 4;
    deny(absent, Snapshot::valid());
    let mut mismatched_reply = Snapshot::valid();
    mismatched_reply.reply_override = Some(CHILD);
    deny(mismatched_reply, Snapshot::valid());
}

#[test]
fn every_second_member_birth_state_and_uid_are_read_again() {
    for changed in [
        Member {
            birth_microseconds: 19,
            ..CHILD
        },
        Member { status: 2, ..CHILD },
        Member { uid: 502, ..CHILD },
        Member {
            birth_microseconds: 18,
            ..LEADER
        },
    ] {
        let mut second = Snapshot::valid();
        let index = if changed.pid == 24029 { 0 } else { 1 };
        second.members[index] = changed;
        deny(Snapshot::valid(), second);
    }
}

#[test]
fn changed_second_membership_is_denied_even_if_all_current_members_are_zombies() {
    let mut second = Snapshot::valid();
    second.ids = vec![24029];
    second.bytes = 4;
    deny(Snapshot::valid(), second);
}

#[test]
fn denied_vanished_and_short_process_info_are_denied_on_both_snapshots() {
    for second_pass in [false, true] {
        for kind in 0..3 {
            let mut invalid = Snapshot::valid();
            match kind {
                0 => invalid.denied = true,
                1 => invalid.members.pop().map(|_| ()).unwrap(),
                _ => invalid.info_bytes = 135,
            }
            if second_pass {
                deny(Snapshot::valid(), invalid);
            } else {
                deny(invalid, Snapshot::valid());
            }
        }
    }
}

#[test]
fn zero_misaligned_negative_full_overflow_duplicate_and_zero_pid_lists_are_denied() {
    for second_pass in [false, true] {
        for bytes in [0, -1, 3, 1024, 1028] {
            let mut invalid = Snapshot::valid();
            invalid.bytes = bytes;
            if second_pass {
                deny(Snapshot::valid(), invalid);
            } else {
                deny(invalid, Snapshot::valid());
            }
        }
        for ids in [vec![24029, 24029], vec![24029, 0], vec![24029, -1]] {
            let mut invalid = Snapshot::valid();
            invalid.ids = ids;
            if second_pass {
                deny(Snapshot::valid(), invalid);
            } else {
                deny(invalid, Snapshot::valid());
            }
        }
    }
}

#[test]
fn missing_spawn_baseline_or_nonexited_leader_never_queries_or_waits() {
    for (baseline, exited) in [(None, true), (Some(BASELINE), false)] {
        let mut probe = kernel(Snapshot::valid(), Snapshot::valid());
        let result: io::Result<Option<()>> =
            reap_after_zombie_proof(baseline, exited, &mut probe, || {
                panic!("no authoritative exit/birth proof")
            });
        assert!(matches!(result, Ok(None)));
        assert!(probe.events.borrow().is_empty());
    }
}

#[test]
fn exact_group_bytes_and_exact_bsdinfo_size_are_accepted() {
    assert_eq!(
        checked_group_ids(&[24029, 24101, 0], 8).unwrap(),
        [24029, 24101]
    );
    assert!(checked_info_size(136, 136).is_ok());
}
