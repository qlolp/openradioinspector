//! Sleep continuity is independent of wall-clock changes. A new connection is
//! required after an epoch break; checking again cannot clear a latched failure.
use std::io;

#[derive(Clone, Copy, Debug)]
pub struct ClockSample {
    lower_offset: i128,
    upper_offset: i128,
}
impl ClockSample {
    pub fn new(absolute_before: u64, continuous: u64, absolute_after: u64) -> io::Result<Self> {
        if absolute_after < absolute_before || absolute_after - absolute_before > 1_000_000 {
            return Err(io::Error::other("invalid sleep clock sample"));
        }
        Ok(Self {
            lower_offset: i128::from(continuous) - i128::from(absolute_after),
            upper_offset: i128::from(continuous) - i128::from(absolute_before),
        })
    }
}

pub struct ContinuityFence {
    initial: ClockSample,
    broken: bool,
}
impl ContinuityFence {
    pub fn new(initial: ClockSample) -> Self {
        Self {
            initial,
            broken: false,
        }
    }
    pub fn system() -> io::Result<Self> {
        Ok(Self::new(system_sample()?))
    }
    pub fn check(&mut self, current: ClockSample) -> io::Result<()> {
        // Five milliseconds exceeds the bounded sampling uncertainty. Actual
        // system sleep advances continuous time, but not absolute uptime.
        self.broken |= current.lower_offset > self.initial.upper_offset.saturating_add(5_000_000)
            || self.initial.lower_offset > current.upper_offset.saturating_add(5_000_000);
        if self.broken {
            Err(io::Error::new(
                io::ErrorKind::ConnectionAborted,
                "system sleep interrupted coverage; reopen and prepare the instrument",
            ))
        } else {
            Ok(())
        }
    }
    pub fn check_system(&mut self) -> io::Result<()> {
        match system_sample() {
            Ok(sample) => self.check(sample),
            Err(error) => {
                self.broken = true;
                Err(error)
            }
        }
    }
}

/// The check runs before every physical read/write, independently of the
/// supervisor timer. Shutdown still reaches the underlying owned transport.
pub struct ContinuityTransport<T> {
    inner: T,
    fence: ContinuityFence,
}
impl<T> ContinuityTransport<T> {
    pub fn new(inner: T, fence: ContinuityFence) -> Self {
        Self { inner, fence }
    }
}
impl<T: tokio::io::AsyncRead + Unpin> tokio::io::AsyncRead for ContinuityTransport<T> {
    fn poll_read(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buffer: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<io::Result<()>> {
        let this = self.get_mut();
        if let Err(error) = this.fence.check_system() {
            return std::task::Poll::Ready(Err(error));
        }
        std::pin::Pin::new(&mut this.inner).poll_read(cx, buffer)
    }
}
impl<T: tokio::io::AsyncWrite + Unpin> tokio::io::AsyncWrite for ContinuityTransport<T> {
    fn poll_write(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        bytes: &[u8],
    ) -> std::task::Poll<io::Result<usize>> {
        let this = self.get_mut();
        if let Err(error) = this.fence.check_system() {
            return std::task::Poll::Ready(Err(error));
        }
        std::pin::Pin::new(&mut this.inner).poll_write(cx, bytes)
    }
    fn poll_flush(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<io::Result<()>> {
        let this = self.get_mut();
        if let Err(error) = this.fence.check_system() {
            return std::task::Poll::Ready(Err(error));
        }
        std::pin::Pin::new(&mut this.inner).poll_flush(cx)
    }
    fn poll_shutdown(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<io::Result<()>> {
        std::pin::Pin::new(&mut self.get_mut().inner).poll_shutdown(cx)
    }
}

#[cfg(not(target_os = "macos"))]
fn system_sample() -> io::Result<ClockSample> {
    ClockSample::new(0, 0, 0)
}

#[cfg(target_os = "macos")]
fn system_sample() -> io::Result<ClockSample> {
    #[repr(C)]
    struct Timebase {
        numer: u32,
        denom: u32,
    }
    unsafe extern "C" {
        fn mach_timebase_info(info: *mut Timebase) -> i32;
        fn mach_absolute_time() -> u64;
        fn mach_continuous_time() -> u64;
    }
    let mut timebase = Timebase { numer: 0, denom: 0 };
    if unsafe { mach_timebase_info(&mut timebase) } != 0
        || timebase.numer == 0
        || timebase.denom == 0
    {
        return Err(io::Error::other("sleep clock is unavailable"));
    }
    let nanos = |ticks: u64| -> io::Result<u64> {
        u64::try_from(u128::from(ticks) * u128::from(timebase.numer) / u128::from(timebase.denom))
            .map_err(|_| io::Error::other("sleep clock overflow"))
    };
    for _ in 0..3 {
        let before = nanos(unsafe { mach_absolute_time() })?;
        let continuous = nanos(unsafe { mach_continuous_time() })?;
        let after = nanos(unsafe { mach_absolute_time() })?;
        if let Ok(sample) = ClockSample::new(before, continuous, after) {
            return Ok(sample);
        }
    }
    Err(io::Error::other("sleep clock sampling remained ambiguous"))
}

/// IOPM only prevents automatic idle sleep. It does not override lid closure
/// or an operator's explicit sleep. The owner releases it on every exit path.
#[cfg(any(unix, test))]
#[derive(Default)]
pub(crate) struct IdleSleepAssertion {
    #[cfg(target_os = "macos")]
    id: Option<u32>,
}
#[cfg(any(unix, test))]
impl IdleSleepAssertion {
    pub fn set_armed(&mut self, armed: bool) -> io::Result<()> {
        #[cfg(target_os = "macos")]
        {
            if armed && self.id.is_none() {
                self.id = Some(darwin::create_assertion()?);
            }
            if !armed && let Some(id) = self.id.take() {
                darwin::release_assertion(id)?;
            }
        }
        #[cfg(not(target_os = "macos"))]
        if armed {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "idle sleep protection requires macOS IOPM",
            ));
        }
        Ok(())
    }
}

#[cfg(all(test, not(target_os = "macos")))]
mod unavailable_tests {
    #[test]
    fn other_platforms_do_not_claim_an_active_macos_sleep_assertion() {
        let mut assertion = super::IdleSleepAssertion::default();
        assert_eq!(
            assertion.set_armed(true).unwrap_err().kind(),
            std::io::ErrorKind::Unsupported
        );
        assertion.set_armed(false).unwrap();
    }
}
#[cfg(any(unix, test))]
impl Drop for IdleSleepAssertion {
    fn drop(&mut self) {
        let _ = self.set_armed(false);
    }
}

#[cfg(target_os = "macos")]
mod darwin {
    use std::{
        ffi::{c_char, c_void},
        io, ptr,
    };
    type CFString = *const c_void;
    #[link(name = "CoreFoundation", kind = "framework")]
    unsafe extern "C" {
        fn CFStringCreateWithCString(
            allocator: *const c_void,
            string: *const c_char,
            encoding: u32,
        ) -> CFString;
        fn CFRelease(value: *const c_void);
    }
    #[link(name = "IOKit", kind = "framework")]
    unsafe extern "C" {
        fn IOPMAssertionCreateWithName(
            kind: CFString,
            level: u32,
            name: CFString,
            id: *mut u32,
        ) -> i32;
        fn IOPMAssertionRelease(id: u32) -> i32;
    }
    pub fn create_assertion() -> io::Result<u32> {
        unsafe {
            let kind = CFStringCreateWithCString(
                ptr::null(),
                c"PreventUserIdleSystemSleep".as_ptr(),
                0x08000100,
            );
            let name = CFStringCreateWithCString(
                ptr::null(),
                c"OpenRadioInspector armed monitoring".as_ptr(),
                0x08000100,
            );
            if kind.is_null() || name.is_null() {
                if !kind.is_null() {
                    CFRelease(kind);
                }
                if !name.is_null() {
                    CFRelease(name);
                }
                return Err(io::Error::other("cannot allocate sleep assertion"));
            }
            let mut id = 0;
            let status = IOPMAssertionCreateWithName(kind, 255, name, &mut id);
            CFRelease(kind);
            CFRelease(name);
            if status != 0 {
                return Err(io::Error::other("cannot prevent idle sleep"));
            }
            Ok(id)
        }
    }
    pub fn release_assertion(id: u32) -> io::Result<()> {
        if unsafe { IOPMAssertionRelease(id) } != 0 {
            return Err(io::Error::other("cannot release idle sleep assertion"));
        }
        Ok(())
    }
}
