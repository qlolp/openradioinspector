import importlib
import unittest


class CommandTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module('radioinspector.tinysa.commands')
        except ImportError as exc:
            self.fail(str(exc))

    def test_receive_units_and_bounds_are_ultra_specific(self):
        m = self.module()
        self.assertEqual(m.setting_command('rbw_hz', 200).wire, 'rbw 0.2')
        self.assertEqual(m.setting_command('rbw_hz', 850000).wire, 'rbw 850')
        self.assertEqual(m.setting_command('attenuation_db', 'auto').wire, 'attenuate auto')
        self.assertEqual(m.setting_command('lna', True).wire, 'lna on')
        self.assertEqual(m.setting_command('start_hz', 123456789).wire, 'sweep start 123456789')
        for name, value in [('rbw_hz',199),('rbw_hz',True),('points',451),('start_hz',1.0),('lna','on\routput on')]:
            with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                m.setting_command(name,value)

    def test_generator_and_service_commands_cannot_be_receive_changes(self):
        m = self.module()
        for name,value in [('output_enabled',True),('output_hz',30000000),('output_dbm',-30),('modulation','am'),('caloutput_mhz',30)]:
            self.assertTrue(m.setting_command(name,value).requires_lab)
        self.assertEqual(m.setting_command('output_enabled',False).wire,'output off')
        with self.assertRaises(ValueError):
            m.setting_command('output_dbm',0)

    def test_no_raw_console_and_unknown_setting_never_gets_a_command(self):
        m = self.module()
        for name in ['raw','command','reset\routput on','unknown']:
            with self.assertRaises(ValueError):
                m.setting_command(name,'anything')

    def test_catalog_returns_copies_and_keeps_physical_menu_routes(self):
        m = self.module()
        first = m.catalog()
        names = {item['label'] for item in first['menu']}
        self.assertTrue({'ZERO SPAN','MULTI BAND','NOISE FIGURE','LO OUTPUT','LOAD CMD'} <= names)
        self.assertTrue(any(item['path'][0]=='generator' for item in first['menu']))
        first['menu'].clear()
        self.assertTrue(m.catalog()['menu'])
