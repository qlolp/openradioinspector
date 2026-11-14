"""Thin client identity checks and an actual Tk/HTTP field-estimation flow."""
import copy
import json
import threading
import time
from types import SimpleNamespace
import unittest

from radioinspector.contracts import to_primitive
from radioinspector.field_strength import field_strength_rms_v2
from radioinspector.session_service import SessionService
from radioinspector.web.server import create_server
from radioinspector.windows.client import PlatformClient
from tests.test_field_strength_rms import profile, source
from tests.test_sweep_comparison import create_session


class FieldClientValidationTests(unittest.TestCase):
    def test_client_rejects_wrong_source_calibration_and_nonfinite_result(self):
        from radioinspector.windows.field_strength import validate_result
        s = source(); p = profile()
        sent = {'sweep_id': s.id, 'frequency_hz': 200, 'calibration': p}
        good = field_strength_rms_v2(s, p, frequency_hz=200)
        validate_result(good, s.session_id, s.stream_id, sent)
        for key, value in (('value', float('nan')), ('unit', 'dBFS'), ('uncertainty_db', -1)):
            bad = copy.deepcopy(good); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_result(bad, s.session_id, s.stream_id, sent)
        for field, value in (('id', 'other'), ('session_id', 'other'), ('stream_id', 'other'), ('frequency_hz', 100)):
            bad = copy.deepcopy(good); bad['source'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_result(bad, s.session_id, s.stream_id, sent)
        bad = copy.deepcopy(good); bad['calibration']['profile']['version'] = 2
        with self.assertRaises(ValueError): validate_result(bad, s.session_id, s.stream_id, sent)

    def test_actual_tk_client_calculates_and_scope_change_clears_result(self):
        import tkinter as tk
        from tkinter import ttk
        from radioinspector.windows.field_strength import FieldStrengthPanel
        root = tk.Tk(); root.withdraw(); self.addCleanup(root.destroy)
        service = SessionService(); session = create_session(service)
        s = source(); s.session_id = session.id; s.stream_id = session.streams[0].id
        service.ingest_sweep(to_primitive(s))
        server = create_server(host='127.0.0.1', port=0, sessions=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def cleanup():
            server.shutdown(); thread.join(3); server.server_close(); service.close()
        self.addCleanup(cleanup)
        app = SimpleNamespace(root=root, tk=tk, ttk=ttk, session_id=session.id,
              client=PlatformClient('http://%s:%s' % server.server_address),
              selected_stream_id=lambda: s.stream_id,
              snapshot=SimpleNamespace(session={'id': session.id}, sweeps=[to_primitive(s)]))
        panel = FieldStrengthPanel(app, ttk.Notebook(root)); self.addCleanup(panel.close)
        panel.frequency.set('200'); panel.editor.insert('1.0', json.dumps(profile()))
        before = copy.deepcopy((service.health(), service.events()))
        panel.analyze()
        deadline = time.monotonic() + 4
        while panel.future is not None and time.monotonic() < deadline:
            root.update(); time.sleep(.005)
        self.assertIsNotNone(panel.data, panel.info.get())
        self.assertAlmostEqual(panel.data['value_dbuv_per_m'], 55)
        self.assertIn('V/m', panel.info.get())
        app.session_id = 'other'; panel.update()
        self.assertIsNone(panel.data)
        self.assertEqual(before, (service.health(), service.events()))
