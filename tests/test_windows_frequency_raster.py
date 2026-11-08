"""Windows raster panel: artifact guards, read-only rounding and safe file IO."""
import json
import tempfile
import unittest
from copy import deepcopy
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from radioinspector.windows.frequency_raster import (
    FrequencyRasterPanel, check_draft, raster_template, read_raster_file, result_text,
    save_raster_file, validate_artifact, validate_result, validate_returned_draft,
)


def artifact():
    return {'format': 'openradioinspector-frequency-raster', 'format_version': 1,
            'name': 'Сетка', 'frequency_unit': 'Hz',
            'entries': [{'name': 'FM', 'start_hz': 100_000_000, 'stop_hz': 110_000_000, 'grid_step_hz': 100_000}]}


def candidates():
    return [{'id': 'candidate-1', 'center_frequency_hz': 101_234_567},
            {'id': 'candidate-2', 'center_frequency_hz': 50_000_000}]


def items():
    return [{'candidate_id': 'candidate-1', 'frequency_hz': 101_234_567, 'matched': True,
             'rounded_frequency_hz': 101_200_000, 'delta_hz': -34_567, 'grid_step_hz': 100_000,
             'entry_name': 'FM'},
            {'candidate_id': 'candidate-2', 'frequency_hz': 50_000_000, 'matched': False,
             'rounded_frequency_hz': None, 'delta_hz': None, 'grid_step_hz': None, 'entry_name': None}]


def response():
    return {'session_id': 'session', 'order': 'request', 'table': artifact(),
            'count': 2, 'matched_count': 1, 'unmatched_count': 1, 'items': items()}


def panel():
    p = FrequencyRasterPanel.__new__(FrequencyRasterPanel)
    p.app = SimpleNamespace(client=Mock(), session_id='session', root=Mock(),
                            snapshot=SimpleNamespace(candidates=deepcopy(candidates())))
    p.scope = (id(p.app.client), 'session')
    p.generation = 0
    p.future = p.retired = p.data = None
    p.io_future = None
    p.closed = False
    p.raw = json.dumps(artifact(), ensure_ascii=False)
    p.candidate_ids = ['candidate-1', 'candidate-2']
    p.expected = 'session'
    p.executor = Mock(); p.executor.submit.side_effect = lambda *args: Future()
    p.io_executor = Mock(); p.io_executor.submit.side_effect = lambda *args: Future()
    for key in ('button', 'save_button', 'info', 'details', 'picker', 'editor'):
        setattr(p, key, Mock())
    p.editor.get.return_value = p.raw
    p.candidates = deepcopy(candidates())
    return p


class RasterValidationTests(unittest.TestCase):
    def test_result_happy_grid_and_unmatched(self):
        self.assertEqual(validate_result(response(), 'session', ['candidate-1', 'candidate-2']), response())
        text = result_text(response())
        self.assertIn('101200000 Гц', text)
        self.assertIn('Δ -34567 Гц', text)
        self.assertIn('вне диапазонов таблицы', text)
        self.assertIn('не изменялись', text)

    def test_result_rejections(self):
        cases = [('session_id', 'wrong'), ('order', 'sorted'), ('count', 3), ('matched_count', 0),
                 ('unmatched_count', 0), ('table', None), ('items', None)]
        for key, value in cases:
            r = response(); r[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_result(r, 'session', ['candidate-1', 'candidate-2'])
        # Off-grid or out-of-range rounded values are refused.
        for changes in ({'rounded_frequency_hz': 101_234_500, 'delta_hz': -67},
                        {'rounded_frequency_hz': 99_000_000, 'delta_hz': -2_234_567},
                        {'rounded_frequency_hz': 101_350_000, 'delta_hz': 115_433}):
            r = response()
            r['items'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_result(r, 'session', ['candidate-1', 'candidate-2'])
        # Unmatched rows never carry derived values.
        for key, value in (('rounded_frequency_hz', 1), ('delta_hz', 0), ('grid_step_hz', 100), ('entry_name', 'FM')):
            r = response(); r['items'][1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_result(r, 'session', ['candidate-1', 'candidate-2'])
        # Identity guards.
        r = response(); r['items'][0]['candidate_id'] = 'other'
        with self.assertRaises(ValueError):
            validate_result(r, 'session', ['candidate-1', 'candidate-2'])
        with self.assertRaises(ValueError):
            validate_result(response(), 'session', ['candidate-1'])
        r = response(); r['items'][0]['matched'] = 1
        with self.assertRaises(ValueError):
            validate_result(r, 'session', ['candidate-1', 'candidate-2'])

    def test_artifact_strict(self):
        validate_artifact(artifact())
        for key, value in [('format_version', True), ('extra', 1), ('name', '\nname'), ('name', ' a '),
                           ('frequency_unit', 'kHz'), ('entries', [])]:
            table = artifact(); table[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_artifact(table)
        for key, value in [('start_hz', True), ('stop_hz', 100_000_000), ('grid_step_hz', 0),
                           ('grid_step_hz', 10_000_001), ('name', 'x' * 81), ('extra', 0)]:
            table = artifact(); table['entries'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_artifact(table)
        # Overlapping and unsorted entries are refused.
        table = artifact()
        table['entries'].append(deepcopy(table['entries'][0]))
        table['entries'][1]['name'] = 'B'
        with self.assertRaises(ValueError):
            validate_artifact(table)

    def test_returned_draft_identity_and_duplicates(self):
        raw = json.dumps(artifact(), ensure_ascii=False)
        validate_returned_draft(raw, artifact())
        loose = json.dumps({'format': 'openradioinspector-frequency-raster', 'format_version': 1,
                            'name': '  Сетка  ', 'frequency_unit': 'Hz',
                            'entries': [{'name': 'FM', 'start_hz': 100_000_000, 'stop_hz': 110_000_000,
                                         'grid_step_hz': 100_000}]}, ensure_ascii=False)
        validate_returned_draft(loose, artifact())
        for raw in ('{"name":"x","name":"y"}', '{}', '{bad'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_returned_draft(raw, artifact())
        changed = artifact(); changed['name'] = 'other'
        with self.assertRaises(ValueError):
            validate_returned_draft(raw, changed)

    def test_draft_and_template_limits(self):
        check_draft(json.dumps(artifact()))
        for raw in ('', '   ', 'é' * 32_769):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                check_draft(raw)
        template = json.loads(raster_template(candidates()))
        self.assertEqual(template['format_version'], 1)
        self.assertIn('НЕНОРМАТИВНЫЙ', template['name'])
        self.assertEqual(template['entries'][0]['grid_step_hz'], 100_000)
        self.assertEqual(template['entries'][0]['start_hz'], 50_000_000 - 1_000_000)
        self.assertEqual(template['entries'][0]['stop_hz'], 101_234_567 + 1_000_000)
        with self.assertRaises(ValueError):
            raster_template([{'id': 'x'}])
        with self.assertRaises(ValueError):
            raster_template([])


class RasterFileTests(unittest.TestCase):
    def test_raw_import_roundtrip_limits_and_safe_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'raster.json'
            for raw in ('{bad', '{"name":"x","name":"y"}', '', 'é' * 32_768):
                path.write_text(raw, encoding='utf-8')
                self.assertEqual(read_raster_file(path), raw)
            path.write_bytes(b'x' * 65_537)
            with self.assertRaises(ValueError):
                read_raster_file(path)
            path.write_bytes(b'\xff')
            with self.assertRaises(UnicodeDecodeError):
                read_raster_file(path)
            save_raster_file(path, artifact(), True)
            self.assertEqual(json.loads(read_raster_file(path)), artifact())
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                save_raster_file(path, artifact())
            self.assertEqual(path.read_bytes(), before)
            with patch('radioinspector.windows.frequency_raster.os.replace', side_effect=OSError('synthetic')):
                with self.assertRaises(OSError):
                    save_raster_file(path, artifact(), True)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])


class PanelTests(unittest.TestCase):
    def test_analyze_rejects_empty_selection_and_forwards_raw_draft(self):
        p = panel()
        p.picker.curselection.return_value = ()
        p.selected_ids = lambda: []
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        self.assertIn('Выберите хотя бы одного кандидата', p.info.set.call_args[0][0])
        p = panel()
        p.selected_ids = lambda: ['candidate-1', 'candidate-2']
        future = Future()
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 1)
        args = p.executor.submit.call_args[0]
        self.assertEqual(args[0], p.app.client.frequency_rounding)
        self.assertEqual(args[1:3], ('session', ['candidate-1', 'candidate-2']))
        self.assertEqual(args[3], p.raw, 'raw draft is never rewritten')
        future.set_result(response())
        p.deadline = p.deadline + 100
        p.finish(p.generation)
        self.assertEqual(validate_result(p.data, 'session', ['candidate-1', 'candidate-2']), response())
        self.assertEqual(p.save_button.configure.call_args[1]['state'], 'normal')
        self.assertIn('не изменялись', p.info.set.call_args[0][0])

    def test_finish_rejects_forged_results_and_keeps_draft(self):
        forged = response()
        forged['items'][0]['rounded_frequency_hz'] = 101_234_500
        forged['items'][0]['delta_hz'] = -67
        p = panel()
        p.selected_ids = lambda: ['candidate-1', 'candidate-2']
        future = Future(); future.set_result(forged)
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.analyze()
        p.deadline = p.deadline + 100
        p.finish(p.generation)
        self.assertIsNone(p.data)
        self.assertIn('некорректн', p.info.set.call_args[0][0].lower())
        self.assertEqual(p.editor.get.call_count, 1)

    def test_dialog_cancel_and_declined_overwrite(self):
        p = panel()
        p.data = response()
        with patch('tkinter.filedialog.askopenfilename', return_value=''):
            p.load()
        with patch('tkinter.filedialog.asksaveasfilename', return_value=''):
            p.save()
        with patch('tkinter.filedialog.asksaveasfilename', return_value='synthetic.json'), \
             patch('os.path.lexists', return_value=True), \
             patch('tkinter.messagebox.askyesno', return_value=False):
            p.save()
        self.assertEqual(p.io_executor.submit.call_count, 0)

    def test_scope_change_clears_result_but_keeps_selection(self):
        p = panel()
        p.data = response()
        p.selected_ids = lambda: ['candidate-1']
        p.refresh_picker = Mock()
        p.app.session_id = 'other'
        p.update()
        self.assertIsNone(p.data)
        self.assertEqual(p.scope, (id(p.app.client), 'other'))
        p.refresh_picker.assert_called_once()


if __name__ == '__main__':
    unittest.main()
