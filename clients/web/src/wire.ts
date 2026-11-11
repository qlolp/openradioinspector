import {
  Float64,
  decodeReceiverObservation,
  decodeSession,
  decodeSweep,
  encodeSweep,
  parseJSON,
  stringifyJSON,
  encodePathSegment,
  normalizeUTC,
  type JSONValue,
  type JSONObject,
  type Sweep as ContractSweep,
} from '../../../contracts/generated/typescript/index.ts';
import type { Observation, Sweep } from './types';
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
  if (event.analysis) event.analysis = numbers(event.analysis, ['level']);
  return event;
}
export function responseValue(path: string, raw: string | Uint8Array): unknown {
  const envelope = object(parseWire(raw));
  if (!Object.hasOwn(envelope, 'data')) return envelope;
  const route = path.split('?')[0];
  let data = envelope.data;
  if (/^\/api\/v1\/sessions\/[^/]+\/sweeps$/.test(route))
    data = array(data).map((value) => sweepValue(value)) as unknown as JSONValue;
  else if (/^\/api\/v1\/sessions\/[^/]+\/observations$/.test(route))
    data = array(data).map((value) => observationValue(value)) as unknown as JSONValue;
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
  } else if (/^\/api\/v1\/monitoring-projects\/[^/]+\/events\/[^/]+(?:\/disposition)?$/.test(route))
    data = eventValue(data);
  else if (/^\/api\/v1\/sessions\/[^/]+\/sweep-summaries$/.test(route)) {
    const page = object(data);
    data = { ...page, items: array(page.items).map((value) => numbers(value, ['peak_level'])) };
  }
  return { ...envelope, data };
}
