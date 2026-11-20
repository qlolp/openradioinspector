//! Start the backend in an unnamed, non-inheritable Job. The job assignment
//! and the explicit stdio allowlist are part of CreateProcessW itself.
use std::{
    collections::BTreeMap,
    ffi::OsStr,
    fs::OpenOptions,
    io::{self, PipeWriter},
    mem::{size_of, size_of_val},
    os::windows::{
        ffi::OsStrExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
    },
    process::Command,
    ptr::{null, null_mut},
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::{HANDLE, HANDLE_FLAG_INHERIT, SetHandleInformation, WAIT_OBJECT_0, WAIT_TIMEOUT},
    System::{JobObjects::*, Threading::*},
};

fn check(result: i32) -> io::Result<()> {
    if result == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
fn wide(value: &OsStr) -> io::Result<Vec<u16>> {
    let units: Vec<_> = value.encode_wide().collect();
    if units.contains(&0) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "embedded NUL"));
    }
    Ok(units)
}

// Quote individual arguments using the Windows C runtime's argv rules. Never
// invoke a shell. lpApplicationName is supplied separately and absolutely.
fn command_line(command: &Command) -> io::Result<Vec<u16>> {
    let mut result = Vec::new();
    for arg in std::iter::once(command.get_program()).chain(command.get_args()) {
        if !result.is_empty() {
            result.push(32);
        }
        result.push(34);
        let mut slashes = 0;
        for unit in wide(arg)? {
            if unit == 92 {
                slashes += 1;
                continue;
            }
            let count = if unit == 34 { slashes * 2 + 1 } else { slashes };
            result.extend(std::iter::repeat_n(92, count));
            result.push(unit);
            slashes = 0;
        }
        result.extend(std::iter::repeat_n(92, slashes * 2));
        result.push(34);
    }
    result.push(0);
    Ok(result)
}

fn environment(command: &Command) -> io::Result<Vec<u16>> {
    // BackendSpec can add/override variables. Preserve native UTF-16 values,
    // including inherited non-Unicode paths.
    let mut vars = BTreeMap::new();
    let key = |name: &OsStr| name.to_string_lossy().to_uppercase();
    for (name, val) in std::env::vars_os() {
        vars.insert(key(&name), (name, val));
    }
    for (name, value) in command.get_envs() {
        if let Some(value) = value {
            vars.insert(key(name), (name.to_owned(), value.to_owned()));
        } else {
            vars.remove(&key(name));
        }
    }
    let mut block = Vec::new();
    for (_, (name, value)) in vars {
        let mut entry = name;
        entry.push("=");
        entry.push(value);
        block.extend(wide(&entry)?);
        block.push(0);
    }
    block.push(0);
    Ok(block)
}

// Windows stores pointers to the two handle slices until CreateProcess returns.
// These and the aligned backing allocation outlive the STARTUPINFOEX.
struct Attributes(Vec<usize>);
impl Attributes {
    fn new() -> io::Result<Self> {
        let mut size = 0;
        unsafe { InitializeProcThreadAttributeList(null_mut(), 2, 0, &mut size) };
        if size == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut storage = vec![0usize; size.div_ceil(size_of::<usize>())];
        check(unsafe {
            InitializeProcThreadAttributeList(storage.as_mut_ptr().cast(), 2, 0, &mut size)
        })?;
        Ok(Self(storage))
    }
    fn set(&mut self, attr: u32, handles: &[HANDLE]) -> io::Result<()> {
        check(unsafe {
            UpdateProcThreadAttribute(
                self.0.as_mut_ptr().cast(),
                0,
                attr as usize,
                handles.as_ptr().cast(),
                size_of_val(handles),
                null_mut(),
                null(),
            )
        })
    }
}
impl Drop for Attributes {
    fn drop(&mut self) {
        unsafe { DeleteProcThreadAttributeList(self.0.as_mut_ptr().cast()) };
    }
}

/// Original process handle retained by the launcher. Closing it leaves the
/// process lifetime to its owner (the installed node, or the backend Job).
pub(crate) struct WindowsProcess {
    handle: OwnedHandle,
    pid: u32,
}
impl WindowsProcess {
    fn spawn(
        command: &Command,
        startup: &STARTUPINFOW,
        inherit_handles: bool,
        flags: u32,
    ) -> io::Result<Self> {
        let mut program = wide(command.get_program())?;
        program.push(0);
        let mut args = command_line(command)?;
        let env = environment(command)?;
        let mut info = PROCESS_INFORMATION::default();
        check(unsafe {
            CreateProcessW(
                program.as_ptr(),
                args.as_mut_ptr(),
                null(),
                null(),
                i32::from(inherit_handles),
                CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | flags,
                env.as_ptr().cast(),
                null(),
                startup,
                &mut info,
            )
        })?;
        drop(unsafe { OwnedHandle::from_raw_handle(info.hThread) });
        Ok(Self {
            handle: unsafe { OwnedHandle::from_raw_handle(info.hProcess) },
            pid: info.dwProcessId,
        })
    }

    pub(crate) fn launch_controller(command: &Command) -> io::Result<Self> {
        // The operator node outlives its start command. Even with NUL stdio,
        // inheriting all handles keeps the caller's redirected pipe writer open.
        let startup = STARTUPINFOW {
            cb: size_of::<STARTUPINFOW>() as u32,
            ..Default::default()
        };
        Self::spawn(command, &startup, false, 0)
    }

    pub(crate) fn exited(&self) -> io::Result<bool> {
        match unsafe { WaitForSingleObject(self.handle.as_raw_handle(), 0) } {
            WAIT_OBJECT_0 => Ok(true),
            WAIT_TIMEOUT => Ok(false),
            _ => Err(io::Error::last_os_error()),
        }
    }
}

pub(crate) struct JobChild {
    job: OwnedHandle,
    process: WindowsProcess,
    _lifeline: Option<PipeWriter>,
}
impl JobChild {
    pub(crate) fn spawn(command: &Command, lifeline: bool) -> io::Result<Self> {
        let raw_job = unsafe { CreateJobObjectW(null(), null()) };
        if raw_job.is_null() {
            return Err(io::Error::last_os_error());
        }
        let job = unsafe { OwnedHandle::from_raw_handle(raw_job) };
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        check(unsafe {
            SetInformationJobObject(
                raw_job,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of_val(&limits) as u32,
            )
        })?;

        let sink = OpenOptions::new().read(true).write(true).open("NUL")?;
        let pipe = if lifeline { Some(io::pipe()?) } else { None };
        let output = sink.as_raw_handle();
        let input = pipe.as_ref().map_or(output, |(r, _)| r.as_raw_handle());
        let mut inherited = vec![output];
        if input != output {
            inherited.push(input);
        }
        for handle in &inherited {
            check(unsafe {
                SetHandleInformation(*handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
            })?;
        }
        let jobs = [raw_job];
        let mut attributes = Attributes::new()?;
        attributes.set(PROC_THREAD_ATTRIBUTE_JOB_LIST, &jobs)?;
        attributes.set(PROC_THREAD_ATTRIBUTE_HANDLE_LIST, &inherited)?;
        let mut startup = STARTUPINFOEXW::default();
        startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = input;
        startup.StartupInfo.hStdOutput = output;
        startup.StartupInfo.hStdError = output;
        startup.lpAttributeList = attributes.0.as_mut_ptr().cast();
        let process = WindowsProcess::spawn(
            command,
            &startup.StartupInfo,
            true,
            EXTENDED_STARTUPINFO_PRESENT,
        )?;
        Ok(Self {
            job,
            process,
            _lifeline: pipe.map(|(_, writer)| writer),
        })
    }
    pub(crate) fn pid(&self) -> u32 {
        self.process.pid
    }
    pub(crate) fn exited(&self) -> io::Result<bool> {
        self.process.exited()
    }
    pub(crate) fn exit_code(&self) -> io::Result<u32> {
        let mut code = 0;
        check(unsafe { GetExitCodeProcess(self.process.handle.as_raw_handle(), &mut code) })?;
        Ok(code)
    }
    pub(crate) fn terminate_and_reap(&self) -> io::Result<()> {
        let handle = self.job.as_raw_handle();
        check(unsafe { TerminateJobObject(handle, 1) })?;
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let mut counts = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
            check(unsafe {
                QueryInformationJobObject(
                    handle,
                    JobObjectBasicAccountingInformation,
                    (&mut counts as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    size_of_val(&counts) as u32,
                    null_mut(),
                )
            })?;
            if counts.ActiveProcesses == 0 {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "owned Windows job has live processes",
                ));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use fs2::FileExt;
    use std::{fs::File, path::PathBuf};

    const DIRECTORY: &str = "ORI_WINDOWS_TEST_JOB_DIRECTORY";
    const PHASE: &str = "ORI_WINDOWS_TEST_JOB_PHASE";
    const CHILD: &str = "process::windows_job::tests::job_fixture";

    const CAPTURE_FIXTURE: &str = "process::windows_job::tests::capture_fixture";
    const CAPTURE_DIRECTORY: &str = "ORI_TEST_WINDOWS_CAPTURE_DIRECTORY";

    #[test]
    fn capture_fixture() {
        let Some(dir) = std::env::var_os(CAPTURE_DIRECTORY).map(PathBuf::from) else {
            return;
        };
        if std::env::var_os("ORI_TEST_WINDOWS_CAPTURE_NODE").is_some() {
            std::fs::write(dir.join("ready"), []).unwrap();
            let deadline = Instant::now() + Duration::from_secs(20);
            while !dir.join("stop").exists() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
            std::fs::write(dir.join("finished"), []).unwrap();
        } else {
            // The start helper itself has the parent's redirected stdout/stderr.
            let mut service = Command::new(std::env::current_exe().unwrap());
            service
                .args(["--exact", CAPTURE_FIXTURE])
                .env("ORI_TEST_WINDOWS_CAPTURE_NODE", "1");
            drop(WindowsProcess::launch_controller(&service).unwrap());
        }
    }

    #[test]
    fn captured_start_reaches_eof_while_the_launched_node_is_still_running() {
        let dir = tempfile::tempdir().unwrap();
        let mut start = Command::new(std::env::current_exe().unwrap());
        start
            .args(["--exact", CAPTURE_FIXTURE])
            .env(CAPTURE_DIRECTORY, dir.path());
        let (sender, result) = std::sync::mpsc::channel();
        let reader = std::thread::spawn(move || sender.send(start.output()).unwrap());
        let ready = dir.path().join("ready");
        let deadline = Instant::now() + Duration::from_secs(10);
        while !ready.exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        let reply = result.recv_timeout(Duration::from_secs(2));
        let finished = dir.path().join("finished");
        let observed_live_node = ready.exists() && !finished.exists();
        std::fs::write(dir.path().join("stop"), []).unwrap();
        reader.join().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !finished.exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(finished.exists() && observed_live_node);
        assert!(
            reply
                .expect("start retained a caller output pipe")
                .unwrap()
                .status
                .success()
        );
    }

    #[test]
    fn job_fixture() {
        let Some(directory) = std::env::var_os(DIRECTORY).map(PathBuf::from) else {
            return;
        };
        if std::env::var_os(PHASE).as_deref() == Some(OsStr::new("descendant")) {
            let lock = File::open(directory.join("lock")).unwrap();
            lock.try_lock_exclusive().unwrap();
            std::fs::write(directory.join("ready"), []).unwrap();
            std::thread::sleep(Duration::from_secs(30));
            return;
        }
        // Leave the descendant alive when the leader exits. The test's original
        // Job is its only cleanup authority.
        let child = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", CHILD])
            .env(PHASE, "descendant")
            .spawn()
            .unwrap();
        drop(child);
        let deadline = Instant::now() + Duration::from_secs(10);
        while !directory.join("ready").exists() {
            assert!(
                Instant::now() < deadline,
                "descendant did not take fixture lock"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    #[test]
    fn job_owns_descendants_after_leader_exit_and_on_last_handle_close() {
        for explicit in [true, false] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("lock");
            let observer = File::create(&path).unwrap();
            let mut command = Command::new(std::env::current_exe().unwrap());
            command.args(["--exact", CHILD]).env(DIRECTORY, dir.path());
            let owner = JobChild::spawn(&command, true).unwrap();
            let deadline = Instant::now() + Duration::from_secs(10);
            while !owner.exited().unwrap() {
                assert!(Instant::now() < deadline);
                std::thread::sleep(Duration::from_millis(10));
            }
            assert_eq!(owner.exit_code().unwrap(), 0);
            assert!(dir.path().join("ready").is_file());
            assert_eq!(
                observer.try_lock_exclusive().unwrap_err().raw_os_error(),
                fs2::lock_contended_error().raw_os_error()
            );
            if explicit {
                owner.terminate_and_reap().unwrap();
            }
            drop(owner);
            let deadline = Instant::now() + Duration::from_secs(5);
            while observer.try_lock_exclusive().is_err() {
                assert!(
                    Instant::now() < deadline,
                    "descendant survived the original Job"
                );
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
}
