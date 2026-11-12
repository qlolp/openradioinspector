"""Other clients must reject the Linux-only assembler before any file/effect."""
import io
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.release import offline_core


class OfflineCorePlatforms(unittest.TestCase):
    def test_unsupported_hosts_reject_before_reading_spec_or_publishing_payloads(self):
        for system in ("Windows", "Darwin"):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                error = io.StringIO()
                with patch.object(offline_core.platform, "system", return_value=system), \
                     patch.object(offline_core, "build") as build, redirect_stderr(error):
                    status = offline_core.main(["build", "--spec", str(root / "absent.json"),
                        "--releases", str(root / "releases"), "--report-dir", str(root / "reports")])
                self.assertEqual(status, 1)
                self.assertIn("linux_only", error.getvalue())
                build.assert_not_called()
                self.assertEqual(list(root.iterdir()), [])
