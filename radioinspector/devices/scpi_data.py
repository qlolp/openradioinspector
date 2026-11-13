"""Strict bounded ASCII SCPI data and private diagnostic artifacts."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

import numpy as np

from .base import InstrumentError

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_POINTS = 600_000
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
_HEADER = re.compile(r'(?:CURVE|TRACE)\s+([0-9]+)(?:\s*,\s*|\s+)(.*)\Z', re.I | re.S)


class ScpiDataError(InstrumentError):
    def __init__(self, reason, payload, *, expected_points=None):
        super().__init__(reason)
        raw = payload.encode('utf-8', errors='surrogatepass') if isinstance(payload, str) else bytes(payload)
        self.raw_bytes = raw[:MAX_RESPONSE_BYTES]
        self.raw_response = raw[:65536].decode('ascii', errors='backslashreplace')
        self.diagnostic = {
            'kind': 'scpi_invalid_response', 'reason': reason,
            'expected_points': expected_points, 'received_bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'truncated': len(raw) > MAX_RESPONSE_BYTES,
            'raw_preview_truncated': len(raw) > 65536,
        }


class BoundedVxiSocket:
    """Bound the RPC library's own fragmented socket reads, not just its output.

    python-vxi11 0.9 exposes client.sock. The wrapper preserves the real socket
    while enforcing one absolute read deadline and a raw-wire allocation cap.
    """
    def __init__(self, peer, deadline, maximum):
        self.peer, self.deadline, self.maximum = peer, deadline, maximum
        self.received = 0
        self.prefix = bytearray()

    def _failure(self, reason):
        error = ScpiDataError(reason, self.prefix)
        error.diagnostic.update(raw_scope='rpc_wire_prefix', wire_bytes_received=self.received,
                                truncated=self.received > len(self.prefix))
        return error

    def __getattr__(self, name):
        return getattr(self.peer, name)

    def recv(self, size, *args):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise self._failure('VXI-11 RPC deadline exceeded')
        self.peer.settimeout(remaining)
        data = self.peer.recv(min(size, self.maximum + 1 - self.received), *args)
        self.received += len(data)
        self.prefix.extend(data[:max(0, 65536 - len(self.prefix))])
        if self.received > self.maximum:
            raise self._failure('VXI-11 RPC wire response limit exceeded')
        return data


def parse_ascii(payload: str, *, expected_points=None, semicolons=False) -> np.ndarray:
    def reject(reason):
        raise ScpiDataError(reason, payload, expected_points=expected_points)
    if not isinstance(payload, str):
        raise InstrumentError('SCPI ASCII response must be text')
    if len(payload) > MAX_RESPONSE_BYTES or not payload.isascii():
        reject('SCPI response exceeds size limit or contains non-ASCII data')
    text = payload.strip()
    declared = None
    header = _HEADER.fullmatch(text)
    if header:
        # Bound before converting an arbitrarily long integer or allocating samples.
        digits = header.group(1)
        if len(digits) > 6:
            reject('SCPI declared point count exceeds limit')
        declared = int(digits)
        text = header.group(2)
    if semicolons:
        text = text.replace(';', ',')
    if not text or text.count(',') >= MAX_POINTS:
        reject('SCPI empty response or point limit exceeded')
    tokens = text.split(',')
    if declared is not None and declared != len(tokens):
        reject('SCPI header point count differs from response')
    if expected_points is not None and len(tokens) != expected_points:
        reject('SCPI point count differs from requested grid')
    values = []
    for token in tokens:
        token = token.strip()
        if not _NUMBER.fullmatch(token):
            reject('SCPI response contains an invalid numeric token')
        value = float(token)
        if not math.isfinite(value):
            reject('SCPI response contains a nonfinite value')
        values.append(value)
    return np.asarray(values, dtype=float)


def validate_config(config):
    if type(config.points) is not int or not 2 <= config.points <= MAX_POINTS:
        raise InstrumentError('SCPI points must be an integer from 2 to 600000')
    if (not all(math.isfinite(v) for v in (config.start_hz, config.stop_hz))
            or not 0 <= config.start_hz < config.stop_hz):
        raise InstrumentError('SCPI frequency range is invalid')


def retain_quality_issue(instrument, error: ScpiDataError, config):
    instrument.last_quality_issue = dict(error.diagnostic)
    directory = instrument.diagnostic_directory
    if directory is None:
        return
    try:
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / (uuid.uuid4().hex + '.json')
        raw_path = path.with_suffix('.response')
        body = {'diagnostic': error.diagnostic, 'raw_response': error.raw_response,
                'raw_artifact': raw_path.name,
                'requested_settings': config.as_dict(), 'instrument': instrument.identify().as_dict()}
        # Exclusive creation; never replace a previous artifact or follow a link.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(raw_path, flags, 0o600), 'wb') as raw_output:
            raw_output.write(error.raw_bytes)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        with os.fdopen(os.open(path, flags, 0o600), 'w', encoding='utf-8') as output:
            json.dump(body, output, ensure_ascii=False, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        instrument.last_quality_issue['artifact_path'] = str(path)
    except (OSError, ValueError, TypeError) as failure:
        instrument.last_quality_issue['artifact_error'] = {
            'type': type(failure).__name__, 'errno': getattr(failure, 'errno', None),
        }
