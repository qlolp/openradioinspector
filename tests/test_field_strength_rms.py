"""Traceable narrowband RMS estimates: physical examples and refusal paths."""
import copy
import unittest

from radioinspector.contracts import Sweep, to_primitive
from radioinspector import measurements
from tests.test_sweep_comparison import measurement


def profile():
    return {
        'id': 'fixture-tract', 'version': 1, 'instrument_profile': 'fixture-rms-v1',
        'measurement_chain_id': 'SYNTHETIC-receiver-antenna-cable-preamp-v1',
        'reference_plane': 'receiver input connector', 'impedance_ohm': 50.,
        'detector': 'RMS', 'rbw_hz': 1000, 'waveform': 'CW',
        'valid_from': '2020-01-01T00:00:00Z', 'valid_until': '2030-01-01T00:00:00Z',
        'uncertainty_basis': 'fixture combined expanded uncertainty, k=2',
        'points': [
            {'frequency_hz': 100, 'antenna_factor_db_per_m': 20., 'cable_loss_db': 2.,
             'preamp_gain_db': 10., 'level_correction_db': 0., 'uncertainty_db': 2.},
            {'frequency_hz': 300, 'antenna_factor_db_per_m': 24., 'cable_loss_db': 4.,
             'preamp_gain_db': 10., 'level_correction_db': 0., 'uncertainty_db': 4.},
        ],
    }


def source(unit='dBm', level=-66.98970004336019):
    return Sweep.from_dict(measurement(frequencies_hz=[100, 200, 300],
        levels=[level, level, level], level_unit=unit,
        settings={'instrument_profile': 'fixture-rms-v1', 'rbw_hz': 1000,
                  'measurement_chain_id': 'SYNTHETIC-receiver-antenna-cable-preamp-v1',
                  'impedance_ohm': 50., 'reference_plane': 'receiver input connector',
                  'detector': 'RMS', 'waveform': 'CW'}))


class FieldStrengthTests(unittest.TestCase):
    def calculate(self, sweep=None, calibration=None, frequency_hz=200):
        return measurements.field_strength_rms_v2(
            sweep or source(), calibration or profile(), frequency_hz=frequency_hz)

    def test_physical_power_voltage_field_and_interpolated_tract(self):
        # -66.9897 dBm -> 100 uV RMS at 50 ohm. Interpolation gives
        # AF=22, cable=3, preamp=10: E=40+22+3-10=55 dBuV/m.
        result = self.calculate()
        self.assertAlmostEqual(result['value_dbuv_per_m'], 55., places=9)
        self.assertAlmostEqual(result['value'], .0005623413251903491, places=12)
        self.assertEqual(result['unit'], 'V/m')
        self.assertEqual(result['uncertainty_db'], 3.)
        self.assertEqual(result['source']['id'], 'sweep-0')
        self.assertEqual(result['source']['frequency_hz'], 200)
        self.assertEqual(result['calibration']['profile'], profile())
        self.assertEqual(len(result['calibration']['sha256']), 64)

    def test_voltage_and_already_calibrated_field_inputs(self):
        self.assertAlmostEqual(self.calculate(source('dBuV', 40))['value_dbuv_per_m'], 55)
        self.assertAlmostEqual(self.calculate(source('dBmV', -20))['value_dbuv_per_m'], 55)
        # A physical field source has already applied antenna/cable corrections.
        self.assertAlmostEqual(self.calculate(source('dBuV/m', 40))['value'], .0001)

    def test_unknown_uncertainty_is_not_zero_and_original_is_unchanged(self):
        p = profile(); p['points'][1]['uncertainty_db'] = None
        sweep = source(); before = copy.deepcopy(to_primitive(sweep))
        result = self.calculate(sweep, p)
        self.assertIsNone(result['uncertainty_db'])
        self.assertTrue(result['warnings'])
        self.assertEqual(to_primitive(sweep), before)

    def test_response_context_is_bounded_even_for_large_unrelated_archive_metadata(self):
        import json
        sweep = source(); sweep.settings['unrelated_import_note'] = 'x' * 2_000_000
        result = self.calculate(sweep)
        self.assertLess(len(json.dumps(result).encode('utf-8')), 65536)
        self.assertEqual(len(sweep.settings['unrelated_import_note']), 2_000_000)
        self.assertEqual(result['source']['settings']['measurement_chain_id'], profile()['measurement_chain_id'])

    def test_no_extrapolation_or_signal_interpolation(self):
        for frequency in (0, 150, 400):
            with self.subTest(frequency=frequency), self.assertRaises(ValueError):
                self.calculate(frequency_hz=frequency)

    def test_missing_calibration_or_incompatible_measurement_is_unavailable(self):
        for key, value in (('instrument_profile', 'other'), ('detector', 'MaxPeak'),
                           ('rbw_hz', 2000), ('waveform', 'unknown')):
            sweep = source(); sweep.settings[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.calculate(sweep)
        for unit in ('dBFS', 's_meter_count', 'dB'):
            with self.subTest(unit=unit), self.assertRaises(ValueError):
                self.calculate(source(unit, -20))
        p = profile(); p['valid_until'] = '2021-01-01T00:00:00Z'
        with self.assertRaises(ValueError): self.calculate(calibration=p)

    def test_measurement_chain_impedance_and_reference_plane_must_be_confirmed(self):
        for key, value in (('measurement_chain_id', 'different-antenna-cable'),
                           ('impedance_ohm', 75), ('reference_plane', 'post-amplifier output')):
            sweep = source(); sweep.settings[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.calculate(sweep)
        # Even no conflicting metadata cannot establish an applicable chain.
        for key in ('measurement_chain_id', 'impedance_ohm', 'reference_plane'):
            sweep = source(); del sweep.settings[key]
            with self.subTest(missing=key), self.assertRaises(ValueError): self.calculate(sweep)
        p = profile(); del p['reference_plane']
        with self.assertRaises(ValueError): self.calculate(calibration=p)

    def test_invalid_calibration_points_are_rejected(self):
        for field, value in (('frequency_hz', True), ('cable_loss_db', float('nan')),
                             ('preamp_gain_db', float('inf')), ('uncertainty_db', -1)):
            p = profile(); p['points'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.calculate(calibration=p)
        p = profile(); p['points'].reverse()
        with self.assertRaises(ValueError): self.calculate(calibration=p)

    def test_legacy_scalar_retains_value_and_unit_but_discloses_nonphysical_estimate(self):
        from radioinspector.trace import Trace
        result = measurements.field_strength(Trace([100, 200], [-80, -50]))
        self.assertEqual((result.value, result.unit), (-50., 'dB'))
        self.assertTrue(result.details['deprecated'])
        self.assertFalse(result.details['physical_field'])
        self.assertIn('некалиброван', ' '.join(result.warnings).lower())
