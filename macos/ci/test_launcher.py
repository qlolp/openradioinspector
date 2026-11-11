"""Run the shipped macOS launcher against owned fake UI/core executables."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest


class LauncherModeTests(unittest.TestCase):
    def run_launcher(self, *, remote):
        with tempfile.TemporaryDirectory(prefix="ori-launcher-test-") as temporary:
            root = Path(temporary)
            contents = root / "Example.app" / "Contents"
            launcher = contents / "MacOS" / "OpenRadioInspector"
            launcher.parent.mkdir(parents=True)
            source = Path(__file__).resolve().parents[2] / "packaging" / "macos" / "launch-app.sh"
            shutil.copyfile(source, launcher)
            helper = contents / "Helpers" / "openradioinspector-core" / "openradioinspector-core"
            helper.parent.mkdir(parents=True)
            helper.write_text('''#!/bin/sh
set -eu
printf started > "$ORI_TEST_CORE_STARTED"
while [ "$#" -gt 0 ]; do
    if [ "$1" = --ready-file ]; then ready=$2; shift; fi
    shift
done
printf 'http://127.0.0.1:12345' > "$ready"
cat > /dev/null
printf closed > "$ORI_TEST_CORE_CLOSED"
''')
            helper.chmod(0o700)
            ui = contents / "Helpers" / "DvoynoyObzorUI"
            ui.write_text('''#!/bin/sh
set -eu
printf '%s\n%s\n' "${ORI_BUNDLED_LEGACY_CORE-unset}" "$ORI_LOCAL_CORE_URL" > "$ORI_TEST_OBSERVED"
''')
            ui.chmod(0o700)
            observed, started, closed = [root / name for name in ("observed", "started", "closed")]
            environment = os.environ.copy()
            environment.update(TMPDIR=str(root), ORI_BUNDLED_LEGACY_CORE="inherited",
                               ORI_TEST_OBSERVED=str(observed), ORI_TEST_CORE_STARTED=str(started),
                               ORI_TEST_CORE_CLOSED=str(closed))
            environment.pop("ORI_REMOTE_SERVER", None)
            if remote:
                environment["ORI_REMOTE_SERVER"] = "https://trusted-core.example"
            process = subprocess.Popen(["/bin/sh", str(launcher)], env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(stdout, b"")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.communicate(timeout=5)
            state = observed.read_text().splitlines()
            self.assertEqual(list(root.glob("openradioinspector-launch.*")), [], "private runtime leaked")
            if remote:
                self.assertEqual(state, ["unset", "https://trusted-core.example"])
                self.assertFalse(started.exists(), "remote mode started a local helper")
            else:
                self.assertEqual(state, ["1", "http://127.0.0.1:12345"])
                self.assertTrue(started.exists())
                self.assertEqual(closed.read_text(), "closed", "owned helper outlived UI exit")

    def test_remote_mode_clears_inherited_helper_marker_and_starts_no_core(self):
        self.run_launcher(remote=True)

    def test_bundled_helper_is_marked_and_exits_with_its_ui(self):
        self.run_launcher(remote=False)


if __name__ == "__main__":
    unittest.main()
