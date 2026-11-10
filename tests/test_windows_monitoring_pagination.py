"""Project selectors only receive complete, successfully loaded page sets."""
import unittest
import threading
from unittest.mock import Mock

from radioinspector.windows.client import PlatformClient, PlatformClientError
from radioinspector.windows.monitoring import MonitoringPanel
from radioinspector.web.server import create_server
from tests.test_device_workers import simulated_session_payload


class MonitoringPaginationTests(unittest.TestCase):
    def client(self, pages):
        client = PlatformClient('http://example.test')
        client._request = Mock(side_effect=pages)
        return client

    def test_loads_projects_beyond_200_and_follows_encoded_cursor(self):
        first = [dict(id=str(i)) for i in range(200)]
        client = self.client([
            dict(items=first, next_cursor='cursor +/&?'),
            dict(items=[dict(id='new-project')], next_cursor='new-project'),
            dict(items=[], next_cursor=None),
        ])
        result = client.monitoring()
        self.assertEqual(result['items'], first + [dict(id='new-project')])
        self.assertIsNone(result['next_cursor'])
        self.assertEqual(client._request.call_args_list[1].args[1],
                         '/monitoring-projects?after=cursor+%2B%2F%26%3F')
        self.assertEqual(client._request.call_count, 3)

    def test_cycle_is_an_error_instead_of_partial_success(self):
        client = self.client([dict(items=[dict(id='one')], next_cursor='repeat')] * 2)
        with self.assertRaises(PlatformClientError):
            client.monitoring()
        self.assertEqual(client._request.call_count, 2)

    def test_later_page_failure_does_not_return_partial_success(self):
        client = self.client([dict(items=[dict(id='one')], next_cursor='one'),
                              PlatformClientError('offline')])
        with self.assertRaisesRegex(PlatformClientError, 'offline'):
            client.monitoring()

    def test_invalid_page_is_not_treated_as_end_of_list(self):
        for page in [dict(items=None), dict(items=[], next_cursor=42)]:
            with self.subTest(page=page):
                client = self.client([page])
                with self.assertRaises(PlatformClientError):
                    client.monitoring()

    def test_detail_and_create_remain_single_requests(self):
        for path, payload in [('/one/preflight', None), ('', dict(name='new'))]:
            with self.subTest(path=path):
                client = self.client([dict(id='one')])
                self.assertEqual(client.monitoring(path, payload), dict(id='one'))
                client._request.assert_called_once()

    def test_panel_preserves_selection_beyond_first_page(self):
        panel = MonitoringPanel.__new__(MonitoringPanel)
        panel.items = [dict(id='selected')]
        panel.choice = Mock()
        panel.choice.current.return_value = 0
        panel.output = Mock()
        projects = [dict(id=str(i), name='Name', site='Site', state='draft') for i in range(201)]
        projects.append(dict(id='selected', name='Selected', site='Site', state='draft'))
        panel.render(dict(items=projects))
        panel.choice.current.assert_called_with(201)

    def test_real_api_lists_all_201_projects(self):
        server = create_server(host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            session = server.session_service.create_session(simulated_session_payload())
            client = PlatformClient(f'http://127.0.0.1:{server.server_port}')
            identifiers = {
                client.monitoring(payload=dict(name=f'Project {i}', site='Office', session_id=session.id))['id']
                for i in range(201)
            }
            result = client.monitoring()
            self.assertEqual({item['id'] for item in result['items']}, identifiers)
            self.assertEqual(len(result['items']), 201)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
