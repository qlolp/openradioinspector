"""Event responses must never replace the monitoring project selector."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from radioinspector.windows.monitoring import MonitoringPanel

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = [dict(id='p1', name='First', site='Site', state='ready'),
            dict(id='p2', name='Second', site='Site', state='ready')]


class Choice:
    def __init__(self):
        self.index = -1
        self.values = []

    def current(self, index=None):
        if index is not None:
            self.index = index
        return self.index

    def configure(self, *, values):
        self.values = values
        self.index = -1


class WindowsMonitoringEventTests(unittest.TestCase):
    def setUp(self):
        self.panel = MonitoringPanel.__new__(MonitoringPanel)
        self.panel.items = []
        self.panel.choice = Choice()
        self.panel.output = Mock()
        self.client = Mock()
        self.calls = []

        def submit(label, operation, render):
            self.calls.append((operation, render))

        self.panel.app = SimpleNamespace(client=self.client, _submit_action=submit)

    def reply(self, data):
        operation, render = self.calls[-1]
        self.client.monitoring.return_value = data
        render(operation())
        rendered = self.panel.output.insert.call_args.args[1]
        self.assertEqual(json.loads(rendered), data)

    def test_events_and_post_keep_projects_and_selected_action_target(self):
        self.panel.refresh()
        self.reply({'items': PROJECTS})
        self.panel.choice.current(1)
        for items in ([{'id': 'event-1', 'kind': 'new_signal'}], []):
            self.panel.scoped('events')
            self.reply({'items': items})
            self.assertEqual(self.panel.items, PROJECTS)
            self.assertEqual(self.panel.selected(), 'p2')
            self.panel.preflight()
            self.reply({'ready': True})
            self.client.monitoring.assert_called_with('/p2/preflight', None)
        # The shared root URL alone is insufficient: POST is not a list refresh.
        self.panel.call(payload={})
        self.reply({'items': []})
        self.assertEqual(self.panel.selected(), 'p2')
        self.panel.refresh()
        self.reply({'items': list(reversed(PROJECTS))})
        self.assertEqual(self.panel.selected(), 'p2')
        self.panel.refresh()
        self.reply({'items': PROJECTS[:1]})
        self.assertEqual(self.panel.selected(), 'p1')
        self.panel.refresh()
        self.reply({'items': []})
        self.assertIsNone(self.panel.selected())
        self.assertEqual(self.panel.choice.values, [])

    def test_request_context_is_bound_to_each_callback(self):
        self.panel.refresh()
        refresh = self.calls[-1][1]
        self.panel.call('/p1/events')
        events = self.calls[-1][1]
        refresh({'items': PROJECTS})
        events({'items': []})
        self.assertEqual(self.panel.items, PROJECTS)
        self.assertEqual(self.panel.selected(), 'p1')


class WebMonitoringEventTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for executable WebUI tests')
    def test_real_monitoring_handlers(self):
        result = subprocess.run(
            [shutil.which('node'), str(ROOT / 'tests/webui_monitoring_events.cjs'),
             str(ROOT / 'radioinspector/web/static/monitoring.js')],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('monitoring event checks completed', result.stdout)


if __name__ == '__main__':
    unittest.main()
