// Captures only original application reads. Does not clone, cancel, or issue requests.
(() => {
 const key=__TRACE_HUNTER_CONTROL_KEY__;
 if(globalThis[key]) throw Error('renderer hook already present');
 const saved={fetch:globalThis.fetch,getReader:ReadableStream.prototype.getReader,read:ReadableStreamDefaultReader.prototype.read};
 const bodies=new WeakMap(),readers=new WeakMap(),queue=[];
 // Paths verified in the running client's request implementations. A sync stream
 // may hand off to async; its EOF is not necessarily the end of the user task.
 const paths=new Set(['/chat/completion','/chat/async/chunk_stream','/samantha/chat/completion','/alice/message/stream_reply','/alice/office/tool_local/chunk_stream']);
 const counts={fetch_seen:0,target_fetches:0,stream_chunks:0,dropped:0};
 const start=performance.now(); let sequence=0,queued=0,total=0,enabled=true,timer;
 function put(type,data){if(!enabled)return;try{const e={sequence:sequence++,type,wall_ms:Date.now(),mono_ms:performance.now()-start,...data};const s=JSON.stringify(e),n=new TextEncoder().encode(s).length;if(n>2**21||queued+n>2**23||total+n>2**25){counts.dropped++;return;}queue.push(s);queued+=n;total+=n;}catch{counts.dropped++;}}
 function metricHeaders(headers){
  const names=[],numeric={},correlation={},serverTiming=[];
  if(!headers?.forEach)return {names,numeric,correlation,server_timing:serverTiming};
  headers.forEach((value,name)=>{
   name=String(name).toLowerCase();
   if(names.length<128)names.push(name.slice(0,128));
   // Only explicitly selected trace correlation IDs are retained; never arbitrary
   // header values, cookies, Authorization, or Server-Timing descriptions.
   if(['x-tt-logid','x-tt-trace-id'].includes(name)&&/^[A-Za-z0-9._:-]{1,256}$/.test(value))correlation[name]=value;
   if(name==='traceparent'&&/^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/i.test(value))correlation[name]=value;
   if(/^(?:x-)?(?:(?:input|output|prompt|completion|reasoning|cache-read|cache-write|total)-tokens|(?:model|inference|prefill|decode)-duration-ms)$/.test(name)&&/^\d+(?:\.\d+)?$/.test(value)&&value.length<24)numeric[name]=value;
   if(name==='server-timing')for(const part of String(value).slice(0,8192).split(',')){
    const match=part.match(/^\s*([\w.-]{1,64})\s*;\s*dur=(\d+(?:\.\d+)?)(?:\s*;|\s*$)/);
    if(match&&serverTiming.length<32)serverTiming.push({name:match[1],duration_ms:Number(match[2])});
   }
  });
  return {names,numeric,correlation,server_timing:serverTiming};
 }
 function fetchHook(...args){
  const p=Reflect.apply(saved.fetch,this,args); counts.fetch_seen++;
  try {const input=args[0],u=new URL(typeof input==='string'?input:input instanceof Request?input.url:'about:blank',location.href);
   if(u.protocol==='https:'&&!u.username&&!u.password&&['www.doubao.com','api5-normal-lq.doubao.com'].includes(u.hostname)&&paths.has(u.pathname)){
    let continuation;
    // Inspect only existing string bodies; no Request-body reads or clones.
    if(u.pathname==='/chat/async/chunk_stream'&&typeof args[1]?.body==='string'&&args[1].body.length<2**21){
     try{const b=JSON.parse(args[1].body);continuation={};
      if(typeof b.task_id==='string'&&/^[A-Za-z0-9._:-]{1,256}$/.test(b.task_id))continuation.task_id=b.task_id;
      if(Number.isSafeInteger(b.seq_start)&&b.seq_start>=0)continuation.seq_start=b.seq_start;
      if(Number.isSafeInteger(b.append_scene))continuation.append_scene=b.append_scene;
     }catch{}
    }
    const id=++counts.target_fetches;put('fetch_start',{request:id,origin:u.origin,path:u.pathname,continuation});
    p.then(r=>{try{if(r.body)bodies.set(r.body,id);put('response_headers',{request:id,status:r.status,body_type:r.body?.constructor?.name,headers:metricHeaders(r.headers)});}catch{counts.dropped++;}},()=>put('fetch_error',{request:id}));
   }
  }catch{}
  return p;
 }
 function getReaderHook(...args){const r=Reflect.apply(saved.getReader,this,args),id=bodies.get(this);if(id)readers.set(r,id);return r;}
 function readHook(...args){const p=Reflect.apply(saved.read,this,args),id=readers.get(this);if(id)p.then(r=>{try{if(r.done)put('stream_end',{request:id});else if(r.value instanceof Uint8Array){counts.stream_chunks++;let s='';for(let i=0;i<r.value.length;i+=8192)s+=String.fromCharCode(...r.value.subarray(i,i+8192));put('stream_chunk',{request:id,length:r.value.length,bytes_b64:btoa(s)});}}catch{counts.dropped++;}},()=>put('stream_error',{request:id}));return p;}
 function status(){return{enabled,counts:{...counts},total_bytes:total,queue_events:queue.length,elapsed_ms:performance.now()-start};}
 function stop(){if(globalThis.fetch===fetchHook)globalThis.fetch=saved.fetch;if(ReadableStream.prototype.getReader===getReaderHook)ReadableStream.prototype.getReader=saved.getReader;if(ReadableStreamDefaultReader.prototype.read===readHook)ReadableStreamDefaultReader.prototype.read=saved.read;enabled=false;clearTimeout(timer);return{...status(),restored:{fetch:globalThis.fetch===saved.fetch,getReader:ReadableStream.prototype.getReader===saved.getReader,read:ReadableStreamDefaultReader.prototype.read===saved.read}};}
 globalThis[key]={status,stop,drain(){const events=queue.splice(0);queued=0;return{events,status:status()};}};
 globalThis.fetch=fetchHook;ReadableStream.prototype.getReader=getReaderHook;ReadableStreamDefaultReader.prototype.read=readHook;
 timer=setTimeout(stop,__TRACE_HUNTER_TIMEOUT_MS__);return status();
})()
