import type { JSONValue } from '../../../contracts/generated/typescript/index.ts';
import {
  decodeRFExplorerPassPage,
  decodeRFExplorerMarkerReading,
  decodeRFExplorerPassPanorama,
  decodeRFExplorerPassWindows,
  type RFExplorerCoverage,
  type RFExplorerPassPoint,
  type RFExplorerPassSummary,
  type RFExplorerPassWindow,
} from '../../../contracts/generated/typescript/runtime';
import type { Interval } from './types';
import type { RfBounds } from './rf-explorer';

export interface PassSummary extends RfBounds, Interval {
  pass_id: string;
  pass: number;
  step_hz: number;
  tiles: number;
  received_tiles: number;
  unique_points: number;
  expected_points: number;
  complete: boolean;
  duration_s: number | null;
  revisit_s: number | null;
  active_board: string;
  active_input: string;
  rbw_hz: number;
  detector: string;
  offset_db: number;
  simulated: boolean;
}
export interface PassWindow extends Interval {
  sweep_id: string;
  tile: number;
}
export interface PassPoint extends PassWindow {
  point_index: number;
  frequency_hz: number;
  level_dbm: number;
  last_measured_at: string;
}
export interface PassDetail {
  summary: PassSummary;
  coverage: RfBounds[];
  columns: (PassPoint | null)[];
  hold_coverage: RfBounds[] | null;
  hold_columns: (PassPoint | null)[] | null;
}
export interface PassPage {
  items: PassSummary[];
  next_before: string | null;
}
export interface WindowPage {
  items: PassWindow[];
  next_after_tile: number | null;
}

function invalid(): never {
  throw Error('Invalid RF Explorer pass response');
}
function exact(value: number | bigint) {
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : invalid();
}
function bounds(value: RFExplorerCoverage): RfBounds {
  const start_hz = exact(value.start_hz),
    stop_hz = exact(value.stop_hz);
  return stop_hz >= start_hz ? { start_hz, stop_hz } : invalid();
}
function summary(s: RFExplorerPassSummary): PassSummary {
  const result = {
    ...s,
    ...bounds(s),
    pass: exact(s.pass),
    tiles: exact(s.tiles),
    received_tiles: exact(s.received_tiles),
    unique_points: exact(s.unique_points),
    expected_points: exact(s.expected_points),
    step_hz: exact(s.step_hz),
    rbw_hz: exact(s.rbw_hz),
    offset_db: exact(s.offset_db),
  };
  if (
    s.ended_at < s.started_at ||
    result.received_tiles > result.tiles ||
    result.unique_points > result.expected_points
  )
    return invalid();
  return result;
}
function passWindow(w: RFExplorerPassWindow): PassWindow {
  return w.ended_at >= w.started_at ? { ...w, tile: exact(w.tile) } : invalid();
}
export interface MarkerReading {
  current: PassPoint | null;
  held: PassPoint | null;
}
function originalPoint(p: RFExplorerPassPoint): PassPoint {
  return {
    ...p,
    ...passWindow(p),
    frequency_hz: exact(p.frequency_hz),
    point_index: exact(p.point_index),
  };
}
export function decodeMarkerReading(value: JSONValue, frequency: number): MarkerReading {
  const pair = decodeRFExplorerMarkerReading(value);
  const point = (p: RFExplorerPassPoint | null) => {
    if (!p) return null;
    const result = originalPoint(p);
    return result.frequency_hz === frequency ? result : invalid();
  };
  return { current: point(pair.current), held: point(pair.held) };
}
export function decodePassResponse(
  value: JSONValue,
  passId?: string,
  windows = false,
): PassPage | WindowPage | PassDetail {
  if (!passId) {
    const page = decodeRFExplorerPassPage(value);
    if (page.items.length > 100) return invalid();
    return { ...page, items: page.items.map(summary) };
  }
  if (windows) {
    const page = decodeRFExplorerPassWindows(value),
      items = page.items.map(passWindow);
    const next = page.next_after_tile === null ? null : exact(page.next_after_tile);
    if (
      items.length > 256 ||
      items.some((w, i) => i > 0 && w.tile <= items[i - 1].tile) ||
      (next !== null && next !== items.at(-1)?.tile)
    )
      return invalid();
    return { items, next_after_tile: next };
  }
  const detail = decodeRFExplorerPassPanorama(value),
    s = summary(detail.summary);
  if (
    s.pass_id !== passId ||
    !detail.columns.length ||
    detail.columns.length > 2048 ||
    (detail.hold_columns && detail.hold_columns.length !== detail.columns.length) ||
    (detail.hold_columns === null) !== (detail.hold_coverage === null)
  )
    return invalid();
  const point = (p: RFExplorerPassPoint | null): PassPoint | null => {
    if (!p) return null;
    const frequency_hz = exact(p.frequency_hz);
    if (
      frequency_hz < s.start_hz ||
      frequency_hz > s.stop_hz ||
      (frequency_hz - s.start_hz) % s.step_hz !== 0
    )
      return invalid();
    return originalPoint(p);
  };
  const ranges = (items: RFExplorerCoverage[]) => {
    const output = items.map(bounds);
    if (
      items.length > s.tiles ||
      output.some(
        (r, i) =>
          r.start_hz < s.start_hz ||
          r.stop_hz > s.stop_hz ||
          (i > 0 && r.start_hz <= output[i - 1].stop_hz),
      )
    )
      return invalid();
    return output;
  };
  return {
    summary: s,
    columns: detail.columns.map(point),
    coverage: ranges(detail.coverage),
    hold_columns: detail.hold_columns?.map(point) ?? null,
    hold_coverage: detail.hold_coverage && ranges(detail.hold_coverage),
  };
}
