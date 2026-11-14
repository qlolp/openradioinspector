import { afterEach, expect, it, vi } from 'vitest';
import { browserConnection } from '../src/transport';
afterEach(() => vi.unstubAllGlobals());
it('preserves integer metadata and negative zero in browser HTTP requests and responses', async () => {
  const raw =
    '{"contract":"openradioinspector-platform","contract_version":1,"data":{"metadata":{"huge":184467440737095516160000,"negative_zero":-0.0,"$serde_json::private::Number":{"$serde_json::private::RawValue":"Ж𐀀"}}}}';
  const fetchMock = vi.fn(
    async () => new Response(raw, { headers: { 'Content-Type': 'application/json' } }),
  );
  vi.stubGlobal('fetch', fetchMock);
  const response = (await browserConnection.request('/api/v1/capabilities')) as {
    data: { metadata: { huge: bigint; negative_zero: number } };
  };
  expect(response.data.metadata.huge).toBe(184467440737095516160000n);
  expect(Object.is(response.data.metadata.negative_zero, -0)).toBe(true);
  await browserConnection.request('/api/v1/sessions', { metadata: response.data.metadata });
  const sent = fetchMock.mock.calls.at(-1) as unknown as [string, { body: string }];
  expect(sent[1].body).toContain('184467440737095516160000');
  expect(sent[1].body).toContain('"negative_zero":-0.0');
  expect(sent[1].body).toContain('$serde_json::private::Number');
});
it('rejects duplicate names and nonfinite JSON before dispatching a parsed response', async () => {
  for (const raw of ['{"same":1,"same":2}', '{"overflow":1e9999}']) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(raw)),
    );
    await expect(browserConnection.request('/api/v1/capabilities')).rejects.toThrow();
  }
});

it('decodes known float64 source fields and preserves unknown metadata for original download', async () => {
  const { responseValue, originalJSON, parseWire } = await import('../src/wire');
  const { parseFrame } = await import('../src/pipeline');
  const sweep =
    '{"id":"edge-source","session_id":"s","stream_id":"a","interval":{"started_at":"2026-11-11T15:00:00.123456+03:00","ended_at":"2026-11-11T12:00:01.123456Z","future_metadata":{"large_float":1e20}},"frequencies_hz":[1,2,3,4],"levels":[-0.0,5e-324,1.7976931348623157e308,1.7976931348623157e308],"level_unit":"dBm","settings":{"integer":184467440737095516160000,"large_float":1e20,"plain":{"value":1},"$serde_json::private::Number":{"$serde_json::private::RawValue":"Ж𐀀"}}}';
  const envelope =
    '{"contract":"openradioinspector-platform","contract_version":1,"data":' + sweep + '}';
  const original = (
    responseValue('/api/v1/monitoring-projects/p/history/id', envelope) as {
      data: import('../src/types').Sweep;
    }
  ).data;
  expect(original.interval.started_at).toBe('2026-11-11T12:00:00.123456Z');
  expect(Object.is(original.levels[0], -0)).toBe(true);
  expect(original.levels[1]).toBe(Number.MIN_VALUE);
  expect(original.levels[2]).toBe(Number.MAX_VALUE);
  expect(original.settings.integer).toBe(184467440737095516160000n);
  const roundtrip = parseWire(originalJSON(original)) as {
    settings: { integer: bigint; plain: { value: number } };
  };
  expect(roundtrip.settings.integer).toBe(184467440737095516160000n);
  expect(roundtrip.settings.plain).toEqual({ value: 1 });
  const frame = parseFrame(
    '{"contract":"openradioinspector-platform","contract_version":1,"data":{"sequence":1,"event_type":"sweep.received","payload":{"sweep":' +
      sweep +
      '}}}',
  );
  expect(frame.sweep?.peak.index).toBe(2);
  expect(frame.sweep).not.toHaveProperty('settings');
  expect(frame.sweep?.interval).not.toHaveProperty('future_metadata');
  const clone = structuredClone(frame);
  expect(Object.is(clone.sweep?.levels[0], -0)).toBe(true);
  expect(clone.sweep?.levels[2]).toBe(Number.MAX_VALUE);
});
