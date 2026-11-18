"""Native compiler failures stay inspectable through the checks API."""
from pathlib import Path
import subprocess
import sys
import unittest


class NativeDiagnosticsTests(unittest.TestCase):
    def test_annotation_escapes_workflow_commands_and_keeps_repository_location(self):
        from macos.ci.report_command import annotation
        root = Path(__file__).resolve().parents[1]
        line = f'{root}/macos/DvoynoyObzor/ContentView.swift:12:9: error: bad 50% [value]'
        self.assertEqual(annotation(line),
            '::error file=macos/DvoynoyObzor/ContentView.swift,line=12,col=9::bad 50%25 [value]')
        self.assertIsNone(annotation('Build succeeded'))
        self.assertEqual(annotation('example.swift:12: error: XCTest failed'),
                         '::error file=example.swift,line=12::XCTest failed')

    def test_wrapper_preserves_child_output_and_failure_exit_status(self):
        result = subprocess.run([sys.executable, '-m', 'macos.ci.report_command', '--',
            sys.executable, '-c', 'print("example.swift:3:4: error: invalid argument"); raise SystemExit(65)'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 65)
        self.assertIn('example.swift:3:4: error: invalid argument', result.stdout)
        self.assertIn('::error file=example.swift,line=3,col=4::invalid argument', result.stdout)
