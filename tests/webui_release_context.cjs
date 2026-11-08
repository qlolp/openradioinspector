const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../radioinspector/web/static/platform.js'), 'utf8');
function loadFunction(name, context) {
  const start = source.indexOf(`  function ${name}(`);
  assert(start >= 0, `Production function ${name} exists`);
  const end = source.indexOf('\n  }', start);
  assert(end >= 0);
  vm.runInContext(source.slice(start + 2, end + 4), context);
}
const which = process.argv[2];
if (which === 'context') {
  const state = {sessionId:'A',spectrumStreamId:'old-stream',sweeps:[1],revision:2};
  const form = {dataset:{observationId:'old-measurement'},reset(){this.resetCalled=true;}};
  const dom = {sessionSelect:{value:'B'},candidateForm:form,candidateEditor:{open:true}};
  const context=vm.createContext({state,dom,updateComparison(){},storeSession(){},closeHandoff(){},refresh(){}});
  loadFunction('changeSession',context);context.changeSession();
  assert.equal(state.sessionId,'B');assert.equal(state.spectrumStreamId,null);
  assert.equal(form.dataset.observationId,undefined);assert.equal(form.resetCalled,true);
  assert.equal(dom.candidateEditor.open,false);
} else if (which === 'events') {
  const state={sessionId:'A',lastEventId:0};const scheduled=[];
  const context=vm.createContext({state,setSseStatus(){},scheduleRefresh(delay){scheduled.push(delay);}});
  const start=source.indexOf('    const receive = (message) => {');
  const end=source.indexOf('\n    };',start);
  vm.runInContext(source.slice(start,end+7)+'\nglobalThis.receive = receive;',context);
  context.receive({lastEventId:'1',data:JSON.stringify({data:{sequence:1,session_id:'B',event_type:'sweep.received'}})});
  assert.deepEqual(scheduled,[]);
  context.receive({lastEventId:'2',data:JSON.stringify({data:{sequence:2,session_id:'A',event_type:'sweep.received'}})});
  assert.deepEqual(scheduled,[1500]);assert.equal(state.lastEventId,2);
} else if (which === 'large') {
  const canvas={width:16,height:16,setAttribute(){}};let drawn=false;
  const drawing={createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},putImageData(){drawn=true;}};
  const context=vm.createContext({dom:{waterfallCanvas:canvas,waterfallScale:{}},clearCanvas(){return drawing;},formatLevel(value){return String(value);}});
  loadFunction('waterfallColor',context);
  if(source.includes('  function waterfallLevels(')) loadFunction('waterfallLevels',context);
  loadFunction('drawWaterfall',context);
  const sweeps=Array.from({length:40},()=>({levels:Array(4096).fill(-50),frequencies_hz:Array.from({length:4096},(_,i)=>100000000+i),level_unit:'dBm'}));
  assert.doesNotThrow(()=>context.drawWaterfall(sweeps));assert(drawn);
} else if (which === 'grid') {
  const context=vm.createContext({});loadFunction('waterfallLevels',context);
  function peak(frequencies){
    const row=context.waterfallLevels({frequencies_hz:frequencies,levels:[0,10,0]},100,200,101);
    return row.indexOf(Math.max(...row));
  }
  assert.equal(peak([100,150,200]),50);assert.equal(peak([100,199,200]),99);
} else {throw new Error('Unknown test case');}
console.log(which+' passed');
