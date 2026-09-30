import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const template = fs.readFileSync(new URL('../scripts/doubao_renderer_hook.js', import.meta.url), 'utf8');
function fixture({overflow=false}={}) {
  const bytes = new TextEncoder().encode('event: TEST\ndata: {"text":"中文"}\n\n');
  let fetchCalls=0, reads=0, lastFetch, lastRead;
  class Reader {
    read() {
      reads++;
      lastRead = Promise.resolve(reads===1 ? {done:false,value:overflow?new Uint8Array(2**21):bytes} : {done:true});
      return lastRead;
    }
  }
  class Stream { getReader(){return new Reader();} }
  const originalFetch = function(){fetchCalls++;lastFetch=Promise.resolve({status:200,body:new Stream(),headers:new Headers({
    'set-cookie':'session=NEVER_CAPTURE', 'authorization':'Bearer NEVER_CAPTURE',
    'x-input-tokens':'120', 'x-output-tokens':'NEVER_CAPTURE',
    'x-tt-logid':'trace-example-123',
    'server-timing':'prefill;dur=12.5;desc="NEVER_CAPTURE", decode;dur=20'
  })});return lastFetch;};
  const originalRead=Reader.prototype.read, originalGet=Stream.prototype.getReader;
  const context=vm.createContext({fetch:originalFetch,ReadableStream:Stream,ReadableStreamDefaultReader:Reader,
    Request,URL,TextEncoder,Uint8Array,performance,location:{href:'doubaowork://doubaowork-chat/chat'},
    setTimeout,clearTimeout,btoa:s=>Buffer.from(s,'binary').toString('base64')});
  const code=template.replace('__TRACE_HUNTER_CONTROL_KEY__',JSON.stringify('ownedHook')).replace('__TRACE_HUNTER_TIMEOUT_MS__','60000');
  vm.runInContext(code,context);
  return {context,bytes,originalFetch,originalRead,originalGet,Reader,Stream,
    get fetchCalls(){return fetchCalls;},get reads(){return reads;},get lastFetch(){return lastFetch;},get lastRead(){return lastRead;}};
}

for (const overflow of [false,true]) {
  const f=fixture({overflow});
  try {
    const p=f.context.fetch('https://www.doubao.com/chat/completion');
    assert.equal(p,f.lastFetch,'fetch Promise identity preserved');
    const response=await p,reader=response.body.getReader();
    const first=reader.read();assert.equal(first,f.lastRead,'read Promise identity preserved');
    const item=await first;assert.equal(item.value.length,overflow?2**21:f.bytes.length);
    await reader.read();
    const batch=f.context.ownedHook.drain(),rows=batch.events.map(JSON.parse);
    assert.equal(f.fetchCalls,1);assert.equal(f.reads,2,'no extra read, clone, or cancel');
    assert.equal(batch.status.counts.target_fetches,1);
    if(overflow) assert.equal(batch.status.counts.dropped,1);
    else assert.deepEqual(Buffer.from(rows.find(r=>r.type==='stream_chunk').bytes_b64,'base64'),Buffer.from(f.bytes));
    assert.ok(rows.some(r=>r.type==='stream_end'));
    const headers=rows.find(r=>r.type==='response_headers').headers;
    assert.equal(headers.numeric['x-input-tokens'],'120');
    assert.equal(headers.numeric['x-output-tokens'],undefined);
    assert.equal(headers.correlation['x-tt-logid'],'trace-example-123');
    assert.equal(headers.server_timing.length,2);
    assert.equal(headers.server_timing[0].duration_ms,12.5);
    assert.ok(!JSON.stringify(rows).includes('NEVER_CAPTURE'),'sensitive header values and descriptions omitted');
  } finally {
    const status=f.context.ownedHook.stop();
    assert.ok(Object.values(status.restored).every(Boolean));
    assert.equal(f.context.fetch,f.originalFetch);assert.equal(f.Reader.prototype.read,f.originalRead);assert.equal(f.Stream.prototype.getReader,f.originalGet);
  }
}

const f=fixture();
try {
  const response=await f.context.fetch('https://www.doubao.com/unrelated?secret=not-captured');
  await response.body.getReader().read();
  assert.equal(f.context.ownedHook.drain().events.length,0,'unrelated response not observed');
  const foreign=()=>{};f.context.fetch=foreign;
  const status=f.context.ownedHook.stop();
  assert.equal(status.restored.fetch,false);assert.equal(f.context.fetch,foreign,'does not overwrite a later foreign hook');
} finally {f.context.ownedHook.stop();}
console.log('Renderer hook: original promises/bytes, no extra reads, quota pass-through, URL scope, and restoration checks passed.');

const resumed=fixture();
try {
  const urls=['https://www.doubao.com/chat/completion','https://www.doubao.com/chat/async/chunk_stream','https://www.doubao.com/samantha/chat/completion','https://www.doubao.com/alice/message/stream_reply','https://www.doubao.com/alice/office/tool_local/chunk_stream'];
  for(const url of urls){
    const response=await resumed.context.fetch(url,{body:JSON.stringify({task_id:'task-123',seq_start:17,append_scene:2,authorization:'NEVER_CAPTURE'})});
    await response.body.getReader().read();
  }
  const rows=resumed.context.ownedHook.drain().events.map(JSON.parse);
  assert.equal(rows.filter(r=>r.type==='fetch_start').length,5,'all verified stream routes observed');
  assert.equal(new Set(rows.filter(r=>r.type==='fetch_start').map(r=>r.request)).size,5,'continuations retain separate transport identities');
  const asyncStart=rows.find(r=>r.path==='/chat/async/chunk_stream');
  assert.equal(asyncStart.continuation.task_id,'task-123');assert.equal(asyncStart.continuation.seq_start,17);
  assert.ok(!JSON.stringify(rows).includes('NEVER_CAPTURE'));
  for(const url of ['https://www.doubao.com.evil.invalid/chat/completion','https://unrelated.invalid/chat/async/chunk_stream','http://www.doubao.com/chat/completion','https://www.doubao.com/account'])await resumed.context.fetch(url);
  assert.equal(resumed.context.ownedHook.drain().events.length,0,'host, scheme and route restrictions remain enforced');
} finally {resumed.context.ownedHook.stop();}
console.log('Renderer continuation coverage and task-only metadata checks passed.');
