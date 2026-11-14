// Canonical standalone codec for contracts/protobuf/measurement_stream.proto.
// This module adds no socket, archive cursor, authentication or reconnect policy.
import {CONTRACT_NAME, Float64, float64, normalizeUTC, parseJSON, type JSONObject, type JSONValue} from './codec.ts';

export const PROTOCOL_VERSION = 1;
export const MAX_FRAME_BYTES = 16 * 1024 * 1024 + 64 * 1024;
export const MAX_POINTS = 1_000_000;
export interface StreamLimits {maxFrameBytes: number; maxPoints: number;}
export interface SpectrumArrays {frequenciesHz: BigUint64Array; levels: Float64Array;}
export interface DecodedFrame {
  protocolVersion: number;
  sequence: bigint;
  /** Original metadata bytes; transfer these across workers to preserve Float64. */
  envelopeJSON: Uint8Array;
  envelope: JSONValue;
  spectrum?: SpectrumArrays;
}

function invalid(reason: string): never {throw new TypeError('Invalid measurement stream: '+reason);}
function budgets(input: Partial<StreamLimits>): StreamLimits {
  const result={maxFrameBytes:input.maxFrameBytes??MAX_FRAME_BYTES,maxPoints:input.maxPoints??MAX_POINTS};
  if (!Number.isSafeInteger(result.maxFrameBytes)||result.maxFrameBytes<1||result.maxFrameBytes>MAX_FRAME_BYTES||
      !Number.isSafeInteger(result.maxPoints)||result.maxPoints<0||result.maxPoints>MAX_POINTS) invalid('limits may only lower the protocol caps');
  return result;
}

class Reader {
  position=0;
  readonly bytes: Uint8Array;
  constructor(bytes: Uint8Array) {this.bytes=bytes;}
  get done(): boolean {return this.position===this.bytes.length;}
  varint(): bigint {
    let value=0n;
    for (let index=0;index<10;index++) {
      const byte=this.bytes[this.position++];
      if (byte===undefined||(index===9&&byte>1)) invalid('truncated or overflowing varint');
      value|=BigInt(byte&127)<<BigInt(index*7);
      if (byte<128)return value;
    }
    return invalid('unterminated varint');
  }
  tag(): [number,number] {
    const key=this.varint();
    if (key<8n||key>0xffffffffn) invalid('invalid protobuf field tag');
    return [Number(key>>3n),Number(key&7n)];
  }
  take(length: number): Uint8Array {
    if (length>this.bytes.length-this.position) invalid('truncated field');
    const result=this.bytes.subarray(this.position,this.position+length);this.position+=length;return result;
  }
  blob(): Uint8Array {
    const length=this.varint();
    if (length>BigInt(this.bytes.length-this.position)) invalid('truncated length-delimited field');
    return this.take(Number(length)); // proved <= remaining bounded frame bytes
  }
  skip(wire: number): void {
    switch(wire) {
      case 0:this.varint();return;
      case 1:this.take(8);return;
      case 2:this.blob();return;
      case 5:this.take(4);return;
      default:invalid('unsupported protobuf wire type');
    }
  }
  double(): number {const bytes=this.take(8);return new DataView(bytes.buffer,bytes.byteOffset,8).getFloat64(0,true);}
}

function walkSpectrum(bytes: Uint8Array, frequency: (value:bigint)=>void, level: (value:number)=>void): void {
  const reader=new Reader(bytes);
  while(!reader.done) {
    const [field,wire]=reader.tag();
    if (field===1) {
      if (wire===0)frequency(reader.varint());
      else if (wire===2) {const packed=new Reader(reader.blob());while(!packed.done)frequency(packed.varint());}
      else invalid('frequency field wire type');
    } else if (field===2) {
      if (wire===1)level(reader.double());
      else if (wire===2) {
        const packed=new Reader(reader.blob());
        if (packed.bytes.length%8!==0) invalid('packed double length');
        while(!packed.done)level(packed.double());
      } else invalid('level field wire type');
    } else reader.skip(wire);
  }
}

function spectrum(bytes: Uint8Array, maxPoints: number): SpectrumArrays {
  let frequencyCount=0,levelCount=0,previous=0n;
  walkSpectrum(bytes,value=>{
    if (++frequencyCount>maxPoints)invalid('point budget');
    if (frequencyCount>1&&value<previous)invalid('unordered frequencies');
    previous=value;
  },value=>{
    if (++levelCount>maxPoints)invalid('point budget');
    if (!Number.isFinite(value))invalid('nonfinite level');
  });
  if (frequencyCount===0||frequencyCount!==levelCount)invalid('empty or unequal arrays');
  const result={frequenciesHz:new BigUint64Array(frequencyCount),levels:new Float64Array(levelCount)};
  let fi=0,li=0;
  walkSpectrum(bytes,value=>{result.frequenciesHz[fi++]=value;},value=>{result.levels[li++]=value;});
  return result;
}

function object(value: JSONValue|undefined): JSONObject {
  if(value===null||typeof value!=='object'||Array.isArray(value)||value instanceof Float64)invalid('expected object');
  return value as JSONObject;
}
function string(value: JSONValue|undefined): string {if(typeof value!=='string')invalid('expected string');return value as string;}
function timestamp(value: JSONValue|undefined): void {normalizeUTC(string(value));}

/** The SDK preserves values but safe integral float tokens share JS's number
 * type. Inspect only the two header token paths after strict JSON validation;
 * never stringify/reparse arrays, or infer numeric kind from arbitrary objects. */
function headerTokens(text: string): {version:string;sequence:string} {
  let position=0,version='',sequence='';
  const space=():void=>{while(/[\t\n\r ]/.test(text[position]??'!'))position++;};
  const quote=():string=>{
    const start=position++;
    while(position<text.length) {const char=text[position++];if(char==='\\')position++;else if(char==='"')return JSON.parse(text.slice(start,position)) as string;}
    return invalid('unterminated validated string');
  };
  const skip=():void=>{
    space();const char=text[position];
    if(char==='"'){quote();return;}
    if(char==='{'||char==='['){
      const close=char==='{'?'}':']';position++;space();
      while(text[position]!==close){if(char==='{'){quote();space();position++;}skip();space();if(text[position]===','){position++;space();}else break;}
      position++;return;
    }
    while(position<text.length&&!/[\s,\]}]/.test(text[position]!))position++;
  };
  const token=():string=>{space();const start=position;skip();return text.slice(start,position);};
  const fields=(data:boolean):void=>{
    space();if(text[position++]!=='{')return invalid('expected header object');space();
    while(text[position]!=='}'){
      const key=quote();space();position++;space();
      if(!data&&key==='contract_version')version=token();
      else if(!data&&key==='data')fields(true);
      else if(data&&key==='sequence')sequence=token();
      else skip();
      space();if(text[position]===','){position++;space();}else break;
    }
    position++;
  };
  fields(false);
  if(!/^(?:0|[1-9][0-9]*)$/.test(version)||! /^(?:0|[1-9][0-9]*)$/.test(sequence))invalid('headers require exact integer tokens');
  return {version,sequence};
}

function validate(envelope: JSONValue, text: string, sequence: bigint, typed: boolean, maxPoints: number): void {
  const root=object(envelope), data=object(root.data), payload=object(data.payload), tokens=headerTokens(text);
  if(root.contract!==CONTRACT_NAME||tokens.version!=='1')invalid('unsupported contract');
  if(BigInt(tokens.sequence)!==sequence||sequence===0n)invalid('sequence mismatch');
  const kind=string(data.event_type);timestamp(data.occurred_at);
  if(data.session_id!==undefined&&data.session_id!==null)string(data.session_id);
  if(typed&&kind!=='sweep.received')invalid('spectrum requires sweep.received');
  if(kind==='sweep.received') {
    const sweep=object(payload.sweep);
    for(const key of ['id','session_id','stream_id','level_unit'])string(sweep[key]);
    const interval=object(sweep.interval);timestamp(interval.started_at);timestamp(interval.ended_at);
    if(sweep.settings!==undefined&&sweep.settings!==null)object(sweep.settings);
    if(typed) {
      if(Object.hasOwn(sweep,'frequencies_hz')||Object.hasOwn(sweep,'levels'))invalid('ambiguous metadata arrays');
    } else {
      const frequencies=sweep.frequencies_hz,levels=sweep.levels;
      if(!Array.isArray(frequencies)||!Array.isArray(levels)||frequencies.length===0||frequencies.length!==levels.length)invalid('sweep arrays');
      if(frequencies.length>maxPoints)invalid('point budget');
      for(const values of [frequencies,levels])for(const value of values)if(typeof value!=='number'&&typeof value!=='bigint'&&!(value instanceof Float64))invalid('numeric sweep arrays');
    }
  }
}

/** Bounded decoder. No point array is JSON-stringified, and Hz always use u64. */
export function decodeFrame(bytes: Uint8Array, limits: Partial<StreamLimits> = {}): DecodedFrame {
  const cap=budgets(limits);
  if(!(bytes instanceof Uint8Array)||bytes.length>cap.maxFrameBytes)invalid('frame byte budget');
  const reader=new Reader(bytes),seen=new Set<number>();
  let version:bigint|undefined,sequence:bigint|undefined,json:Uint8Array|undefined,packed:Uint8Array|undefined;
  while(!reader.done) {
    const [field,wire]=reader.tag();
    if(field<=4) {
      if(seen.has(field))invalid('duplicate unique field');seen.add(field);
      if((field<=2&&wire!==0)||(field>=3&&wire!==2))invalid('header field wire type');
      if(field===1)version=reader.varint();
      else if(field===2)sequence=reader.varint();
      else if(field===3)json=reader.blob();
      else packed=reader.blob();
    } else reader.skip(wire);
  }
  if(version!==1n||sequence===undefined||sequence===0n||!json?.length)invalid('missing or unsupported header');
  const arrays=packed===undefined?undefined:spectrum(packed,cap.maxPoints);
  // Preserve a BOM as a character so strict JSON rejects it, matching Rust and
  // Swift. TextDecoder's default silently strips this non-JSON prefix.
  const text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(json);
  const envelope=parseJSON(text,cap.maxFrameBytes);
  validate(envelope,text,sequence,arrays!==undefined,cap.maxPoints);
  return {protocolVersion:1,sequence,envelopeJSON:Uint8Array.from(json),envelope,...(arrays?{spectrum:arrays}:{})};
}

/** Optional structured reconstruction. Render/worker paths can use typed arrays
 * directly. Exact JSON serialization uses SDK Float64 markers for these doubles. */
export function intoEnvelope(frame: DecodedFrame): JSONValue {
  if(!frame.spectrum)return frame.envelope;
  const root=object(frame.envelope),data=object(root.data),payload=object(data.payload),sweep=object(payload.sweep);
  const result={...root,data:{...data,payload:{...payload,sweep:{...sweep,
    frequencies_hz:Array.from(frame.spectrum.frequenciesHz,value=>value<=BigInt(Number.MAX_SAFE_INTEGER)?Number(value):value),
    levels:Array.from(frame.spectrum.levels,float64)
  }}}};
  return result;
}
