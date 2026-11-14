//! Private proof for the Darwin zombie-only group-signal EPERM case.
use std::io;

const MAX_GROUP_MEMBERS: usize = 256;
const BSD_INFO_BYTES: usize = 136;
const ZOMBIE: u32 = 5;
#[cfg(target_os = "macos")]
const PROC_PGRP_ONLY: u32 = 2;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Member {
    pid: i32,
    pgid: i32,
    uid: u32,
    birth_seconds: u64,
    birth_microseconds: u64,
    status: u32,
}

#[derive(Clone, Copy, Debug)]
pub(super) struct Baseline {
    pid: i32,
    uid: u32,
    birth_seconds: u64,
    birth_microseconds: u64,
}

pub(super) trait Probe {
    fn group(&mut self, pgid: i32) -> io::Result<Vec<i32>>;
    fn member(&mut self, pid: i32) -> io::Result<Member>;
}

fn unknown() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "unproved original process group",
    )
}

fn checked_group_ids(buffer: &[i32], bytes: i32) -> io::Result<Vec<i32>> {
    let bytes = usize::try_from(bytes).map_err(|_| unknown())?;
    if bytes == 0 || !bytes.is_multiple_of(size_of::<i32>()) {
        return Err(unknown());
    }
    let count = bytes / size_of::<i32>();
    // A full buffer cannot distinguish a complete list from truncation.
    if count >= buffer.len() || count >= MAX_GROUP_MEMBERS {
        return Err(unknown());
    }
    let mut ids = buffer[..count].to_vec();
    ids.sort_unstable();
    if ids.iter().any(|&pid| pid <= 1) || ids.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(unknown());
    }
    Ok(ids)
}

fn checked_info_size(bytes: i32, expected: usize) -> io::Result<()> {
    if usize::try_from(bytes).ok() == Some(expected) && expected == BSD_INFO_BYTES {
        Ok(())
    } else {
        Err(unknown())
    }
}

fn snapshot(baseline: Baseline, probe: &mut impl Probe) -> io::Result<Vec<Member>> {
    let ids = probe.group(baseline.pid)?;
    if ids.is_empty()
        || ids.len() >= MAX_GROUP_MEMBERS
        || !ids.contains(&baseline.pid)
        || ids.windows(2).any(|pair| pair[0] >= pair[1])
    {
        return Err(unknown());
    }
    let mut members = Vec::with_capacity(ids.len());
    for pid in ids {
        let member = probe.member(pid)?;
        if member.pid != pid
            || member.pgid != baseline.pid
            || member.uid != baseline.uid
            || member.status != ZOMBIE
            || member.birth_seconds == 0
            || member.birth_microseconds >= 1_000_000
            || (pid == baseline.pid
                && (member.birth_seconds != baseline.birth_seconds
                    || member.birth_microseconds != baseline.birth_microseconds))
        {
            return Err(unknown());
        }
        members.push(member);
    }
    Ok(members)
}

pub(super) fn reap_after_zombie_proof<T>(
    baseline: Option<Baseline>,
    leader_exited_without_reaping: bool,
    probe: &mut impl Probe,
    wait_original_child: impl FnOnce() -> io::Result<T>,
) -> io::Result<Option<T>> {
    let Some(baseline) = baseline else {
        return Ok(None);
    };
    if !leader_exited_without_reaping
        || baseline.pid <= 1
        || baseline.birth_seconds == 0
        || baseline.birth_microseconds >= 1_000_000
    {
        return Ok(None);
    }
    let complete = (|| {
        let first = snapshot(baseline, probe)?;
        // Re-read every birth, UID, PGID and state, not just the second PID list.
        let second = snapshot(baseline, probe)?;
        Ok::<_, io::Error>(first == second)
    })();
    if !matches!(complete, Ok(true)) {
        return Ok(None);
    }
    wait_original_child().map(Some)
}

#[cfg(target_os = "macos")]
// The SDK's libproc library implements the existing libc declarations below.
#[link(name = "proc")]
unsafe extern "C" {}

#[cfg(target_os = "macos")]
const _: () = {
    assert!(size_of::<libc::proc_bsdinfo>() == BSD_INFO_BYTES);
    assert!(std::mem::offset_of!(libc::proc_bsdinfo, pbi_status) == 4);
    assert!(std::mem::offset_of!(libc::proc_bsdinfo, pbi_pid) == 12);
    assert!(std::mem::offset_of!(libc::proc_bsdinfo, pbi_uid) == 20);
    assert!(std::mem::offset_of!(libc::proc_bsdinfo, pbi_pgid) == 100);
    assert!(std::mem::offset_of!(libc::proc_bsdinfo, pbi_start_tvsec) == 120);
    assert!(std::mem::offset_of!(libc::proc_bsdinfo, pbi_start_tvusec) == 128);
    assert!(libc::SZOMB == ZOMBIE);
};

#[cfg(target_os = "macos")]
pub(super) struct Native;

#[cfg(target_os = "macos")]
impl Probe for Native {
    fn group(&mut self, pgid: i32) -> io::Result<Vec<i32>> {
        let mut ids = [0_i32; MAX_GROUP_MEMBERS];
        // Unlike proc_listpgrppids, proc_listpids retains the exact returned byte
        // count, so malformed partial PID replies cannot be rounded into success.
        let bytes = unsafe {
            libc::proc_listpids(
                PROC_PGRP_ONLY,
                pgid as u32,
                ids.as_mut_ptr().cast(),
                std::mem::size_of_val(&ids) as libc::c_int,
            )
        };
        checked_group_ids(&ids, bytes)
    }

    fn member(&mut self, pid: i32) -> io::Result<Member> {
        let mut info = std::mem::MaybeUninit::<libc::proc_bsdinfo>::zeroed();
        // arg=1 explicitly includes zombies in PROC_PIDTBSDINFO.
        let bytes = unsafe {
            libc::proc_pidinfo(
                pid,
                libc::PROC_PIDTBSDINFO,
                1,
                info.as_mut_ptr().cast(),
                size_of::<libc::proc_bsdinfo>() as libc::c_int,
            )
        };
        checked_info_size(bytes, size_of::<libc::proc_bsdinfo>())?;
        let info = unsafe { info.assume_init() };
        Ok(Member {
            pid: i32::try_from(info.pbi_pid).map_err(|_| unknown())?,
            pgid: i32::try_from(info.pbi_pgid).map_err(|_| unknown())?,
            uid: info.pbi_uid,
            birth_seconds: info.pbi_start_tvsec,
            birth_microseconds: info.pbi_start_tvusec,
            status: info.pbi_status,
        })
    }
}

#[cfg(target_os = "macos")]
pub(super) fn capture_baseline(pid: u32) -> Option<Baseline> {
    let pid = i32::try_from(pid).ok().filter(|&pid| pid > 1)?;
    let uid = unsafe { libc::geteuid() };
    let member = Native.member(pid).ok()?;
    if member.pid != pid
        || member.pgid != pid
        || member.uid != uid
        || member.birth_seconds == 0
        || member.birth_microseconds >= 1_000_000
        || !(1..=ZOMBIE).contains(&member.status)
    {
        return None;
    }
    Some(Baseline {
        pid,
        uid,
        birth_seconds: member.birth_seconds,
        birth_microseconds: member.birth_microseconds,
    })
}

#[cfg(test)]
mod tests;
