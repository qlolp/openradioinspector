"""Actual fixture producer publication with a barrier before its first write."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

from tools.modernization import api_parity


class ReadyPublicationTests(unittest.TestCase):
    def test_complete_lf_sentinel_has_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready=Path(temporary)/"ready"
            identity=api_parity._publish_fixture_ready(ready,"http://127.0.0.1:12345")
            self.assertEqual(ready.read_bytes(),b"http://127.0.0.1:12345\n")
            self.assertEqual(list(Path(temporary).iterdir()),[ready])
            api_parity._remove_fixture_ready(ready,identity)
            self.assertEqual(list(Path(temporary).iterdir()),[])

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready=Path(temporary)/"ready"
            ready.write_bytes(b"previous original bytes\x00\r\n")
            with self.assertRaises(FileExistsError):
                api_parity._publish_fixture_ready(ready,"http://127.0.0.1:12345")
            self.assertEqual(ready.read_bytes(),b"previous original bytes\x00\r\n")
            self.assertEqual(list(Path(temporary).iterdir()),[ready])

    def test_write_and_flush_errors_close_the_file_and_remove_only_its_temporary(self):
        original_fdopen=os.fdopen
        for operation in ("write","flush"):
            outputs=[]

            class FailingOutput:
                def __init__(self,descriptor,*args,**kwargs):
                    self.output=original_fdopen(descriptor,*args,**kwargs)
                    outputs.append(self.output)

                def __enter__(self):return self
                def __exit__(self,*error):return self.output.__exit__(*error)
                def fileno(self):return self.output.fileno()
                def write(self,value):
                    if operation=="write":
                        self.output.write(value[:1])
                        raise OSError("controlled partial write")
                    return self.output.write(value)
                def flush(self):
                    if operation=="flush":raise OSError("controlled flush error")
                    return self.output.flush()

            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                ready=Path(temporary)/"ready"
                with patch.object(api_parity.os,"fdopen",FailingOutput), self.assertRaises(OSError):
                    api_parity._publish_fixture_ready(ready,"http://127.0.0.1:12345")
                self.assertEqual(len(outputs),1)
                self.assertTrue(outputs[0].closed)
                self.assertEqual(list(Path(temporary).iterdir()),[])

    def test_link_failure_leaves_no_partial_sentinel(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready=Path(temporary)/"ready"
            with patch.object(api_parity.os,"link",side_effect=OSError("controlled publication error")):
                with self.assertRaisesRegex(OSError,"controlled publication error"):
                    api_parity._publish_fixture_ready(ready,"http://127.0.0.1:12345")
            self.assertEqual(list(Path(temporary).iterdir()),[])

    def test_replaced_final_path_is_retained_as_unowned(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready=Path(temporary)/"ready"
            identity=api_parity._publish_fixture_ready(ready,"http://127.0.0.1:12345")
            ready.rename(Path(temporary)/"retained-original")
            ready.write_bytes(b"unknown replacement")
            with self.assertRaisesRegex(api_parity.ProtocolError,"ownership changed"):
                api_parity._remove_fixture_ready(ready,identity)
            self.assertEqual(ready.read_bytes(),b"unknown replacement")

    def test_replaced_temporary_is_retained_and_original_error_stays_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready=Path(temporary)/"ready"
            replacements=[]

            def replace(source,_destination):
                source=Path(source)
                source.rename(Path(temporary)/"retained-original")
                source.write_bytes(b"unknown replacement")
                replacements.append(source)
                raise OSError("original controlled publication error")

            with patch.object(api_parity.os,"link",replace):
                with self.assertRaisesRegex(api_parity.ProtocolError,"ownership changed") as failure:
                    api_parity._publish_fixture_ready(ready,"http://127.0.0.1:12345")
            self.assertIsInstance(failure.exception.__context__,OSError)
            self.assertEqual(str(failure.exception.__context__),"original controlled publication error")
            self.assertFalse(ready.exists())
            self.assertEqual(replacements[0].read_bytes(),b"unknown replacement")

    def test_actual_fixture_never_exposes_an_unwritten_ready_file(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="ori-ready-publication-") as temporary, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(15)
            port = listener.getsockname()[1]

            class BarrierNode(api_parity.OwnedNode):
                def _command(self, data):
                    program = textwrap.dedent(f"""
                        import inspect,json,os,socket,sys
                        from pathlib import Path
                        from types import SimpleNamespace
                        from tools.modernization.api_parity import fixture_node
                        ready=Path({str(self.ready)!r})
                        original_open=Path.open
                        original_fdopen=os.fdopen
                        paused=False
                        def barrier(output):
                            global paused
                            if paused:return output
                            frame=inspect.currentframe().f_back
                            while frame is not None and frame.f_code.co_name!='fixture_node':frame=frame.f_back
                            if frame is not None and 'core' in frame.f_locals:
                                paused=True
                                with socket.create_connection(('127.0.0.1',{port}),timeout=10) as control:
                                    control.settimeout(15)
                                    control.sendall(json.dumps(dict(url=frame.f_locals['core'].url)).encode()+bytes([10]))
                                    assert control.recv(1)==b'g'
                            return output
                        def opened(path,*args,**kwargs):
                            output=original_open(path,*args,**kwargs)
                            return barrier(output) if path==ready and args and args[0]=='x' else output
                        def fdopened(*args,**kwargs):return barrier(original_fdopen(*args,**kwargs))
                        Path.open=opened
                        os.fdopen=fdopened
                        raise SystemExit(fixture_node(SimpleNamespace(source_root=Path({str(root)!r}),data_dir=Path({str(data)!r}),ready_file=ready)))
                    """)
                    return [sys.executable, "-u", "-c", program]

            node = BarrierNode(api_parity.NodeSpec("core", Path(sys.executable), root), Path(temporary) / "node")
            errors = []
            strict_reads = []
            original_read = Path.read_text

            def read(path, *args, **kwargs):
                value = original_read(path, *args, **kwargs)
                if path == node.ready:
                    strict_reads.append(value)
                return value

            def start():
                try:
                    node.__enter__()
                except BaseException as error:
                    errors.append(error)

            connection = None
            with patch.object(Path, "read_text", read):
                thread = threading.Thread(target=start)
                thread.start()
                try:
                    connection, _ = listener.accept()
                    connection.settimeout(15)
                    with connection.makefile("rb") as stream:
                        actual = json.loads(stream.readline())
                    exposed = node.ready.exists()
                    raw_at_barrier = node.ready.read_bytes().hex() if exposed else None
                    connection.sendall(b"g")
                    thread.join(20)
                    self.assertFalse(thread.is_alive(), "original owned reader did not finish")
                    evidence = dict(exposed_before_write=exposed, raw_ready_hex_at_barrier=raw_at_barrier,
                                    strict_reader_values=strict_reads, actual_bound_url=actual["url"],
                                    errors=[str(error) for error in errors])
                    print(json.dumps(evidence), flush=True)
                    self.assertFalse(exposed, evidence)
                    self.assertEqual(errors, [], evidence)
                    self.assertEqual(node.url, actual["url"])
                    self.assertEqual(node.request("GET", "/api/v1/health").status, 200)
                finally:
                    if connection is not None:
                        try:
                            connection.sendall(b"g")
                        except OSError:
                            pass
                        connection.close()
                    thread.join(20)
                    if not thread.is_alive():
                        node.close()
            self.assertTrue(node.joined)
            self.assertEqual(node.process.returncode, 0)
            self.assertFalse(node.ready.exists())


if __name__ == "__main__":
    unittest.main()
