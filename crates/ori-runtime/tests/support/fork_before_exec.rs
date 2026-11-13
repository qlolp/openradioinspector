//! Hold an unrelated, owned child between fork and exec without using sleeps.
use std::{
    io::{self, Read, Write},
    os::{fd::AsRawFd, unix::net::UnixStream, unix::process::CommandExt},
    process::{Child, Command, ExitStatus},
    thread::JoinHandle,
    time::Duration,
};

pub struct ForkBeforeExec {
    barrier: UnixStream,
    spawning: Option<JoinHandle<io::Result<Child>>>,
}

impl ForkBeforeExec {
    pub fn spawn() -> io::Result<Self> {
        let (barrier, child_barrier) = UnixStream::pair()?;
        barrier.set_read_timeout(Some(Duration::from_secs(5)))?;
        let spawning = std::thread::Builder::new()
            .name("lease-fork-before-exec".into())
            .spawn(move || {
                let descriptor = child_barrier.as_raw_fd();
                let mut command = Command::new("/usr/bin/true");
                // Only async-signal-safe system calls run in the forked child.
                unsafe {
                    command.pre_exec(move || {
                        let ready = b'R';
                        if libc::write(descriptor, (&ready as *const u8).cast(), 1) != 1 {
                            return Err(io::Error::last_os_error());
                        }
                        let mut event = libc::pollfd {
                            fd: descriptor,
                            events: libc::POLLIN,
                            revents: 0,
                        };
                        // Bound cleanup even if the parent fails before releasing us.
                        if libc::poll(&mut event, 1, 10_000) <= 0 {
                            return Err(io::Error::from_raw_os_error(libc::ETIMEDOUT));
                        }
                        let mut release = 0u8;
                        if libc::read(descriptor, (&mut release as *mut u8).cast(), 1) != 1 {
                            return Err(io::Error::last_os_error());
                        }
                        Ok(())
                    });
                }
                let child = command.spawn();
                drop(child_barrier);
                child
            })?;
        let mut child = Self {
            barrier,
            spawning: Some(spawning),
        };
        let mut ready = [0];
        child.barrier.read_exact(&mut ready)?;
        if ready != *b"R" {
            return Err(io::Error::other("child did not enter the exec barrier"));
        }
        Ok(child)
    }

    pub fn finish(&mut self) -> io::Result<ExitStatus> {
        let release = self.barrier.write_all(b"X");
        let mut child = self
            .spawning
            .take()
            .ok_or_else(|| io::Error::other("fork child was already joined"))?
            .join()
            .map_err(|_| io::Error::other("spawn thread panicked"))??;
        let status = child.wait();
        if status.is_err() {
            let _ = child.kill();
            let _ = child.wait();
        }
        release?;
        status
    }
}

impl Drop for ForkBeforeExec {
    fn drop(&mut self) {
        if self.spawning.is_some() {
            let _ = self.finish();
        }
    }
}
