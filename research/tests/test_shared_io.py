"""Actual worker IPC uses the same fixed POSIX ledger as native callers."""
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest

import test_worker as legacy


def ledger(root):
    root.mkdir(mode=0o700)
    path=root/'budget'
    nonce=bytes(range(16))
    now=time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    raw=bytearray(128)
    raw[:8]=b'ORIIOV1\0';raw[8:24]=nonce
    struct.pack_into('<5Q',raw,24,20*1024*1024,now,0,now,1)
    path.write_bytes(raw);path.chmod(0o600)
    return dict(version=1,path=str(path),nonce=nonce.hex(),clock='posix-monotonic')


class SharedWorkerIoTests(unittest.TestCase):
    run_worker=legacy.WorkerTests.run_worker
    success=legacy.WorkerTests.success
    failure=legacy.WorkerTests.failure

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.snapshot=legacy.make_snapshot(self.root/'source')
        self.cache=self.root/'cache'
        self.capability=(ledger(self.root/'private-io') if os.name=='posix' else dict(
            version=1,path=str(self.root/'unavailable-budget'),nonce=bytes(range(16)).hex(),clock='posix-monotonic'))

    def request(self):
        return dict(version=1,request_id='shared-io-fixture',operation='export',
                    snapshot_path=str(self.snapshot),cache_directory=str(self.cache),
                    filters={},shared_io=self.capability)

    def test_actual_worker_accounts_or_explicitly_rejects_unavailable_platform(self):
        if os.name!='posix':
            self.failure(self.request(),code='shared_io_unavailable')
            self.assertFalse(self.cache.exists())
            return
        response=self.success(self.request())
        metrics=response['metrics']
        total=sum(metrics[name] for name in ('source_bytes_read','cache_bytes_read','cache_bytes_written'))
        raw=Path(self.capability['path']).read_bytes()
        self.assertEqual(struct.unpack_from('<Q',raw,40)[0],total)
        self.assertGreater(total,0)
        self.assertTrue(metrics['cross_process_logical_io'])
        encoded=json.dumps(response)
        self.assertNotIn(self.capability['nonce'],encoded)
        self.assertNotIn(self.capability['path'],encoded)

    def test_wrong_nonce_or_unsafe_ledger_fails_before_cache_creation(self):
        self.capability['nonce']='f'*32
        self.failure(self.request(),code='shared_io_unavailable')
        self.assertFalse(self.cache.exists())
        if os.name!='posix':
            return
        self.capability['nonce']=bytes(range(16)).hex()
        Path(self.capability['path']).chmod(0o644)
        self.failure(self.request(),code='shared_io_unavailable')
        self.assertFalse(self.cache.exists())

    def test_unknown_capability_fields_are_rejected_and_never_echoed(self):
        self.capability['unexpected']='private'
        response=self.failure(self.request(),code='invalid_request')
        self.assertNotIn('private',json.dumps(response))


if __name__=='__main__':
    unittest.main()
