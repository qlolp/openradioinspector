import base64
import json
import unittest
from tests import test_tinysa_controller as fixtures


class WorkflowTests(unittest.TestCase):
    # Reuse the real framing fixture, not a mocked controller.
    setup_controller=fixtures.ControllerTests.setup_controller
    acquire=fixtures.ControllerTests.acquire
    def test_catalog_separates_current_wiki_from_verified_firmware(self):
        from radioinspector.tinysa.commands import catalog
        data=catalog()
        self.assertFalse(any(x['label']=='Getting support' for x in data['menu']))
        self.assertIn('wiki_version',data)
        self.assertEqual(data['menu'][0]['availability'],'check_live_screen')

    def test_marker_peak_is_real_native_response(self):
        c=self.setup_controller({'marker 8 peak':b'8 2 101000000 -7.00e+01\r\n'})
        result=c.execute({'operation':'marker','parameters':{'marker':8,'action':'peak'}})
        self.assertIn('101000000',result['device_response'])
        self.assertEqual(self.wire.sent[-1],'marker 8 peak')

    def test_trace_subtraction_is_processed_and_invalid_source_sends_nothing(self):
        c=self.setup_controller({'trace 1 subtract 2':b''});self.adapter._instrument=c
        c.execute({'operation':'trace','parameters':{'action':'subtract','trace':1,'source_trace':2}})
        self.assertEqual(self.adapter.poll().artifact['kind'],'processed_trace')
        before=list(self.wire.sent)
        with self.assertRaises(ValueError):c.execute({'operation':'trace','parameters':{'action':'copy','source_trace':9}})
        self.assertEqual(before,self.wire.sent)

    def test_preset_import_is_prevalidated_and_exports_no_lab_secrets(self):
        c=self.setup_controller({'rbw 30':b''});token=self.acquire(c)
        before=list(self.wire.sent)
        invalid=json.dumps({'schema_version':1,'profile':c.status()['profile'],'settings':{'rbw_hz':30000,'output_enabled':True}})
        with self.assertRaises(ValueError):
            c.execute({'operation':'preset.import','lease_id':token,'parameters':{'document':invalid,'confirm':True}})
        self.assertEqual(before,self.wire.sent)
        artifact=c.execute({'operation':'preset.export','parameters':{}})['artifact']
        value=base64.b64decode(artifact['data_base64'])
        self.assertNotIn(token.encode(),value)
        self.assertNotIn('output_enabled',json.loads(value)['settings'])

    def test_exported_preset_can_be_imported_without_field_loss(self):
        c=self.setup_controller({'ultra off':b'','sweep 100000000 101000000 3':b''})
        exported=c.execute({'operation':'preset.export','parameters':{}})['artifact']
        document=base64.b64decode(exported['data_base64']).decode()
        expected=json.loads(document)['settings'];token=self.acquire(c)
        result=c.execute({'operation':'preset.import','lease_id':token,'parameters':{'document':document,'confirm':True}})
        for key,value in expected.items():self.assertEqual(result['status']['settings'][key],value)

    def test_service_guide_exposes_physical_steps_without_fake_success(self):
        c=self.setup_controller()
        result=c.execute({'operation':'service.guide','parameters':{'name':'calibration'}})
        self.assertEqual(result['execution'],'physical_and_screen')
        self.assertTrue(result['steps'])
        self.assertNotEqual(result['hardware_validation'],'passed')

    def test_selftest_ack_does_not_report_test_pass(self):
        c=self.setup_controller({'selftest 1':b''});token=self.acquire(c)
        result=c.execute({'operation':'selftest.start','lease_id':token,'parameters':{'test':1,'confirm':True}})
        self.assertEqual(result['completion'],'inspect_live_screen')
        self.assertEqual(self.wire.sent[-1],'selftest 1')

    def test_emergency_off_does_not_require_lease_or_enter_output_mode(self):
        c=self.setup_controller();before=len(self.wire.sent)
        c.execute({'operation':'output.off','parameters':{}})
        self.assertEqual(self.wire.sent[before:],['output off','caloutput off','release'])

    def test_sweep_all_parameters_validated_before_enabling_rf(self):
        c=self.setup_controller();token=self.acquire(c);before=list(self.wire.sent)
        with self.assertRaises(ValueError):
            c.execute({'operation':'generator.sweep','lease_id':token,'parameters':{'start_hz':800000000,'stop_hz':100000,'confirm':True}})
        self.assertEqual(before,self.wire.sent)


class AnalysisTests(unittest.TestCase):
    def test_channel_power_uses_rbw_and_irregular_grid_not_bin_sum(self):
        from radioinspector.tinysa.measurements import analyze
        data={'frequencies_hz':[100000000,100001000,100003000], 'levels':[-30,-30,-30], 'level_unit':'dBm'}
        result=analyze(data,'channel_power',rbw_hz=1000)
        self.assertAlmostEqual(result['value'],-25.22878745)
        self.assertEqual(result['unit'],'dBm')
        self.assertTrue(result['warnings'])

    def test_duplicate_frequency_is_not_silently_a_bandwidth_measurement(self):
        from radioinspector.tinysa.measurements import analyze
        with self.assertRaises(ValueError):
            analyze({'frequencies_hz':[100000000]*3,'levels':[-90,-80,-70],'level_unit':'dBm'},'occupied_bandwidth',rbw_hz=1000)
