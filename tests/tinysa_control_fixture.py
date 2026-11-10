"""Own synthetic shell transcript; never a captured device response."""
from collections import deque
import threading
from radioinspector.serial_instruments import _SerialFraming, TinySASerialAdapter
from radioinspector.session_service import SessionService
from tests.test_serial_instruments import serial_session

COMMANDS = 'info version deviceid help output mode lna attenuate rbw calc pause resume capture touch release menu text refresh caloutput sweep scan scanraw zero frequencies data trace marker trigger repeat spur ultra ext_gain level freq modulation levelchange save recall sd_list sd_read sd_delete selftest status'


class UltraWire(_SerialFraming):
    def __init__(self, extra=None):
        self.path='synthetic://full-ultra'
        self._stash=bytearray();self._cancelled=threading.Event()
        self.pending=deque();self.sent=[];self.closed=False
        self.responses={
            'info': b'tinySA Ultra ZS405\r\n',
            'version': b'tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n',
            'deviceid': b'synthetic\r\n', 'help': ('commands: '+COMMANDS+'\r\n').encode(),
            'zero': b'zero {level}\r\n174dBm\r\n',
            'status': b'Paused\r\n',
            'scan 100M 101M 3 3': b'100000000 -90 0\r\n100500000 -80 0\r\n101000000 -70 0\r\n',
        }
        for c in ['output off','mode input','lna off','attenuate auto','rbw 10','calc off','pause','caloutput off','release','refresh off','trace dBm','trigger auto','resume']:
            self.responses[c]=b''
        self.responses.update(extra or {})

    def send(self, data):
        if self._stash or self.pending:
            raise ValueError('Unread synthetic reply')
        command=data.decode('ascii').rstrip('\r')
        if command not in self.responses:
            raise ValueError('Unspecified synthetic command: '+command)
        self.sent.append(command)
        body=self.responses[command]
        if isinstance(body,Exception):raise body
        self.pending.extend([command.encode()+b'\r\n',body,b'ch> '])

    def _read_once(self, timeout, label):
        data=self.pending.popleft() if self.pending else b''
        self._stash.extend(data)
        return data

    def close(self):self.closed=True


def make_adapter(test, extra=None):
    service=SessionService();test.addCleanup(service.close)
    session=service.create_session(serial_session())
    wire=UltraWire(extra);adapter=TinySASerialAdapter(session.streams[0],transport=wire)
    test.addCleanup(adapter.close)
    return service,session,adapter,wire
