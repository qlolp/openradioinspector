from types import SimpleNamespace
import time
import unittest

from radioinspector.tinysa.api import InstrumentOperations

class LimitTests(unittest.TestCase):
    def test_preview_does_not_exhaust_mutation_ledger_or_block_emergency_controls(self):
        class Controller:
            def renew_only(self,p):return {'lease_active':True}
        worker=SimpleNamespace(adapter=SimpleNamespace(_instrument=Controller(),_restart_required=False))
        class Sessions:
            def instrument_context(self,*args):return SimpleNamespace(settings={'adapter':'tinysa-serial'})
        class Workers:
            def instrument_worker(self,*args):return worker
            def instrument(self,*args,**kwargs):return {'mode':'lab'}
        service=InstrumentOperations(Sessions(),Workers());self.addCleanup(service.close)
        # Public no-I/O heartbeats reproduce a long-running panel in bounded time.
        for i in range(2050):service.submit('s','t',{'operation':'lease.renew','lease_id':'owner','request_id':f'heartbeat-{i}'})
        off=service.submit('s','t',{'operation':'output.off','request_id':'emergency'})
        deadline=time.monotonic()+2
        while service.get('s','t',off['operation_id'])['state'] not in {'succeeded','failed'} and time.monotonic()<deadline:time.sleep(.001)
        self.assertEqual(service.get('s','t',off['operation_id'])['state'],'succeeded')
        self.assertLess(len(service._requests),10)
