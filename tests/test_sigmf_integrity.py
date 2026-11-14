"""SigMF declarations describe actual bytes; spectrum synthesis is not RF IQ."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

import numpy as np

from radioinspector.export.sigmf import SigmfAnnotation, SigmfRecording, SigmfPublicationError, export_sigmf
from radioinspector.trace import Trace


class SigMFIntegrityTests(unittest.TestCase):
    def test_ci16_is_interleaved_signed_little_endian_counts_without_scaling(self):
        expected = struct.pack('<hhhhhh', -32768, 32767, 0, -1, 123, -456)
        samples = np.array([-32768+32767j, -1j, 123-456j])
        with tempfile.TemporaryDirectory() as directory:
            data, metadata = SigmfRecording(samples, 48000, 433920000,
                datatype='ci16_le').write(Path(directory)/'counts')
            self.assertEqual(data.read_bytes(), expected)
            stored = json.loads(metadata.read_text())
            self.assertEqual(stored['global']['core:datatype'], 'ci16_le')
            self.assertEqual(stored['global']['core:sha512'], hashlib.sha512(expected).hexdigest())
        np.testing.assert_array_equal(samples, [-32768+32767j, -1j, 123-456j])

    def test_cf32_is_little_endian_even_for_big_endian_source_and_preserves_signed_zero(self):
        samples = np.array([complex(.5,-.25),complex(-0.,-0.)], dtype='>c8')
        expected = struct.pack('<ffff',.5,-.25,-0.,-0.)
        with tempfile.TemporaryDirectory() as directory:
            data, _ = SigmfRecording(samples,48000,100000000).write(Path(directory)/'floats')
            self.assertEqual(data.read_bytes(), expected)

    def test_invalid_formats_values_and_metadata_leave_no_output(self):
        from datetime import datetime
        cases = [
            dict(datatype='unknown'), dict(datatype='ci16_le',samples=np.array([1.5+0j])),
            dict(datatype='ci16_le',samples=np.array([32768+0j])),
            dict(samples=np.array([complex(float('nan'),0)])),
            dict(samples=np.array([1e100+0j])), dict(samples=np.zeros((2,2),complex)),
            dict(samples=np.array([],complex)), dict(sample_rate_hz=0),
            dict(sample_rate_hz=float('inf')), dict(center_frequency_hz=-1),
            dict(captured_at=datetime(2026,11,14)), dict(location=(91,0)),
            dict(annotations=[SigmfAnnotation(1,sample_count=2)]),
        ]
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                arguments = dict(samples=np.array([1+0j]),sample_rate_hz=48000,center_frequency_hz=100000000)
                arguments.update(changes)
                with self.assertRaises(ValueError):
                    SigmfRecording(**arguments).write(Path(directory)/'recording')
                self.assertEqual(list(Path(directory).iterdir()),[])

    def test_spectrum_export_explicitly_labels_synthetic_zero_phase_and_source_unit(self):
        trace = Trace(np.array([100000000,100010000]),np.array([-60.,-30.]),'dBm')
        original = trace.levels.copy()
        with tempfile.TemporaryDirectory() as directory:
            _, metadata = export_sigmf(trace,Path(directory)/'spectrum')
            value = json.loads(metadata.read_text())['global']
            self.assertEqual(value['ori:sample_origin'],'synthetic_spectrum_zero_phase')
            self.assertTrue(value['ori:synthetic'])
            self.assertEqual(value['ori:source_level_unit'],'dBm')
            self.assertFalse(value['ori:physical_capture_verified'])
            self.assertIn({'name':'ori','version':'1.0.0','optional':True},value['core:extensions'])
        np.testing.assert_array_equal(trace.levels,original)

    def test_large_recording_hash_and_serialization_use_bounded_working_memory(self):
        samples = np.full(4_000_000,complex(.25,-.75),dtype=np.complex64)
        with tempfile.TemporaryDirectory() as directory:
            tracemalloc.start()
            try:
                data, metadata = SigmfRecording(samples,48000,100000000).write(Path(directory)/'bounded')
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(data.stat().st_size,32_000_000)
            digest = hashlib.sha512()
            with data.open('rb') as stream:
                for block in iter(lambda:stream.read(65536),b''): digest.update(block)
            self.assertEqual(json.loads(metadata.read_text())['global']['core:sha512'],digest.hexdigest())
            self.assertLess(peak,8*1024*1024,'file size must not become temporary Python memory')

    def test_metadata_size_is_bounded_before_any_files_are_created(self):
        with tempfile.TemporaryDirectory() as directory:
            record = SigmfRecording(np.zeros(2000,dtype=np.complex64),48000,100000000,
                annotations=[SigmfAnnotation(i,label='Ж'*4000) for i in range(2000)])
            with self.assertRaises(ValueError): record.write(Path(directory)/'oversized')
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_ci16_float16_boundaries_never_wrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for value in [32768,40000]:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    SigmfRecording(np.array([value],dtype=np.float16),48000,100000000,
                        datatype='ci16_le').write(root/'invalid')
            data,_=SigmfRecording(np.array([-32768,32752],dtype=np.float16),48000,100000000,
                datatype='ci16_le').write(root/'valid')
            self.assertEqual(data.read_bytes(),struct.pack('<hhhh',-32768,0,32752,0))

    def test_capture_datetime_is_utc_z_in_capture_scope_only(self):
        from datetime import datetime,timezone,timedelta
        value=SigmfRecording(np.array([1+0j]),48000,100000000,
            captured_at=datetime(2026,11,14,3,4,5,123456,tzinfo=timezone(timedelta(hours=3)))).metadata()
        self.assertNotIn('core:datetime',value['global'])
        self.assertEqual(value['captures'][0]['core:datetime'],'2026-11-14T00:04:05.123456Z')

    def test_complex_and_numpy_boolean_metadata_are_rejected_without_discarding_q(self):
        cases=[{'sample_rate_hz':np.complex128(48000+5j)},
            {'sample_rate_hz':np.bool_(True)}, {'center_frequency_hz':np.bool_(True)},
            {'center_frequency_hz':np.complex128(complex(1,float('nan')))},
            {'center_frequency_hz':np.complex128(complex(1,float('inf')))},
            {'location':(np.complex128(1+5j),0)},
            {'annotations':[SigmfAnnotation(0,freq_lower_edge_hz=np.complex64(1+5j))]}]
        for changes in cases:
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as directory:
                arguments=dict(samples=np.array([1+0j]),sample_rate_hz=48000,center_frequency_hz=100000000)
                arguments.update(changes)
                with self.assertRaises(ValueError): SigmfRecording(**arguments).write(Path(directory)/'invalid')
                self.assertEqual(list(Path(directory).iterdir()),[])

    def test_nonfinite_spectrum_input_cannot_become_valid_synthetic_zero(self):
        for frequencies,levels in [([1,2],[-np.inf,-40]),([1,2],[np.nan,-40]),
                ([1,np.inf],[-40,-40]),([1,np.nan],[-40,-40])]:
            with self.subTest(frequencies=frequencies,levels=levels),tempfile.TemporaryDirectory() as directory:
                trace=Trace(np.array(frequencies),np.array(levels))
                with self.assertRaises(ValueError): export_sigmf(trace,Path(directory)/'invalid')
                self.assertEqual(list(Path(directory).iterdir()),[])

    def test_caught_replacement_failure_restores_existing_valid_pair(self):
        import os
        for failed_step in [1,2]:
            with self.subTest(step=failed_step),tempfile.TemporaryDirectory() as directory:
                base=Path(directory)/'recording'
                data,meta=SigmfRecording(np.array([1+2j]),48000,100000000).write(base)
                original=(data.read_bytes(),meta.read_bytes())
                replace=os.replace
                calls=0
                def fail_once(source,destination):
                    nonlocal calls
                    calls+=1
                    if calls==failed_step: raise PermissionError('synthetic publication failure')
                    return replace(source,destination)
                with patch('radioinspector.export.sigmf.os.replace',side_effect=fail_once):
                    with self.assertRaises(OSError):
                        SigmfRecording(np.array([3+4j]),48000,200000000).write(base)
                self.assertEqual((data.read_bytes(),meta.read_bytes()),original)
                self.assertEqual(sorted(p.name for p in Path(directory).iterdir()),sorted([data.name,meta.name]))

    def test_metadata_uses_same_lf_bytes_on_windows_and_posix(self):
        from tempfile import NamedTemporaryFile
        def windows_text(*args,**kwargs):
            if kwargs.get('mode')=='w': kwargs.setdefault('newline','\r\n')
            return NamedTemporaryFile(*args,**kwargs)
        with tempfile.TemporaryDirectory() as directory:
            with patch('radioinspector.export.sigmf.tempfile.NamedTemporaryFile',side_effect=windows_text):
                _,metadata=SigmfRecording(np.array([1+0j]),48000,100000000).write(Path(directory)/'recording')
            raw=metadata.read_bytes()
            self.assertNotIn(b'\r\n',raw)
            value=json.loads(raw)
            self.assertEqual(len(raw),len(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')))

    def test_failed_first_publication_leaves_no_half_recording(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            replace=os.replace
            def failure(source,destination):
                if str(destination).endswith('.sigmf-meta'): raise PermissionError('synthetic metadata refusal')
                return replace(source,destination)
            with patch('radioinspector.export.sigmf.os.replace',side_effect=failure):
                with self.assertRaises(OSError):
                    SigmfRecording(np.array([3+4j]),48000,200000000).write(Path(directory)/'new')
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_failed_rollback_keeps_the_previous_pair_for_explicit_recovery(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)/'recording'
            data,meta=SigmfRecording(np.array([1+2j]),48000,100000000).write(base)
            originals={data.name:data.read_bytes(),meta.name:meta.read_bytes()}
            replace=os.replace
            calls=0
            def failure(source,destination):
                nonlocal calls
                calls+=1
                if calls>1: raise PermissionError('synthetic metadata and rollback failure')
                return replace(source,destination)
            with patch('radioinspector.export.sigmf.os.replace',side_effect=failure):
                with self.assertRaises(SigmfPublicationError) as caught:
                    SigmfRecording(np.array([3+4j]),48000,200000000).write(base)
            recovery=caught.exception.recovery_directory
            self.assertTrue(recovery.is_relative_to(Path(directory)))
            self.assertEqual({p.name:p.read_bytes() for p in recovery.iterdir()},originals)

    def test_real_numpy_scalar_coordinates_are_serialized_as_json_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            _,meta=SigmfRecording(np.array([1+0j]),48000,100000000,
                location=(np.float32(55.5),np.float32(37.5))).write(Path(directory)/'valid')
            self.assertEqual(json.loads(meta.read_text())['global']['core:geolocation']['coordinates'],[37.5,55.5])

    def test_masked_missing_samples_are_not_silently_exported_as_received_values(self):
        samples=np.ma.array([complex(float('nan'),0),1+1j],mask=[True,False])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                SigmfRecording(samples,48000,100000000).write(Path(directory)/'missing')
            self.assertEqual(list(Path(directory).iterdir()),[])


if __name__ == '__main__': unittest.main()
