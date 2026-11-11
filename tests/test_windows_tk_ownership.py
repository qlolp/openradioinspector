"""Native interpreter disposal must survive later worker-thread collection."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

from tests import run_release_checks as release


def run_owned_python(code: str, *, name: str, timeout: float) -> str:
    """Bounded owned child, isolated before any Tcl/test module is imported."""
    environment = release.child_environment()
    # The normal unit suite must not opt into a graphical display implicitly.
    environment["ORI_WINDOWS_RELEASE_TK"] = os.getenv("ORI_WINDOWS_RELEASE_TK", "0")
    with tempfile.TemporaryDirectory(prefix="ori-tk-ownership-") as temporary:
        directory = Path(temporary)
        home, child_temp = directory / "home", directory / "tmp"
        home.mkdir()
        child_temp.mkdir()
        environment.update(HOME=str(home), TMPDIR=str(child_temp),
                           TEMP=str(child_temp), TMP=str(child_temp))
        preamble = (
            "import os\n"
            "if os.name == 'posix':\n"
            "    import resource\n"
            "    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        )
        # run_command preserves the parent's process registry/Windows Job
        # ownership, so aggregate interruption also reaps this child subtree.
        return release.run_command([sys.executable, "-B", "-c", preamble + code],
                                   name=name, environment=environment,
                                   directory=directory, timeout=timeout,
                                   shutdown_timeout=5)


class InterpreterOwnershipSubprocessTests(unittest.TestCase):
    def test_lifecycle_then_worker_gc_and_synthetic_http(self):
        output = run_owned_python(
            """import gc
import os
import threading
import unittest
gc.disable()
from tests import test_windows_tk_lifecycle as lifecycle
from tests.loopback_dns import local_loopback_names

# Explicit classes avoid discovering this subprocess regression recursively.
classes = [lifecycle.WindowsTimerLifecycleTests, lifecycle.OwnedTimerTests,
           lifecycle.PanelTimerLifecycleTests, lifecycle.CompletionOwnershipTests]
if os.getenv('ORI_WINDOWS_RELEASE_TK') == '1':
    classes.append(lifecycle.HiddenTkTimerLifecycleTests)
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                           for cls in classes)
result = unittest.TextTestRunner(verbosity=1).run(suite)
if not result.wasSuccessful() or result.skipped:
    raise SystemExit('lifecycle checks failed or skipped')
del suite, result

# Reproduce the old failure deterministically: do NOT collect on main between
# releasing the fixture and forcing collection on a different thread.
root = lifecycle.TclRoot()
root.dispose()
root.cycle = root
retired_wrappers = [root]
del root

def allocate_and_collect():
    # Keep the disposed wrapper reachable until this worker starts, so an
    # incidental main-thread collection cannot hide native ownership errors.
    retired_wrappers.clear()
    cycles = []
    for _ in range(1000):
        value = []
        value.append(value)
        cycles.append(value)
    del cycles, value
    gc.collect()

worker = threading.Thread(target=allocate_and_collect)
worker.start()
worker.join(10)
if worker.is_alive():
    raise SystemExit('worker collection timed out')
print('worker collection complete', flush=True)

# Exercise the exact following HTTP test from the aggregate crash. It uses
# in-memory synthetic sessions and its own ephemeral loopback server only.
with local_loopback_names():
    suite = unittest.defaultTestLoader.loadTestsFromName(
        'tests.test_worker_api_lifecycle.WorkerStopAdmissionHttpTests.'
        'test_failed_session_state_save_releases_start_admission')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
if not result.wasSuccessful() or result.skipped:
    raise SystemExit('synthetic HTTP checks failed or skipped')
print('synthetic worker HTTP complete', flush=True)
""",
            name="lifecycle-worker-gc", timeout=60,
        )
        self.assertIn("worker collection complete", output)
        self.assertIn("synthetic worker HTTP complete", output)
        self.assertNotIn("Tcl_AsyncDelete", output)


if __name__ == "__main__":
    unittest.main()
