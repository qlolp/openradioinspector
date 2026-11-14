"""The gate must catch real wire differences, not just matching status codes."""
import json
import copy
from contextlib import closing
import hashlib
import math
from pathlib import Path
import sys
import tempfile
import unittest

from tools.modernization.api_parity import (
    HTTPResult, NodeSpec, OwnedNode, ProtocolError, approved_source_snapshot, compare_http, compare_value, execute_cases, load_json, probe_cli, run_parity,
)


class ExactWireTests(unittest.TestCase):
    def test_reviewed_snapshot_history_is_bounded_without_dropping_old_revisions(self):
        with tempfile.TemporaryDirectory(prefix='ori-reviewed-history-') as directory:
            root = Path(directory)
            (root / 'route.py').write_bytes(b'current reviewed route')
            digest = hashlib.sha256(b'current reviewed route').hexdigest()
            history = [{'revision': format(i, '040x'), 'source_sha256': {
                'route.py': digest if i == 8 else format(i, '064x')}} for i in range(1, 9)]
            inventory = {'python_source_revision': 'a'*40, 'source_sha256': {'route.py': '0'*64},
                         'approved_source_snapshots': history}
            self.assertEqual(approved_source_snapshot(root, inventory),
                             (format(8, '040x'), {'route.py': digest}))
            inventory['approved_source_snapshots'] = history + [history[-1]]
            with self.assertRaises(ProtocolError):
                approved_source_snapshot(root, inventory)

    def test_public_inventory_covers_openapi_and_calibrated_field_success(self):
        from radioinspector.web.openapi import document
        from tools.modernization.api_parity import public_cases
        cases, inventory = public_cases()
        operations = {(item['method'], item['path']) for item in inventory['operations']}
        expected = {(method.upper(), path) for path, item in document()['paths'].items()
                    for method in item if method in ('get', 'post', 'put', 'patch', 'delete', 'head', 'options')}
        self.assertEqual(operations, expected)
        by_id = {case['id']: case for case in cases}
        success = by_id['field-rms-calibrated']
        self.assertEqual(success['status'], 200)
        self.assertTrue(success['unchanged'])
        self.assertEqual(success['original_fields']['/data/value_dbuv_per_m'], 55.0)
        unknown = by_id['field-rms-unknown-uncertainty']
        self.assertEqual(unknown['status'], 200)
        self.assertIsNone(unknown['original_fields']['/data/uncertainty_db'])
        refused = by_id['field-rms-wrong-chain']
        self.assertEqual(refused['status'], 409)
        self.assertTrue(refused['unchanged'])

    def test_approved_source_snapshot_requires_one_whole_reviewed_revision(self):
        import hashlib
        with tempfile.TemporaryDirectory(prefix='ori-reviewed-inventory-') as directory:
            root=Path(directory)
            old={name:hashlib.sha256(value).hexdigest() for name,value in {'one.py':b'old-one','two.py':b'old-two'}.items()}
            new={name:hashlib.sha256(value).hexdigest() for name,value in {'one.py':b'new-one','two.py':b'new-two'}.items()}
            inventory={'python_source_revision':'a'*40,'source_sha256':old,
                       'approved_source_snapshots':[{'revision':'b'*40,'source_sha256':new}]}
            for values,revision,hashes in (([b'old-one',b'old-two'],'a'*40,old),([b'new-one',b'new-two'],'b'*40,new)):
                for name,value in zip(old,values): (root/name).write_bytes(value)
                self.assertEqual(approved_source_snapshot(root,inventory),(revision,hashes))
            (root/'one.py').write_bytes(b'old-one')
            self.assertIsNone(approved_source_snapshot(root,inventory)[0], 'Per-file approved revisions cannot be mixed')
            (root/'one.py').write_bytes(b'unknown')
            self.assertIsNone(approved_source_snapshot(root,inventory)[0])
            (root/'two.py').unlink()
            self.assertIsNone(approved_source_snapshot(root,inventory)[0])

    def test_malformed_reviewed_source_snapshot_fails_closed(self):
        base={'python_source_revision':'a'*40,'source_sha256':{'one.py':'1'*64}}
        malformed=[None,{},[{'revision':'b'*40,'source_sha256':{}}],
                   [{'revision':'b'*40,'source_sha256':{'one.py':True}}],
                   [{'revision':'invalid','source_sha256':{'one.py':'2'*64}}],
                   [{'revision':'b'*40,'source_sha256':{'one.py':'2'*64},'ignore':True}]]
        for snapshots in malformed:
            with self.subTest(snapshots=snapshots),self.assertRaises(ProtocolError):
                approved_source_snapshot(Path('/unused'),{**base,'approved_source_snapshots':snapshots})
        for malformed in ({},None,[],{'../one.py':'1'*64},{'/one.py':'1'*64},
                          {'C:\\one.py':'1'*64},{'a//one.py':'1'*64},{True:'1'*64}):
            with self.subTest(authority=malformed),self.assertRaises(ProtocolError):
                approved_source_snapshot(Path('/unused'),{**base,'source_sha256':malformed})

    def test_reviewed_stream_helper_is_pinned_with_its_route_snapshot(self):
        import hashlib
        with tempfile.TemporaryDirectory(prefix='ori-reviewed-support-') as directory:
            root=Path(directory)
            route=root/'route.py';route.write_bytes(b'route')
            helper=root/'stream.py';helper.write_bytes(b'stream')
            inventory={'python_source_revision':'a'*40,
                       'source_sha256':{'route.py':hashlib.sha256(route.read_bytes()).hexdigest()},
                       'source_support_sha256':{'stream.py':hashlib.sha256(helper.read_bytes()).hexdigest()}}
            self.assertEqual(approved_source_snapshot(root,inventory)[0],'a'*40)
            helper.write_bytes(b'changed transport')
            self.assertEqual(approved_source_snapshot(root,inventory),(None,['stream.py']))
            for malformed in (None,[],{'route.py':'1'*64}):
                with self.subTest(malformed=malformed),self.assertRaises(ProtocolError):
                    approved_source_snapshot(root,{**inventory,'source_support_sha256':malformed})

    def test_decoder_rejects_duplicate_nonfinite_bad_utf8_and_oversized_json(self):
        for value in [b'{"id":1,"id":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":"\xff"}',br'{"x":"\ud800"}']:
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                load_json(value)
        with self.assertRaises(ProtocolError):
            load_json(b'{"value":"large"}', maximum=8)

    def test_float_bits_integer_hz_utc_microseconds_and_utf8_identity_are_exact(self):
        vectors = [
            ({"levels": [-0.0]}, {"levels": [0.0]}),
            ({"level": 1.0}, {"level": math.nextafter(1.0, math.inf)}),
            ({"frequency_hz": 9007199254740992}, {"frequency_hz": 9007199254740993}),
            ({"id": "é"}, {"id": "e\u0301"}),
            ({"metadata": {"é": 1, "e\u0301": 2}}, {"metadata": {"é": 2, "e\u0301": 1}}),
            ({"interval": {"started_at": "2026-11-11T12:00:00.123456Z"}},
             {"interval": {"started_at": "2026-11-11T12:00:00.123457Z"}}),
        ]
        for left, right in vectors:
            with self.subTest(left=left):
                self.assertTrue(compare_value(left, right))
                self.assertEqual(compare_value(left, left), [])

    def test_declared_lifecycle_clock_variation_cannot_mask_source_or_metadata(self):
        left={"data":{"created_at":"2026-11-11T12:00:00.123456Z",
                      "interval":{"started_at":"2026-11-11T12:00:00.123456Z"},
                      "metadata":{"created_at":"original"}}}
        right=json.loads(json.dumps(left))
        right["data"]["created_at"]="2026-11-11T12:01:00.123456Z"
        self.assertEqual(compare_value(left,right,volatile_paths=("/data/created_at",)),[])
        right["data"]["metadata"]["created_at"]="changed"
        self.assertTrue(compare_value(left,right,volatile_paths=("/data/created_at",)))
        right["data"]["metadata"]["created_at"]="original"
        right["data"]["created_at"]="invalid"
        self.assertTrue(compare_value(left,right,volatile_paths=("/data/created_at",)))

    def test_array_clock_pattern_cannot_mask_an_opaque_object_key(self):
        left={"data":{"metadata":{"created_at":"2026-11-11T12:00:00.123456Z"}}}
        right={"data":{"metadata":{"created_at":"2026-11-11T12:00:01.123456Z"}}}
        self.assertTrue(compare_value(left,right,volatile_paths=("/data/*/created_at",)))
        self.assertEqual(compare_value({"data":[left["data"]["metadata"]]},
                                       {"data":[right["data"]["metadata"]]},volatile_paths=("/data/*/created_at",)),[])

    def test_matching_status_does_not_hide_changed_envelope_error_or_measurements(self):
        def response(value):
            return HTTPResult(200,{"content-type":"application/json"},json.dumps(value).encode())
        left=response({"contract":"openradioinspector-platform","contract_version":1,"data":{"level":-0.0}})
        changed=response({"contract":"openradioinspector-platform","contract_version":1,"data":{"level":0.0}})
        self.assertEqual(compare_http(left,changed).state,"mismatch")
        self.assertEqual(compare_http(left,left).state,"pass")

    def test_only_named_diagnostic_extensions_are_additive(self):
        def response(data):
            return HTTPResult(200, {"content-type":"application/json"},
                              json.dumps({"data":data}).encode())
        before=response({"status":"ok", "last_event_id":7})
        after=response({"status":"ok", "last_event_id":7, "backend":"rust-candidate"})
        self.assertEqual(compare_http(before,after,operation="GET /api/v1/health").state,"pass")
        self.assertEqual(compare_http(before,after).state,"mismatch")
        for data in ({"status":"error", "last_event_id":7, "backend":"rust-candidate"},
                     {"status":"ok", "last_event_id":8, "backend":"rust-candidate"},
                     {"status":"ok", "last_event_id":7, "backend":False},
                     {"status":"ok", "last_event_id":7, "unknown":True}):
            self.assertNotEqual(compare_http(before,response(data),operation="GET /api/v1/health").state,"pass")
        a=response({"enabled":False}); b=response({"enabled":False,"execution_available":False})
        self.assertEqual(compare_http(a,b,operation="GET /api/v1/task-scheduler").state,"pass")
        self.assertEqual(compare_http(a,b,operation="GET /api/v1/sessions").state,"mismatch")
        shared=response({"enabled":False,"execution_available":True})
        self.assertEqual(compare_http(shared,b,operation="GET /api/v1/task-scheduler").state,"mismatch")
        self.assertEqual(compare_http(shared,a,operation="GET /api/v1/task-scheduler").state,"mismatch")
        self.assertEqual(compare_http(after,before,operation="GET /api/v1/health").state,"mismatch")

    def test_capacity_layout_requires_independent_database_proof_and_exact_policy(self):
        def response(size, **changes):
            data={"storage_mode":"sqlite", "database_bytes":size,
                  "max_database_bytes":1048576, "remaining_bytes":1048576-size,
                  "state":"ok", "warning_threshold":0.9,
                  "measurement_writes_blocked":False, "automatic_deletion":False}
            data.update(changes)
            return HTTPResult(200,{"content-type":"application/json"},json.dumps({"data":data}).encode())
        operation="GET /api/v1/archive/capacity"
        a,b=response(4096),response(8192)
        self.assertEqual(compare_http(a,b,operation=operation).state,"mismatch")
        self.assertEqual(compare_http(a,b,operation=operation,storage_layout_verified=True).state,"pass")
        for changes in ({"remaining_bytes":1}, {"database_bytes":True}, {"state":"full"},
                        {"automatic_deletion":True}, {"warning_threshold":0.5},
                        {"max_database_bytes":2097152,"remaining_bytes":2097152-8192}):
            with self.subTest(changes=changes):
                self.assertNotEqual(compare_http(a,response(8192,**changes),operation=operation,
                                               storage_layout_verified=True).state,"pass")

    def test_capabilities_allow_only_new_negotiation_fields_and_keep_shared_values(self):
        def response(data):
            return HTTPResult(200,{"content-type":"application/json"},json.dumps({"data":data}).encode())
        before={"session_storage":"sqlite", "features":{"measurement_ingest":True}}
        metadata={"mode":"replay", "compatibility_complete":False, "release_accepted":False,
                  "hardware_validation":"not_run", "unsupported_features":["tasks"],
                  "resource_limits":{"request_slots":16}}
        after=dict(before,storage_mode="sqlite",candidate=metadata,
                   features={"measurement_ingest":True,"new_binary_stream":{"version":1}})
        operation="GET /api/v1/capabilities"
        self.assertEqual(compare_http(response(before),response(after),operation=operation).state,"pass")
        flags=dict(after,candidate=dict(metadata,resource_limits={"request_slots":16,
                   "native_connector_enabled":False,"measured_rss_gate_passed":False}))
        self.assertEqual(compare_http(response(before),response(flags),operation=operation).state,"pass")
        history=dict(after,candidate=dict(metadata,history_worker_configured=True))
        self.assertEqual(compare_http(response(before),response(history),operation=operation).state,"pass")
        qos={"acquisition_pressure":False,"registered_work":0,"logical_bytes":0,
             "logical_bytes_per_second":20*1024*1024,"physical_io_enforced":False,
             "cross_process_logical_io":True,"cleanup_failed":False}
        background=dict(after,candidate=dict(metadata,aggregate_background_io_complete=False,
                                             background_qos=qos))
        self.assertEqual(compare_http(response(before),response(background),operation=operation).state,"pass")
        for changed in (dict(qos,registered_work=True),dict(qos,registered_work=9),
                        dict(qos,logical_bytes=2**64),dict(qos,logical_bytes=-1),
                        dict(qos,logical_bytes_per_second=1),dict(qos,physical_io_enforced=True),
                        dict(qos,cleanup_failed=1),dict(qos,unknown=True)):
            bad=dict(after,candidate=dict(metadata,aggregate_background_io_complete=False,
                                          background_qos=changed))
            self.assertNotEqual(compare_http(response(before),response(bad),operation=operation).state,"pass")
        for claim in (True,1):
            bad=dict(after,candidate=dict(metadata,aggregate_background_io_complete=claim))
            self.assertNotEqual(compare_http(response(before),response(bad),operation=operation).state,"pass")
        self.assertEqual(compare_http(response(before),response(after)).state,"mismatch")
        for changed in (dict(after,features={"measurement_ingest":False}),
                        dict(after,features={"new_binary_stream":True}),
                        dict(after,storage_mode="memory"),
                        dict(after,candidate=dict(metadata,release_accepted=True)),
                        dict(after,candidate=dict(metadata,hardware_validation="passed")),
                        dict(after,candidate=dict(metadata,history_worker_configured=1)),
                        dict(after,candidate=dict(metadata,resource_limits={"request_slots":True})),
                        dict(after,candidate=dict(metadata,resource_limits={"native_connector_enabled":2**40})),
                        dict(after,unrelated=True)):
            with self.subTest(changed=changed):
                self.assertNotEqual(compare_http(response(before),response(changed),operation=operation).state,"pass")
        self.assertEqual(compare_http(response(after),response(before),operation=operation).state,"mismatch")
        shared=dict(after,features=dict(after['features'],new_binary_stream={"version":2}))
        self.assertEqual(compare_http(response(after),response(shared),operation=operation).state,"mismatch")

    def test_native_connector_diagnostic_matches_its_existing_resource_flag(self):
        def response(metadata=None):
            data={'features':{'hardware_control':True}}
            if metadata is not None:
                data['candidate']=metadata
            return HTTPResult(200,{'content-type':'application/json'},json.dumps({'data':data}).encode())
        metadata={'mode':'replay','compatibility_complete':False,'release_accepted':False,
                  'hardware_validation':'not_run','unsupported_features':[],
                  'resource_limits':{'native_connector_enabled':False},
                  'native_connector_enabled':False}
        operation='GET /api/v1/capabilities'
        self.assertEqual(compare_http(response(),response(metadata),operation=operation).state,'pass')
        for changed in (dict(metadata,native_connector_enabled=True),
                        dict(metadata,native_connector_enabled=0),
                        dict(metadata,resource_limits={'request_slots':16}),
                        dict(metadata,resource_limits=None),dict(metadata,resource_limits=[])):
            with self.subTest(changed=changed):
                self.assertNotEqual(compare_http(response(),response(changed),operation=operation).state,'pass')
        self.assertEqual(compare_http(response(metadata),response(),operation=operation).state,'mismatch')

    def test_acquisition_pressure_diagnostic_has_bounded_exact_schema(self):
        def response(status=None):
            data={'features':{'hardware_control':True}}
            if status is not None:
                data['candidate']={'mode':'replay','compatibility_complete':False,
                    'release_accepted':False,'hardware_validation':'not_run',
                    'unsupported_features':[],'resource_limits':{'device_workers':32},
                    'acquisition_pressure':status}
            return HTTPResult(200,{'content-type':'application/json'},json.dumps({'data':data}).encode())
        status={'observer_running':True,'acquisition_pressure':False,'active_analyzers':2,
                'calibrating_analyzers':1,'delayed_analyzers':0,'last_error':None}
        operation='GET /api/v1/capabilities'
        self.assertEqual(compare_http(response(),response(status),operation=operation).state,'pass')
        bad=[dict(status,observer_running=1),dict(status,acquisition_pressure=0),
             dict(status,active_analyzers=True),dict(status,active_analyzers=-1),
             dict(status,active_analyzers=33),dict(status,calibrating_analyzers=3),
             dict(status,delayed_analyzers=3),dict(status,delayed_analyzers=True),
             dict(status,last_error=False),dict(status,last_error='x'*257),
             dict(status,last_error='line\nbreak'),dict(status,unknown=True)]
        for key in status:
            omitted=dict(status)
            del omitted[key]
            bad.append(omitted)
        for changed in bad:
            with self.subTest(changed=changed):
                self.assertNotEqual(compare_http(response(),response(changed),operation=operation).state,'pass')
        changed=dict(status,acquisition_pressure=True,delayed_analyzers=1)
        self.assertEqual(compare_http(response(),response(changed),operation=operation).state,'pass')
        overlapping=dict(changed,calibrating_analyzers=2)
        self.assertEqual(compare_http(response(),response(overlapping),operation=operation).state,'pass')
        self.assertEqual(compare_http(response(status),response(changed),operation=operation).state,'mismatch')
        self.assertEqual(compare_http(response(status),response(),operation=operation).state,'mismatch')

    def test_unavailable_candidate_route_is_never_a_pass_or_skip(self):
        left=HTTPResult(200,{"content-type":"application/json"},b'{"data":{"policy":{"enabled":false}}}')
        right=HTTPResult(501,{"content-type":"application/json"},b'{"error":{"code":"candidate_route_unavailable","message":"not integrated"}}')
        outcome=compare_http(left,right)
        self.assertEqual(outcome.state,"unavailable")
        self.assertFalse(outcome.successful)

    def test_capability_raw_export_diagnostics_require_exact_typed_limits(self):
        def response(data):
            return HTTPResult(200,{'content-type':'application/json'},json.dumps({'data':data}).encode())
        metadata={'mode':'replay','compatibility_complete':False,'release_accepted':False,
                  'hardware_validation':'not_run','unsupported_features':[],
                  'resource_limits':{'request_slots':16}}
        limits={'live_readers':2,'max_record_bytes':16*1024*1024,'read_timeout_ms':5000,
                'whole_database_copy':False,'heavy_research_permit':False,
                'native_source_io_accounted':False}
        before={'features':{'session_archive_export':True}}
        operation='GET /api/v1/capabilities'
        def candidate(value):
            return response(dict(before,candidate=dict(metadata,raw_export_limits=value)))
        self.assertEqual(compare_http(response(before),candidate(limits),operation=operation).state,'pass')
        for field,value in (('live_readers',True),('max_record_bytes',True),('read_timeout_ms',5000.0),
                            ('whole_database_copy',0),('heavy_research_permit',True),
                            ('native_source_io_accounted',True),('unknown',False)):
            changed=dict(limits,**{field:value})
            self.assertNotEqual(compare_http(response(before),candidate(changed),operation=operation).state,'pass')
        self.assertNotEqual(compare_http(response(before),candidate({}),operation=operation).state,'pass')
        shared=response(dict(before,candidate=dict(metadata,raw_export_limits=limits)))
        changed=dict(limits,read_timeout_ms=5001)
        self.assertNotEqual(compare_http(shared,candidate(changed),operation=operation).state,'pass')

    def test_openapi_additions_require_the_exact_generated_contract_and_baseline(self):
        from copy import deepcopy
        root = Path(__file__).resolve().parents[1]
        baseline = json.loads((root/'contracts/generated/openapi-v1.json').read_text())
        runtime = json.loads((root/'contracts/generated/runtime-openapi-v1.json').read_text())
        def response(document):
            return HTTPResult(200, {'content-type':'application/json'}, json.dumps(document).encode())
        operation = 'GET /api/v1/openapi.json'
        self.assertEqual(compare_http(response(baseline), response(runtime), operation=operation).state, 'pass')
        self.assertEqual(compare_http(response(baseline), response(runtime)).state, 'mismatch')
        self.assertNotEqual(compare_http(response(runtime), response(baseline), operation=operation).state, 'pass')
        changed = deepcopy(runtime)
        next(iter(changed['paths'].values()))['get']['responses']['200']['description'] = 'changed common response'
        self.assertNotEqual(compare_http(response(baseline), response(changed), operation=operation).state, 'pass')
        for document, key, value in ((runtime, 'x-ori-baseline-schema-sha256', '0'*64),
                                     (baseline, 'info', {'title':'changed producer'}),
                                     (runtime, 'unreviewed_extension', True)):
            changed = deepcopy(document)
            changed[key] = value
            left, right = (changed, runtime) if document is baseline else (baseline, changed)
            self.assertNotEqual(compare_http(response(left), response(right), operation=operation).state, 'pass')

    def test_missing_cli_executable_is_explicit_unavailable(self):
        result=probe_cli([str(Path("/nonexistent/ori-parity-owned-command")),"--version"])
        self.assertEqual(result["state"],"unavailable")
        self.assertFalse(result["successful"])

    def test_ndjson_compares_records_and_rejects_partial_successful_eof(self):
        headers={"content-type":"application/x-ndjson","x-archive-sweeps":"1","x-archive-observations":"0"}
        header=b'{"format":"fixture","format_version":1}\n'
        left=HTTPResult(200,headers,header+b'{"kind":"sweep","measurement":{"id":"x","levels":[-0.0]}}\n')
        right=HTTPResult(200,headers,header+b'{"measurement":{"levels":[-0.0],"id":"x"},"kind":"sweep"}\n')
        self.assertEqual(compare_http(left,right).state,"pass")
        changed=HTTPResult(200,headers,right.body.replace(b'-0.0',b'0.0'))
        self.assertEqual(compare_http(left,changed).state,"mismatch")
        partial=HTTPResult(200,headers,left.body[:-1])
        self.assertEqual(compare_http(partial,partial).state,"error")

    def test_fast_cli_output_overflow_cannot_look_successful_after_zero_exit(self):
        import time
        from unittest.mock import patch

        from tools.modernization.owned_process import ManagedProcess
        wait=ManagedProcess.wait
        def after_output_ready(process,timeout):
            deadline=time.monotonic()+10
            while not process.output_exceeded:
                if time.monotonic()>=deadline:raise AssertionError("controlled output never reached its budget")
                time.sleep(.005)
            return wait(process,timeout)
        with patch.object(ManagedProcess,'wait',after_output_ready):
            result=probe_cli([sys.executable,"-c","import sys;sys.stdout.write('x'*1048576)"])
        self.assertFalse(result["successful"])
        self.assertEqual(result["error"],"output_budget")
        self.assertLessEqual(len(result["stdout"]),256*1024)

    def test_cli_deadline_owns_inherited_descendant_pipes_after_parent_exit(self):
        from tests.test_api_process_ownership import inherited_pipe_scenario
        inherited_pipe_scenario(self,changed_group=False)


class TypedControlExtensionTests(unittest.TestCase):
    operation='tinySA typed202/poll/idempotency/conflict/cancel'

    def bodies(self):
        root=Path(__file__).resolve().parents[1]
        fixture=load_json((root/'tests/fixtures/api_parity_async_control_v1.json').read_bytes())
        self.assertEqual(fixture['catalogue']['source'],'crates/ori-control/src/resources/capabilities.json')
        source=root/fixture['catalogue']['source']
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),fixture['catalogue']['sha256'])
        catalogue=load_json(source.read_bytes())
        for key in ['reference','candidate_with_wire_regressions']:
            body=fixture[key]
            self.assertEqual(body['capabilities']['data']['capabilities'],{'$fixture_catalogue':'tinysa-control-v1'})
            body['capabilities']['data']['capabilities']=copy.deepcopy(catalogue)
            self.assertEqual(body['terminal_records'][0]['result']['capabilities'],{'$fixture_catalogue':'tinysa-control-v1'})
            body['terminal_records'][0]['result']['capabilities']=copy.deepcopy(catalogue)
        old=fixture['candidate_with_wire_regressions']
        for name,available in fixture['candidate_operation_availability'].items():
            old['capabilities']['data']['capabilities']['operations'][name]['available']=available
        return fixture['reference'],old

    def additions_only(self):
        reference,old=self.bodies();candidate=copy.deepcopy(reference)
        # Copy only actual captured additions; shared scalar/message regressions
        # remain separately tested using the untouched original RED payload.
        pairs=[(reference['capabilities']['data']['status'],old['capabilities']['data']['status'],candidate['capabilities']['data']['status']),
               (reference['terminal_records'][0]['result']['status'],old['terminal_records'][0]['result']['status'],candidate['terminal_records'][0]['result']['status']),
               (reference['terminal_records'][2],old['terminal_records'][2],candidate['terminal_records'][2])]
        for name,spec in reference['capabilities']['data']['capabilities']['operations'].items():
            pairs.append((spec,old['capabilities']['data']['capabilities']['operations'][name],candidate['capabilities']['data']['capabilities']['operations'][name]))
        for original,captured,target in pairs:
            target.update({key:copy.deepcopy(value) for key,value in captured.items() if key not in original})
        return reference,candidate

    @staticmethod
    def response(body):
        return HTTPResult(200,{'content-type':'application/json'},json.dumps(body,ensure_ascii=False,allow_nan=False).encode())

    def outcome(self,reference,candidate,**options):
        return compare_http(self.response(reference),self.response(candidate),operation=self.operation,
                            typed_control_collected=True,**options)

    def test_actual_additions_require_scope_and_retain_raw_bytes_and_evidence(self):
        reference,candidate=self.additions_only()
        left,right=self.response(reference),self.response(candidate);before=right.body
        self.assertEqual(compare_http(left,right,operation=self.operation).state,'mismatch')
        outcome=compare_http(left,right,operation=self.operation,typed_control_collected=True)
        self.assertEqual(outcome.state,'pass',outcome)
        self.assertEqual(len(outcome.validated_extensions),27)
        self.assertEqual(len({item['path'] for item in outcome.validated_extensions}),27)
        self.assertEqual(right.body,before)
        self.assertEqual(compare_http(left,right,operation='GET /api/v1/instrument/status',typed_control_collected=True).state,'mismatch')

    def test_actual_original_wire_errors_and_float_zero_still_fail(self):
        reference,old=self.bodies()
        outcome=self.outcome(reference,old)
        self.assertEqual(outcome.state,'mismatch')
        self.assertEqual(len(outcome.details),5,outcome.details)
        self.assertTrue(any('lease_seconds: JSON value type differs' in detail for detail in outcome.details))
        self.assertTrue(any('conflicting_request/error/message' in detail for detail in outcome.details))
        self.assertEqual(len(outcome.validated_extensions),27)

    def test_trace_scope_requires_original_case_and_keeps_evidence_on_other_failure(self):
        from tools.modernization.api_parity import _compare_traces
        reference,candidate=self.additions_only()
        case={'id':'instrument-async-protocol','operation':self.operation,'method':'PROTOCOL',
              'path':'/api/v1/sessions/lab/streams/one/instrument','volatile_paths':[]}
        left={'case':case,'response':self.response(reference),'checks':[]}
        right={'case':copy.deepcopy(case),'response':self.response(candidate),'checks':[]}
        result=_compare_traces([left],[right])[0]
        self.assertTrue(result['successful']);self.assertEqual(len(result['validated_extensions']),27)
        right['checks']=['canonical state proof failed']
        result=_compare_traces([left],[right])[0]
        self.assertFalse(result['successful']);self.assertEqual(len(result['validated_extensions']),27)
        right['checks']=[]
        for key,bad in [('operation','GET /api/v1/instrument/status'),('method','GET'),('path','/other')]:
            changed=copy.deepcopy(right);changed['case'][key]=bad
            with self.subTest(key=key):self.assertFalse(_compare_traces([left],[changed])[0]['successful'])
        for row in [left,right]:row['case']['id']='generic-response'
        self.assertFalse(_compare_traces([left],[right])[0]['successful'])

    def test_every_declared_availability_must_match_the_actual_no_audio_fixture(self):
        reference,candidate=self.additions_only()
        for name,spec in candidate['capabilities']['data']['capabilities']['operations'].items():
            for bad in [not spec['available'],0,1,None,'false']:
                changed=copy.deepcopy(candidate)
                changed['capabilities']['data']['capabilities']['operations'][name]['available']=bad
                with self.subTest(name=name,bad=bad):self.assertFalse(self.outcome(reference,changed).successful)

    def test_stage_incoherent_or_physical_claims_never_pass(self):
        reference,candidate=self.additions_only()
        for key,bad_values in {'device_mode':['input','output','other',True],
                'device_mode_readback':[True,0,None],'generator_output_readback':[True,0,None],
                'hardware_validation':['passed','not-run',False],'lease_seconds':[0.0,1,False,None]}.items():
            for bad in bad_values:
                changed=copy.deepcopy(candidate);changed['capabilities']['data']['status'][key]=bad
                with self.subTest(key=key,bad=bad):self.assertFalse(self.outcome(reference,changed).successful)
        for key in ['acquisition_paused','restart_required']:
            for bad in [True,0,None]:
                changed=copy.deepcopy(candidate);changed['terminal_records'][0]['result']['status'][key]=bad
                with self.subTest(key=key,bad=bad):self.assertFalse(self.outcome(reference,changed).successful)
        for bad in [False,1,None]:
            changed=copy.deepcopy(candidate);changed['terminal_records'][2]['context_current']=bad
            with self.subTest(context=bad):self.assertFalse(self.outcome(reference,changed).successful)

    def test_unknown_missing_partial_and_reversed_additions_fail(self):
        reference,candidate=self.additions_only()
        self.assertFalse(self.outcome(candidate,reference).successful)
        changes=[]
        changed=copy.deepcopy(candidate);changed['capabilities']['data']['status']['unknown']=False;changes.append(changed)
        changed=copy.deepcopy(candidate);del changed['capabilities']['data']['capabilities']['operations']['inspect'];changes.append(changed)
        changed=copy.deepcopy(candidate);changed['capabilities']['data']['capabilities']['operations']['unknown']={'available':True};changes.append(changed)
        changed=copy.deepcopy(candidate);del changed['capabilities']['data']['status']['settings_source'];changes.append(changed)
        changed=copy.deepcopy(candidate);del changed['capabilities']['data']['capabilities']['operations']['inspect']['available'];changes.append(changed)
        for index,changed in enumerate(changes):
            with self.subTest(index=index):self.assertFalse(self.outcome(reference,changed).successful)

    def test_shared_measurements_opaque_keys_and_original_float_bits_remain_exact(self):
        reference,candidate=self.additions_only()
        original={'é':-0.0,'e\u0301':math.nextafter(1.0,math.inf),'frequency_hz':9007199254740993,
                  'started_at':'2026-11-11T12:00:00.123456Z','status':{'restart_required':False}}
        for body in [reference,candidate]:body['terminal_records'][0]['result']['status']['settings']['opaque']=copy.deepcopy(original)
        self.assertTrue(self.outcome(reference,candidate).successful)
        for key,bad in [('é',0.0),('e\u0301',1.0),('frequency_hz',9007199254740992),
                        ('started_at','2026-11-11T12:00:00.123457Z'),('status',{'restart_required':False,'acquisition_paused':False})]:
            changed=copy.deepcopy(candidate);changed['terminal_records'][0]['result']['status']['settings']['opaque'][key]=bad
            with self.subTest(key=key):self.assertFalse(self.outcome(reference,changed).successful)
        changed=copy.deepcopy(candidate);changed['terminal_records'][0]['result']['status']['lease_active']=True
        self.assertFalse(self.outcome(reference,changed).successful)


class OwnedProducerTests(unittest.TestCase):
    def test_embedding_cannot_substitute_a_successful_cli_for_the_original_executable(self):
        root=Path(__file__).resolve().parents[1]
        directory=Path(tempfile.mkdtemp(prefix='ori-parity-embedding-cli-')).resolve()
        try:
            original=OwnedNode(NodeSpec('python',Path(sys.executable),root),directory/'node')
            with original:
                # Real HTTP/SQLite and real executable probes on every platform.
                # Python accepts help/version, but cannot implement Core's
                # --check-ready. An embedding must not hide that CLI failure.
                embedding=root/'crates/ori-core/examples/parity_node.rs'
                node=OwnedNode(NodeSpec('core',Path(sys.executable),root,
                                       fixture_executable=embedding),original.directory)
                node.url=original.url;node.environment=original.environment
                node.request=original.request
                command=node._command(original.directory/'data')
                self.assertEqual(command[0],str(embedding))
                self.assertEqual(command[1::2],['--data-dir','--ready-file'])
                probes=node.readonly_cli()
                self.assertTrue(probes['commands']['help']['successful'],probes)
                self.assertTrue(probes['commands']['version']['successful'],probes)
                self.assertFalse(probes['commands']['readiness']['successful'],probes)
                self.assertFalse(probes['successful'])
                self.assertNotEqual(probes['commands']['readiness']['exit_code'],0)
            self.assertTrue(original.joined)
            self.assertEqual(original.process.returncode,0)
            self.assertFalse(original.ready.exists())
        except BaseException:
            print('Retained failed separate-executable parity fixture:',directory)
            raise
        else:
            import shutil
            shutil.rmtree(directory)

    def test_missing_synthetic_embedding_fails_before_any_owned_process_or_directory(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='ori-parity-missing-embedding-') as directory:
            directory=Path(directory)
            spec=NodeSpec('core',Path(sys.executable),root,
                          fixture_executable=directory/'missing-fixture')
            node=OwnedNode(spec,directory/'node')
            with self.assertRaises(FileNotFoundError):
                node.__enter__()
            self.assertIsNone(node.process)
            self.assertFalse(node.directory.exists())

    def test_python_producer_cannot_be_replaced_by_a_candidate_embedding(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='ori-parity-producer-embedding-') as directory:
            node=OwnedNode(NodeSpec('python',Path(sys.executable),root,
                                   fixture_executable=Path(sys.executable)),Path(directory)/'node')
            with self.assertRaises(ProtocolError):
                node.__enter__()
            self.assertIsNone(node.process)
            self.assertFalse(node.directory.exists())

    def test_documented_script_cli_runs_isolated_outside_the_checkout(self):
        import os
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='ori-api-direct-cli-') as directory:
            workspace = Path(directory)
            artifacts = workspace/'artifacts'
            environment = {key:value for key,value in os.environ.items()
                           if key not in ('PYTHONPATH', 'PYTHONHOME')}
            result = probe_cli([sys.executable, '-I', '-B',
                                str(root/'tools/modernization/api_parity.py'),
                                '--source-root', str(root), '--profile', 'sources',
                                '--artifacts', str(artifacts)],
                               timeout=60, environment=environment, cwd=workspace)
            report = artifacts/'report.json'
            self.assertTrue(report.is_file(), result)
            value = json.loads(report.read_text())
            self.assertTrue(value['producer_sanity']['successful'], value)
            self.assertEqual(value['producer_sanity']['compared_cases'], 24)
            self.assertEqual(value['candidate']['state'], 'unavailable')
            self.assertFalse(value['successful'])
            self.assertEqual(result['exit_code'], 1)
            self.assertIsNone(result['error'])
            self.assertEqual(len(value['nodes']), 2)
            self.assertTrue(all(node['joined'] and node['exit_code']==0 for node in value['nodes']))

    def test_capacity_layout_checks_the_owned_read_only_database_before_comparison(self):
        import sqlite3
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(prefix="ori-capacity-layout-") as directory:
            root=Path(directory); (root/"data").mkdir()
            path=root/"data"/"sessions.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE retained (id TEXT PRIMARY KEY)")
                size=connection.execute("PRAGMA page_count").fetchone()[0]*connection.execute("PRAGMA page_size").fetchone()[0]
            try:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            finally:
                connection.close()
            original=path.read_bytes()
            case={"id":"capacity", "method":"GET", "path":"/api/v1/archive/capacity",
                  "operation":"GET /api/v1/archive/capacity", "payload":None, "status":200,
                  "unchanged":False}
            for reported, expected in ((size,True),(size+4096,False)):
                data={"storage_mode":"sqlite", "database_bytes":reported,
                      "max_database_bytes":1048576, "remaining_bytes":1048576-reported,
                      "state":"ok", "warning_threshold":0.9,
                      "measurement_writes_blocked":False, "automatic_deletion":False}
                body=json.dumps({"contract":"openradioinspector-platform", "contract_version":1,
                                 "data":data}).encode()
                response=HTTPResult(200,{"content-type":"application/json"},body)
                node=SimpleNamespace(directory=root,request=lambda *_args:response)
                result=execute_cases(node,[case])[0]
                self.assertEqual(result["capacity_verified"],expected)
                self.assertEqual(bool(result["checks"]),not expected)
                self.assertEqual(path.read_bytes(),original)

    def test_changed_authority_inventory_fails_before_launching_or_claiming_coverage(self):
        with tempfile.TemporaryDirectory(prefix="ori-api-parity-stale-") as directory:
            root=Path(directory).resolve();source=root/"source";source.mkdir()
            result=run_parity(NodeSpec("python",Path(sys.executable),source),None,root/"artifacts",profile="public")
            self.assertFalse(result["successful"])
            self.assertTrue(result["inventory"].get("stale_files"),result)
            self.assertEqual(result["nodes"],[])

    def test_public_profile_covers_published_families_and_async_protocol_with_real_producers(self):
        root=Path(__file__).resolve().parents[1]
        directory=Path(tempfile.mkdtemp(prefix="ori-api-parity-public-")).resolve()
        try:
            result=run_parity(NodeSpec("python",Path(sys.executable),root),None,directory,profile="public")
            self.assertTrue(result["producer_sanity"]["successful"],result)
            self.assertGreaterEqual(result["producer_sanity"]["compared_cases"],140)
            self.assertTrue(result["producer_sanity"]["async_protocol_verified"])
            self.assertEqual(result["inventory"]["published_v1_operations"],136)
            self.assertTrue(all(node["joined"] for node in result["nodes"]))
        except BaseException:
            print("Retained failed public API parity fixture:",directory)
            raise
        else:
            import shutil
            shutil.rmtree(directory)

    def test_python_python_sanity_runs_before_explicit_missing_candidate_result(self):
        root=Path(__file__).resolve().parents[1]
        directory=Path(tempfile.mkdtemp(prefix="ori-api-parity-sanity-")).resolve()
        try:
            result=run_parity(NodeSpec("python",Path(sys.executable),root),None,directory,profile="sources")
            self.assertTrue(result["producer_sanity"]["successful"],result)
            self.assertEqual(result["candidate"]["state"],"unavailable")
            self.assertFalse(result["successful"])
            self.assertGreaterEqual(result["producer_sanity"]["compared_cases"],10)
            self.assertTrue(all(node["joined"] for node in result["nodes"]))
        except BaseException:
            print("Retained failed API parity sanity:",directory)
            raise
        else:
            import shutil
            shutil.rmtree(directory)

    def test_actual_http_owned_python_fixture_preserves_source_and_joins_before_cleanup(self):
        root=Path(__file__).resolve().parents[1]
        directory=Path(tempfile.mkdtemp(prefix="ori-api-parity-test-")).resolve()
        spec=NodeSpec("python",Path(sys.executable),root)
        node=OwnedNode(spec,directory/"node")
        try:
            with node:
                session={"id":"session-é","name":"Synthetic wire parity","streams":[
                    {"id":"spectrum/e\u0301","device":"synthetic","role":"spectrum_analyzer","level_unit":"dBm"}]}
                created=node.request("POST","/api/v1/sessions",session)
                self.assertEqual(created.status,201,created.body)
                source={"id":"é","stream_id":"spectrum/e\u0301","interval":{
                    "started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.123457Z"},
                    "frequencies_hz":[100000001,100000003],"levels":[-0.0,math.nextafter(1.0,math.inf)],"level_unit":"dBm",
                    "settings":{"opaque":{"é":-0.0,"e\u0301":math.nextafter(1.0,math.inf)}}}
                saved=node.request("POST","/api/v1/sessions/session-%C3%A9/sweeps",source)
                self.assertEqual(saved.status,201,saved.body)
                expected=dict(source,session_id="session-é")
                self.assertEqual(compare_value(saved.json()["data"],expected),[])
                listed=node.request("GET","/api/v1/sessions/session-%C3%A9/sweeps")
                self.assertEqual(compare_value(listed.json()["data"],[expected]),[])
                # This is an actual owned SQLite service, not an HTTP stub.
                self.assertTrue((node.directory/"data"/"sessions.sqlite3").is_file())
            self.assertEqual(node.process.returncode,0)
            self.assertTrue(node.joined)
            self.assertFalse(node.ready.exists())
        except BaseException:
            print("Retained failed API parity fixture:",directory)
            raise
        else:
            import shutil
            shutil.rmtree(directory)


if __name__=="__main__":
    unittest.main()
