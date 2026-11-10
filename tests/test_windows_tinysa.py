import threading
import unittest


class ControlTests(unittest.TestCase):
    def test_stale_acquire_is_released_in_its_original_scope(self):
        from radioinspector.windows.tinysa import InstrumentConnection
        class Client:
            calls=[]
            def instrument(self,s,t,resource,payload=None,**kwargs):
                self.calls.append((s,t,resource,payload))
                if resource=='operations':return {'operation_id':'job'}
                return {'state':'succeeded','result':{'lease_id':'secret'}}
        client=Client();context=InstrumentConnection(client,'old-session','old-stream')
        context.closed.set()
        with self.assertRaises(ValueError):context.operation('lease.acquire',{'acknowledge_rf':True})
        self.assertFalse(client.calls)

    def test_context_change_while_request_in_flight_cannot_retain_lease(self):
        from radioinspector.windows.tinysa import InstrumentConnection
        class Client:
            calls=[]
            def instrument(self,s,t,resource,payload=None,**kwargs):
                self.calls.append((s,t,resource,payload))
                if resource=='operations':return {'operation_id':'job'}
                context.closed.set()
                return {'state':'succeeded','result':{'lease_id':'secret'}}
        client=Client();context=InstrumentConnection(client,'s','t')
        with self.assertRaises(ValueError):context.operation('lease.acquire',{'acknowledge_rf':True})
        self.assertIsNone(context.lease)
        self.assertEqual(client.calls[-1][3]['operation'],'lease.release')

    def test_operation_is_submitted_once_and_cancel_uses_exact_job(self):
        from radioinspector.windows.tinysa import InstrumentConnection
        class Client:
            calls=[]
            def instrument(self,s,t,resource,payload=None,**kwargs):
                self.calls.append((s,t,resource,payload))
                if resource=='operations':return {'operation_id':'exact-job'}
                if resource.endswith('/cancel'):return {'state':'cancelled'}
                context.cancel()
                return {'state':'succeeded','result':{'mode':'monitoring'}}
        client=Client();context=InstrumentConnection(client,'s','t')
        context.operation('setting',{'name':'rbw_hz','value':200})
        self.assertEqual(sum(x[2]=='operations' for x in client.calls),1)
        self.assertEqual(client.calls[-1][2],'operations/exact-job/cancel')

    def test_semantic_input_preserves_boolean_and_rejects_nan(self):
        from radioinspector.windows.tinysa import parse_value
        self.assertIs(parse_value({'type':'boolean'},'true'),True)
        self.assertEqual(parse_value({'type':'integer_or_auto'},'auto'),'auto')
        self.assertEqual(parse_value({'type':'integer'},'100000000'),100000000)
        for value in ['NaN','Infinity','1.5']:
            with self.assertRaises(ValueError):parse_value({'type':'integer'},value)
