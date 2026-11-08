"""Synthetic shared presentation contract and opt-in bounded hidden Tk checks."""
import copy
import json
import math
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from radioinspector.windows.spectrum_view import SpectrumView, fit_bounds, fraction

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/spectrum-presentation-v1.json').read_text())


class SpectrumViewTests(unittest.TestCase):
    def setUp(self):
        self.view = SpectrumView()
        self.sweeps = copy.deepcopy(FIXTURE['frame']['sweeps'])

    def update(self, sweeps=None, **scope):
        values = dict(FIXTURE['frame']['scope'], **scope)
        return self.view.update(**values, sweeps=self.sweeps if sweeps is None else sweeps)

    def test_shared_scales_and_finite_coordinates(self):
        for case in FIXTURE['scale_cases']:
            with self.subTest(case=case['name']):
                lower, upper = fit_bounds(case['levels'])
                self.assertTrue(math.isfinite(lower) and math.isfinite(upper) and lower < upper)
                self.assertAlmostEqual(lower / max(abs(lower), 1), case['expected_lower'] / max(abs(lower), 1))
                self.assertAlmostEqual(upper / max(abs(upper), 1), case['expected_upper'] / max(abs(upper), 1))
                for value in case['levels']:
                    self.assertTrue(0 <= fraction(value, lower, upper) <= 1)

    def test_near_double_limit_products_remain_finite(self):
        for value in (-1.796180088652537e308, 1.796180088652537e308):
            with self.subTest(value=value):
                lower, upper = fit_bounds([value])
                self.assertTrue(math.isfinite(lower) and math.isfinite(upper))
                self.assertLess(lower, upper)
                self.assertLessEqual(lower, value)
                self.assertGreaterEqual(upper, value)
                for level in (lower, value, upper):
                    coordinate = fraction(level, lower, upper)
                    self.assertTrue(math.isfinite(coordinate))
                    self.assertTrue(0 <= coordinate <= 1)

    def test_shared_frame_pause_fit_resume_and_source_immutability(self):
        original = copy.deepcopy(self.sweeps)
        self.assertTrue(self.update())
        self.assertEqual(self.view.bounds, (FIXTURE['frame']['expected_lower'], FIXTURE['frame']['expected_upper']))
        self.view.toggle_pause()
        frozen = copy.deepcopy(self.view.displayed)
        new = copy.deepcopy(self.sweeps)
        new[-1]['id'] = 'new'
        new[-1]['levels'] = [-20, -10, 0]
        self.update(new)
        self.assertEqual(self.view.displayed, frozen)
        self.view.fit()
        self.assertEqual(self.view.bounds, (-104, -46))
        self.view.set_bounds(-80, -60)
        self.assertEqual(self.view.clipped, 3)
        self.view.toggle_pause()
        self.assertEqual(self.view.displayed[-1]['id'], 'new')
        self.assertEqual(self.view.bounds, (-80, -60))
        self.assertEqual(self.sweeps, original)
        new[-1]['levels'][0] = 999
        self.assertEqual(self.view.displayed[-1]['levels'][0], -20)

    def test_capacity_suffix_and_scope_reset(self):
        many = [dict(copy.deepcopy(self.sweeps[-1]), id=str(i)) for i in range(70)]
        self.update(many)
        self.assertEqual(len(self.view.displayed), FIXTURE['expected_capacity'])
        self.view.set_bounds(-1, 1)
        self.view.toggle_pause()
        new = copy.deepcopy(many)
        new[-1]['frequencies_hz'] = [100, 201, 400]
        self.update(new)
        self.assertEqual(len(self.view.displayed), 1)
        self.assertFalse(self.view.paused)
        self.assertNotEqual(self.view.bounds, (-1, 1))
        self.view.toggle_pause()
        self.update(server='http://other')
        self.assertFalse(self.view.paused)

    def test_invalid_foreign_and_empty_fail_closed(self):
        for field, value in [('stream_id', 'foreign'), ('session_id', 'foreign'), ('level_unit', 's_meter_count'), ('levels', [1, float('nan'), 2]), ('levels', [1, float('inf'), 2]), ('frequencies_hz', [1, 0, 2]), ('frequencies_hz', [1, True, 3]), ('interval', {}), ('id', '')]:
            with self.subTest(field=field, value=value):
                self.update()
                self.view.toggle_pause()
                bad = copy.deepcopy(self.sweeps)
                bad[0][field] = value
                self.assertFalse(self.update(bad))
                self.assertEqual(self.view.displayed, [])
                self.assertIsNone(self.view.bounds)
                self.assertFalse(self.view.paused)
        self.update()
        self.assertFalse(self.update([]))

    def test_strict_utc_and_full_input_validation(self):
        self.assertFalse(self.view.set_bounds(-100, 0))
        self.assertIsNone(self.view.bounds)
        for interval in FIXTURE['valid_intervals']:
            self.assertTrue(self.update([dict(self.sweeps[-1], interval=interval)]))
            self.assertEqual(self.view.displayed[-1]['interval'], interval)
        for interval in FIXTURE['invalid_intervals']:
            self.assertFalse(self.update([dict(self.sweeps[-1], interval=interval)]))
        many = [copy.deepcopy(self.sweeps[-1]) for _ in range(42)]
        many[0]['stream_id'] = 'foreign'
        self.assertFalse(self.update(many))

    def test_duplicate_bins_preserve_raw_levels(self):
        self.sweeps[-1]['frequencies_hz'] = [100, 100, 400]
        self.assertTrue(self.update())
        self.assertEqual(self.view.displayed[-1], self.sweeps[-1])
        self.sweeps[-1]['frequencies_hz'][-1] = FIXTURE['maximum_frequency_hz']
        self.assertTrue(self.update())
        self.sweeps[-1]['frequencies_hz'][-1] += 1
        self.assertFalse(self.update())

    def test_shared_manual_text_cases(self):
        self.update()
        for case in FIXTURE['manual_cases']:
            with self.subTest(case=case):
                previous = self.view.bounds
                self.assertEqual(self.view.set_bounds(case['lower'], case['upper']), case['valid'])
                if case['valid']:
                    self.assertEqual(self.view.bounds, (float(case['lower']), float(case['upper'])))
                else:
                    self.assertEqual(self.view.bounds, previous)

    def test_manual_validation_and_stable_live_scale(self):
        self.update()
        bounds = self.view.bounds
        for lower, upper in FIXTURE['invalid_manual_bounds'] + [['', '1'], ['0x1', '2'], ['nan', '2'], ['1', 'inf'], [True, 2], ['1_0', '20']]:
            self.assertFalse(self.view.set_bounds(lower, upper))
            self.assertEqual(self.view.bounds, bounds)
        self.assertTrue(self.view.set_bounds('-1.2e2', '-.5'))
        self.update([dict(self.sweeps[-1], levels=[100, 200, 300])])
        self.assertEqual(self.view.bounds, (-120, -.5))
        self.assertEqual(self.view.clipped, 3)


@unittest.skipUnless(os.getenv('ORI_WINDOWS_RELEASE_TK') == '1', 'Opt-in hidden Tk desktop test')
class HiddenSpectrumControlsTests(unittest.TestCase):
    def test_controls_and_lifecycle_in_owned_child(self):
        from tests.test_windows_tk_ownership import run_owned_python
        output = run_owned_python(
            'from tests.test_windows_spectrum_view import HiddenSpectrumControlsTests\n'
            'HiddenSpectrumControlsTests().exercise()\n'
            "print('spectrum controls complete', flush=True)\n",
            name='hidden-spectrum-controls', timeout=30)
        self.assertIn('spectrum controls complete', output)

    def exercise(self):
        import tkinter as tk
        from tkinter import ttk
        from unittest.mock import Mock

        from radioinspector.windows.app import WindowsApp
        from radioinspector.windows.client import PlatformSnapshot

        class NoProfileTk(tk.Tk):
            def readprofile(self, *_args):
                pass

        for direct_destroy in (False, True):
            root = NoProfileTk()
            root.withdraw()
            errors = []
            root.report_callback_exception = lambda *args, errors=errors: errors.append(args)
            root.tk.createcommand('bgerror', lambda error, errors=errors: errors.append(error))
            def forbidden(*args, errors=errors):
                errors.append('native dialog')
                raise tk.TclError('No dialogs')
            for name in ('tk_messageBox', 'tk_getOpenFile', 'tk_getSaveFile', 'tk_chooseDirectory'):
                root.tk.createcommand(name, forbidden)
            app = WindowsApp(root, tk, ttk, refresh_ms=60000)
            try:
                app.client = SimpleNamespace(base_url=FIXTURE['frame']['scope']['server'])
                app.session_id = 'session-A'
                app._selected_stream = lambda: {'id': 'stream-A', 'role': 'spectrum_analyzer', 'level_unit': 'dBm'}
                app.selected_stream_id = lambda: 'stream-A'
                for name in ('comparison_panel', 'threshold_panel', 'mask_panel', 'three_thresholds_panel', 'sweep_context_panel', 'frequency_rounding_panel', 'average_panel'):
                    getattr(app, name).update = Mock()
                app.snapshot = PlatformSnapshot({}, {}, [], spectrum_stream_id='stream-A', sweeps=copy.deepcopy(FIXTURE['frame']['sweeps']))
                app._submit_action = Mock(side_effect=AssertionError('No mutation'))
                app._render_spectrum()
                initial = app.spectrum_info_var.get()
                scale = app.spectrum_view.bounds
                app.lower_view_var.set('-123.4')
                app._render_spectrum()
                self.assertEqual(app.lower_view_var.get(), '-123.4')
                app.pause_view_button.invoke()
                frozen = app.spectrum_info_var.get()
                self.assertEqual(str(app.peak_candidate_button['state']), 'disabled')
                app.snapshot.sweeps[-1]['id'] = 'new'
                app.snapshot.sweeps[-1]['levels'] = [100, 200, 300]
                app._render_spectrum()
                self.assertEqual(app.spectrum_info_var.get(), frozen)
                self.assertEqual(app.spectrum_view.bounds, scale)
                app._open_candidate_form(from_peak=True)
                self.assertIsNone(getattr(app, '_active_form', None))
                app.lower_view_var.set('NaN')
                app.apply_view_button.invoke()
                self.assertIn('Finite', app.view_status_var.get())
                app.fit_view_button.invoke()
                self.assertEqual(app.spectrum_view.bounds, scale)
                app.pause_view_button.invoke()
                self.assertIn('new', app.spectrum_info_var.get())
                self.assertNotEqual(app.spectrum_info_var.get(), initial)
                self.assertIn('clipped: 3', app.view_status_var.get())
                self.assertIn('bin-order', app.waterfall_info_var.get())
                self.assertIn('sweep-order', app.waterfall_info_var.get())
                app._spectrum_available = False
                app._render_spectrum()
                self.assertEqual(app.spectrum_view.displayed, [])
                app._submit_action.assert_not_called()
                if direct_destroy:
                    root.destroy()
                else:
                    app.close()
                root.tk.call('update')
                self.assertEqual(root.tk.call('after', 'info'), '')
                self.assertEqual(errors, [])
            finally:
                if not app._closed:
                    app.close()
                app._executor.shutdown(wait=True, cancel_futures=True)
