"""Independent RMS reference values, rather than self-consistent round trips."""
import math
import unittest

from radioinspector.units import UnitError, convert, dbmv_to_v, v_to_dbmv


class UnitPhysicsTests(unittest.TestCase):
    def test_dbmv_reference_is_one_millivolt(self):
        for db, volts in ((0, .001), (20, .01), (-20, .0001), (60, 1)):
            with self.subTest(db=db):
                self.assertAlmostEqual(dbmv_to_v(db), volts, places=12)
                self.assertAlmostEqual(v_to_dbmv(volts), db, places=10)

    def test_complete_matrix_at_one_milliwatt_into_fifty_ohms(self):
        # P=1 mW; U_rms=sqrt(.001*50)=.22360679774997896 V.
        # E=U for an explicitly selected antenna factor of 0 dB/m.
        reference = {
            'dBW': -30., 'dBm': 0., 'W': .001, 'mW': 1., 'uW': 1000.,
            'dBmV': 46.98970004336019, 'dBuV': 106.98970004336019,
            'V': .22360679774997896, 'mV': 223.60679774997897,
            'uV': 223606.79774997896, 'dBuV/m': 106.98970004336019,
            'dBV/m': -13.010299956639813, 'V/m': .22360679774997896,
            'mV/m': 223.60679774997897, 'uV/m': 223606.79774997896,
        }
        for source, value in reference.items():
            for target, expected in reference.items():
                with self.subTest(source=source, target=target):
                    actual = convert(value, source, target, impedance=50)
                    self.assertTrue(math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-10),
                                    (actual, expected))

    def test_antenna_factor_is_field_over_terminal_voltage(self):
        for source, value, target, expected in (
            ('dBuV', 40., 'dBuV/m', 60.), ('dBuV/m', 60., 'dBuV', 40.),
            ('V', .0001, 'V/m', .001), ('V/m', .001, 'V', .0001),
            ('dBm', -66.98970004336019, 'dBuV/m', 60.),
            ('dBuV/m', 60., 'dBm', -66.98970004336019),
        ):
            with self.subTest(source=source, target=target):
                self.assertAlmostEqual(convert(value, source, target, antenna_factor_db=20.), expected, places=9)

    def test_non_fifty_ohm_power_voltage(self):
        self.assertAlmostEqual(convert(1., 'V', 'dBm', impedance=75), 11.249387366082999)
        self.assertAlmostEqual(convert(0., 'dBm', 'V', impedance=75), .27386127875258304)

    def test_zero_linear_units_remain_convertible(self):
        for source, target in (('W', 'mW'), ('V', 'mV'), ('V/m', 'uV/m')):
            self.assertEqual(convert(0, source, target), 0)

    def test_invalid_inputs_cannot_be_returned_as_physical_values(self):
        for value, source, target, kwargs in (
            (float('nan'), 'dBm', 'dBm', {}), (float('inf'), 'V', 'mV', {}),
            (-1., 'V', 'W', {}), (1., 'counts', 'counts', {}),
            (1., 'V', 'W', {'impedance': float('nan')}),
            (1., 'V', 'V/m', {'antenna_factor_db': float('inf')}),
            (1e308, 'W', 'mW', {}), (1e308, 'V', 'mV', {}),
        ):
            with self.subTest(value=value, source=source, target=target, kwargs=kwargs):
                with self.assertRaises(UnitError):
                    convert(value, source, target, **kwargs)
