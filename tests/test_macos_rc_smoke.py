"""The packaged smoke must verify UTC instants and complete source fidelity."""
import copy
import json
from pathlib import Path
import unittest
from tools.release.smoke_macos_personal_rc import verify_original


class MacOSRCSmokeFidelityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).resolve().parents[1]/'contracts/fixtures/measurement-v1.json').read_text())

    def canonical(self, key):
        source = copy.deepcopy(self.fixture[key])
        source['interval'] = dict(started_at='2026-11-11T12:00:00.123456Z', ended_at='2026-11-11T12:00:00.223456Z')
        return source

    def test_offset_fixture_restored_in_utc_is_the_same_instant(self):
        verify_original(self.canonical('sweep'), self.fixture['sweep'], 'sweep')
        verify_original(self.canonical('observation'), self.fixture['observation'], 'observation')

    def test_one_nanosecond_or_duration_change_is_rejected(self):
        for field in ['started_at', 'ended_at']:
            source = self.canonical('observation')
            source['interval'][field] = source['interval'][field].replace('456Z', '456001Z')
            with self.assertRaises(RuntimeError): verify_original(source, self.fixture['observation'], 'observation')

    def test_output_must_be_utc_and_retains_fractional_precision(self):
        source = self.canonical('observation')
        source['interval']['started_at'] = '2026-11-11T12:00:00.123456000Z'
        verify_original(source, self.fixture['observation'], 'observation')
        source['interval']['started_at'] = '2026-11-11T15:00:00.123456+03:00'
        with self.assertRaises(RuntimeError): verify_original(source, self.fixture['observation'], 'observation')

    def test_negative_zero_is_not_equal_to_positive_zero(self):
        source = self.canonical('sweep'); source['levels'][1] = 0.0
        with self.assertRaises(RuntimeError): verify_original(source, self.fixture['sweep'], 'sweep')
        expected = copy.deepcopy(self.fixture['observation']); expected['level'] = -0.0
        source = copy.deepcopy(expected); source['level'] = 0.0
        with self.assertRaises(RuntimeError): verify_original(source, expected, 'observation')

    def test_receiver_mode_metadata_and_source_identity_are_required(self):
        for field, changed in [('mode', 'AM'), ('metadata', {}), ('stream_id', 'another receiver')]:
            source = self.canonical('observation'); source[field] = changed
            with self.assertRaises(RuntimeError): verify_original(source, self.fixture['observation'], 'observation')


if __name__ == '__main__': unittest.main()
