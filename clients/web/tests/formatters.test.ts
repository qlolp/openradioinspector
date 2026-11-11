import { expect, it } from 'vitest';
import { utc } from '../src/components';
it('shows offset timestamps in UTC without losing the six microsecond digits', () => {
  expect(utc('2026-11-11T15:00:00.123456+03:00')).toBe('2026-11-11 12:00:00.123456 UTC');
  expect(utc('2026-01-01T00:30:00.000001+01:00')).toBe('2025-12-31 23:30:00.000001 UTC');
});
