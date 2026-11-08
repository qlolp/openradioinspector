"""Bounded, view-only spectrum state. No I/O or measurement transformations."""

import math
import re
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import pairwise

CAPACITY = 40


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def fraction(value, lower, upper):
    """Overflow-safe clipped display coordinate."""
    if lower == upper or value <= lower:
        return 0.0
    if value >= upper:
        return 1.0
    scale = max(abs(lower), abs(upper), abs(value), 1.0)
    return max(0.0, min(1.0, (value / scale - lower / scale) / (upper / scale - lower / scale)))


def fit_bounds(levels):
    lower, upper = min(levels), max(levels)
    scale = max(abs(lower), abs(upper), 1.0)
    padding = max((upper / scale - lower / scale) * .08, 1.0 / scale)
    limit = sys.float_info.max / scale
    lo = max(-limit, lower / scale - padding) * scale
    hi = min(limit, upper / scale + padding) * scale
    if lo == hi:
        padding = max(abs(lower / scale) * .08, 1.0 / scale)
        lo = max(-limit, lower / scale - padding) * scale
        hi = min(limit, upper / scale + padding) * scale
    # The normalized clamp can still round past MAX when multiplied back.
    maximum = sys.float_info.max
    return max(-maximum, min(maximum, lo)), max(-maximum, min(maximum, hi))


def validate_sweep(sweep, session, stream):
    if not isinstance(sweep, dict) or not isinstance(sweep.get('id'), str) or not sweep['id']:
        raise ValueError('Missing source ID')
    if sweep.get('session_id') != session or sweep.get('stream_id') != stream or sweep.get('level_unit') != 'dBm':
        raise ValueError('Foreign scope or unit')
    grid, levels = sweep.get('frequencies_hz'), sweep.get('levels')
    if not isinstance(grid, list) or not isinstance(levels, list) or not grid or len(grid) != len(levels):
        raise ValueError('Empty or mismatched spectrum')
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 9_007_199_254_740_991 for v in grid) or any(a > b for a, b in pairwise(grid)):
        raise ValueError('Invalid integer-Hz grid')
    if not all(finite(v) for v in levels):
        raise ValueError('Nonfinite level')
    interval = sweep.get('interval')
    try:
        pattern = r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)'
        if any(not isinstance(interval[k], str) or not re.fullmatch(pattern, interval[k]) for k in ('started_at', 'ended_at')):
            raise ValueError()
        times = [datetime.fromisoformat(interval[k].replace('Z', '+00:00')) for k in ('started_at', 'ended_at')]
        if any(t.utcoffset() != timedelta(0) for t in times) or times[0] > times[1]:
            raise ValueError()
    except (TypeError, KeyError, AttributeError, ValueError):
        raise ValueError('Invalid UTC interval') from None


class SpectrumView:
    def __init__(self):
        self.clear()

    def clear(self):
        self.scope = None
        self.live = []
        self.displayed = []
        self.bounds = None
        self.paused = False
        self.error = ''

    def update(self, server, session, stream, sweeps):
        try:
            if not all(isinstance(v, str) and v for v in (server, session, stream)) or not isinstance(sweeps, list) or not sweeps:
                raise ValueError('No spectrum data')
            for sweep in sweeps:
                validate_sweep(sweep, session, stream)
            sweeps = sweeps[-CAPACITY:]
            grid = sweeps[-1]['frequencies_hz']
            start = len(sweeps) - 1
            while start and sweeps[start - 1]['frequencies_hz'] == grid:
                start -= 1
            scope = (server, session, stream, 'dBm', tuple(grid))
            if scope != self.scope:
                self.clear()
                self.scope = scope
            self.live = deepcopy(sweeps[start:])
            if not self.paused:
                self.displayed = deepcopy(self.live)
            if self.bounds is None:
                self.fit()
            return True
        except (ValueError, TypeError, OverflowError) as exc:
            self.clear()
            self.error = str(exc)
            return False

    def toggle_pause(self):
        if self.displayed:
            self.paused = not self.paused
            if not self.paused:
                self.displayed = deepcopy(self.live)

    def fit(self):
        if self.displayed:
            self.bounds = fit_bounds([v for s in self.displayed for v in s['levels']])
            self.error = ''

    def set_bounds(self, lower, upper):
        if not self.displayed:
            self.error = 'No spectrum data'
            return False
        try:
            pattern = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
            if any(isinstance(v, bool) or not re.fullmatch(pattern, str(v).strip(" \t\r\n\f\v")) for v in (lower, upper)):
                raise ValueError()
            lower, upper = float(lower), float(upper)
            if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            self.error = 'Finite lower < upper required'
            return False
        self.bounds = (lower, upper)
        self.error = ''
        return True

    @property
    def clipped(self):
        if self.bounds is None:
            return 0
        lower, upper = self.bounds
        return sum(not lower <= v <= upper for s in self.displayed for v in s['levels'])
