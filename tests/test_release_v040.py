"""Regression gates for the expanded v0.4.0 release."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from radioinspector.panorama import PanoramaStore
from radioinspector.trace import Trace
from radioinspector.threshold import ThresholdLine, power_over_threshold
from radioinspector.units import UnitError
from radioinspector.web.server import ApplicationState


class AbsolutePowerTests(unittest.TestCase):
    def test_relative_units_cannot_be_reported_as_watts(self):
        for unit in ['dBFS', 's_meter_count', 'dB', 'dBuV']:
            with self.subTest(unit=unit):
                with self.assertRaises(UnitError):
                    power_over_threshold(Trace([1,2,3],[-10]*3,unit),
                                         ThresholdLine([1,3],[-20]*2,unit=unit),(1,3))

    def test_mixed_threshold_scale_is_rejected(self):
        with self.assertRaises(UnitError):
            power_over_threshold(Trace([1,2,3],[-10]*3,'dBm'),
                                 ThresholdLine([1,3],[-20]*2,unit='dBW'),(1,3))

    def test_dbw_and_dbm_report_the_same_physical_power(self):
        for unit,offset in [('dBm',0),('dBW',-30)]:
            result=power_over_threshold(Trace([1,2,3],[-10+offset]*3,unit),
                                        ThresholdLine([1,3],[-20+offset]*2,unit=unit),(1,3))
            self.assertAlmostEqual(result,0.00027)


class LegacyDatabaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.a=self.root/'first'/'x.pan';self.b=self.root/'second'/'x.pan'
        for path in [self.a,self.b]:
            with PanoramaStore(path) as store:
                store.append(Trace([1,2,3],[-90,-20,-90]))
        self.state=ApplicationState(str(self.a))

    def tearDown(self):
        self.state.store.close();self.temp.cleanup()

    def test_failed_load_preserves_every_active_field_and_closes_candidate(self):
        (self.b.parent/'signals.json').write_text('{',encoding='utf-8')
        old=(self.state.store,self.state.database,self.state.signals,self.state.threshold,self.state.version)
        closed=[];real_close=PanoramaStore.close
        def close(store):
            closed.append(store.path);return real_close(store)
        with patch.object(PanoramaStore,'close',close):
            with self.assertRaises(ValueError):self.state.load_database(str(self.b))
        self.assertEqual((self.state.store,self.state.database,self.state.signals,
                          self.state.threshold,self.state.version),old)
        self.assertEqual(closed,[self.b])
        self.assertEqual(self.state.snapshot()['panoramas'],1)

    def test_successful_swap_closes_previous_store_and_notifies_once(self):
        old=self.state.store;version=self.state.version;event=self.state.subscribe()
        closed=[];real_close=PanoramaStore.close
        def close(store):closed.append(store);return real_close(store)
        with patch.object(PanoramaStore,'close',close):self.state.load_database(str(self.b))
        self.assertEqual(closed,[old]);self.assertEqual(self.state.version,version+1)
        self.assertTrue(event.is_set());self.assertEqual(self.state.database,str(self.b))
        self.assertEqual(self.state.store.count,1)

    def test_nonexistent_database_cannot_create_files(self):
        missing=self.root/'absent'/'x.pan'
        with self.assertRaises(ValueError):self.state.load_database(str(missing))
        self.assertFalse(missing.parent.exists())
