"""Portable serial configuration for monitoring tests with injected transports."""

from tests.test_serial_instruments import serial_session


def monitoring_serial_session():
    payload = serial_session()
    # Native COM names also pass POSIX configuration-only preflight. Keep the
    # generic serial fixture's /dev paths for tests that route transports by path.
    payload['streams'][0]['port'] = 'COM3'
    payload['streams'][1]['port'] = 'COM4'
    return payload
