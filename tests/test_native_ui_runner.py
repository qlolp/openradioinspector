import unittest
from macos.ci.ui_smoke import configure_test_run, passed_cases


CASES = (
    'testNavigationPreservesSessionAndCoreConnection',
    'testPreparationRejectsDuplicatePortsAndAmbiguousFrequency',
    'testReplayFixtureCannotBePresentedAsLiveHardwarePreparation',
    'testArchiveOpensOriginalSpectrumAndMarksHistory',
    'testWorkspaceSwitchKeepsCoreAndSelectedSession',
)


class NativeUIRunnerTests(unittest.TestCase):
    def test_relocated_run_keeps_the_original_built_product_root(self):
        source = {'UI': {'TestBundlePath': '__TESTHOST__/PlugIns/DvoynoyObzorUITests.xctest',
                         'TestHostPath': '__TESTROOT__/Debug/Runner.app',
                         'UITargetAppPath': '__TESTROOT__/Debug/DvoynoyObzor.app'}}
        result = configure_test_run(source, '/private/profile.json', test_root='/build/Products')
        self.assertEqual(result['UI']['TestHostPath'], '/build/Products/Debug/Runner.app')
        self.assertEqual(result['UI']['UITargetAppPath'], '/build/Products/Debug/DvoynoyObzor.app')
        self.assertEqual(result['UI']['TestBundlePath'], '__TESTHOST__/PlugIns/DvoynoyObzorUITests.xctest')

    def test_profile_is_injected_only_into_the_single_ui_test_target(self):
        ui = {'TestBundlePath': '__TESTROOT__/DvoynoyObzorUITests.xctest',
              'EnvironmentVariables': {'UNCHANGED': 'yes'}}
        unit = {'TestBundlePath': '__TESTROOT__/OtherTests.xctest', 'EnvironmentVariables': {}}
        source = {'TestConfigurations': [{'TestTargets': [ui, unit]}]}
        result = configure_test_run(source, '/private/ui/profile.json')
        targets = result['TestConfigurations'][0]['TestTargets']
        self.assertEqual(targets[0]['EnvironmentVariables'], {
            'UNCHANGED': 'yes', 'ORI_UI_TEST_PROFILE': '/private/ui/profile.json'})
        self.assertEqual(targets[1], unit)
        self.assertNotIn('ORI_UI_TEST_PROFILE', ui['EnvironmentVariables'])

    def test_missing_or_ambiguous_ui_target_is_rejected(self):
        with self.assertRaises(ValueError):
            configure_test_run({'OtherTests': {'TestBundlePath': 'OtherTests.xctest'}}, '/p')
        with self.assertRaises(ValueError):
            configure_test_run({'A': {'TestBundlePath': 'DvoynoyObzorUITests.xctest'},
                                'B': {'TestBundlePath': 'DvoynoyObzorUITests.xctest'}}, '/p')

    def test_success_requires_each_real_ui_case_and_no_failed_attempt(self):
        output = '\n'.join(f"Test Case '-[DvoynoyObzorUITests.WorkbenchUITests {name}]' passed (1 seconds)." for name in CASES)
        self.assertEqual(passed_cases(output), sorted(CASES))
        for invalid in [output.splitlines()[0], output.replace(CASES[0], 'testDifferent'),
                        output + "\nTest Case '-[WorkbenchUITests testExample]' failed (1 seconds)."]:
            with self.assertRaises(ValueError):
                passed_cases(invalid)
