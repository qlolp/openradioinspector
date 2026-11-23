export interface ReceiverBin {
  start_index: number;
  end_index: number;
  measured_points: number;
  frequency_hz: number;
  level: number;
  observation_id: string;
  started_at: string;
  ended_at: string;
}
export interface ReceiverPass {
  pass: number;
  context_id?: string;
  start_hz: number;
  stop_hz: number;
  step_hz: number;
  total_points: number;
  measured_points: number;
  coverage_hex: string;
  coverage: Uint8Array;
  complete: boolean;
  lower_is_stronger: true;
  level_unit: 's_meter_count';
  bins: ReceiverBin[];
  started_at: string;
  ended_at: string;
}

export const frequencyAt = (p: ReceiverPass, i: number) =>
  Math.min(p.stop_hz, p.start_hz + i * p.step_hz);

const measured = (mask: Uint8Array, index: number) => (mask[index >> 3] & (1 << (index % 8))) !== 0;

export function decodeReceiverPass(
  value: unknown,
  selectedPass?: number,
  selectedContext?: string,
) {
  const invalid = () => Error('Invalid receiver panorama evidence');
  if (!value || typeof value !== 'object') throw invalid();
  const p = value as ReceiverPass;
  if (
    p.level_unit !== 's_meter_count' ||
    p.lower_is_stronger !== true ||
    ![p.pass, p.start_hz, p.stop_hz, p.step_hz, p.total_points, p.measured_points].every(
      Number.isSafeInteger,
    ) ||
    p.pass < 1 ||
    (selectedPass !== undefined && p.pass !== selectedPass) ||
    (p.context_id !== undefined && !/^[0-9a-f]{64}$/.test(p.context_id)) ||
    (selectedContext !== undefined && p.context_id !== selectedContext) ||
    p.start_hz < 100_000 ||
    p.stop_hz > 1_300_000_000 ||
    p.start_hz >= p.stop_hz ||
    p.step_hz < 10 ||
    p.total_points !== Math.ceil((p.stop_hz - p.start_hz) / p.step_hz) + 1 ||
    p.total_points > 129991 ||
    p.measured_points < 0 ||
    p.measured_points > p.total_points ||
    p.complete !== (p.measured_points === p.total_points) ||
    !Array.isArray(p.bins) ||
    p.bins.length > 1024
  )
    throw invalid();
  if (
    typeof p.coverage_hex !== 'string' ||
    p.coverage_hex.length !== Math.ceil(p.total_points / 8) * 2 ||
    !/^[0-9a-f]+$/.test(p.coverage_hex)
  )
    throw invalid();
  const mask = new Uint8Array(p.coverage_hex.length / 2);
  for (let i = 0; i < mask.length; i++)
    mask[i] = parseInt(p.coverage_hex.slice(i * 2, i * 2 + 2), 16);
  let bits = 0;
  for (const byte of mask) {
    let word = byte;
    while (word) {
      bits++;
      word &= word - 1;
    }
  }
  if (bits !== p.measured_points) throw invalid();
  let last = -1,
    sum = 0;
  for (const b of p.bins) {
    if (
      !b ||
      ![b.start_index, b.end_index, b.measured_points, b.frequency_hz].every(
        Number.isSafeInteger,
      ) ||
      b.start_index <= last ||
      b.end_index < b.start_index ||
      b.end_index >= p.total_points ||
      b.measured_points < 1 ||
      b.measured_points > b.end_index - b.start_index + 1 ||
      b.frequency_hz < frequencyAt(p, b.start_index) ||
      b.frequency_hz > frequencyAt(p, b.end_index) ||
      (b.frequency_hz !== p.stop_hz && (b.frequency_hz - p.start_hz) % p.step_hz !== 0) ||
      !Number.isFinite(b.level) ||
      typeof b.observation_id !== 'string' ||
      !b.observation_id
    )
      throw invalid();
    const originalIndex =
      b.frequency_hz === p.stop_hz ? p.total_points - 1 : (b.frequency_hz - p.start_hz) / p.step_hz;
    if (!measured(mask, originalIndex)) throw invalid();
    let binCount = 0;
    for (let i = b.start_index; i <= b.end_index; i++) if (measured(mask, i)) binCount++;
    if (binCount !== b.measured_points) throw invalid();
    last = b.end_index;
    sum += b.measured_points;
  }
  if (sum !== p.measured_points) throw invalid();
  return { ...p, coverage: mask };
}

/** The archive mask stores one bit per frequency, low bit first. */
export function measuredRuns(p: ReceiverPass, b: ReceiverBin): [number, number][] {
  if (b.measured_points === b.end_index - b.start_index + 1) return [[b.start_index, b.end_index]];
  const runs: [number, number][] = [];
  for (let i = b.start_index; i <= b.end_index; i++) {
    if (!measured(p.coverage, i)) continue;
    const prev = runs.at(-1);
    if (prev && prev[1] === i - 1) prev[1] = i;
    else runs.push([i, i]);
  }
  return runs;
}
