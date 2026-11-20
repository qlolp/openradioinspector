import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, expect, it } from 'vitest';
import { CoverageRow, Qualification } from '../src/StationPanels';
import { setLocale } from '../src/i18n';

beforeEach(() => setLocale('en'));

it('keeps band coverage and the last measured window’s qualification independent', () => {
  const html = renderToStaticMarkup(
    <CoverageRow
      band={{
        id: 'crossing',
        name: 'Boundary band',
        state: 'covered',
        deadline_missed: false,
        age_seconds: 3,
        last_revisit_seconds: 5,
        measurement_qualification: {
          status: 'partial',
          reason: 'reference_required_above_800_mhz',
          verified_through_hz: 800000000,
        },
        last_window: {
          index: 1,
          points: 2,
          measurement_id: 'last',
          start_hz: 810000000,
          stop_hz: 810010000,
          started_at: '2026-11-19T00:00:00Z',
          ended_at: '2026-11-19T00:00:01Z',
          measurement_qualification: {
            status: 'unverified',
            reason: 'reference_required_above_800_mhz',
            verified_through_hz: null,
          },
        },
      }}
    />,
  );
  expect(html).toContain('Partially verified frequency range');
  expect(html).toContain('Unverified frequency range');
  expect(html).toContain('800,000,000 Hz');
  expect(html).toContain('Above 800 MHz a reference measurement is required.');
});
it.each([
  [undefined, 'Not reported by the core'],
  [{ status: 'future' }, 'Not reported by the core'],
  [{ status: 'synthetic' }, 'Training data'],
  [{ status: 'supported' }, 'Supported by the receiver profile'],
])(
  'reports supplied evidence without inferring verification from a frequency or an adapter',
  (value, message) => {
    expect(
      renderToStaticMarkup(<Qualification label="Source qualification" value={value} />),
    ).toContain(message);
  },
);
