"""Private release-process ownership. No application or hardware dispatch lives here."""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_PROCESSES = 262144
MAX_RECORD_BYTES = 4096
MAX_REGISTRIES = 4096

# A cleanup failure must retain its Popen/stdin handle as well as its files.
# In particular, garbage collection must not close the parent-death lifeline.
_retained_commands = {}


class OwnershipError(RuntimeError):
    """Numeric ownership evidence is unavailable or no longer matches."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    sid: int
    start: int
    zombie: bool = False

    def __post_init__(self):
        for name in ("pid", "ppid", "pgid", "sid", "start"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise OwnershipError("Invalid numeric process identity")
        if self.pid <= 0 or type(self.zombie) is not bool:
            raise OwnershipError("Reserved process identity")

    def same_process(self, other):
        return other is not None and (self.pid, self.start) == (other.pid, other.start)


class LinuxKernel:
    def __init__(self, proc_root=Path("/proc")):
        self.proc_root = Path(proc_root)

    def identity(self, pid):
        try:
            raw = (self.proc_root / str(pid) / "stat").read_bytes()
        except FileNotFoundError:
            return None
        fields = raw[raw.rfind(b")") + 2 :].split()
        if len(fields) < 20:
            raise OwnershipError("Incomplete numeric process evidence")
        # Ignore the kernel comm/name field completely, including its contents.
        return ProcessIdentity(
            pid,
            int(fields[1]),
            int(fields[2]),
            int(fields[3]),
            int(fields[19]),
            fields[0] in (b"Z", b"X"),
        )

    def session(self, sid):
        result = []
        scanned = 0
        for entry in self.proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            scanned += 1
            if scanned > MAX_PROCESSES:
                raise OwnershipError("Numeric process budget exceeded")
            # PID1 cannot belong to the newly created private session.
            if int(entry.name) <= 1:
                continue
            identity = self.identity(int(entry.name))
            if identity is not None and identity.sid == sid:
                result.append(identity)
        return result

    def signal(self, identity, signum, sid, *, deadline=None):
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise OwnershipError("Stable Linux process signaling is unavailable")
        try:
            descriptor = os.pidfd_open(identity.pid, 0)
        except ProcessLookupError:
            return
        try:
            current = self.identity(identity.pid)
            if current is None:
                return
            if not identity.same_process(current) or current.sid != sid:
                raise OwnershipError("Process identity changed before signaling")
            if not current.zombie:
                signal.pidfd_send_signal(descriptor, signum, None, 0)
        except ProcessLookupError:
            pass
        finally:
            os.close(descriptor)


class _BSDInfo(ctypes.Structure):
    # Public proc_bsdinfo layout. Character storage is opaque padding: never
    # inspected, decoded, logged or used as ownership evidence.
    _fields_ = [
        ("numeric", ctypes.c_uint32 * 12),
        ("_opaque", ctypes.c_byte * 48),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("jobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_seconds", ctypes.c_uint64),
        ("start_microseconds", ctypes.c_uint64),
    ]


class DarwinKernel:
    def __init__(self, api=None):
        self.groups = None
        self.api = api or ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.api.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.api.proc_pidinfo.restype = ctypes.c_int
        self.api.proc_listpids.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.api.proc_listpids.restype = ctypes.c_int

    def identity(self, pid):
        info = _BSDInfo()
        count = self.api.proc_pidinfo(
            pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if count == 0 and ctypes.get_errno() == errno.ESRCH:
            return None
        if count != ctypes.sizeof(info):
            raise OwnershipError("Darwin numeric process evidence unavailable")
        try:
            sid = os.getsid(pid)
        except ProcessLookupError:
            return None
        return ProcessIdentity(
            int(info.numeric[3]),
            int(info.numeric[4]),
            int(info.pgid),
            sid,
            int(info.start_seconds) * 1000000 + int(info.start_microseconds),
            int(info.numeric[1]) == 5,
        )

    def session(self, sid):
        for _ in range(3):
            needed = self.api.proc_listpids(1, 0, None, 0)
            if needed <= 0 or needed > MAX_PROCESSES * ctypes.sizeof(ctypes.c_int):
                raise OwnershipError("Darwin numeric process budget unavailable")
            capacity = min(MAX_PROCESSES, needed // ctypes.sizeof(ctypes.c_int) + 64)
            buffer = (ctypes.c_int * capacity)()
            count = self.api.proc_listpids(1, 0, buffer, ctypes.sizeof(buffer))
            if count < 0:
                raise OwnershipError("Darwin numeric process enumeration failed")
            if count >= ctypes.sizeof(buffer):
                continue
            result = []
            for pid in buffer[: count // ctypes.sizeof(ctypes.c_int)]:
                if pid <= 1:
                    continue
                try:
                    if os.getsid(pid) != sid:
                        continue
                except ProcessLookupError:
                    continue
                identity = self.identity(pid)
                if identity is not None and identity.sid == sid:
                    result.append(identity)
            return result
        raise OwnershipError("Darwin process enumeration did not fit its bound")

    def signal(self, identity, signum, sid, *, deadline=None):
        current = self.identity(identity.pid)
        if current is None:
            return
        if not identity.same_process(current) or current.sid != sid:
            raise OwnershipError("Process identity changed before signaling")
        if not current.zombie:
            if deadline is None:
                raise OwnershipError("Pinned group signaling needs a cleanup deadline")
            if self.groups is None:
                anchor = self.identity(sid)
                if anchor is None:
                    raise OwnershipError("Private session anchor is unavailable")
                self.groups = PinnedProcessGroups(self, anchor)
            self.groups.signal(current, signum, deadline)

    def is_keeper(self, identity):
        return self.groups is not None and self.groups.is_keeper(identity)

    def release_keepers(self, deadline):
        if self.groups is not None:
            self.groups.release(deadline)


def local_kernel():
    if sys.platform.startswith("linux"):
        return LinuxKernel()
    if sys.platform == "darwin":
        return DarwinKernel()
    raise OwnershipError("Private POSIX sessions are unavailable on this platform")


class SessionAuthority:
    def __init__(self, anchor, *, kernel=None):
        self.anchor = anchor
        self.kernel = kernel or local_kernel()
        if anchor.pid <= 1 or anchor.pid != anchor.sid or anchor.pid != anchor.pgid:
            raise OwnershipError(
                "Session anchor is not its private session/group leader"
            )

    def _verify_anchor(self):
        current = self.kernel.identity(self.anchor.pid)
        if (
            not self.anchor.same_process(current)
            or current.zombie
            or current.sid != self.anchor.sid
            or current.pgid != self.anchor.pgid
        ):
            raise OwnershipError("Session anchor identity changed or disappeared")

    def members(self):
        try:
            self._verify_anchor()
            return [
                item
                for item in self.kernel.session(self.anchor.sid)
                if item.sid == self.anchor.sid
                and item.pid != self.anchor.pid
                and not item.zombie
                and not getattr(self.kernel, "is_keeper", lambda _item: False)(item)
            ]
        except (OSError, ValueError, TypeError) as error:
            raise OwnershipError("Owned session membership is unknown") from error

    def signal(self, member, signum, *, deadline=None):
        try:
            self._verify_anchor()
            if member.pid == self.anchor.pid:
                raise OwnershipError("The session anchor must remain held")
            current = self.kernel.identity(member.pid)
            if current is None:
                return
            if not member.same_process(current) or current.sid != self.anchor.sid:
                raise OwnershipError("Owned member identity changed")
            if not current.zombie:
                self.kernel.signal(current, signum, self.anchor.sid, deadline=deadline)
        except (OSError, ValueError, TypeError) as error:
            raise OwnershipError("Owned member signaling is unverified") from error


@dataclass
class GroupKeeper:
    """An unreaped child pins its PGID even after an authorized group KILL."""

    pid: int
    pgid: int
    control_fd: int
    ready_fd: int
    identity: ProcessIdentity | None = None
    ready: bool = False
    ready_buffer: bytes = b""
    invalid: bool = False
    kill_sent: bool = False
    reaped: bool = False


class PinnedProcessGroups:
    """Stable group signaling for Darwin, runnable against the Linux kernel too.

    A private child joins the target group from inside the held SID. The kernel
    rejects foreign-session joins, including target-exit/PID-reuse races. Only
    a ready, numerically verified live keeper authorizes a new group signal;
    every keeper remains unreaped until all actual/nested members have closed.
    """

    def __init__(self, kernel, anchor):
        self.kernel = kernel
        self.anchor = anchor
        self.keepers = []
        self.current = {}
        self.delivered = set()
        self._verify_anchor()

    def _verify_anchor(self):
        if (
            os.getpid() != self.anchor.pid
            or self.anchor.pid != self.anchor.sid
            or self.anchor.pgid != self.anchor.sid
            or self.anchor.pid <= 1
        ):
            raise OwnershipError(
                "Group keepers must join from inside the held anchor SID"
            )
        current = self.kernel.identity(self.anchor.pid)
        if (
            not self.anchor.same_process(current)
            or current.zombie
            or current.sid != self.anchor.sid
            or current.pgid != self.anchor.pgid
        ):
            raise OwnershipError("Group keeper anchor identity is unavailable")

    def is_keeper(self, identity):
        return any(
            not held.reaped
            and held.identity is not None
            and held.identity.same_process(identity)
            and identity.ppid == self.anchor.pid
            and identity.sid == self.anchor.sid
            and identity.pgid == held.pgid
            for held in self.keepers
        )

    def spawn_keeper(self, pgid):
        self._verify_anchor()
        if pgid <= 1 or pgid == self.anchor.pgid:
            raise OwnershipError("The session anchor group cannot be a signal target")
        control_read, control_write = os.pipe()
        try:
            ready_read, ready_write = os.pipe()
        except BaseException:
            os.close(control_read)
            os.close(control_write)
            raise
        try:
            # -I/-S excludes user/site startup hooks. The keeper only writes its
            # readiness byte sequence, then blocks on its private EOF lifeline.
            pid = os.posix_spawnp(
                sys.executable,
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "import os; os.write(1,b'ready\\n'); os.read(0,1)",
                ],
                os.environ,
                setpgroup=pgid,
                setsigmask=(signal.SIGTERM, signal.SIGINT),
                setsigdef=(signal.SIGTERM, signal.SIGINT),
                file_actions=[
                    (os.POSIX_SPAWN_DUP2, control_read, 0),
                    (os.POSIX_SPAWN_DUP2, ready_write, 1),
                    (os.POSIX_SPAWN_CLOSE, control_read),
                    (os.POSIX_SPAWN_CLOSE, ready_write),
                ],
            )
            return GroupKeeper(pid, pgid, control_write, ready_read)
        except BaseException:
            os.close(control_write)
            os.close(ready_read)
            raise
        finally:
            os.close(control_read)
            os.close(ready_write)

    def _keeper_identity(self, held, *, live=True):
        if held.reaped or held.invalid:
            raise OwnershipError("Group keeper ownership is unavailable")
        current = self.kernel.identity(held.pid)
        if (
            current is None
            or current.ppid != self.anchor.pid
            or current.sid != self.anchor.sid
            or current.pgid != held.pgid
            or (held.identity is not None and not held.identity.same_process(current))
            or (live and current.zombie)
        ):
            raise OwnershipError("Group keeper exited or changed identity")
        if held.identity is None:
            held.identity = current
        return current

    def _ready(self, held, deadline):
        self._keeper_identity(held)
        while not held.ready:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OwnershipError(
                    "Group keeper readiness deadline; ownership retained"
                )
            readable, _, _ = select.select(
                [held.ready_fd], [], [], min(0.01, remaining)
            )
            if not readable:
                continue
            chunk = os.read(held.ready_fd, 17)
            held.ready_buffer += chunk
            if not chunk or len(held.ready_buffer) > 16:
                held.invalid = True
                raise OwnershipError("Group keeper readiness is unavailable")
            if b"\n" in held.ready_buffer:
                if held.ready_buffer != b"ready\n":
                    held.invalid = True
                    raise OwnershipError("Invalid group keeper readiness")
                self._keeper_identity(held)
                held.ready = True
                os.close(held.ready_fd)
                held.ready_fd = -1
        self._verify_anchor()
        self._keeper_identity(held)

    def signal(self, member, signum, deadline):
        try:
            self._verify_anchor()
            if signum not in (signal.SIGTERM, getattr(signal, "SIGKILL", 9)):
                raise OwnershipError("Unsupported private group signal")
            if deadline <= time.monotonic():
                raise OwnershipError("Group signaling deadline; ownership retained")
            current = self.kernel.identity(member.pid)
            if current is None or current.zombie:
                return
            if not member.same_process(current) or current.sid != self.anchor.sid:
                raise OwnershipError("Owned group member identity changed")
            pgid = current.pgid
            if pgid <= 1 or pgid == self.anchor.pgid:
                raise OwnershipError(
                    "The session anchor group cannot be a signal target"
                )
            token = (member.pid, member.start, signum)
            if token in self.delivered:
                return
            held = self.current.get(pgid)
            # An authorized KILL may have killed this keeper too. It remains
            # unreaped through closure; a subsequent signal needs a fresh
            # ready/live keeper, never authority from an exited one.
            if (
                held is not None
                and held.kill_sent
                and self._keeper_identity(held, live=False).zombie
            ):
                held = None
            if held is None:
                if len(self.keepers) >= MAX_REGISTRIES:
                    raise OwnershipError(
                        "Group keeper budget exceeded; ownership retained"
                    )
                held = self.spawn_keeper(pgid)
                self.keepers.append(held)
                self.current[pgid] = held
            self._ready(held, deadline)
            current = self.kernel.identity(member.pid)
            if current is None or current.zombie:
                return
            if (
                not member.same_process(current)
                or current.sid != self.anchor.sid
                or current.pgid != pgid
            ):
                raise OwnershipError("Owned group member moved during keeper join")
            # Keeper identity is checked immediately before the kernel group
            # operation. It cannot be reaped here, so PGID reuse is impossible
            # even if it exits between this check and killpg.
            self._keeper_identity(held)
            if deadline <= time.monotonic():
                raise OwnershipError("Group signaling deadline; ownership retained")
            os.killpg(pgid, signum)
            if signum == getattr(signal, "SIGKILL", 9):
                held.kill_sent = True
                if held.control_fd >= 0:
                    os.close(held.control_fd)
                    held.control_fd = -1
            self.delivered.add(token)
        except (OSError, ValueError, TypeError) as error:
            raise OwnershipError(
                "Private group join/signaling is unverified"
            ) from error

    def release(self, deadline):
        self._verify_anchor()
        members = [
            item
            for item in self.kernel.session(self.anchor.sid)
            if item.sid == self.anchor.sid
            and item.pid != self.anchor.pid
            and not item.zombie
            and not self.is_keeper(item)
        ]
        if members:
            raise OwnershipError("Cannot reap group keepers with live owned members")
        self._verify_anchor()
        for held in self.keepers:
            if held.control_fd >= 0:
                os.close(held.control_fd)
                held.control_fd = -1
        for held in self.keepers:
            while not held.reaped:
                try:
                    pid, _status = os.waitpid(held.pid, os.WNOHANG)
                except ChildProcessError as error:
                    raise OwnershipError(
                        "Group keeper was reaped before verified closure"
                    ) from error
                if pid:
                    held.reaped = True
                else:
                    _condition_wait(deadline)
            if held.ready_fd >= 0:
                os.close(held.ready_fd)
                held.ready_fd = -1
        self.current.clear()


def _private_directory(path):
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise OwnershipError("Ownership directory is not private")


def write_record(path, value):
    """Atomic bounded numeric/control records, never command arguments."""
    path = Path(path)
    _private_directory(path.parent)
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RECORD_BYTES:
        raise OwnershipError("Ownership record exceeds its byte bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".owner-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_record(path, *, optional=False):
    path = Path(path)
    _private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if optional:
            return None
        raise OwnershipError("Ownership record is missing") from None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise OwnershipError("Ownership record is not private")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise OwnershipError("Ownership record exceeds its byte bound")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise OwnershipError("Duplicate ownership record field")
                result[key] = value
            return result

        try:
            value = json.loads(
                raw,
                object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (ValueError, UnicodeError) as error:
            raise OwnershipError("Invalid ownership record") from error
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or type(value.get("version")) is not int
        ):
            raise OwnershipError("Unsupported ownership record")
        return value
    finally:
        os.close(descriptor)


def _meta(path):
    value = read_record(path)
    owner = value.get("owner")
    if (
        not isinstance(owner, str)
        or len(owner) != 32
        or any(c not in "0123456789abcdef" for c in owner)
    ):
        raise OwnershipError("Invalid ownership generation")
    try:
        identity = ProcessIdentity(**value["anchor"])
    except (KeyError, TypeError) as error:
        raise OwnershipError("Invalid anchor record") from error
    if identity.pid != identity.sid or identity.pid != identity.pgid:
        raise OwnershipError("Recorded anchor is not a private session leader")
    if "registry" in value or "registration" in value:
        if value.get("registry") != str(Path(path).parent / "groups"):
            raise OwnershipError("Invalid private nested registry")
        registration = value.get("registration")
        if not isinstance(registration, str) or not Path(registration).is_absolute():
            raise OwnershipError("Invalid private recovery registration")
    return value, identity


def _status(directory, meta):
    value = read_record(directory / "status.json", optional=True)
    if value is None:
        return None
    if value.get("owner") != meta["owner"] or value.get("anchor") != meta["anchor"]:
        raise OwnershipError("Command status belongs to another owner")
    if value.get("phase") not in (
        "running",
        "exited",
        "cancelling",
        "quiescent",
        "failed",
        "released",
    ):
        raise OwnershipError("Invalid owned command phase")
    code = value.get("exit_code")
    if code is not None and (type(code) is not int or not -255 <= code <= 255):
        raise OwnershipError("Invalid actual command exit status")
    if type(value.get("cleanup_failed")) is not bool:
        raise OwnershipError("Invalid cleanup status")
    if type(value.get("control_serial")) is not int or value["control_serial"] < 0:
        raise OwnershipError("Invalid control acknowledgement")
    if value["phase"] == "exited" and code is None:
        raise OwnershipError("Actual command exit is unknown")
    return value


_changed = threading.Event()


def _condition_wait(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Owned command deadline")
    _changed.wait(min(0.01, remaining))


def _independent_directory():
    # Child TMPDIR/TEMP are deliberately inside the release TemporaryDirectory.
    # Linux and Darwin both provide /tmp; resolve Darwin's /private/tmp alias.
    root = Path("/tmp").resolve() if os.name == "posix" else Path(tempfile.gettempdir())
    directory = Path(tempfile.mkdtemp(prefix="ori-release-owner-", dir=root))
    os.chmod(directory, 0o700)
    return directory


class OwnedCommand:
    """Parent/recovery handle. The anchor is reaped only after session proof."""

    def __init__(self, directory, *, process=None, kernel=None):
        self.directory = Path(directory)
        self.meta, identity = _meta(self.directory / "active.session.json")
        self.authority = SessionAuthority(identity, kernel=kernel)
        retained = _retained_commands.get(self.directory)
        if process is None and retained is not None:
            if retained.meta != self.meta:
                raise OwnershipError(
                    "Retained process belongs to another ownership generation"
                )
            process = retained.process
        self.process = process
        self._released = False

    @classmethod
    def start(cls, command, *, environment, registry, root, log, shutdown_timeout):
        directory = _independent_directory()
        registration = None
        process = None
        owner = None
        gate_attempted = False
        try:
            groups = directory / "groups"
            groups.mkdir(mode=0o700)
            registration = Path(tempfile.mkdtemp(prefix="session-link-", dir=registry))
            os.chmod(registration, 0o700)
            environment = dict(environment, ORI_RELEASE_PROCESS_REGISTRY=str(groups))
            actual = [
                sys.executable,
                "-m",
                "tests.run_release_checks",
                "--_supervise-command",
                json.dumps(command),
                "--_session-owner",
                str(directory),
                "--shutdown-timeout",
                str(shutdown_timeout),
            ]
            process = subprocess.Popen(
                actual,
                cwd=root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                bufsize=0,
            )
            identity = local_kernel().identity(process.pid)
            if identity is None:
                raise OwnershipError(
                    "Private session anchor disappeared before registration"
                )
            SessionAuthority(identity)
            meta = {
                "version": 1,
                "owner": uuid.uuid4().hex,
                "anchor": asdict(identity),
                "registry": str(groups),
                "registration": str(registration),
            }
            write_record(directory / "active.session.json", meta)
            write_record(
                directory / "recovery.json", dict(meta, directory=str(directory))
            )
            write_record(
                registration / "active.session.link.json",
                {"version": 1, "owner": meta["owner"], "directory": str(directory)},
            )
            owner = cls(directory, process=process)
            _retained_commands[directory] = owner
            # A signal can interrupt Python after the atomic pipe write reached
            # the kernel. From this point, only verified session cleanup may
            # release the anchor, even if write/flush itself raises.
            gate_attempted = True
            process.stdin.write(b"go\n")
            process.stdin.flush()
            return owner
        except BaseException as error:
            if gate_attempted:
                try:
                    process.stdin.close()
                    owner.stop(shutdown_timeout)
                except BaseException as cleanup_error:  # noqa: BLE001 - interrupted cleanup retains proof
                    raise OwnershipError(
                        f"Uncertain launch cleanup failed: {cleanup_error}; "
                        f"recovery record: {directory / 'recovery.json'}"
                    ) from error
                raise
            # No go write was attempted: no command/descendant can exist.
            if process is not None:
                process.stdin.close()
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=shutdown_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=shutdown_timeout)
            _retained_commands.pop(directory, None)
            if registration is not None:
                shutil.rmtree(registration)
            shutil.rmtree(directory)
            raise

    def status(self):
        return _status(self.directory, self.meta)

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self.authority._verify_anchor()
            value = self.status()
            if value is not None:
                if value["phase"] == "failed" or value["cleanup_failed"]:
                    raise OwnershipError(
                        "Owned command anchor reported an unverified failure"
                    )
                if value["phase"] in ("quiescent", "released"):
                    raise OwnershipError(
                        "Owned command was cancelled before completion"
                    )
                if value["phase"] == "exited":
                    return value["exit_code"]
            try:
                _condition_wait(deadline)
            except TimeoutError:
                raise subprocess.TimeoutExpired(
                    "owned release command", timeout
                ) from None

    def request(self, request, deadline):
        previous = read_record(self.directory / "control.json", optional=True)
        if previous is not None and (
            previous.get("owner") != self.meta["owner"]
            or type(previous.get("serial")) is not int
            or previous["serial"] <= 0
        ):
            raise OwnershipError("Private control ownership is unknown")
        serial = 1 if previous is None else previous.get("serial", 0) + 1
        write_record(
            self.directory / "control.json",
            {
                "version": 1,
                "owner": self.meta["owner"],
                "serial": serial,
                "request": request,
                "deadline": deadline,
            },
        )
        # Normal and outer recovery paths both use the same private record.
        # The pipe provides immediate wakeup; EOF also owns parent-death cleanup.
        if self.process is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.write(b"wake\n")
                self.process.stdin.flush()
            except BrokenPipeError:
                pass
        return serial

    def release(self, timeout):
        if getattr(self, "_released", False):
            return
        deadline = time.monotonic() + timeout
        value = self.status()
        if value is not None and value["phase"] == "released":
            self._finish_release(deadline)
            return
        if self.authority.members():
            raise OwnershipError("Cannot release an anchor with live members")
        self.request("release", deadline)
        while True:
            value = self.status()
            # Terminal proof can precede this request when the original parent
            # died. A released anchor cannot acknowledge another serial.
            if value is not None and value["phase"] == "released":
                break
            _condition_wait(deadline)
        self._finish_release(deadline)

    def _finish_release(self, deadline):
        if self.process is not None:
            if not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            self.process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if self.process.returncode != 0:
                raise OwnershipError("Anchor exited unsuccessfully")
        else:
            while True:
                identity = self.authority.kernel.identity(self.authority.anchor.pid)
                if identity is None or identity.zombie:
                    break
                # The old instance already published terminal closure. Do not
                # signal, inspect its session or wait on a replacement process.
                if not self.authority.anchor.same_process(identity):
                    break
                _condition_wait(deadline)
        (self.directory / "active.session.json").unlink(missing_ok=True)
        self._released = True
        _retained_commands.pop(self.directory, None)
        if "registration" in self.meta:
            registration = Path(self.meta["registration"])
            link = registration / "active.session.link.json"
            if registration.exists():
                value = read_record(link, optional=True)
                if value is not None:
                    if value.get("owner") != self.meta["owner"] or value.get(
                        "directory"
                    ) != str(self.directory):
                        raise OwnershipError("Recovery link belongs to another owner")
                    link.unlink()
                registration.rmdir()
            shutil.rmtree(self.directory)

    def stop(self, timeout):
        if getattr(self, "_released", False):
            return
        deadline = time.monotonic() + timeout
        value = self.status()
        if value is not None and value["phase"] == "released":
            self._finish_release(deadline)
            return
        self.authority._verify_anchor()
        serial = self.request("cancel", deadline)
        while True:
            value = self.status()
            if value is not None and (
                value["phase"] == "released"
                or (value["control_serial"] >= serial and value["phase"] == "quiescent")
            ):
                break
            if (
                value is not None
                and value["control_serial"] >= serial
                and value["phase"] == "failed"
            ):
                raise OwnershipError(
                    "Owned session cleanup failed; anchor ownership retained"
                )
            _condition_wait(deadline)
        if value["phase"] == "released":
            self._finish_release(deadline)
            return
        self.release(max(0.001, deadline - time.monotonic()))


def _registry_records(root, name):
    # Callers may stop/release an owner immediately after it is yielded. Finish
    # the filesystem walk first: Python3.10 rglob otherwise resumes by scanning
    # that owner's now-retired registration directory. Missing/invalid records
    # still fail at their original validation boundary; none are ignored here.
    paths = []
    for path in root.rglob(name):
        if len(paths) >= MAX_REGISTRIES:
            raise OwnershipError("Ownership registry record budget exceeded")
        paths.append(path)
    return paths


def registered_commands(registry, *, exclude=None):
    pending = [Path(registry)]
    visited = set()
    while pending:
        root = pending.pop()
        if root in visited:
            raise OwnershipError("Cyclic ownership registry")
        visited.add(root)
        if len(visited) > MAX_REGISTRIES:
            raise OwnershipError("Ownership registry budget exceeded")
        for path in _registry_records(root, "active.session.link.json"):
            value = read_record(path)
            destination = value.get("directory")
            if not isinstance(destination, str) or not Path(destination).is_absolute():
                raise OwnershipError("Invalid private recovery link")
            directory = Path(destination)
            owner = _retained_commands.get(directory) or OwnedCommand(directory)
            if value.get("owner") != owner.meta["owner"] or owner.meta.get(
                "registration"
            ) != str(path.parent):
                raise OwnershipError("Recovery link belongs to another owner")
            pending.append(directory / "groups")
            if exclude is None or directory != Path(exclude):
                yield owner
        # Support registrations created before the independent-scope handoff.
        for path in _registry_records(root, "active.session.json"):
            if exclude is None or path.parent != Path(exclude):
                yield OwnedCommand(path.parent)


def supervise_anchor(command, directory, shutdown_timeout, cleanup_nested):
    """Single-threaded POSIX spawn, private status/control, held SID authority."""
    directory = Path(directory)
    meta, anchor = _meta(directory / "active.session.json")
    actual = local_kernel().identity(os.getpid())
    if (
        not anchor.same_process(actual)
        or actual.sid != anchor.sid
        or actual.pgid != anchor.pgid
    ):
        raise OwnershipError("Anchor launch identity does not match registration")
    authority = SessionAuthority(anchor)
    state = dict(
        meta,
        phase="running",
        child_pid=None,
        child_start=None,
        exit_code=None,
        cleanup_failed=False,
        control_serial=0,
    )
    child = None
    last_serial = 0
    interrupted = False
    abandoned = False
    eof = False
    status_unavailable = False

    def interrupt(_signum, _frame):
        nonlocal interrupted
        interrupted = True

    previous = {
        signum: signal.signal(signum, interrupt)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }

    def save(phase):
        state["phase"] = phase
        state["control_serial"] = last_serial
        write_record(directory / "status.json", state)

    def collect():
        if child is not None and state["exit_code"] is None:
            pid, status = os.waitpid(child, os.WNOHANG)
            if pid:
                state["exit_code"] = os.waitstatus_to_exitcode(status)

    def cleanup(deadline):
        save("cancelling")
        began = time.monotonic()
        sent = set()
        while True:
            collect()
            members = authority.members()
            if not members:
                break
            now = time.monotonic()
            if now >= deadline:
                raise OwnershipError(
                    "Owned session cleanup deadline; ownership retained"
                )
            signum = (
                signal.SIGKILL
                if now >= began + max(0, deadline - began) / 2
                else signal.SIGTERM
            )
            for member in members:
                token = (member.pid, member.start, signum)
                if token not in sent:
                    authority.signal(member, signum, deadline=deadline)
                    sent.add(token)
            _condition_wait(deadline)
        cleanup_nested(
            Path(meta.get("registry", directory.parent)),
            max(0.001, deadline - time.monotonic()),
            directory,
        )
        if hasattr(authority.kernel, "release_keepers"):
            authority.kernel.release_keepers(deadline)
        collect()
        if child is not None and state["exit_code"] is None:
            raise OwnershipError("Actual command exit is still unknown")
        save("quiescent")

    def failed():
        nonlocal status_unavailable
        state["cleanup_failed"] = True
        try:
            save("failed")
        except (OSError, OwnershipError, ValueError):
            # Keep the anchor and ownership evidence; never exit through an
            # unverified cleanup merely because its status write also failed.
            if not status_unavailable:
                print(
                    "[release-owner] private status unavailable; ownership retained",
                    file=sys.stderr,
                    flush=True,
                )
                status_unavailable = True

    try:
        # This spawn occurs before any worker thread; posix_spawnp also avoids
        # preexec callbacks on Python3.10 and resets inherited ignored signals.
        null = os.open(os.devnull, os.O_RDONLY)
        try:
            child = os.posix_spawnp(
                command[0],
                command,
                os.environ,
                setpgroup=0,
                setsigmask=(),
                setsigdef=(signal.SIGTERM, signal.SIGINT, signal.SIGPIPE),
                file_actions=[
                    (os.POSIX_SPAWN_DUP2, null, 0),
                    (os.POSIX_SPAWN_CLOSE, null),
                ],
            )
        finally:
            os.close(null)
        state["child_pid"] = child
        identity = authority.kernel.identity(child)
        state["child_start"] = identity.start if identity is not None else None
        save("running")
    except BaseException:  # noqa: BLE001 - failed spawn retains the session anchor
        failed()
    try:
        while True:
            try:
                before = state["exit_code"]
                collect()
                if (
                    before is None
                    and state["exit_code"] is not None
                    and state["phase"] == "running"
                ):
                    save("exited")
                control = read_record(directory / "control.json", optional=True)
                if control is not None:
                    if (
                        control.get("owner") != meta["owner"]
                        or type(control.get("serial")) is not int
                        or control["serial"] <= 0
                        or control.get("request") not in ("cancel", "release")
                        or type(control.get("deadline")) not in (int, float)
                        or not math.isfinite(control["deadline"])
                    ):
                        raise OwnershipError("Invalid private control record")
                    if control["serial"] > last_serial:
                        last_serial = control["serial"]
                        deadline = min(
                            float(control["deadline"]),
                            time.monotonic() + shutdown_timeout,
                        )
                        if control["request"] == "cancel":
                            cleanup(deadline)
                        elif not authority.members() and (
                            child is None or state["exit_code"] is not None
                        ):
                            cleanup_nested(
                                Path(meta.get("registry", directory.parent)),
                                max(0.001, deadline - time.monotonic()),
                                directory,
                            )
                            if hasattr(authority.kernel, "release_keepers"):
                                authority.kernel.release_keepers(deadline)
                            save("released")
                            return 0
                        else:
                            raise OwnershipError(
                                "Release requested before verified closure"
                            )
                if interrupted or abandoned:
                    interrupted = False
                    cleanup(time.monotonic() + shutdown_timeout)
                    if abandoned:
                        save("released")
                        return 0
                    abandoned = False
            except BaseException:  # noqa: BLE001 - no exception may discard unknown ownership
                # Sticky failure: retain a live anchor and accept only another
                # private retry/release after fresh numeric ownership checks.
                failed()
                interrupted = False
                abandoned = False
            readable, _, _ = select.select(
                [] if eof else [sys.stdin.fileno()], [], [], 0.02
            )
            if readable:
                line = sys.stdin.buffer.readline(33)
                if line == b"":
                    eof = True
                    abandoned = True
                elif line != b"wake\n":
                    failed()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
