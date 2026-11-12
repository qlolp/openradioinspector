"""Private synthetic fixture I/O; never normalize application or HTTP bytes."""
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch


READY_STDOUT = "import sys;sys.stdout.buffer.write(b'ready'+bytes([10]));sys.stdout.buffer.flush()"
CAPTURED_STDOUT = "import sys;sys.stdout.buffer.write(b'captured'+bytes([10]));sys.stdout.buffer.flush()"


def owned_control_closed(connection, *, joined):
    if joined is not True:
        raise AssertionError("Original owned process closure is unproved")
    try:
        return connection.recv(1) == b""
    except ConnectionResetError as error:
        code = getattr(error, "winerror", None)
        if sys.platform == "win32" and type(code) is int and code == 10054:
            return True
        raise


def command_joined(owners, jobs):
    return bool(owners or jobs) and all(getattr(owner, "_released", False) is True for owner in owners) and all(getattr(job, "closed_empty", False) is True for job in jobs)


@contextmanager
def record_command_closure(release):
    """Observe the original POSIX release or original empty Windows Job close."""
    owners, jobs = [], []
    initialize = release.process_ownership.OwnedCommand.__init__
    job_type = release.WindowsJob

    def remember(owner, *args, **kwargs):
        initialize(owner, *args, **kwargs)
        owners.append(owner)

    class ObservedJob(job_type):
        def __init__(self):
            self.closed_empty = False
            super().__init__()
            jobs.append(self)

        def close(self):
            self.closed_empty = False
            empty = False
            try:
                active = self.active()
                empty = type(active) is int and active == 0
            finally:
                super().close()
            self.closed_empty = empty

    with patch.object(release.process_ownership.OwnedCommand, "__init__", remember), patch.object(release, "WindowsJob", ObservedJob):
        yield SimpleNamespace(joined=lambda: command_joined(owners, jobs))
