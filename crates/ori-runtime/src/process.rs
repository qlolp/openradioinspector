//! Process handles are created here and never reconstructed from a PID or port.
use crate::Result;
use std::process::{Child, Command, ExitStatus};

#[cfg(any(target_os = "macos", test))]
mod darwin;

#[cfg(all(test, target_os = "macos"))]
mod darwin_native_tests;

pub(crate) struct OwnedChild {
    child: Child,
    pid: u32,
    reaped: bool,
    #[cfg(target_os = "macos")]
    darwin_baseline: Option<darwin::Baseline>,
}
impl OwnedChild {
    pub(crate) fn spawn(command: &mut Command) -> Result<Self> {
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let child = command.spawn()?;
        let pid = child.id();
        let owned = Self {
            child,
            pid,
            reaped: false,
            #[cfg(target_os = "macos")]
            darwin_baseline: None,
        };
        // Construct the original Child guard first. An unavailable baseline is
        // unusable, never a spawn error which drops an unowned created Child.
        #[cfg(target_os = "macos")]
        let owned = {
            let mut owned = owned;
            owned.darwin_baseline = darwin::capture_baseline(pid);
            owned
        };
        Ok(owned)
    }
    pub(crate) fn pid(&self) -> u32 {
        self.pid
    }
    pub(crate) fn child_mut(&mut self) -> &mut Child {
        &mut self.child
    }
    /// On Unix leave the owned leader unreaped, reserving its PID/PGID until group cleanup.
    pub(crate) fn exited(&mut self) -> Result<bool> {
        #[cfg(unix)]
        {
            let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
            let result = unsafe {
                libc::waitid(
                    libc::P_PID,
                    self.pid as libc::id_t,
                    &mut info,
                    libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                )
            };
            if result < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            Ok(unsafe { info.si_pid() } != 0)
        }
        #[cfg(not(unix))]
        {
            let status = self.child.try_wait()?;
            self.reaped = status.is_some();
            Ok(self.reaped)
        }
    }
    pub(crate) fn terminate_and_reap(&mut self) -> Result<ExitStatus> {
        if !self.reaped {
            #[cfg(unix)]
            {
                // This group was created for this unreaped owned Child. Never signal foreign PIDs.
                let result = unsafe { libc::kill(-(self.pid as i32), libc::SIGKILL) };
                if result < 0 {
                    let error = std::io::Error::last_os_error();
                    if error.raw_os_error() != Some(libc::ESRCH) {
                        #[cfg(target_os = "macos")]
                        if error.raw_os_error() == Some(libc::EPERM) {
                            let exited = self.exited().unwrap_or(false);
                            if let Some(status) = darwin::reap_after_zombie_proof(
                                self.darwin_baseline,
                                exited,
                                &mut darwin::Native,
                                || self.child.wait(),
                            )? {
                                self.reaped = true;
                                return Ok(status);
                            }
                        }
                        return Err(error.into());
                    }
                }
            }
            #[cfg(not(unix))]
            if self.child.try_wait()?.is_none() {
                self.child.kill()?;
            }
            let status = self.child.wait()?;
            self.reaped = true;
            return Ok(status);
        }
        Ok(self.child.wait()?)
    }
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        if !self.reaped {
            let _ = self.terminate_and_reap();
        }
    }
}
