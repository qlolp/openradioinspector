"""RF off is not a transition from output/unknown to receiver input."""
import unittest
from tests import test_tinysa_controller as fixtures


class TinySADeviceModeTests(unittest.TestCase):
    setup_controller = fixtures.ControllerTests.setup_controller
    acquire = fixtures.ControllerTests.acquire

    def make_controller(self, extra=None):
        commands = {'mode output': b'', 'freq 30000000': b'',
                    'scanraw 100000000 101000000 3 0': b'{' + b'x\x00\x10' * 3 + b'}'}
        commands.update(extra or {})
        return self.setup_controller(commands)

    def test_output_off_preserves_output_mode_and_blocks_both_receive_actions(self):
        c = self.make_controller()
        token = self.acquire(c)
        c.execute({'operation': 'setting', 'lease_id': token,
                   'parameters': {'name': 'output_hz', 'value': 30000000}})
        c.execute({'operation': 'output.off', 'parameters': {}})
        self.assertEqual(c.status()['device_mode'], 'output')
        self.assertFalse(c.status()['device_mode_readback'])
        before = list(self.wire.sent)
        for operation in ('acquire.raw', 'acquire.zero'):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'mode input'):
                c.execute({'operation': operation, 'lease_id': token, 'parameters': {}})
        self.assertEqual(self.wire.sent, before)
        c.execute({'operation': 'monitor.resume', 'lease_id': token, 'parameters': {}})
        self.assertEqual(c.status()['device_mode'], 'input')
        result = c.execute({'operation': 'acquire.raw', 'parameters': {}})
        self.assertEqual(result['artifact']['kind'], 'raw_scan')
        resumed = self.wire.sent[len(before):]
        self.assertEqual(resumed[:4], ['output off', 'caloutput off', 'release', 'mode input'])
        self.assertEqual(resumed[-2:], ['zero', 'scanraw 100000000 101000000 3 0'])

    def test_manual_menu_preset_and_selftest_remain_unknown_after_output_off(self):
        cases = [('touch', {'x': 1, 'y': 1}, {'touch 1 1': b''}),
                 ('preset.load', {'slot': 0, 'confirm': True}, {'recall 0': b''}),
                 ('selftest.start', {'test': 1, 'confirm': True}, {'selftest 1': b''})]
        for operation, parameters, extra in cases:
            with self.subTest(operation=operation):
                c = self.make_controller(extra)
                token = self.acquire(c)
                c.execute({'operation': operation, 'lease_id': token, 'parameters': parameters})
                c.execute({'operation': 'output.off', 'parameters': {}})
                self.assertEqual(c.status()['device_mode'], 'unknown')
                before = list(self.wire.sent)
                with self.assertRaisesRegex(ValueError, 'mode input'):
                    c.execute({'operation': 'acquire.raw', 'lease_id': token, 'parameters': {}})
                self.assertEqual(before, self.wire.sent)

    def test_failed_input_transition_remains_unknown_and_tainted(self):
        c = self.make_controller()
        token = self.acquire(c)
        c.execute({'operation': 'setting', 'lease_id': token,
                   'parameters': {'name': 'output_hz', 'value': 30000000}})
        self.wire.responses['mode input'] = b'ERROR invalid\r\n'
        with self.assertRaises(Exception):
            c.execute({'operation': 'monitor.resume', 'lease_id': token, 'parameters': {}})
        self.assertEqual(c.status()['device_mode'], 'unknown')
        self.assertTrue(self.adapter._restart_required)
        self.assertFalse(any(command.startswith('scanraw') for command in self.wire.sent))
