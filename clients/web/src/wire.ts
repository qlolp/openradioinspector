import {
  Float64,
  decodeFrequencyBookmarkPage,
  decodeFrequencyBookmarkSet,
  decodeReceiverObservation,
  decodeSession,
  decodeSweep,
  encodeSweep,
  encodeReceiverObservation,
  type ReceiverObservation as ContractObservation,
  parseJSON,
  stringifyJSON,
  encodePathSegment,
  normalizeUTC,
  type JSONValue,
  type JSONObject,
  type Sweep as ContractSweep,
} from '../../../contracts/generated/typescript/index.ts';
import { decodeReceiverPass } from './receiver-panorama';
import { decodeMarkerReading, decodePassResponse } from './rf-pass';
import type { Observation, Sweep } from './types';
import {
  decodeReceiverPanorama,
  decodePairedRecheckResult,
} from '../../../contracts/generated/typescript/runtime';
export { stringifyJSON, encodePathSegment, normalizeUTC };
const MAX_JSON = 8_388_608;
function object(value: JSONValue): JSONObject {
  if (!value || typeof value !== 'object' || Array.isArray(value) || value instanceof Float64)
    throw Error('Expected an API object');
  return value;
}
function array(value: JSONValue): JSONValue[] {
  if (!Array.isArray(value)) throw Error('Expected an API array');
  return value;
}
// Adapters run only at known API/event positions. Metadata is never guessed by shape.
export const sweepValue = (value: JSONValue): Sweep => decodeSweep(value) as unknown as Sweep;
export const observationValue = (value: JSONValue): Observation =>
  decodeReceiverObservation(value) as unknown as Observation;
export const originalObservationJSON = (value: Observation) =>
  encodeReceiverObservation(value as unknown as ContractObservation);
export const originalJSON = (value: Sweep) => encodeSweep(value as unknown as ContractSweep);
export function parseWire(raw: string | Uint8Array): JSONValue {
  return parseJSON(raw, MAX_JSON);
}
function numbers(value: JSONValue, keys: string[]): JSONObject {
  const record = { ...object(value) };
  for (const key of keys) {
    const number = record[key];
    if (number instanceof Float64) record[key] = number.value;
  }
  return record;
}
function eventValue(value: JSONValue): JSONObject {
  const event = numbers(value, ['peak_level']);
  if (event.analysis) {
    const analysis = numbers(event.analysis, ['level']);
    if (Array.isArray(analysis.receiver_status))
      analysis.receiver_status = analysis.receiver_status.map(
        observationValue,
      ) as unknown as JSONValue;
    event.analysis = analysis;
  }
  return event;
}
export function responseValue(path: string, raw: string | Uint8Array): unknown {
  const envelope = object(parseWire(raw));
  if (!Object.hasOwn(envelope, 'data')) return envelope;
  const route = path.split('?')[0];
  let data = envelope.data;
  if (/^\/api\/v1\/sessions\/[^/]+\/streams\/[^/]+\/receiver-panorama$/.test(route)) {
    const selected = new URLSearchParams(path.split('?')[1]).get('pass');
    return {
      ...envelope,
      data: decodeReceiverPass(
        decodeReceiverPanorama(data),
        selected === null ? undefined : Number(selected),
      ),
    };
  }
  if (/^\/api\/v1\/sessions\/[^/]+\/streams\/[^/]+\/receiver-panorama\/point$/.test(route))
    return { ...envelope, data: observationValue(data) };
  if (/^\/api\/v1\/sessions\/[^/]+\/frequency-bookmarks$/.test(route)) {
    const page = object(data);
    return {
      ...envelope,
      data: Object.hasOwn(page, 'items')
        ? decodeFrequencyBookmarkPage(page)
        : decodeFrequencyBookmarkSet(page),
    };
  }
  if (
    /^\/api\/v1\/sessions\/[^/]+\/streams\/[^/]+\/rf-explorer\/passes\/[^/]+\/point$/.test(route)
  ) {
    const frequency = new URLSearchParams(path.split('?')[1]).get('frequency_hz');
    return {
      ...envelope,
      data: decodeMarkerReading(data, frequency === null ? NaN : Number(frequency)),
    };
  }
  const passRoute =
    /^\/api\/v1\/sessions\/[^/]+\/streams\/[^/]+\/rf-explorer\/passes(?:\/([^/]+)(\/windows)?)?$/.exec(
      route,
    );
  if (passRoute)
    return {
      ...envelope,
      data: decodePassResponse(
        data,
        passRoute[1] && decodeURIComponent(passRoute[1]),
        !!passRoute[2],
      ),
    };
  if (/^\/api\/v1\/sessions\/[^/]+\/sweeps\/[^/]+$/.test(route))
    return { ...envelope, data: sweepValue(data) };
  if (/^\/api\/v1\/sessions\/[^/]+\/sweeps$/.test(route))
    data = array(data).map((value) => sweepValue(value)) as unknown as JSONValue;
  else if (/^\/api\/v1\/sessions\/[^/]+\/observations$/.test(route))
    data = array(data).map((value) => observationValue(value)) as unknown as JSONValue;
  else if (/^\/api\/v1\/sessions\/[^/]+\/observations\/[^/]+$/.test(route))
    data = observationValue(data) as unknown as JSONValue;
  else if (route === '/api/v1/sessions' && Array.isArray(data))
    data = data.map((value) => decodeSession(value)) as unknown as JSONValue;
  else if (/^\/api\/v1\/monitoring-projects\/[^/]+\/history$/.test(route)) {
    const page = object(data);
    data = { ...page, items: array(page.items).map(sweepValue) } as unknown as JSONValue;
  } else if (/^\/api\/v1\/monitoring-projects\/[^/]+\/history\/[^/]+$/.test(route))
    data = sweepValue(data) as unknown as JSONValue;
  else if (/^\/api\/v1\/monitoring-projects\/[^/]+\/events$/.test(route)) {
    const page = object(data);
    data = { ...page, items: array(page.items).map(eventValue) };
  } else if (
    /^\/api\/v1\/monitoring-projects\/[^/]+\/events\/[^/]+(?:\/(?:disposition|review))?$/.test(
      route,
    )
  )
    data = eventValue(data);
  else if (/^\/api\/v1\/monitoring-projects\/[^/]+\/events\/[^/]+\/paired-rechecks$/.test(route)) {
    const report = decodePairedRecheckResult(data);
    const parts = route.split('/');
    if (
      report.project_id !== decodeURIComponent(parts[4]) ||
      report.event_id !== decodeURIComponent(parts[6])
    )
      throw Error('Source evidence mismatch');
    if (
      report.state === 'evaluated' &&
      (report.frequency_hz == null ||
        !report.instrument ||
        report.methodology !== 'paired-local-recheck-v1' ||
        typeof report.simulated !== 'boolean' ||
        report.repeat_total !== report.visits.length ||
        report.signal_repeats == null ||
        report.discrepancy_repeats == null)
    )
      throw Error('Incomplete paired evidence');
    data = report as unknown as JSONValue;
  } else if (/^\/api\/v1\/sessions\/[^/]+\/sweep-summaries$/.test(route)) {
    const page = object(data);
    data = { ...page, items: array(page.items).map((value) => numbers(value, ['peak_level'])) };
  }
  return { ...envelope, data };
}
