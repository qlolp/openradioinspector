//! Test-only child ownership, including failed startup before the ready line.
use std::{
    io::{self, Read},
    process::{Child, ChildStdin, ChildStdout, Command, ExitStatus, Stdio},
    thread::JoinHandle,
};

pub const STDERR_BYTES: usize = 16 * 1024;

#[derive(Debug)]
pub struct Output {
    pub status: ExitStatus,
    pub stderr: Vec<u8>,
    pub stderr_overflow: bool,
}

struct CapturedStderr {
    bytes: Vec<u8>,
    overflow: bool,
}

pub struct OwnedChild {
    child: Option<Child>,
    stderr: Option<JoinHandle<io::Result<CapturedStderr>>>,
}

impl OwnedChild {
    pub fn spawn(command: &mut Command) -> io::Result<Self> {
        let child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;
        // Register ownership before thread creation or any readiness I/O.
        let mut owned = Self {
            child: Some(child),
            stderr: None,
        };
        let mut stderr = owned.child.as_mut().unwrap().stderr.take().unwrap();
        owned.stderr = Some(
            std::thread::Builder::new()
                .name("shared-io-fixture-stderr".into())
                .spawn(move || {
                    let mut kept = Vec::new();
                    let mut overflow = false;
                    let mut buffer = [0; 4096];
                    loop {
                        let size = stderr.read(&mut buffer)?;
                        if size == 0 {
                            return Ok(CapturedStderr {
                                bytes: kept,
                                overflow,
                            });
                        }
                        let retain = size.min(STDERR_BYTES - kept.len());
                        kept.extend_from_slice(&buffer[..retain]);
                        overflow |= retain != size;
                        // Keep draining after the cap so stderr cannot block
                        // the owned process before its actual wait/JOIN.
                    }
                })?,
        );
        Ok(owned)
    }

    pub fn stdin(&mut self) -> ChildStdin {
        self.child.as_mut().unwrap().stdin.take().unwrap()
    }

    pub fn stdout(&mut self) -> ChildStdout {
        self.child.as_mut().unwrap().stdout.take().unwrap()
    }

    pub fn id(&self) -> u32 {
        self.child.as_ref().unwrap().id()
    }

    pub fn join(mut self) -> io::Result<Output> {
        let child = self.child.as_mut().unwrap();
        drop(child.stdin.take());
        let status = child.wait()?;
        self.child.take();
        let stderr = self
            .stderr
            .take()
            .unwrap()
            .join()
            .map_err(|_| io::Error::other("stderr reader panicked"))??;
        Ok(Output {
            status,
            stderr: stderr.bytes,
            stderr_overflow: stderr.overflow,
        })
    }
}

impl Drop for OwnedChild {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            drop(child.stdin.take());
            let _ = child.kill(); // Only this still-owned child, never a found PID.
            let _ = child.wait();
        }
        if let Some(stderr) = self.stderr.take() {
            let _ = stderr.join();
        }
    }
}
