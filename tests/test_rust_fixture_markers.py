"""Readers must never observe a partially published worker PID announcement."""
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


class WorkerMarkerPublicationTests(unittest.TestCase):
    def test_both_worker_announcements_remain_absent_until_payload_is_complete(self):
        root=Path(__file__).resolve().parents[1]
        request={"request_id":"marker-fixture", "method":"fixture", "session":{},
                 "sweeps":[], "candidates":[], "payload":{}}
        for name in ("legacy_echo.py", "analysis_echo.py"):
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as directory:
                marker=Path(directory)/"owned.pid"
                entered=threading.Event(); release=threading.Event(); errors=[]
                original=Path.write_text

                def paused_write(path,data,*args,**kwargs):
                    if path.parent != marker.parent:
                        return original(path,data,*args,**kwargs)
                    # Hold the actual first file write, after creation. A
                    # reader may run here under real scheduler contention.
                    with path.open("w",encoding=kwargs.get("encoding")) as stream:
                        entered.set()
                        if not release.wait(5):raise TimeoutError("fixture writer was not released")
                        return stream.write(data)

                def execute():
                    try:runpy.run_path(str(root/"crates/ori-core/tests"/name),run_name="__main__")
                    except SystemExit as error:
                        if error.code not in (None,0):errors.append(error)
                    except BaseException as error:errors.append(error)

                output=io.StringIO()
                with patch.object(sys,"argv",[name,"ok",str(marker)]), \
                     patch.object(sys,"stdin",io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))), \
                     patch.object(sys,"stdout",output), patch.object(Path,"write_text",paused_write):
                    worker=threading.Thread(target=execute)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(5),"fixture did not reach its marker write")
                        self.assertFalse(marker.exists(),"reader observed an incomplete PID announcement")
                    finally:
                        release.set();worker.join(5)
                    self.assertFalse(worker.is_alive(),"fixture writer did not join")
                self.assertEqual(errors,[])
                self.assertEqual(marker.read_text(),str(os.getpid()))
                self.assertEqual(json.loads(output.getvalue())["request_id"],request["request_id"])
