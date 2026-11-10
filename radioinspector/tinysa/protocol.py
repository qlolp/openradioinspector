"""Bounded codecs for the stock Ultra ea71dfd shell (not an RF validation)."""
from __future__ import annotations

import math
import struct
import time
import zlib

MAX_BINARY_BYTES = 8 * 1024 * 1024
MAX_RAW_POINTS = 65536
MAX_SCREEN_PIXELS = 480 * 320


def _count(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('Invalid binary size / недопустимый размер')
    return value


def read_exact(transport, count: int, deadline: float, label: str) -> bytes:
    """Consume bytes by length, never by looking for ASCII inside a payload."""
    _count(count, MAX_BINARY_BYTES)
    result = bytearray()
    while len(result) < count:
        transport._check_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f'Binary deadline exceeded: {label}')
        if transport._stash:
            take = min(count-len(result), len(transport._stash))
            result.extend(transport._stash[:take])
            del transport._stash[:take]
        else:
            transport._read_once(min(remaining, 0.25), label)
    return bytes(result)


def read_binary(transport, command: str, *, size: int | None = None, timeout: float = 30) -> bytes:
    """Pinned shell echoes every command, then payload, then exactly ``ch> ``.

    A None size means the SD protocol's unsigned little-endian length prefix.
    The caller must taint its connection after *any* unsuccessful exchange.
    """
    if not isinstance(command, str) or not command or len(command) > 200 or not command.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in command):
        raise ValueError('Invalid binary command')
    if size is not None:
        _count(size, MAX_BINARY_BYTES)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError('Invalid binary deadline')
    deadline = time.monotonic() + timeout
    transport.send(command.encode('ascii') + b'\r')
    echo = (command + '\r\n').encode('ascii')
    if read_exact(transport, len(echo), deadline, command) != echo:
        raise ValueError('Unexpected binary echo / shell error')
    if size is None:
        size, = struct.unpack('<I', read_exact(transport, 4, deadline, command))
        _count(size, MAX_BINARY_BYTES)
    result = read_exact(transport, size, deadline, command)
    if read_exact(transport, 4, deadline, command) != b'ch> ' or transport._stash:
        raise ValueError('Invalid binary terminal / trailing data')
    return result


def _float32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def decode_scanraw(raw: bytes, start_hz: int, stop_hz: int, points: int, *, zero_db: float):
    """Firmware uses integer division, float32 multiplication and an open end.

    Decode the exact returned sample count. An aborted, closed-brace partial
    scan is still incomplete and must never enter the measurement archive.
    """
    _count(points, MAX_RAW_POINTS)
    if points < 1 or type(start_hz) is not int or type(stop_hz) is not int or not 0 <= start_hz <= stop_hz <= 20_000_000_000:
        raise ValueError('Invalid scanraw grid')
    if isinstance(zero_db, bool) or not isinstance(zero_db, (int, float)) or not math.isfinite(zero_db) or not 0 <= zero_db <= 2047:
        raise ValueError('Invalid scanraw zero')
    if len(raw) != 3*points+2 or raw[:1] != b'{' or raw[-1:] != b'}':
        raise ValueError('Incomplete scanraw frame')
    step = _float32((stop_hz-start_hz)//points)
    hz, levels = [], []
    for i in range(points):
        offset = 1+3*i
        if raw[offset] != ord('x'):
            raise ValueError('Invalid scanraw sample')
        value, = struct.unpack_from('<H', raw, offset+1)
        hz.append(start_hz + int(_float32(step * _float32(i))))
        levels.append(value/32 - zero_db)
    return tuple(hz), tuple(levels)


def capture_png(raw: bytes, *, width: int = 480, height: int = 320) -> bytes:
    """Convert display-wire RGB565 (big endian) without a GUI dependency."""
    _count(width, 480)
    _count(height, 320)
    if not width or not height or width*height > MAX_SCREEN_PIXELS or len(raw) != width*height*2:
        raise ValueError('Invalid capture geometry/length')
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)  # PNG filter: none
        for x in range(width):
            pixel, = struct.unpack_from('>H', raw, 2*(y*width+x))
            scanlines.extend((((pixel >> 11) & 31)*255//31, ((pixel >> 5) & 63)*255//63, (pixel & 31)*255//31))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(scanlines))) + chunk(b'IEND', b''))
