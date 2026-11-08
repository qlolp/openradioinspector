import unittest
import json
from concurrent.futures import Future
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

from radioinspector.windows.client import PlatformClient, PlatformClientError
from radioinspector.windows.sweep_references import NamedReferenceCatalog, normalize_name, validate_catalog, validate_record
from tests.test_windows_sweep_comparison import panel as comparison_panel, response

TIME = '2026-11-08T12:00:00Z'


def record(**changes):
    return dict(id='ref/1', session_id='session', stream_id='s', sweep_id='archived', name='Éталон <&>',
                created_at=TIME, interval={'started_at': TIME, 'ended_at': TIME}, level_unit='dBm', points=3, **changes)


def catalog_data():
    return dict(session_id='session', stream_id='s', storage_mode='sqlite', persistent=True, limit=100, items=[record()])


def catalog():
    c = NamedReferenceCatalog.__new__(NamedReferenceCatalog)
    c.panel = comparison_panel()
    c.panel.catalog = c
    c.scope = c.panel.scope
    c.generation = 0; c.future = None; c.retired = None; c.closed = False; c.uncertain = False; c.verified = True; c.items = []
    c.executor = Mock(); c.executor.submit.side_effect = lambda *args: Future()
    for key in ('name', 'box', 'status', 'storage', 'save_button', 'refresh_button', 'use_button'):
        setattr(c, key, Mock())
    c.name.get.return_value = ' E\u0301талон <&> '
    c.box.current.return_value = 0
    return c


class ValidationTests(unittest.TestCase):
    def test_unicode_names(self):
        self.assertEqual(normalize_name(' E\u0301талон <&> '), 'Éталон <&>')
        self.assertEqual(normalize_name('😀' * 80), '😀' * 80)
        for value in ('', ' ', 'x' * 81, 'a\x00b', 'a\u202eb', '\ud800', 'name\n', '\tname', None, 3):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                normalize_name(value)

    def test_catalog_validation(self):
        self.assertEqual(validate_catalog(catalog_data(), 'session', 's')['items'][0]['sweep_id'], 'archived')
        for key, value in [('session_id', 'other'), ('stream_id', 'other'), ('storage_mode', 'unknown'),
                           ('persistent', 1), ('limit', True), ('limit', 101),
                           ('items', [record()] * 101), ('items', [record(), record()])]:
            data = catalog_data(); data[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_catalog(data, 'session', 's')
        data = catalog_data(); data.update(storage_mode='memory', persistent=False)
        validate_catalog(data, 'session', 's')
        data['persistent'] = True
        with self.assertRaises(ValueError):
            validate_catalog(data, 'session', 's')

    def test_record_validation(self):
        for key, value in [('id', ''), ('sweep_id', ''), ('name', ' E\u0301талон <&> '), ('session_id', 'wrong'),
                           ('stream_id', 'wrong'), ('level_unit', 'S'), ('points', True), ('points', 16385),
                           ('created_at', '2026-11-08'), ('interval', {})]:
            data = record(); data[key] = value
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError, KeyError)):
                validate_record(data, 'session', 's')

    def test_client_exact_payload_and_encoded_get(self):
        client = PlatformClient('http://localhost:8770')
        client._request = Mock()
        client.list_sweep_references('s/1', 't&2')
        client._request.assert_called_once_with('GET', '/sessions/s%2F1/sweep-references?stream_id=t%262', expected_statuses=(200,))
        client._request.reset_mock(); client.list_sweep_references('s/1')
        client._request.assert_called_once_with('GET', '/sessions/s%2F1/sweep-references', expected_statuses=(200,))
        client._request.reset_mock()
        client.save_sweep_reference('s/1', ' E\u0301талон <&> ', 'archived')
        client._request.assert_called_once_with('POST', '/sessions/s%2F1/sweep-references', {'name': 'Éталон <&>', 'sweep_id': 'archived'}, expected_statuses=(200, 201))

    def test_catalog_transport_requires_exact_success_status(self):
        for method, statuses in [('list_sweep_references', (201, 202, 204, 206)),
                                 ('save_sweep_reference', (202, 204, 206))]:
            for status in statuses:
                reply = Mock(status=status)
                reply.read.return_value = json.dumps({'contract': 'openradioinspector-platform', 'contract_version': 1,
                                                     'data': record()}).encode()
                context = Mock(); context.__enter__ = Mock(return_value=reply); context.__exit__ = Mock(return_value=False)
                opener = Mock(); opener.open.return_value = context
                client = PlatformClient('http://localhost:8770', opener=opener)
                args = ('session', 's') if method == 'list_sweep_references' else ('session', 'Name', 'archived')
                with self.subTest(method=method, status=status), self.assertRaises(PlatformClientError) as error:
                    getattr(client, method)(*args)
                self.assertEqual(error.exception.code, 'unexpected_status')
                self.assertEqual(error.exception.status, status)


class LifecycleTests(unittest.TestCase):
    def test_catalog_is_not_polled(self):
        c = catalog(); c.scope = None
        c.update(); c.update(); c.update()
        self.assertEqual(c.executor.submit.call_count, 1)
        c.future.set_result(catalog_data()); c.finish(c.generation)
        c.update(); self.assertEqual(c.executor.submit.call_count, 1)
        self.assertIn('SQLite', c.storage.set.call_args.args[0])
        c.refresh(); c.future.set_result(dict(catalog_data(), storage_mode='memory', persistent=False)); c.finish(c.generation)
        self.assertIn('Memory', c.storage.set.call_args.args[0])

    def test_sqlite_in_memory_is_labeled_temporary(self):
        c = catalog(); c.refresh()
        c.future.set_result(dict(catalog_data(), persistent=False)); c.finish(0)
        self.assertTrue(c.verified)
        label = c.storage.set.call_args.args[0]
        self.assertTrue(label.startswith('SQLite (:memory:): '))
        memory = catalog(); memory.refresh()
        memory.future.set_result(dict(catalog_data(), storage_mode='memory', persistent=False)); memory.finish(0)
        self.assertEqual(label.split(': ', 1)[1], memory.storage.set.call_args.args[0].split(': ', 1)[1])

    def test_save_is_explicit_single_flight_and_scoped(self):
        c = catalog(); c.update(); c.save(); c.save()
        c.executor.submit.assert_called_once_with(c.panel.app.client.save_sweep_reference, 'session', 'Éталон <&>', 'r')
        c.future.set_result(dict(record(), sweep_id='r')); c.finish(0)
        self.assertEqual(c.items[0]['name'], 'Éталон <&>')
        self.assertIn('<&>', c.box.configure.call_args.kwargs['values'][0])
        self.assertFalse(c.uncertain)

    def test_saved_reference_outside_snapshot_compares_via_sweep_id(self):
        c = catalog(); c.items = [record()]; c.use()
        self.assertEqual(c.panel.reference, 'archived')
        c.panel.compare()
        c.panel.executor.submit.assert_called_once_with(c.panel.app.client.compare_sweeps, 'session', 'archived', 't')
        result = response(); result['reference']['id'] = 'archived'
        c.panel.future.set_result(result); c.panel.finish(c.panel.generation)
        self.assertIsNotNone(c.panel.data)

    def test_unknown_write_requires_explicit_refresh(self):
        for exc in (RuntimeError('network failed'), ValueError('bad response'), PlatformClientError('server failed', status=500), PlatformClientError('unexpected success', status=202)):
            c = catalog(); c.save(); c.future.set_exception(exc); c.finish(0)
            self.assertTrue(c.uncertain); self.assertIn('могла сохраниться', c.status.set.call_args.args[0])
            c.save(); self.assertEqual(c.executor.submit.call_count, 1)
            c.refresh(); c.future.set_result(catalog_data()); c.finish(0)
            self.assertFalse(c.uncertain)
            c.save(); self.assertEqual(c.executor.submit.call_count, 3)

    def test_invalid_write_response_is_uncertain(self):
        for key, value in [('session_id', 'wrong'), ('stream_id', 'wrong'), ('sweep_id', 'wrong'), ('name', 'wrong')]:
            c = catalog(); c.save(); value_record = record(); value_record[key] = value
            c.future.set_result(value_record); c.finish(0)
            self.assertTrue(c.uncertain); self.assertEqual(c.items, [])

    def test_timeout_keeps_single_flight_until_transport_finishes(self):
        c = catalog(); c.save(); old = c.future; old.set_running_or_notify_cancel()
        c.deadline = monotonic() - 1; c.finish(0)
        self.assertTrue(c.uncertain); self.assertIsNone(c.future)
        c.refresh(); self.assertEqual(c.executor.submit.call_count, 1)
        old.set_result(dict(record(), sweep_id='r'))
        c.finish(0); self.assertEqual(c.items, [])
        c.refresh(); self.assertEqual(c.executor.submit.call_count, 2)

    def test_scope_change_and_close_ignore_late_write(self):
        for change in ('server', 'session', 'stream', 'close'):
            c = catalog(); c.save(); old = c.future; old.set_running_or_notify_cancel()
            if change == 'server': c.panel.app.client = Mock()
            if change == 'session': c.panel.app.session_id = 'other'
            if change == 'stream': c.panel.app.selected_stream_id = lambda: 'other'
            if change == 'close': c.close()
            else: c.panel.update()
            old.set_result(dict(record(), sweep_id='r')); c.finish(0)
            self.assertEqual(c.items, [], change)
            if change != 'close': self.assertIsNone(c.panel.reference)

    def test_old_server_preserves_local_pin(self):
        c = catalog(); c.refresh()
        c.future.set_exception(PlatformClientError('not found', status=404)); c.finish(0)
        self.assertEqual(c.panel.reference, 'r')
        self.assertIn('Локальное', c.status.set.call_args.args[0])
        c.panel.compare(); c.panel.future.set_result(response()); c.panel.finish(0)
        self.assertIsNotNone(c.panel.data)

    def test_refresh_button_synchronizes_new_scope_before_request(self):
        c = catalog()
        c.panel.app.session_id = 'other'
        c.refresh()
        c.executor.submit.assert_called_once_with(c.panel.app.client.list_sweep_references, 'other', 's')
        self.assertIsNone(c.panel.reference)

    def test_all_stream_catalog_cannot_pin_foreign_reference(self):
        c = catalog(); c.panel.app.selected_stream_id = lambda: None
        c.panel.scope = (id(c.panel.app.client), 'session', None)
        c.scope = c.panel.scope; c.items = [record()]
        c.use()
        self.assertEqual(c.panel.reference, 'r')
        self.assertIn('выберите поток', c.status.set.call_args.args[0])
        c.controls()
        c.use_button.configure.assert_called_with(state='disabled')

    def test_completed_identical_save_is_not_posted_again(self):
        c = catalog(); c.save(); c.future.set_result(dict(record(), sweep_id='r')); c.finish(0)
        c.save(); self.assertEqual(c.executor.submit.call_count, 1)
        self.assertIn('уже сохранены', c.status.set.call_args.args[0])

    def test_failed_refresh_clears_catalog_and_blocks_save_but_not_local_pin(self):
        for value in (PlatformClientError('not found', status=404), {'invalid': True}):
            c = catalog(); c.items = [record()]; c.refresh()
            if isinstance(value, Exception): c.future.set_exception(value)
            else: c.future.set_result(value)
            c.finish(0)
            self.assertFalse(c.verified); self.assertEqual(c.items, [])
            c.save(); self.assertEqual(c.executor.submit.call_count, 1)
            self.assertEqual(c.panel.reference, 'r')

    def test_known_rejection_allows_manual_new_save(self):
        for status in (400, 404, 409):
            c = catalog(); c.save(); c.future.set_exception(PlatformClientError('rejected', status=status)); c.finish(0)
            self.assertFalse(c.uncertain)
            self.assertEqual(c.executor.submit.call_count, 1)
