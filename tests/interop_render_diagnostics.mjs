import assert from 'node:assert/strict';
const { installRenderDiagnostics } = await import(process.argv[2]);
let clock=0, identifier=0, observer;
const callbacks=new Map();
globalThis.performance={now:()=>clock,timeOrigin:1234};
globalThis.document={};
globalThis.requestAnimationFrame=callback=>{callbacks.set(++identifier,callback);return identifier;};
globalThis.cancelAnimationFrame=id=>callbacks.delete(id);
globalThis.MutationObserver=class {
  constructor(callback){this.callback=callback;observer=this;}
  observe(target,options){this.target=target;this.options=options;}
};
installRenderDiagnostics();
assert.equal(callbacks.size,0,'diagnostics must not request its own frames');
let delivered;
const id=requestAnimationFrame(stamp=>{delivered=stamp;clock=12;});
assert.equal(id,1);clock=10;callbacks.get(id)(9.5);callbacks.delete(id);
assert.equal(delivered,9.5,'native RAF timestamp preserved');
assert.equal(__oriRenderDiagnostics.raf[0].wait_ms,10);
assert.equal(__oriRenderDiagnostics.raf[0].duration_ms,2);
const cancelled=requestAnimationFrame(()=>{throw Error('cancelled callback ran');});
cancelAnimationFrame(cancelled);assert.equal(callbacks.size,0);
const target={tagName:'CANVAS',className:'spectrum',dataset:{sweepId:'new',renderFrame:'8'},
  isConnected:true,width:960,height:280};
observer.callback([{target,attributeName:'data-sweep-id',oldValue:'old'}]);
assert.equal(__oriRenderDiagnostics.markers[0].id,'new');
assert.equal(__oriRenderDiagnostics.markers[0].old_value,'old');
for(let i=0;i<300;i++){
  clock++;
  const next=requestAnimationFrame(()=>{});callbacks.get(next)(clock);callbacks.delete(next);
  observer.callback([{target,attributeName:'data-sweep-id',oldValue:String(i)}]);
}
assert.equal(__oriRenderDiagnostics.raf_count,301);
assert.equal(__oriRenderDiagnostics.raf.length,128);
assert.equal(__oriRenderDiagnostics.marker_count,301);
assert.equal(__oriRenderDiagnostics.markers.length,64);
assert.equal(callbacks.size,0);
console.log('native RAF and marker diagnostics preserve scheduling and bounds');
