import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';
const source=await readFile(new URL('../apps/web/src/plugins/classification-results.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const {latestClassifications,validClassificationJobs,classificationRows,classificationContextId,defaultClassification}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
const bundle=id=>({run:{id},digest:'digest-'+id});
const selected=[bundle('run-a'),bundle('run-b'),bundle('run-c')];
function batch(id,at,runs,settings={}){
  const {version='1.2.0',scope='task',config={},state='completed',value='table'}=settings;
  const data={id,created_at:at,plugin_id:'official.base-stages',plugin_version:version,contribution_id:'classify',
    plugin_title:'Base stages',collection_id:'collection',scope,config,output_kind:'facets'};
  data.jobs=runs.map(run_id=>({id:id+'-'+run_id,batch_id:id,run_id,query_id:'same-case',scope,config,state,input_digest:'digest-'+run_id,
    snapshot_digest:'snapshot-'+run_id,plugin_id:data.plugin_id,plugin_version:version,contribution_id:'classify',
    result:state==='completed'?{scope,plugin_ref:{id:data.plugin_id,version},contribution_id:'classify',
      input_ref:{document_id:run_id,document_digest:'digest-'+run_id,selection_digest:'snapshot-'+run_id},
      output:{kind:'facets',data:{facets:[{id:'base.stage',values:[{id:value,title:value}]}],assignments:[{facet_id:'base.stage',
        target:{entity_id:'tool-'+run_id},status:'assigned',value_ids:[value]}]}}}:null}));
  data.counts=Object.fromEntries(['queued','running','completed','failed','cancelled'].map(key=>[key,key===state?runs.length:0]));
  return data;
}

test('same Case across fifty-run chunks merges each run latest success during reclassification',()=>{
  const history=[batch('chunk-0','2026-09-10T10:00:00Z',['run-a']),batch('chunk-1','2026-09-10T10:01:00Z',['run-b']),
    batch('a-new','2026-09-11T10:00:00Z',['run-a'],{value:'form'}),batch('a-pending','2026-09-12T10:00:00Z',['run-a'],{state:'queued'})];
  const before=structuredClone(history),[latest]=latestClassifications(history,selected,'task');
  assert.equal(latest.aggregate,true);
  assert.deepEqual(latest.jobs.map(j=>j.id),['a-new-run-a','chunk-1-run-b']);
  assert.equal(latest.counts.queued,1);assert.equal(latest.counts.completed,1);
  assert.deepEqual(latest.latest_jobs.map(j=>j.id),['a-pending-run-a','chunk-1-run-b']);
  const rows=classificationRows(latest.jobs,'base.stage');
  assert.deepEqual(rows['run-a'].get('tool-run-a').values,['form']);
  assert.deepEqual(rows['run-b'].get('tool-run-b').values,['table']);
  assert.equal(rows['run-c'],undefined);
  assert.deepEqual(history,before);
});

test('explicit historical selection preserves the selected batch instead of mixing latest',()=>{
  const original=batch('original','2026-09-10',['run-a']);
  const recent=batch('recent','2026-09-11',['run-a'],{value:'form'});
  const latest=latestClassifications([original,recent],selected,'task')[0];
  assert.notEqual(latest.id,original.id);
  const historyJobs=validClassificationJobs(original,selected,'task');
  assert.deepEqual(classificationRows(historyJobs,'base.stage')['run-a'].get('tool-run-a').values,['table']);
  assert.deepEqual(classificationRows(latest.jobs,'base.stage')['run-a'].get('tool-run-a').values,['form']);
});

test('different plugin versions scopes and configurations never mix',()=>{
  const defaults=batch('default','2026-09-10',['run-a']);
  const configured=batch('configured','2026-09-11',['run-b'],{config:{mode:'strict',depth:2}});
  const sameConfig=batch('same-config','2026-09-12',['run-c'],{config:{depth:2,mode:'strict'}});
  const oldVersion=batch('old','2026-09-13',['run-a'],{version:'1.1.0'});
  const allScope=batch('all','2026-09-14',['run-a'],{scope:'all'});
  const contexts=latestClassifications([defaults,configured,sameConfig,oldVersion,allScope],selected,'task');
  assert.equal(contexts.length,3);
  assert.equal(classificationContextId(configured),classificationContextId(sameConfig));
  const chosen=defaultClassification(contexts,'official.base-stages@1.2.0:classify',{});
  assert.deepEqual(chosen.jobs.map(j=>j.run_id),['run-a']);
  assert.equal(defaultClassification(contexts,'official.base-stages@1.2.0:classify',{missing:true}),undefined);
  assert.deepEqual(latestClassifications([allScope,defaults],selected,'all')[0].jobs.map(j=>j.id),['all-run-a']);
});

test('valid empty artifact and absent artifact remain distinct for unknown and unclassified filters',()=>{
  const empty=batch('empty','2026-09-10',['run-a']);
  empty.jobs[0].result.output.data.assignments=[];
  const pending=batch('pending','2026-09-11',['run-b'],{state:'running'});
  const [context]=latestClassifications([empty,pending],selected,'task');
  const rows=classificationRows(context.jobs,'base.stage');
  assert.ok(rows['run-a'] instanceof Map);assert.equal(rows['run-a'].size,0);
  assert.equal(rows['run-b'],undefined);assert.equal(rows['run-c'],undefined);
  assert.equal(classificationRows(context.jobs,'absent-dimension')['run-a'].size,0);
});

test('newer invalid input binding does not suppress an older valid success',()=>{
  const old=batch('old','2026-09-10',['run-a']);
  for(const field of ['document_id','document_digest','selection_digest']){
    const invalid=batch('invalid','2026-09-11',['run-a']);
    invalid.jobs[0].result.input_ref[field]='foreign';
    const [context]=latestClassifications([old,invalid],selected,'task');
    assert.deepEqual(context.jobs.map(j=>j.id),['old-run-a']);
  }
  const changed=[{run:{id:'run-a'},digest:'different-snapshot'}];
  assert.deepEqual(latestClassifications([old],changed,'task'),[]);
  assert.deepEqual(validClassificationJobs(old,changed,'task'),[]);
});

test('progress is limited to selected runs and failed reruns preserve successful results',()=>{
  const old=batch('old','2026-09-10',['run-a']);
  const failure=batch('failed','2026-09-11',['run-a'],{state:'failed'});
  const foreign=batch('foreign','2026-09-12',['other-case-run'],{state:'queued'});
  const [context]=latestClassifications([old,failure,foreign],selected,'task');
  assert.equal(context.counts.failed,1);assert.equal(context.counts.queued,0);
  assert.deepEqual(context.jobs.map(j=>j.id),['old-run-a']);
});
