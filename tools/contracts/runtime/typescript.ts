import {schemas} from './schema.ts';

export type Integer = number | bigint;
/** Explicit float64 marker for integral-valued floats outside JS safe integers. */
export class Float64 {
  readonly value: number;
  constructor(value: number) {
    if (!Number.isFinite(value)) throw new TypeError('Nonfinite float64');
    this.value = value;
  }
}
export const float64 = (value: number): Float64 => new Float64(value);
export type JSONValue = null | boolean | string | number | bigint | Float64 | JSONValue[] | JSONObject;
export interface JSONObject {[key: string]: JSONValue;}
export const CONTRACT_NAME = 'openradioinspector-platform';
export const CONTRACT_VERSION = 1;
export const DEFAULT_MAX_BYTES = 16 * 1024 * 1024;

function validUnicode(text: string): void {
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) throw new SyntaxError('Unpaired UTF-16 surrogate');
    } else if (code >= 0xdc00 && code <= 0xdfff) throw new SyntaxError('Unpaired UTF-16 surrogate');
  }
}
function put(object: JSONObject, key: string, value: JSONValue): void {
  Object.defineProperty(object, key, {value, enumerable: true, writable: true, configurable: true});
}

/** Parse before dispatch/mutation. Duplicate names are rejected at every depth;
 * large integer tokens become bigint, without passing through binary64. */
export function parseJSON(input: string | Uint8Array, maxBytes = DEFAULT_MAX_BYTES): JSONValue {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 0) throw new RangeError('Invalid byte limit');
  if (input instanceof Uint8Array && input.byteLength > maxBytes) throw new RangeError('JSON body too large');
  const text = typeof input === 'string' ? input : new TextDecoder('utf-8', {fatal: true}).decode(input);
  validUnicode(text);
  if (new TextEncoder().encode(text).length > maxBytes) throw new RangeError('JSON body too large');
  let i = 0;
  const space = (): void => { while (i < text.length && /[ \t\r\n]/.test(text[i]!)) i++; };
  const string = (): string => {
    const start = i++;
    for (; i < text.length; i++) {
      const char = text[i]!;
      if (char === '\\') { i++; continue; }
      if (char === '"') {
        const value = JSON.parse(text.slice(start, ++i)) as string;
        validUnicode(value);
        return value;
      }
    }
    throw new SyntaxError('Unterminated JSON string');
  };
  const value = (depth: number): JSONValue => {
    if (depth > 128) throw new RangeError('JSON nesting exceeds 128');
    space();
    if (text[i] === '"') return string();
    if (text[i] === '{') {
      i++; space();
      const result: JSONObject = {}, names = new Set<string>();
      if (text[i] === '}') { i++; return result; }
      while (true) {
        space(); if (text[i] !== '"') throw new SyntaxError('Expected object key');
        const key = string();
        if (names.has(key)) throw new SyntaxError('Duplicate JSON key: ' + key);
        names.add(key); space();
        if (text[i++] !== ':') throw new SyntaxError('Expected colon');
        put(result, key, value(depth+1)); space();
        const next = text[i++]; if (next === '}') return result;
        if (next !== ',') throw new SyntaxError('Expected object separator');
      }
    }
    if (text[i] === '[') {
      i++; space(); const result: JSONValue[] = [];
      if (text[i] === ']') { i++; return result; }
      while (true) {
        result.push(value(depth+1)); space();
        const next = text[i++]; if (next === ']') return result;
        if (next !== ',') throw new SyntaxError('Expected array separator');
      }
    }
    for (const [token, primitive] of [['true',true],['false',false],['null',null]] as const) {
      if (text.startsWith(token,i)) { i+=token.length; return primitive; }
    }
    const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(text.slice(i));
    if (!match) throw new SyntaxError('Invalid JSON value');
    const token = match[0]; i+=token.length;
    if (!/[.eE]/.test(token)) {
      if (token === '-0') return 0;
      const integer = BigInt(token);
      return integer >= BigInt(Number.MIN_SAFE_INTEGER) && integer <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(integer) : integer;
    }
    const number = Number(token);
    if (!Number.isFinite(number)) throw new SyntaxError('Nonfinite JSON number');
    return Number.isInteger(number) && !Number.isSafeInteger(number) ? new Float64(number) : number;
  };
  const result = value(0); space();
  if (i !== text.length) throw new SyntaxError('Trailing JSON data');
  return result;
}

/** Explicit finite-only wire serialization. JSON.stringify alone erases -0.
 * Supply bigint for large metadata integers, Float64 for large integral floats,
 * or a generated encodeModel helper for known double fields. */
export function stringifyJSON(input: unknown): string {
  const ancestors = new Set<object>();
  const number = (value: number, allowIntegralFloat = false): string => {
    if (!Number.isFinite(value)) throw new TypeError('Nonfinite JSON number');
    if (!allowIntegralFloat && Number.isInteger(value) && !Number.isSafeInteger(value)) throw new TypeError('Unsafe integer number; supply bigint or float64(value)');
    return Object.is(value,-0) ? '-0.0' : String(value);
  };
  const write = (value: unknown, depth: number): string => {
    if (depth > 128) throw new RangeError('JSON nesting exceeds 128');
    if (value === null) return 'null';
    if (typeof value === 'string') { validUnicode(value); return JSON.stringify(value); }
    if (typeof value === 'number') return number(value);
    if (typeof value === 'bigint') return String(value);
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (value instanceof Float64) {
      const result = number(value.value,true);
      // Keep the float interpretation on a later parse when the value is integral.
      return /[.eE]/.test(result) ? result : result + '.0';
    }
    if (typeof value !== 'object') throw new TypeError('Unsupported JSON value');
    if (ancestors.has(value)) throw new TypeError('Cyclic JSON value');
    ancestors.add(value);
    try {
      if (Array.isArray(value)) {
        const entries: string[] = [];
        for (let i=0;i<value.length;i++) {
          if (!Object.hasOwn(value,i)) throw new TypeError('Sparse JSON array');
          entries.push(write(value[i],depth+1));
        }
        return '[' + entries.join(',') + ']';
      }
      const proto = Object.getPrototypeOf(value);
      if (proto !== Object.prototype && proto !== null) throw new TypeError('Unsupported JSON object');
      return '{' + Object.keys(value).map(key => {
        validUnicode(key);
        const descriptor = Object.getOwnPropertyDescriptor(value,key)!;
        if (!('value' in descriptor)) throw new TypeError('JSON accessor property');
        return JSON.stringify(key) + ':' + write(descriptor.value,depth+1);
      }).join(',') + '}';
    } finally { ancestors.delete(value); }
  };
  return write(input,0);
}

/** Separate integer seconds/calendar arithmetic from the six microsecond digits. */
export function normalizeUTC(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?(Z|[+-]\d{2}:?\d{2})?$/.exec(value);
  if (!match || match[0] !== value) throw new TypeError('Invalid ISO timestamp');
  const year=Number(match[1]), month=Number(match[2]), day=Number(match[3]), hour=Number(match[4]??0), minute=Number(match[5]??0), second=Number(match[6]??0);
  if (year<1 || month<1 || month>12 || day<1 || hour>23 || minute>59 || second>59) throw new TypeError('Invalid ISO timestamp');
  const date = new Date(0); date.setUTCFullYear(year,month-1,day); date.setUTCHours(hour,minute,second,0);
  if (date.getUTCFullYear()!==year || date.getUTCMonth()!==month-1 || date.getUTCDate()!==day) throw new TypeError('Invalid calendar date');
  const zone = match[8];
  if (zone && zone!=='Z') {
    const digits=zone.slice(1).replace(':',''), zh=Number(digits.slice(0,2)), zm=Number(digits.slice(2,4));
    if (zh>23 || zm>59) throw new TypeError('Invalid timestamp offset');
    date.setTime(date.getTime()-(zone[0]==='+'?1:-1)*(zh*60+zm)*60000);
  }
  if (date.getUTCFullYear()<1 || date.getUTCFullYear()>9999) throw new TypeError('UTC year outside four-digit range');
  return date.toISOString().slice(0,19)+'.'+(match[7]??'').slice(0,6).padEnd(6,'0')+'Z';
}

interface Schema {type?: string | readonly string[]; $ref?: string; format?: string; properties?: {[key:string]: Schema}; required?: readonly string[]; items?: Schema; enum?: readonly JSONValue[]; const?: JSONValue; minimum?: number; minLength?: number;}
function walk(schema: Schema, value: JSONValue, encode: boolean): JSONValue {
  if (schema.$ref) return walk((schemas as unknown as Record<string,Schema>)[schema.$ref.split('/').at(-1)!]!,value,encode);
  if (schema.const !== undefined && value !== schema.const) throw new TypeError('Unsupported contract value');
  if (value===null && Array.isArray(schema.type) && schema.type.includes('null')) return null;
  const type = Array.isArray(schema.type) ? schema.type.find(t=>t!=='null') : schema.type;
  if (schema.enum && !schema.enum.includes(value)) throw new TypeError('Invalid enum value');
  if (type==='object') {
    if (typeof value!=='object' || value===null || Array.isArray(value) || value instanceof Float64) throw new TypeError('Expected object');
    for (const key of schema.required??[]) if (!Object.hasOwn(value,key)) throw new TypeError('Missing '+key);
    const result: JSONObject = {};
    for (const [key,item] of Object.entries(value)) put(result,key,schema.properties?.[key] ? walk(schema.properties[key]!,item,encode) : item);
    return result;
  }
  if (type==='array') { if (!Array.isArray(value)) throw new TypeError('Expected array'); return value.map(v=>walk(schema.items??{},v,encode)); }
  if (type==='string') {
    if (typeof value!=='string' || value.length<(schema.minLength??0)) throw new TypeError('Expected string');
    validUnicode(value); return schema.format==='date-time' ? normalizeUTC(value) : value;
  }
  if (type==='integer') {
    if (!(typeof value==='bigint' || typeof value==='number' && Number.isSafeInteger(value))) throw new TypeError('Expected exact integer');
    if (schema.minimum!==undefined && value<BigInt(schema.minimum)) throw new TypeError('Integer below minimum');
  }
  if (type==='number') {
    const n=value instanceof Float64?value.value:typeof value==='bigint'?Number(value):value;
    if (typeof n!=='number' || !Number.isFinite(n)) throw new TypeError('Expected finite float64');
    return encode?new Float64(n):n;
  }
  if (type==='boolean' && typeof value!=='boolean') throw new TypeError('Expected boolean');
  return value;
}
export function decodeModel(name: keyof typeof schemas, value: JSONValue): JSONValue { return walk(schemas[name] as unknown as Schema,value,false); }
export function encodeModel(name: keyof typeof schemas, value: unknown): string { return stringifyJSON(walk(schemas[name] as unknown as Schema,value as JSONValue,true)); }

export function encodePathSegment(id: string): string {
  validUnicode(id);
  return encodeURIComponent(id).replace(/[!'()*]/g,char=>'%'+char.charCodeAt(0).toString(16).toUpperCase());
}
export function operationPath(template: string, parameters: Record<string,string>): string {
  return template.replace(/\{([^}]+)\}/g,(_,key: string)=> {
    if (!Object.hasOwn(parameters,key)) throw new TypeError('Missing path parameter '+key);
    return encodePathSegment(parameters[key]!);
  });
}
