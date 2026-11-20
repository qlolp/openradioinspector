import copy,unittest,json,tempfile
from pathlib import Path
from unittest.mock import patch
from benchmarks.ori_bench.mac_acceptance.preflight import assess_preflight

class PreflightRegressions(unittest.TestCase):
    def facts(self):
        return dict(host=dict(system='Darwin',architecture='arm64',macos_version='27.3',memory_bytes=16*1024**3),
                    signature_verified=True,inventory_verified=True,private_directory=True,cloud_directory=False,
                    local_volume=True,free_bytes=40*1024**3,required_bytes=30*1024**3,power_source='AC Power',
                    native=dict(screen_capture_allowed=True,screen_locked=False,screen_width=1470,screen_height=956),
                    test_port_available=True,acceptance_pointer_present=False,device_owners=[])
    def test_complete_actual_host_facts_are_ready_without_accepting_load_or_hardware(self):
        result=assess_preflight(self.facts());self.assertTrue(result['ready'])
        self.assertIs(result['point2_passed'],False);self.assertIs(result['point4_passed'],False)
    def test_each_missing_precondition_fails_closed(self):
        for key,value in [('private_directory',False),('cloud_directory',True),('local_volume',False),
                          ('free_bytes',1),('power_source','Battery Power'),('signature_verified',False),
                          ('inventory_verified',False),('acceptance_pointer_present',True),
                          ('test_port_available',False),('device_owners',['owned elsewhere'])]:
            with self.subTest(key=key):
                facts=self.facts();facts[key]=value;self.assertFalse(assess_preflight(facts)['ready'])
    def test_native_permission_and_actual_geometry_are_required(self):
        for key,value in [('screen_capture_allowed',False),('screen_locked',True),('screen_width',1000),('screen_height',700)]:
            with self.subTest(key=key):
                facts=self.facts();facts['native'][key]=value;self.assertFalse(assess_preflight(facts)['ready'])
    def test_wrong_host_and_missing_native_receipt_do_not_pass(self):
        facts=self.facts();facts['host']['macos_version']='14.7';self.assertFalse(assess_preflight(facts)['ready'])
        facts=self.facts();facts.pop('native');self.assertFalse(assess_preflight(facts)['ready'])

    def test_old_candidate_is_refused_without_starting_its_unknown_cli(self):
        from benchmarks.ori_bench.mac_acceptance.preflight import native_host_info
        with tempfile.TemporaryDirectory() as directory:
            app=Path(directory)/'RC.app';resources=app/'Contents/Resources';resources.mkdir(parents=True)
            (resources/'rc-build-info.json').write_text(json.dumps({'acceptance_protocol':1}))
            with patch('benchmarks.ori_bench.mac_acceptance.preflight.subprocess.run',side_effect=AssertionError('old UI launched')):
                with self.assertRaises(ValueError):native_host_info(app,Path(directory),{'native_sha256':'a'*64})

if __name__=='__main__':unittest.main()
