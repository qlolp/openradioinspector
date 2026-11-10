import io
import threading
import unittest
import wave


class AnalogTests(unittest.TestCase):
    def test_wav_preserves_every_accepted_frame_and_releases_input(self):
        from radioinspector.tinysa.audio import capture
        class Input:
            closed=False
            def __init__(self,**kwargs):self.callback=kwargs['callback']
            def __enter__(self):
                self.callback(b'\x01\0'*800,800,None,False)
                return self
            def __exit__(self,*args):Input.closed=True
        class Backend:RawInputStream=Input
        result=capture(dict(seconds=.1,sample_rate_hz=8000,device=0),check=lambda:None,backend=Backend)
        with wave.open(io.BytesIO(result)) as wav:
            self.assertEqual(wav.getnframes(),800)
            self.assertEqual(wav.readframes(800),b'\x01\0'*800)
        self.assertTrue(Input.closed)

    def test_overflow_is_visible_and_does_not_save_clean_audio(self):
        from radioinspector.tinysa.audio import capture
        class Input:
            def __init__(self,**kwargs):self.callback=kwargs['callback']
            def __enter__(self):self.callback(b'',0,None,True);return self
            def __exit__(self,*args):pass
        class Backend:RawInputStream=Input
        with self.assertRaisesRegex(ValueError,'overflow'):
            capture(dict(seconds=.1,sample_rate_hz=8000,device=0),check=lambda:None,backend=Backend)

    def test_cancel_releases_input_without_waiting_for_requested_duration(self):
        from radioinspector.tinysa.audio import capture
        closed=threading.Event()
        class Input:
            def __init__(self,**kwargs):pass
            def __enter__(self):return self
            def __exit__(self,*args):closed.set()
        class Backend:RawInputStream=Input
        def cancelled():raise ValueError('cancelled')
        with self.assertRaisesRegex(ValueError,'cancelled'):
            capture(dict(seconds=30,sample_rate_hz=8000,device=0),check=cancelled,backend=Backend)
        self.assertTrue(closed.is_set())
