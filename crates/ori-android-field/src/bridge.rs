use crate::{Capture, Plan};
use jni::{
    Env, EnvUnowned,
    errors::{Result as JniResult, ThrowRuntimeExAndDefault},
    jni_str,
    objects::{JObject, JString},
    strings::JNIString,
};
use std::{
    collections::HashMap,
    io,
    os::{fd::FromRawFd, unix::net::UnixStream},
    sync::{
        Arc, LazyLock, Mutex, MutexGuard,
        atomic::{AtomicI64, Ordering},
    },
};
use tokio::runtime::{Builder, Runtime};

struct Session {
    runtime: Runtime,
    capture: Capture,
    failed: bool,
}

type Shared = Arc<Mutex<Session>>;
static HANDLES: LazyLock<Mutex<HashMap<i64, Shared>>> = LazyLock::new(Mutex::default);
static NEXT: AtomicI64 = AtomicI64::new(1);

fn lock<T>(value: &Mutex<T>) -> io::Result<MutexGuard<'_, T>> {
    value
        .lock()
        .map_err(|_| io::Error::other("field capture lock poisoned"))
}

impl Session {
    fn open(fd: i32, json: &str) -> io::Result<Self> {
        if json.len() > 256 * 1024 {
            return Err(io::Error::other("capture plan exceeds 256 KiB"));
        }
        let plan: Plan = serde_json::from_str(json)?;
        // fcntl validates even a stale number. Kotlin keeps its descriptor live
        // across open(); Rust exclusively owns the independently closable copy.
        let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
        if duplicate < 0 {
            return Err(io::Error::last_os_error());
        }
        let socket = unsafe { UnixStream::from_raw_fd(duplicate) };
        socket.peer_addr()?;
        socket.set_nonblocking(true)?;
        let runtime = Builder::new_current_thread().enable_all().build()?;
        let stream = {
            let _entered = runtime.enter();
            tokio::net::UnixStream::from_std(socket)?
        };
        let capture = Capture::new(stream, plan).map_err(io::Error::other)?;
        Ok(Self {
            runtime,
            capture,
            failed: false,
        })
    }

    fn next(&mut self) -> io::Result<Option<String>> {
        if self.failed {
            return Err(io::Error::other("capture failed; reconnect the instrument"));
        }
        match self.runtime.block_on(self.capture.next()) {
            Ok(value) => Ok(value.map(|v| v.to_string())),
            Err(e) => {
                self.failed = true;
                Err(io::Error::other(e))
            }
        }
    }

    fn close(&mut self) -> io::Result<()> {
        self.runtime
            .block_on(self.capture.close())
            .map_err(io::Error::other)
    }
}

fn report<T: Default>(env: &mut Env<'_>, result: io::Result<T>) -> JniResult<T> {
    match result {
        Ok(value) => Ok(value),
        Err(error) => {
            env.throw_new(
                jni_str!("java/io/IOException"),
                JNIString::from(error.to_string()),
            )?;
            Ok(T::default())
        }
    }
}

#[unsafe(no_mangle)]
pub extern "system" fn Java_org_openradioinspector_field_NativeCapture_open<'a>(
    mut entry: EnvUnowned<'a>,
    _this: JObject<'a>,
    fd: i32,
    plan: JString<'a>,
) -> i64 {
    entry
        .with_env(|env| {
            let json = plan.try_to_string(env)?;
            let result = Session::open(fd, &json).and_then(|session| {
                let mut handles = lock(&HANDLES)?;
                let id = NEXT.fetch_add(1, Ordering::Relaxed);
                handles.insert(id, Arc::new(Mutex::new(session)));
                Ok(id)
            });
            report(env, result)
        })
        .resolve::<ThrowRuntimeExAndDefault>()
}

#[unsafe(no_mangle)]
pub extern "system" fn Java_org_openradioinspector_field_NativeCapture_next<'a>(
    mut entry: EnvUnowned<'a>,
    _this: JObject<'a>,
    id: i64,
) -> JString<'a> {
    entry
        .with_env(|env| {
            let result = (|| {
                let handle = lock(&HANDLES)?
                    .get(&id)
                    .cloned()
                    .ok_or_else(|| io::Error::other("capture closed"))?;
                lock(&handle)?.next()
            })();
            match report(env, result)? {
                Some(json) => env.new_string(json),
                None => Ok(JString::default()),
            }
        })
        .resolve::<ThrowRuntimeExAndDefault>()
}

#[unsafe(no_mangle)]
pub extern "system" fn Java_org_openradioinspector_field_NativeCapture_close<'a>(
    mut entry: EnvUnowned<'a>,
    _this: JObject<'a>,
    id: i64,
) {
    entry
        .with_env(|env| {
            let result = (|| {
                let removed = lock(&HANDLES)?.remove(&id);
                if let Some(handle) = removed {
                    lock(&handle)?.close()?;
                }
                Ok(())
            })();
            report(env, result)
        })
        .resolve::<ThrowRuntimeExAndDefault>()
}

#[cfg(test)]
mod tests;
