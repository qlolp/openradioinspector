"""Real temporary SQLite/core with explicitly synthetic RX profiles; no hardware I/O.

Owns only a temporary directory and localhost sockets. It is not shipped in dist.
"""
from datetime import timedelta
from pathlib import Path
import io
import base64
import hashlib
import signal
import sys
import tempfile
import threading
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from radioinspector.web.server import create_server
from radioinspector.contracts import TimeInterval, format_time, utc_now
from radioinspector.device_workers import TinySASimulator
from binary_fixture import install as install_binary_fixture


def main():
    with tempfile.TemporaryDirectory(prefix='ori-modern-browser-') as directory:
        server = create_server(port=18771, session_database=str(Path(directory) / 'archive.sqlite'))
        install_binary_fixture(server)
        sessions, monitoring = server.session_service, server.monitoring
        session = sessions.create_session(dict(name='Synthetic receive pair', streams=[
            dict(device='tinySA Ultra', role='spectrum_analyzer', level_unit='dBm', port='sim://fixture-analyzer',
                 settings=dict(adapter='tinysa-sim', start_hz=88000000, stop_hz=108000000, points=201, rbw_hz=10000,
                               period_s=.25, noise_sigma_db=0,
                               emitters=[dict(frequency_hz=99500000, bandwidth_hz=150000, level_dbm=-64)])),
            dict(device='AR-DV1', role='receiver', level_unit='s_meter_count', port='sim://fixture-receiver',
                 settings=dict(adapter='ardv1-sim', frequency_hz=100000000, mode='FM', period_s=.25))]))
        project = monitoring.create(dict(name='North laboratory', site='Synthetic acceptance fixture', session_id=session.id))
        monitoring.save_preparation(project['id'], dict(antenna_notes='Synthetic receive antenna', receive_path_confirmed=True, location_confirmed=True))
        adapter = TinySASimulator(session.streams[0]); now = utc_now()
        for index in range(48):
            sweep = adapter.poll(); started = now - timedelta(seconds=200 - index * 4)
            sweep.interval = TimeInterval(started, started + timedelta(seconds=1))
            sessions.ingest_sweep(sweep)
        monitoring.create_baseline(project['id'], dict(name='Quiet reference', location='Laboratory', confirm=True))
        levels = [-100.] * 201; levels[120] = -55.
        for index in range(3):
            sweep = adapter.poll(); sweep.levels = tuple(levels)
            started = now - timedelta(seconds=8-index*2); sweep.interval = TimeInterval(started, started+timedelta(seconds=1))
            sessions.ingest_sweep(sweep)
        monitoring.analyze(project['id'])
        events = monitoring.store.recent('event', project['id'])
        if not events:
            raise RuntimeError('Synthetic core detector did not produce an event')
        event = events[0]
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(48000); output.writeframes(b'\0\0' * 24000)
        artifact = sessions.save_instrument_artifact(session.id, session.streams[1].id,
            dict(kind='audio', mime_type='audio/wav', filename='synthetic.wav',
                 data_base64=base64.b64encode(audio.getvalue()).decode(), sha256=hashlib.sha256(audio.getvalue()).hexdigest(),
                 metadata={'synthetic': True, 'occurred_at': format_time(now)}))
        event['analysis'] = dict(state='observed', mode='FM', audio=dict(id=artifact['id'], session_id=session.id, stream_id=session.streams[1].id))
        monitoring.store.put('event', event)
        # A second independent project verifies cancellation/selection without hardware.
        monitoring.create(dict(name='Unprepared project', site='Second synthetic scope', session_id=session.id))
        stop = threading.Event()
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, lambda *_: stop.set())
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        print('Synthetic core fixture ready', flush=True)
        stop.wait()
        server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == '__main__':
    main()
