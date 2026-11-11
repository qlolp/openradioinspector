"""Validate complete immutable PCM WAV segments before archive mutation."""
import base64
import hashlib
import io
import struct
import wave

from .contracts import TimeInterval, to_primitive
from .request_limits import MAX_MEASUREMENT_REQUEST_BYTES


class AudioSegmentConflict(ValueError):
    pass


def make_audio_segment(session_id, payload):
    required = {'id','stream_id','frequency_hz','mode','interval','data_base64','sha256'}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError('Audio segment requires id, stream_id, frequency_hz, mode, interval, data_base64 and sha256')
    for name in ('id', 'stream_id', 'mode'):
        value = payload[name]
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError('Invalid audio segment ' + name)
        value.encode('utf-8', errors='strict')
    if type(payload['frequency_hz']) is not int or payload['frequency_hz'] <= 0:
        raise ValueError('Audio frequency must be positive integer Hz')
    interval = TimeInterval.from_dict(payload['interval'])
    encoded = payload['data_base64']
    if not isinstance(encoded, str) or len(encoded) > MAX_MEASUREMENT_REQUEST_BYTES:
        raise ValueError('Audio segment exceeds request budget')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid audio base64') from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != payload['sha256']:
        raise ValueError('Audio segment checksum mismatch')
    if len(raw) < 44 or raw[:4] != b'RIFF' or raw[8:12] != b'WAVE' or struct.unpack('<I',raw[4:8])[0]+8 != len(raw):
        raise ValueError('Audio segment must contain a complete RIFF/WAV file')
    offset, formats, data_chunks = 12, 0, 0
    while offset < len(raw):
        if len(raw)-offset < 8:
            raise ValueError('Incomplete WAV chunk header')
        kind = raw[offset:offset+4]
        size = struct.unpack_from('<I',raw,offset+4)[0]
        end = offset+8+size
        if end+(size % 2) > len(raw):
            raise ValueError('Incomplete WAV chunk or padding')
        if kind == b'fmt ':formats += 1
        if kind == b'data':data_chunks += 1
        offset = end+(size % 2)
    if formats != 1 or data_chunks != 1:
        raise ValueError('WAV must have exactly one format and one data chunk')
    try:
        with wave.open(io.BytesIO(raw), 'rb') as source:
            rate, frames = source.getframerate(), source.getnframes()
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getcomptype() != 'NONE':
                raise ValueError('Audio must be uncompressed mono PCM16')
            if not 1 <= rate <= 192000 or frames <= 0 or len(source.readframes(frames+1)) != frames*2:
                raise ValueError('Incomplete audio frames or unsupported sample rate')
    except (wave.Error, EOFError, struct.error) as exc:
        raise ValueError('Invalid PCM WAV audio') from exc
    delta = interval.ended_at - interval.started_at
    micros = (delta.days*86400+delta.seconds)*1000000 + delta.microseconds
    if abs(micros*rate - frames*1000000) > rate:
        raise ValueError('Audio interval does not match PCM frame duration')
    metadata = dict(frequency_hz=payload['frequency_hz'], mode=payload['mode'],
                    interval=to_primitive(interval), sample_rate=rate, frame_count=frames,
                    occurred_at=to_primitive(interval)['started_at'])
    return dict(id=payload['id'],session_id=session_id,stream_id=payload['stream_id'],
                schema_version=1,kind='audio',mime_type='audio/wav',filename='audio-'+digest[:24]+'.wav',
                sha256=digest,created_at=metadata['occurred_at'],payload=raw,metadata=metadata,size_bytes=len(raw))
