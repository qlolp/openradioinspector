"""The real adapter's bounded prefix must exist before forced cleanup is allowed."""
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

from tools.modernization.api_parity import probe_cli


class OwnedOutputPrefixTests(unittest.TestCase):
    def test_full_bounded_prefix_is_visible_when_overflow_is_published(self):
        with tempfile.TemporaryDirectory(prefix="ori-output-prefix-") as temporary:
            program = textwrap.dedent(f"""
                import json,sys
                from pathlib import Path
                from tools.modernization import owned_process as owned
                directory=Path({temporary!r})
                nonce='a'*32
                child="import sys;sys.stdout.buffer.write(b'o'*150000);sys.stdout.buffer.flush();sys.stderr.buffer.write(b'e'*150000);sys.stderr.buffer.flush();sys.stdin.buffer.read()"
                spec=dict(nonce=nonce,argv=[sys.executable,'-u','-c',child],cwd=None,stdin_pipe=True,merge=True,limit=262144)
                (directory/'command.json').write_text(json.dumps(spec))
                original_open=Path.open
                original_publish=owned._publish
                observed={{}}
                def buffered(path,*args,**kwargs):
                    if path.name=='stdout.log' and args and args[0]=='wb':
                        kwargs['buffering']=131072
                    return original_open(path,*args,**kwargs)
                def publish(path,value):
                    if path.name=='stdout.overflow.json':
                        observed['prefix_bytes_at_publication']=(directory/'stdout.log').stat().st_size
                        original_publish(path,value)
                        original_publish(directory/'stop.json',dict(nonce=nonce))
                    else:
                        original_publish(path,value)
                Path.open=buffered
                owned._publish=publish
                code=owned.adapter(directory/'command.json')
                actual=(directory/'stdout.log').read_bytes()
                observed.update(code=code,final_bytes=len(actual),
                    exact_prefix=actual==b'o'*150000+b'e'*112144,
                    streams_closed=json.loads((directory/'complete.json').read_text())['streams_closed'])
                print(json.dumps(observed),flush=True)
            """)
            result = probe_cli([sys.executable, "-u", "-c", program])
            self.assertTrue(result["successful"], result)
            observed = json.loads(result["stdout"])
            self.assertEqual(observed["code"], 0)
            self.assertTrue(observed["streams_closed"])
            self.assertTrue(observed["exact_prefix"])
            self.assertEqual(observed["final_bytes"], 262144)
            self.assertEqual(observed["prefix_bytes_at_publication"], 262144,
                             "a parent may force-stop the Job as soon as overflow is visible")


if __name__ == "__main__":
    unittest.main()
