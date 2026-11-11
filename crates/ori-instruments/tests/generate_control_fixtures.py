"""Deterministic real Python controller traces on owned memory transport only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import unittest
import zlib

from radioinspector.tinysa.controller import Controller
from radioinspector.tinysa.protocol import capture_png
from tests.tinysa_control_fixture import make_adapter


def main():
    test = unittest.TestCase()
    try:
        _, _, adapter, wire = make_adapter(test, {c: b'' for c in [
            'ultra on', 'sweep 900000000 901000000 7', 'rbw 30',
            'repeat 3', 'attenuate 0', 'lna on',
        ]})
        controller = Controller(adapter, monotonic=lambda: 100.0)
        controller.execute({'operation': 'inspect'})
        wire.sent.clear()
        acquired = controller.execute({'operation': 'lease.acquire', 'parameters': {'acknowledge_rf': True}})
        lease = acquired['lease_id']
        acquire_commands = list(wire.sent)
        wire.sent.clear()
        settings = {'rbw_hz': 30000, 'stop_hz': 901000000, 'repeat': 3,
                    'start_hz': 900000000, 'points': 7, 'ultra': True,
                    'attenuation_db': 0, 'lna': True}
        document = json.dumps({'schema_version': 1, 'profile': 'tinysa-ultra-zs405-control-v1', 'settings': settings})
        imported = controller.execute({'operation': 'preset.import', 'lease_id': lease,
                                       'parameters': {'document': document, 'confirm': True}})
        preset_commands = list(wire.sent)
        wire.sent.clear()
        controller.execute({'operation': 'lease.release', 'lease_id': lease})
        release_commands = list(wire.sent)
        wire.sent.clear()
        controller.execute({'operation': 'lease.acquire', 'parameters': {'acknowledge_rf': True}})
        reenter_commands = list(wire.sent)

        # Exercise every possible RGB565 word through the real Python encoder.
        words = [n % 65536 for n in range(480 * 320)]
        raw = b''.join(struct.pack('>H', n) for n in words)
        png = capture_png(raw)
        compressed = b''
        offset = 8
        while offset < len(png):
            size = int.from_bytes(png[offset:offset+4], 'big')
            if png[offset+4:offset+8] == b'IDAT':
                compressed += png[offset+8:offset+8+size]
            offset += size + 12
        rows = zlib.decompress(compressed)
        assert all(rows[n * 1441] == 0 for n in range(320))
        rgb = b''.join(rows[n*1441+1:(n+1)*1441] for n in range(320))
        data = {
            'source': 'Python Controller and capture_png; owned UltraWire, no hardware',
            'acquire_commands': acquire_commands, 'release_commands': release_commands,
            'reenter_commands': reenter_commands, 'preset_document': document,
            'preset_commands': preset_commands, 'preset_settings': imported['status']['settings'],
            'preset_application': imported['application'], 'preset_rollback': imported['rollback'],
            'rgb565_pattern': 'big-endian u16 index modulo 65536 for 480*320 pixels',
            'raw_rgb565_sha256': hashlib.sha256(raw).hexdigest(),
            'decoded_rgb_sha256': hashlib.sha256(rgb).hexdigest(),
            'python_png_sha256': hashlib.sha256(png).hexdigest(),
        }
        path = Path(__file__).with_name('fixtures') / 'control_python.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    finally:
        test.doCleanups()


if __name__ == '__main__':
    main()
