import { describe, expect, it } from 'vitest';
import { decimate, parseFrame, FrameBuffer, SequenceGate } from '../src/pipeline';

const sweep = {
  id: 'source/1',
  session_id: 's',
  stream_id: 'a',
  level_unit: 'dBm',
  interval: { started_at: '2026-11-11T00:00:00.123456Z', ended_at: '2026-11-11T00:00:01.123456Z' },
  frequencies_hz: [100, 101, 102, 103, 104, 105, 106, 107],
  levels: [-90, -80, -10, -70, -10, -60, -85, -95],
  settings: {},
};

describe('display-only reduction', () => {
  it('preserves the first global maximum, source indices, UTC precision, and raw values', () => {
    const before = structuredClone(sweep);
    const result = decimate(sweep, 4);
    expect(result.source_indices).toContain(2);
    expect(result.peak).toEqual({ index: 2, frequency_hz: 102, level: -10 });
    expect(result.id).toBe('source/1');
    expect(result.interval.started_at).toBe('2026-11-11T00:00:00.123456Z');
    expect(result.levels.length).toBeLessThanOrEqual(4);
    expect(sweep).toEqual(before);
  });
  it('keeps a one-point trace and rejects non-finite or mismatched raw arrays', () => {
    expect(decimate({ ...sweep, frequencies_hz: [100], levels: [-50] }, 4).levels).toEqual([-50]);
    expect(() => decimate({ ...sweep, levels: [NaN] }, 4)).toThrow();
    expect(() => decimate({ ...sweep, frequencies_hz: [100.5], levels: [-50] }, 4)).toThrow();
  });
});

describe('committed event stream', () => {
  it('parses a complete committed sweep without a follow-up aggregate fetch', () => {
    const frame = parseFrame(
      JSON.stringify({
        contract: 'openradioinspector-platform',
        contract_version: 1,
        data: {
          sequence: 8,
          event_type: 'sweep.received',
          session_id: 's',
          occurred_at: sweep.interval.ended_at,
          payload: { sweep },
        },
      }),
    );
    expect(frame.sequence).toBe(8);
    expect(frame.sweep?.id).toBe('source/1');
    expect(frame.invalidations).toEqual([]);
  });
  it('rejects unknown contracts and malformed measurements instead of rendering invented data', () => {
    expect(() => parseFrame('{"contract_version":2,"data":{}}')).toThrow();
    expect(() =>
      parseFrame(
        JSON.stringify({
          contract: 'openradioinspector-platform',
          contract_version: 1,
          data: {
            sequence: 1,
            event_type: 'sweep.received',
            payload: { sweep: { ...sweep, levels: [] } },
          },
        }),
      ),
    ).toThrow();
  });
  it('detects global sequence gaps, ignores replay duplicates, and requires an explicit snapshot reset', () => {
    const gate = new SequenceGate(10);
    expect(gate.accept(10)).toBe('duplicate');
    expect(gate.accept(11)).toBe('accept');
    expect(gate.accept(14)).toBe('gap');
    expect(gate.last).toBe(11);
    expect(gate.accept(15)).toBe('gap');
    gate.reset(14);
    expect(gate.accept(15)).toBe('accept');
  });
  it('bounds render memory and clears old data when a project or stream changes', () => {
    const buffer = new FrameBuffer(3);
    buffer.scope('s', 'a');
    for (let i = 0; i < 10; i++) buffer.append(decimate({ ...sweep, id: `s${i}` }, 4));
    buffer.append(decimate({ ...sweep, id: 'wrong', session_id: 'other' }, 4));
    expect(buffer.rows.map((x) => x.id)).toEqual(['s7', 's8', 's9']);
    buffer.scope('other', 'a');
    expect(buffer.rows).toEqual([]);
  });
  it('keeps a one-row render buffer bounded and rejects non-positive capacities', () => {
    const buffer = new FrameBuffer(1);
    buffer.scope('s', 'a');
    buffer.append(decimate(sweep));
    buffer.append(decimate({ ...sweep, id: 'newer' }));
    expect(buffer.rows.map((row) => row.id)).toEqual(['newer']);
    expect(() => new FrameBuffer(0)).toThrow();
  });
});
