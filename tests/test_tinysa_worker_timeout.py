import threading
import time
import unittest
from radioinspector.device_workers import DeviceWorker,TinySASimulator,WorkerError
from radioinspector.session_service import SessionService
from tests.test_device_workers import simulated_session_payload


class ActiveTimeoutTests(unittest.TestCase):
    def test_active_timeout_interrupts_worker_instead_of_leaving_untracked_io(self):
        service=SessionService();self.addCleanup(service.close)
        session=service.create_session(simulated_session_payload());service.start_session(session.id)
        entered=threading.Event();released=threading.Event();stopped=threading.Event()
        class Blocking(TinySASimulator):
            def execute(self,command,payload):
                entered.set();released.wait(3)
                raise WorkerError('I/O interrupted')
            def request_stop(self):stopped.set();released.set()
        adapter=Blocking(session.streams[0]);worker=DeviceWorker(service,adapter)
        worker.start();self.addCleanup(lambda:(released.set(),worker.stop(timeout=3)))
        error=[]
        def submit():
            try:worker.submit('instrument',{'operation':'acquire.raw'},timeout=.1)
            except WorkerError as exc:error.append(exc)
        thread=threading.Thread(target=submit);thread.start()
        self.assertTrue(entered.wait(1));thread.join(1)
        self.assertTrue(stopped.is_set(),'active timed-out I/O must be interrupted')
        self.assertFalse(worker.alive)
        self.assertTrue(error)
        self.assertTrue(getattr(error[0],'uncertain',False),'timeout result must remain unknown')
