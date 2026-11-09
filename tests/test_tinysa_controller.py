import importlib
import unittest
from tests.tinysa_control_fixture import make_adapter


class ControllerTests(unittest.TestCase):
    def setup_controller(self,extra=None):
        try:m=importlib.import_module('radioinspector.tinysa.controller')
        except ImportError as exc:self.fail(str(exc))
        self.service,self.session,self.adapter,self.wire=make_adapter(self,extra)
        self.now=100.0
        self.controller=m.Controller(self.adapter,monotonic=lambda:self.now)
        self.controller.execute({'operation':'inspect','parameters':{}})
        return self.controller

    def acquire(self,c):
        return c.execute({'operation':'lease.acquire','parameters':{'acknowledge_rf':True}})['lease_id']

    def test_screen_pixels_with_prompt_are_png_and_not_measurements(self):
        c=self.setup_controller({'capture':b'\xf8\0'*153600})
        result=c.execute({'operation':'capture','parameters':{}})
        self.assertEqual(result['artifact']['kind'],'screen')
        self.assertEqual(result['artifact']['metadata']['connection_id'],c.connection_id)
        self.assertEqual(result['artifact']['metadata']['segment_id'],f'{c.connection_id}:0')
        import base64
        self.assertTrue(base64.b64decode(result['artifact']['data_base64']).startswith(b'\x89PNG'))
        self.assertEqual(len(self.service.list_sweeps(self.session.id)),0)

    def test_monitoring_cannot_send_touch_or_output_on(self):
        c=self.setup_controller();before=list(self.wire.sent)
        for op,params in [('touch',{'x':1,'y':1}),('setting',{'name':'output_enabled','value':True})]:
            with self.subTest(op=op),self.assertRaises(ValueError):
                c.execute({'operation':op,'parameters':params})
        self.assertEqual(self.wire.sent,before)

    def test_lease_is_exclusive_secret_not_in_status_and_stale_token_rejected(self):
        c=self.setup_controller();token=self.acquire(c)
        self.assertNotIn(token,str(c.status()))
        with self.assertRaises(ValueError):self.acquire(c)
        with self.assertRaises(ValueError):
            c.execute({'operation':'touch','lease_id':'stale','parameters':{'x':1,'y':1}})
        c.execute({'operation':'lease.release','lease_id':token,'parameters':{}})
        with self.assertRaises(ValueError):
            c.execute({'operation':'keypad','lease_id':token,'parameters':{'text':'30M'}})

    def test_expiry_stops_output_but_never_auto_resumes_monitoring(self):
        c=self.setup_controller();self.acquire(c);self.now+=31
        self.assertFalse(c.before_poll())
        self.assertEqual(c.status()['mode'],'lab')
        self.assertFalse(c.status()['lease_active'])
        self.assertEqual(self.wire.sent[-3:],['output off','caloutput off','release'])

    def test_touch_range_and_release_are_firmware_coordinates(self):
        c=self.setup_controller({'touch 479 319':b''});token=self.acquire(c)
        c.execute({'operation':'touch','lease_id':token,'parameters':{'x':479,'y':319}})
        c.execute({'operation':'release','lease_id':token,'parameters':{}})
        self.assertEqual(self.wire.sent[-2:],['touch 479 319','release'])
        for x in [480,-1,True,1.5]:
            with self.assertRaises(ValueError):
                c.execute({'operation':'touch','lease_id':token,'parameters':{'x':x,'y':0}})

    def test_receive_change_records_requested_settings_and_invalid_revision_sends_nothing(self):
        c=self.setup_controller({'rbw 0.2':b''})
        c.execute({'operation':'setting','expected_revision':0,'parameters':{'name':'rbw_hz','value':200}})
        self.assertEqual(self.adapter.rbw_hz,200)
        self.assertEqual(c.status()['settings_source'],'command_acknowledged')
        self.assertFalse(c.status()['settings_readback'])
        before=list(self.wire.sent)
        with self.assertRaises(ValueError):
            c.execute({'operation':'setting','expected_revision':0,'parameters':{'name':'rbw_hz','value':10000}})
        self.assertEqual(self.wire.sent,before)

    def test_ambiguous_ack_taints_and_never_retries(self):
        c=self.setup_controller({'rbw 30':b'ERROR invalid\r\n'})
        with self.assertRaises(Exception):
            c.execute({'operation':'setting','parameters':{'name':'rbw_hz','value':30000}})
        self.assertTrue(self.adapter.details()['restart_required'])
        before=list(self.wire.sent)
        with self.assertRaises(Exception):c.execute({'operation':'inspect','parameters':{}})
        self.assertEqual(self.wire.sent,before)

    def test_lna_attenuator_conflict_is_rejected_before_io(self):
        c=self.setup_controller({'attenuate 10':b''})
        c.execute({'operation':'setting','parameters':{'name':'attenuation_db','value':10}})
        before=list(self.wire.sent)
        with self.assertRaises(ValueError):c.execute({'operation':'setting','parameters':{'name':'lna','value':True}})
        self.assertEqual(self.wire.sent,before)

    def test_generator_requires_explicit_setup_and_lab(self):
        c=self.setup_controller({'mode output':b'','freq 30000000':b'','level -30':b'','output on':b''})
        token=self.acquire(c)
        with self.assertRaises(ValueError):c.execute({'operation':'setting','lease_id':token,'parameters':{'name':'output_enabled','value':True}})
        for name,value in [('output_hz',30000000),('output_dbm',-30),('output_enabled',True)]:
            c.execute({'operation':'setting','lease_id':token,'parameters':{'name':name,'value':value}})
        self.assertEqual(self.wire.sent[-1],'output on')
        self.assertFalse(c.before_poll())

    def test_sd_paths_and_size_are_checked_and_bytes_preserved(self):
        c=self.setup_controller({'sd_read A.CSV':b'\x04\0\0\0a\0b\n'})
        result=c.execute({'operation':'sd.read','parameters':{'filename':'A.CSV'}})
        import base64
        self.assertEqual(base64.b64decode(result['artifact']['data_base64']),b'a\0b\n')
        for name in ['../A.CSV','A.CSV\routput on','/A.CSV']:
            with self.assertRaises(ValueError):c.execute({'operation':'sd.read','parameters':{'filename':name}})

    def test_readonly_capture_does_not_confirm_unknown_manual_settings(self):
        c=self.setup_controller({'capture':b'\0\0'*153600,'touch 1 1':b''})
        token=self.acquire(c)
        c.execute({'operation':'touch','lease_id':token,'parameters':{'x':1,'y':1}})
        c.execute({'operation':'capture','parameters':{}})
        self.assertEqual(c.status()['settings_source'],'unknown')

    def test_lease_renewal_is_pure_and_can_extend_a_long_transfer(self):
        c=self.setup_controller();token=self.acquire(c);self.now+=25
        c.renew_only({'lease_id':token})
        self.now+=10
        before=list(self.wire.sent)
        self.assertFalse(c.before_poll())
        self.assertTrue(c.status()['lease_active'])
        self.assertEqual(self.wire.sent,before)

    def test_off_never_enters_generator_mode(self):
        c=self.setup_controller();token=self.acquire(c)
        before=len(self.wire.sent)
        c.execute({'operation':'setting','lease_id':token,'parameters':{'name':'output_enabled','value':False}})
        self.assertEqual(self.wire.sent[before:],['output off'])

    def test_manual_change_invalidates_generator_confirmation(self):
        c=self.setup_controller({'mode output':b'','freq 30000000':b'','level -30':b'','touch 1 1':b''})
        token=self.acquire(c)
        for name,value in [('output_hz',30000000),('output_dbm',-30)]:
            c.execute({'operation':'setting','lease_id':token,'parameters':{'name':name,'value':value}})
        c.execute({'operation':'touch','lease_id':token,'parameters':{'x':1,'y':1}})
        before=list(self.wire.sent)
        with self.assertRaises(ValueError):
            c.execute({'operation':'setting','lease_id':token,'parameters':{'name':'output_enabled','value':True}})
        self.assertEqual(self.wire.sent,before)

    def test_center_that_crosses_unconfirmed_range_sends_nothing(self):
        c=self.setup_controller();before=list(self.wire.sent)
        with self.assertRaises(ValueError):
            c.execute({'operation':'setting','parameters':{'name':'center_hz','value':800000000}})
        self.assertEqual(self.wire.sent,before)

    def test_receive_changes_cannot_steal_another_lab_owner(self):
        c=self.setup_controller();self.acquire(c);before=list(self.wire.sent)
        with self.assertRaises(ValueError):
            c.execute({'operation':'setting','parameters':{'name':'rbw_hz','value':200}})
        self.assertEqual(self.wire.sent,before)

    def test_expiry_during_binary_transfer_taints_without_later_touch(self):
        c=self.setup_controller({'capture':b'\0\0'*153600});self.acquire(c)
        original=self.wire._read_once
        def delayed(*args):
            self.now+=31
            return original(*args)
        self.wire._read_once=delayed
        with self.assertRaises(Exception):c.execute({'operation':'capture','parameters':{}})
        self.assertTrue(self.adapter._restart_required)

    def test_processed_trace_never_becomes_raw_sweep(self):
        c=self.setup_controller({'calc maxh':b''})
        self.adapter._instrument=c
        c.execute({'operation':'setting','parameters':{'name':'calculation','value':'maxh'}})
        result=self.adapter.poll()
        from radioinspector.tinysa.artifacts import InstrumentSample
        self.assertIsInstance(result,InstrumentSample)
        self.assertEqual(result.artifact['kind'],'processed_trace')

    def test_raw_sweep_keeps_control_revision_and_profile(self):
        c=self.setup_controller();self.adapter._instrument=c
        result=self.adapter.poll()
        self.assertEqual(result.settings['instrument']['profile'],c.status()['profile'])
        self.assertEqual(result.settings['instrument']['revision'],0)
        self.assertNotIn('lease_seconds',result.settings['instrument'])
        self.assertEqual(result.settings['generator_output'],'unknown')
        self.assertEqual(result.settings['receive_profile'],c.status()['profile'])
        self.assertTrue(result.settings['instrument']['connection_id'])

    def test_zero_span_has_no_fabricated_sample_timestamps(self):
        c=self.setup_controller({'sweep span 0':b'','scanraw 100500000 100500000 3 0':b'{x\0\nx\0\nx\0\n}'})
        self.adapter._instrument=c
        c.execute({'operation':'setting','parameters':{'name':'span_hz','value':0}})
        result=self.adapter.poll()
        import base64,json
        self.assertEqual(result.artifact['kind'],'zero_span')
        data=json.loads(base64.b64decode(result.artifact['data_base64']))
        self.assertIsNone(data['sample_offsets_seconds'])
        self.assertEqual(data['frequencies_hz'],[100500000]*3)
        self.assertIsNone(data['stop_is_inclusive'])
