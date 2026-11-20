import unittest
import json
import stat
import os
import tempfile
from pathlib import Path
from macos.ci.ui_smoke import collect_training_images, configure_test_run, passed_cases, prepare_profiles


CASES = (
    'testNavigationPreservesSessionAndCoreConnection',
    'testPreparationRejectsDuplicatePortsAndAmbiguousFrequency',
    'testReplayFixtureCannotBePresentedAsLiveHardwarePreparation',
    'testArchiveOpensOriginalSpectrumAndMarksHistory',
    'testWorkspaceSwitchKeepsCoreAndSelectedSession',
    'testEventOpensExactOriginalAlongsideReception',
    'testHistoricalEventOpensReadOnlyOriginalWithoutSwitchingAcquisition',
    'testTrainingWindowCaptures',
)


class NativeUIFixtureTests(unittest.TestCase):
    def test_each_project_event_resolves_to_the_exact_synthetic_original(self):
        from radioinspector.session_service import SessionService
        from radioinspector.session_store import SQLiteSessionStore
        from radioinspector.monitoring.store import ProjectStore
        from tests.ui_smoke_server import _populate
        with SQLiteSessionStore(':memory:') as store:
            service = SessionService(store=store)
            _populate(service)
            projects = ProjectStore(store)
            self.assertEqual(len(projects.list('project')), 2)
            for project in projects.list('project'):
                events = projects.list('event', project['id'])
                self.assertEqual(len(events), 2 if project['id'] == 'ui-project-one' else 1)
                for event in events:
                    source = event['source_refs'][0]
                    self.assertIn(source['session_id'], project['session_ids'])
                    originals = service.list_sweeps(source['session_id'], stream_id=source['stream_id'])
                    original = next(row for row in originals if row.id == source['sweep_id'])
                    self.assertEqual(original.level_unit, 'dBm')
                    self.assertEqual(len(original.levels), 201)
                    self.assertEqual(original.frequencies_hz[110], 100_100_000)
                    self.assertEqual(original.levels[110], -51)
                    self.assertEqual(original.settings['measurement_qualification']['status'], 'synthetic')
                    context = service.sweep_context(source['session_id'], {
                        'sweep_id': source['sweep_id'], 'before': 0, 'after': 0})
                    self.assertEqual(context['anchor_id'], original.id)
                    self.assertEqual(context['items'][0]['settings'], original.settings)
                    self.assertEqual(context['items'][0]['levels'], list(original.levels))
                    for reading in event['analysis'].get('receiver_status', []):
                        self.assertEqual(reading['session_id'], source['session_id'])
                        self.assertTrue(reading['metadata']['simulated'])
                        self.assertEqual((reading['level'], reading['level_unit']), (23, 's_meter_count'))
                self.assertFalse(project['autostart'])
                self.assertEqual(project['state'], 'draft')


class NativeUIRunnerTests(unittest.TestCase):
    def test_owner_prepares_private_profiles_before_read_only_xctest_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            index = prepare_profiles(root, 'http://127.0.0.1:12345', 'a' * 64)
            profiles = json.loads(index.read_text())
            self.assertEqual(set(profiles), set(CASES))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(index.stat().st_mode), 0o600)
            runs, outputs = set(), set()
            for name, filename in profiles.items():
                path = Path(filename)
                self.assertTrue(path.is_absolute())
                self.assertTrue(path.is_relative_to(root))
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                profile = json.loads(path.read_text())
                output = Path(profile['output_dir'])
                self.assertTrue(output.is_dir())
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
                self.assertTrue(output.is_relative_to(root))
                self.assertEqual(profile['phase'], 'ui-test')
                self.assertEqual(profile['session_id'], 'ui-one')
                self.assertEqual(profile['core_url'], 'http://127.0.0.1:12345')
                self.assertEqual(profile['expected_ui_sha256'], 'a' * 64)
                self.assertNotIn('core_data_dir', profile)
                runs.add(profile['run_id']); outputs.add(str(output))
            self.assertEqual(len(runs), len(CASES))
            self.assertEqual(len(outputs), len(CASES))
            with self.assertRaises(FileExistsError):
                prepare_profiles(root, 'http://127.0.0.1:12345', 'a' * 64)
    def test_relocated_run_keeps_the_original_built_product_root(self):
        source = {'UI': {'TestBundlePath': '__TESTHOST__/PlugIns/DvoynoyObzorUITests.xctest',
                         'TestHostPath': '__TESTROOT__/Debug/Runner.app',
                         'UITargetAppPath': '__TESTROOT__/Debug/DvoynoyObzor.app'}}
        result = configure_test_run(source, '/private/profile.json', test_root='/build/Products')
        self.assertEqual(result['UI']['TestHostPath'], '/build/Products/Debug/Runner.app')
        self.assertEqual(result['UI']['UITargetAppPath'], '/build/Products/Debug/DvoynoyObzor.app')
        self.assertEqual(result['UI']['TestBundlePath'], '__TESTHOST__/PlugIns/DvoynoyObzorUITests.xctest')

    def test_installed_target_and_window_exports_are_scoped_to_the_training_case(self):
        source = {'UI': {'TestBundlePath': '__TESTHOST__/PlugIns/DvoynoyObzorUITests.xctest',
                         'UITargetAppPath': '__TESTROOT__/Debug/DvoynoyObzor.app'}}
        configured = configure_test_run(source, '/private/profiles', target_app='/Applications/Owned.app')
        self.assertEqual(configured['UI']['UITargetAppPath'], '/Applications/Owned.app')
        self.assertEqual(configured['UI']['EnvironmentVariables']['ORI_UI_TEST_APP'], '/Applications/Owned.app')
        import struct
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            attachments = path / 'attachments'
            attachments.mkdir()
            records = []
            for name in ('macos-monitoring', 'macos-event'):
                filename = name + '.png'
                (attachments / filename).write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + struct.pack('>II', 1200, 792))
                records.append(dict(exportedFileName=filename, suggestedHumanReadableName=filename,
                                    isAssociatedWithFailure=False))
            document = [dict(testIdentifier='WorkbenchUITests/testTrainingWindowCaptures()', attachments=records)]
            (attachments / 'manifest.json').write_text(json.dumps(document))
            report = collect_training_images(attachments, path)
            self.assertEqual(set(report), {'macos-monitoring.png', 'macos-event.png'})
            self.assertEqual(report['macos-event.png']['width'], 1200)
            records[0]['isAssociatedWithFailure'] = True
            (attachments / 'manifest.json').write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                collect_training_images(attachments, path)

    def test_profile_is_injected_only_into_the_single_ui_test_target(self):
        ui = {'TestBundlePath': '__TESTROOT__/DvoynoyObzorUITests.xctest',
              'EnvironmentVariables': {'UNCHANGED': 'yes'}}
        unit = {'TestBundlePath': '__TESTROOT__/OtherTests.xctest', 'EnvironmentVariables': {}}
        source = {'TestConfigurations': [{'TestTargets': [ui, unit]}]}
        result = configure_test_run(source, '/private/ui/profile.json')
        targets = result['TestConfigurations'][0]['TestTargets']
        self.assertEqual(targets[0]['EnvironmentVariables'], {
            'UNCHANGED': 'yes', 'ORI_UI_TEST_PROFILES': '/private/ui/profile.json'})
        self.assertEqual(targets[1], unit)
        self.assertNotIn('ORI_UI_TEST_PROFILES', ui['EnvironmentVariables'])

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
