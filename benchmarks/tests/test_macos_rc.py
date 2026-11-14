"""RC-only load accounting; never an assertion of native/hardware acceptance."""
from pathlib import Path
import unittest
from benchmarks.ori_bench.config import Config
from benchmarks.ori_bench.source import scheduled


class MacOSRCWorkloadTests(unittest.TestCase):
    def test_rc_profile_retains_every_offered_measurement_and_excludes_pcm(self):
        config = Config('http://127.0.0.1:1', Path('/unused'), workload_profile='macos-rc1')
        self.assertEqual(config.planned_counts(), {'sweep': 18000, 'observation': 18000, 'audio': 0})
        self.assertEqual(config.planned_audio_frames, 0)
        self.assertEqual((config.points, config.duration_seconds, config.speed), (4096, 1800, 1))
        self.assertEqual((config.request_timeout, config.bulk_timeout, config.drain_seconds), (10, 600, 60))
        self.assertEqual(sum(1 for _ in scheduled(config)), 36000)
        self.assertEqual(config.public()['audio_scope'], 'excluded_from_rc1')

    def test_no_audio_peer_accepts_rc_measurements_without_audio_routes(self):
        import json, subprocess, sys, tempfile
        from benchmarks.tests.test_benchmarks import http_fixture, REPO
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with http_fixture(root/'fixture.sqlite', audio=False) as (peer, url):
                result = subprocess.run([sys.executable, '-m', 'benchmarks.ori_bench', 'run',
                    '--base-url', url, '--output-dir', str(root/'results'), '--run-id', 'rc-profile',
                    '--workload-profile', 'macos-rc1', '--duration-seconds', '0.4', '--points', '4',
                    '--sweep-rate', '5', '--observation-rate', '5', '--speed', '1', '--history-repeats', '1'], cwd=REPO, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads((root/'results/report.json').read_text())
                self.assertGreaterEqual(report['production']['production_elapsed_seconds'], .4)
                self.assertEqual(report['checks']['complete_delivery']['status'], 'passed')
                self.assertEqual(report['counts']['audio']['offered'], 0)
                self.assertFalse(any('audio' in str(path) for path in peer.posts))

    def test_rc_fixture_profile_has_no_scientific_worker_or_silent_fallback(self):
        import json, sys, tempfile
        from benchmarks.ori_bench.concurrent import BackgroundCandidate
        from benchmarks.ori_bench.config import BenchmarkError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = Path(sys.executable).resolve()
            for options in [dict(), dict(workload_profile='macos-rc1', python=binary)]:
                values = dict(python=None); values.update(options)
                with self.assertRaises(BenchmarkError): BackgroundCandidate(binary, root, **values)
            node = BackgroundCandidate(binary, root, None, workload_profile='macos-rc1')
            phase = root/'node'; phase.mkdir()
            node.ready, node.stop, node.nonce = phase/'ready', phase/'stop', 'owned-fixture'
            command = node._adapter_command(root/'data')
            value = json.loads((phase/'fixture.json').read_text())
            self.assertEqual(value['workload_profile'], 'macos-rc1')
            self.assertIsNone(value['analysis_python'])
            self.assertIsNone(value['research_cgroup'])
            self.assertFalse(value['research_best_effort'])
            self.assertEqual(command[-2:], ['--fixture-profile', str(phase/'fixture.json')])

    def test_original_full_reference_is_unchanged(self):
        config = Config('http://127.0.0.1:1', Path('/unused'))
        self.assertEqual(config.planned_counts(), {'sweep': 18000, 'observation': 18000, 'audio': 180})
        self.assertEqual(config.planned_audio_frames, 86400000)


if __name__ == '__main__': unittest.main()
