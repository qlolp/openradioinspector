/** Optional single-process host diagnostic; do not run during shared CPU gates. */
import {performance} from 'node:perf_hooks';
import {decodeFrame} from '../../../../contracts/generated/typescript/measurement_stream.ts';
import {parseJSON} from '../../../../contracts/generated/typescript/codec.ts';
import {readFileSync} from 'node:fs';

// Rust's example produces the same synthetic 4096-point standard envelope and
// its library-encoded frame. This diagnostic accepts only those explicit files.
const [jsonFile,frameFile]=process.argv.slice(2);
if(!jsonFile||!frameFile)throw new TypeError('Usage: node benchmark.ts synthetic.json synthetic.protobuf');
const json=readFileSync(jsonFile),frame=readFileSync(frameFile);
let start=performance.now();
for(let index=0;index<20;index++)decodeFrame(frame);
const binaryMilliseconds=performance.now()-start;
start=performance.now();
for(let index=0;index<20;index++)parseJSON(json);
console.log(JSON.stringify({synthetic:true,points:4096,json_bytes:json.length,protobuf_bytes:frame.length,decode20_binary_ms:binaryMilliseconds,decode20_json_ms:performance.now()-start,acceptance:'host diagnostic only'}));
