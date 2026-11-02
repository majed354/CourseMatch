import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, rmSync, statSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Worker} from 'node:worker_threads';
import {SQLiteRepository, SqliteRepository} from '../netlify/lib/sqlite-repository.mjs';
import {createApi} from '../netlify/lib/api.mjs';
import {MemoryPdfs} from './helpers.mjs';

const NOW = Date.UTC(2026,10,2,12), HOUR=3_600_000,DAY=86_400_000,LEASE=900_000;
const sid = n => n.toString(16).padStart(64,'0');
const id = n => n.toString(16).padStart(32,'0');
const prepare = (task,result) => ({...result,id:task.id});
async function fixture(t) {
  const directory=mkdtempSync(join(tmpdir(),'coursematch-sqlite-test-')),path=join(directory,'queue.sqlite'),handles=[];
  const open=()=>{const repository=new SQLiteRepository(path);handles.push(repository);return repository;};
  t.after(()=>{for(const r of handles)r.close();rmSync(directory,{recursive:true,force:true});});
  const repository=open();
  await repository.ensureSession('production',{id:sid(1),expires:(NOW+7*DAY)/1000},NOW);
  return {repository,path,open};
}
function parallel(path,action,worker,now=NOW) {
  return new Promise((resolve,reject)=>{
    let result;
    const instance=new Worker(`const {parentPort,workerData}=require('node:worker_threads');
      (async()=>{const {SQLiteRepository}=await import(workerData.module);const r=new SQLiteRepository(workerData.path);
        try {let value;if(workerData.action==='claim'){const task=await r.claim('production',workerData.worker,workerData.now);value={id:task?.id,attempts:task?.attempts};}
        else {await r.enqueue('production',workerData.owner,'compare',{},null,workerData.id,workerData.now);value={accepted:true};}
        parentPort.postMessage(value);}catch(error){parentPort.postMessage({status:error.status,error:error.message});}finally{r.close();}})().catch(error=>{throw error;});`,
      {eval:true,workerData:{path,action,worker,now,owner:sid(1),id:id(worker),module:new URL('../netlify/lib/sqlite-repository.mjs',import.meta.url).href}});
    instance.once('message',value=>{result=value;});instance.once('error',reject);instance.once('exit',code=>{if(code)reject(Error('SQLite test worker exited '+code));else if(result)resolve(result);else reject(Error('SQLite test worker returned no result'));});
  });
}

test('SQLite source and artifact persist across reopening, with durable completion acknowledgment',async t=>{
  const {repository:r,path,open}=await fixture(t);assert.equal(SqliteRepository,SQLiteRepository);
  assert.equal(statSync(path).mode & 0o777,0o600);
  await r.enqueue('production',sid(1),'source',{name:'مصدر',topics:[]},'pdf/private/source.pdf',id(1),NOW);
  const leased=await r.claim('production','mac-test',NOW);assert.equal(leased.attempts,1);assert.equal(leased.lease_token.length,48);assert.ok(leased.lease_expires_at instanceof Date);
  const result={name:'مصدر',topics:[{id:'t1',text:'تعريف علوم القرآن'}],source_sha256:'fixture'};
  assert.equal((await r.complete('production',id(1),leased.lease_token,result,'',NOW,prepare)).status,'done');r.close();
  const restored=open(),task=await restored.getTask('production',id(1),sid(1),NOW+1);
  assert.equal(task.status,'done');assert.ok(task.created_at instanceof Date);assert.deepEqual(task.payload,{name:'مصدر',topics:[]});
  const artifact=await restored.getArtifact('production',id(1),sid(1),NOW+1,'source');assert.equal(artifact.value.name,'مصدر');assert.equal(artifact.pdf_key,'pdf/private/source.pdf');
  assert.deepEqual(await restored.complete('production',id(1),leased.lease_token,{name:'مختلف'},'',NOW+2,prepare),{id:id(1),status:'done',idempotent:true});
  assert.equal((await restored.getArtifact('production',id(1),sid(1),NOW+3,'source')).value.name,'مصدر');
});
test('SQLite ownership, artifact kind and deployment scope remain separate',async t=>{
  const {repository:r}=await fixture(t);await r.ensureSession('production',{id:sid(2),expires:(NOW+DAY)/1000},NOW);await r.ensureSession('preview',{id:sid(1),expires:(NOW+DAY)/1000},NOW);
  await r.enqueue('production',sid(1),'source',{},null,id(1),NOW);await r.enqueue('preview',sid(1),'source',{},null,id(1),NOW);
  const task=await r.claim('production','worker',NOW);await r.complete('production',id(1),task.lease_token,{name:'خاص'},'',NOW,prepare);
  assert.equal(await r.getTask('production',id(1),sid(2),NOW),null);
  assert.equal(await r.getArtifact('production',id(1),sid(2),NOW,'source'),null);
  assert.equal(await r.getArtifact('production',id(1),sid(1),NOW,'report'),null);
  assert.equal(await r.getArtifact('preview',id(1),sid(1),NOW,'source'),null);
  assert.equal((await r.getTask('preview',id(1),sid(1),NOW)).status,'queued');
});
test('SQLite rolling quotas and expired sessions reject writes without consuming a task',async t=>{
  const {repository:r}=await fixture(t);
  for(let i=1;i<=5;i++)await r.enqueue('production',sid(1),'compare',{},null,id(i),NOW);
  await assert.rejects(r.enqueue('production',sid(1),'compare',{},null,id(6),NOW),e=>e.status===429);
  await r.enqueue('production',sid(1),'compare',{},null,id(6),NOW+HOUR+1);
  await r.ensureSession('production',{id:sid(2),expires:(NOW+1000)/1000},NOW);
  await assert.rejects(r.enqueue('production',sid(2),'source',{},null,id(7),NOW+1001),e=>e.status===401);
  assert.equal(await r.getTask('production',id(7),sid(2),NOW+1001),null);
});
test('SQLite recovery reuses active leases and fences reclaimed task completions',async t=>{
  const {repository:r,open}=await fixture(t);await r.enqueue('production',sid(1),'source',{},null,id(1),NOW);
  const first=await r.claim('production','worker-a',NOW);r.close();const restored=open();
  const same=await restored.claim('production','worker-a',NOW+1000);assert.equal(same.lease_token,first.lease_token);assert.equal(same.attempts,1);
  const second=await restored.claim('production','worker-b',NOW+LEASE+1);assert.notEqual(second.lease_token,first.lease_token);assert.equal(second.attempts,2);
  await assert.rejects(restored.complete('production',id(1),first.lease_token,{},'',NOW+LEASE+2,prepare),e=>e.status===409);
  assert.equal(await restored.leasedTask('production',id(1),first.lease_token,NOW+LEASE+2),null);
  assert.equal((await restored.leasedTask('production',id(1),second.lease_token,NOW+LEASE+2)).id,id(1));
});
test('SQLite retries end after three abandoned leases and reject stale final replies',async t=>{
  const {repository:r}=await fixture(t);await r.enqueue('production',sid(1),'source',{},null,id(1),NOW);let task;
  for(let attempt=1;attempt<=3;attempt++){task=await r.claim('production','worker',NOW+(attempt-1)*(LEASE+1));assert.equal(task.attempts,attempt);}
  const end=NOW+3*(LEASE+1);assert.equal(await r.claim('production','worker',end),null);
  const failed=await r.getTask('production',id(1),sid(1),end);assert.equal(failed.status,'error');assert.equal(failed.lease_token,null);
  await assert.rejects(r.complete('production',id(1),task.lease_token,{},'',end,prepare),e=>e.status===409);
});
test('SQLite heartbeat renews leases, respects task expiry and persists worker health',async t=>{
  const {repository:r,open}=await fixture(t);await r.enqueue('production',sid(1),'source',{},null,id(1),NOW);
  const claimTime=NOW+DAY-10_000,task=await r.claim('production','worker',claimTime);
  assert.equal(task.lease_expires_at.getTime(),NOW+DAY);
  const renewed=await r.heartbeat('production',task.id,task.lease_token,claimTime+1000);assert.equal(renewed.lease_expires_at.getTime(),NOW+DAY);
  assert.equal(await r.workerOnline('production',claimTime+1001),true);r.close();const restored=open();assert.equal(await restored.workerOnline('production',claimTime+1001),true);
  await assert.rejects(restored.heartbeat('production',task.id,task.lease_token,NOW+DAY),e=>e.status===409);
  await assert.rejects(restored.complete('production',task.id,task.lease_token,{},'',NOW+DAY,prepare),e=>e.status===404);
  assert.equal(await restored.workerOnline('production',NOW+DAY+120_001),false);
});
test('SQLite completion rolls artifact and task writes back together on a real SQLite failure',async t=>{
  const {repository:r}=await fixture(t);await r.enqueue('production',sid(1),'source',{},null,id(1),NOW);const task=await r.claim('production','worker',NOW);
  r.db.exec("CREATE TRIGGER fixture_reject_done BEFORE UPDATE OF status ON cm_tasks WHEN NEW.status='done' BEGIN SELECT RAISE(ABORT,'fixture failure'); END");
  await assert.rejects(r.complete('production',task.id,task.lease_token,{name:'لا يُحفظ'},'',NOW,prepare),/fixture failure/);
  assert.equal(await r.getArtifact('production',id(1),sid(1),NOW,'source'),null);assert.equal((await r.getTask('production',id(1),sid(1),NOW)).status,'running');
  r.db.exec('DROP TRIGGER fixture_reject_done');await r.complete('production',task.id,task.lease_token,{name:'محفوظ'},'',NOW,prepare);
  assert.equal((await r.getArtifact('production',id(1),sid(1),NOW,'source')).value.name,'محفوظ');
});
test('SQLite transaction and result preparation reject asynchronous callbacks',async t=>{
  const {repository:r}=await fixture(t);let invoked=false;
  assert.throws(()=>r.transaction(async()=>{invoked=true;}),/synchronous/);assert.equal(invoked,false);
  await r.enqueue('production',sid(1),'source',{},null,id(1),NOW);const task=await r.claim('production','worker',NOW);
  await assert.rejects(r.complete('production',task.id,task.lease_token,{},'',NOW,async()=>({})),/synchronous/);
  assert.equal((await r.getTask('production',id(1),sid(1),NOW)).status,'running');
});
test('SQLite beneficiary changes persist and preserve the calculation and evidence',async t=>{
  const {repository:r,open}=await fixture(t);await r.enqueue('production',sid(1),'compare',{},null,id(1),NOW);const task=await r.claim('production','worker',NOW);
  const result={beneficiary:{name:'قبل',number:'٠٠١'},calculation:{decision:'equivalent',strict_coverage:100},evidence:{pairs:[{score:100}]}};
  await r.complete('production',task.id,task.lease_token,result,'',NOW,prepare);
  await assert.rejects(r.updateBeneficiary('production',id(1),sid(2),{name:'آخر'},NOW),e=>e.status===404);
  await r.updateBeneficiary('production',id(1),sid(1),{name:'بعد',number:'000123'},NOW+1);r.close();const restored=open();
  const report=await restored.getArtifact('production',id(1),sid(1),NOW+2,'report');assert.equal(report.value.beneficiary.number,'000123');assert.deepEqual(report.value.calculation,result.calculation);assert.deepEqual(report.value.evidence,result.evidence);
  assert.deepEqual((await restored.getTask('production',id(1),sid(1),NOW+2)).result,report.value);
});
test('SQLite expires reads and retries durable PDF deletion after data cleanup and reopening',async t=>{
  const {repository:r,open}=await fixture(t),key='private/source.pdf';await r.reservePdf('production',key,NOW);await r.enqueue('production',sid(1),'source',{},key,id(1),NOW);
  const task=await r.claim('production','worker',NOW);await r.complete('production',id(1),task.lease_token,{name:'مصدر'},'',NOW,prepare);
  assert.deepEqual(await r.cleanup('production',NOW+DAY-1),[]);assert.ok(await r.getArtifact('production',id(1),sid(1),NOW+DAY-1,'source'));
  assert.equal(await r.getTask('production',id(1),sid(1),NOW+DAY),null);assert.equal(await r.getArtifact('production',id(1),sid(1),NOW+DAY,'source'),null);
  assert.deepEqual(await r.cleanup('production',NOW+DAY+1),[key]);r.close();const restored=open();
  assert.deepEqual(await restored.cleanup('production',NOW+DAY+2),[key]);await restored.ackPdfDeletion('production',key);assert.deepEqual(await restored.cleanup('production',NOW+DAY+3),[]);
});
test('SQLite orphan PDF expiry and cleanup are isolated by scope and do not delete active foreign-key owners',async t=>{
  const {repository:r}=await fixture(t);await r.reservePdf('production','orphan',NOW);await r.reservePdf('preview','private-preview',NOW);
  await r.ensureSession('production',{id:sid(2),expires:(NOW+1000)/1000},NOW);await r.enqueue('production',sid(2),'source',{},null,id(1),NOW);
  assert.deepEqual(await r.cleanup('production',NOW+2000),[]);assert.equal(r.db.prepare('SELECT count(*) AS n FROM cm_sessions WHERE scope=? AND id=?').get('production',sid(2)).n,1);
  assert.deepEqual(await r.cleanup('production',NOW+DAY+1),['orphan']);assert.deepEqual(await r.cleanup('preview',NOW+DAY+1),['private-preview']);
  assert.equal(r.db.prepare('SELECT count(*) AS n FROM cm_sessions WHERE scope=? AND id=?').get('production',sid(2)).n,0);
});
test('SQLite global pending bound survives new browser sessions and releases capacity after completion',async t=>{
  const {repository:r}=await fixture(t);
  for(let owner=1;owner<=6;owner++)await r.ensureSession('production',{id:sid(owner),expires:(NOW+7*DAY)/1000},NOW);
  for(let n=1;n<=100;n++)await r.enqueue('production',sid(Math.ceil(n/20)),'source',{},null,id(n),NOW);
  await assert.rejects(r.enqueue('production',sid(6),'source',{},null,id(101),NOW),e=>e.status===429 && /ممتلئة/.test(e.message));
  const task=await r.claim('production','worker',NOW);await r.complete('production',task.id,task.lease_token,{name:'منجز'},'',NOW,prepare);
  await r.enqueue('production',sid(6),'source',{},null,id(101),NOW);
  await r.enqueue('production',sid(6),'source',{},null,id(102),NOW+DAY+1);
});
test('two SQLite connections do not lease the same task to parallel worker threads',async t=>{
  const {repository:r,path}=await fixture(t);for(let n=1;n<=4;n++)await r.enqueue('production',sid(1),'source',{},null,id(n),NOW+n);
  const results=await Promise.all([1,2,3,4].map(worker=>parallel(path,'claim',worker,NOW+10)));
  assert.equal(new Set(results.map(result=>result.id)).size,4);assert.ok(results.every(result=>result.attempts===1));
});
test('parallel SQLite writers cannot exceed a session comparison quota',async t=>{
  const {repository:r,path}=await fixture(t);for(let n=1;n<=4;n++)await r.enqueue('production',sid(1),'compare',{},null,id(n),NOW);
  const results=await Promise.all([5,6].map(worker=>parallel(path,'enqueue',worker,NOW)));
  assert.equal(results.filter(result=>result.accepted).length,1);assert.equal(results.filter(result=>result.status===429).length,1);
  assert.equal(r.db.prepare("SELECT count(*) AS n FROM cm_tasks WHERE scope='production' AND kind='compare'").get().n,5);
});
test('createApi source, search, comparison and beneficiary flow works over real SQLite and reopens',async t=>{
  const {repository:r,open}=await fixture(t);
  const course={id:'a'.repeat(20),code:'2001',name:'علوم القرآن',context_id:'plan47',context:{program:'القرآن وعلومه',degree:'بكالوريوس',version:'47'},topics:[{id:'t1',text:'أسباب النزول'}],source_pdf:'assets/course-specifications/test.pdf'};
  const catalog={catalog_id:'catalog-test',contexts:[{id:'plan47'}],courses:[course],library_sources:[]};
  const env={CONTEXT:'production',COURSEMATCH_SESSION_SECRET:'test-only-session-secret-long-enough',COURSEMATCH_WORKER_TOKEN:'test-only-worker-secret-long-enough'};
  const pdfs=new MemoryPdfs(),build=repository=>createApi({repository,pdfs,catalog,env,clock:()=>NOW,logger:{error(){}}});let api=build(r);
  let cookie,csrf;
  const request=(path,body,worker=false)=>api(new Request('https://example.net'+path,{method:body===undefined?'GET':'POST',headers:{...(body===undefined?{}:{'Content-Type':'application/json'}),...(worker?{Authorization:'Bearer '+env.COURSEMATCH_WORKER_TOKEN}:{...(cookie?{Cookie:cookie}:{}),...(csrf?{'X-Equivalency-Token':csrf}:{}),...(body===undefined?{}:{Origin:'https://example.net'})})},...(body===undefined?{}:{body:JSON.stringify(body)})}));
  const boot=await request('/api/catalog');cookie=boot.headers.get('set-cookie').split(';')[0];const metadata=await boot.json();csrf=metadata.token;assert.equal(metadata.capabilities.reference_editable,false);
  const submit=await request('/api/sources',{name:'مصدر',topics:course.topics});assert.equal(submit.status,202);const sourceId=(await submit.json()).operation_id;
  let task=(await(await request('/api/worker/claim',{worker_id:'mac-test'},true)).json()).task;assert.equal(task.id,sourceId);assert.equal(typeof task.lease_expires_at,'string');
  assert.equal((await request('/api/worker/tasks/'+task.id+'/complete',{lease_token:task.lease_token,result:{name:'مصدر',topics:course.topics}},true)).status,200);
  const source=(await(await request('/api/operations/'+sourceId)).json()).result;assert.equal(source.id,sourceId);
  assert.equal((await request('/api/search',{source_id:sourceId,all_contexts:true})).status,202);
  task=(await(await request('/api/worker/claim',{worker_id:'mac-test'},true)).json()).task;assert.equal(task.payload.source.id,sourceId);
  assert.equal((await request('/api/worker/tasks/'+task.id+'/complete',{lease_token:task.lease_token,result:{candidates:[course],method:'lexical'}},true)).status,200);
  const compare=await request('/api/jobs',{source_id:sourceId,target_id:course.id,reviewed:true,beneficiary:{name:'مستفيد',number:'٠٠١23'}});assert.equal(compare.status,202);const reportId=(await compare.json()).id;
  task=(await(await request('/api/worker/claim',{worker_id:'mac-test'},true)).json()).task;assert.deepEqual(task.payload.target_topics,course.topics);
  const report={source,target:course,catalog_id:catalog.catalog_id,evidence:{pairs:[]},calculation:{decision:'equivalent',strict_coverage:100,lenient_coverage:100}};
  assert.equal((await request('/api/worker/tasks/'+task.id+'/complete',{lease_token:task.lease_token,result:report},true)).status,200);
  r.close();api=build(open());
  const restored=(await(await request('/api/jobs/'+reportId)).json()).result;assert.equal(restored.beneficiary.number,'٠٠١23');assert.equal(restored.id,reportId);
  assert.equal((await request('/api/reports/'+reportId+'/beneficiary',{beneficiary:{name:'تحديث',number:'000123'}})).status,200);
  const updated=(await(await request('/api/jobs/'+reportId)).json()).result;assert.equal(updated.beneficiary.number,'000123');assert.equal(updated.calculation.strict_coverage,100);
});

test('interactive SQLite claims continue source and search while a comparison lease stays active',async t=>{
  const {repository:r,open}=await fixture(t),interactive=open();
  await r.enqueue('production',sid(1),'compare',{},null,id(1),NOW);
  await r.enqueue('production',sid(1),'source',{},null,id(2),NOW+1);
  await r.enqueue('production',sid(1),'search',{},null,id(3),NOW+2);
  const comparison=await r.claim('production','mac-compare',NOW+3,['compare']);
  assert.equal(comparison.kind,'compare');assert.equal(comparison.id,id(1));
  const source=await interactive.claim('production','mac-interactive',NOW+4,['source','search']);
  assert.equal(source.kind,'source');assert.equal(source.id,id(2));
  const retry=await interactive.claim('production','mac-interactive',NOW+5,['source','search']);
  assert.equal(retry.id,source.id);assert.equal(retry.lease_token,source.lease_token);assert.equal(retry.attempts,1);
  // Changing a role cannot lease a second job to an already active worker ID.
  assert.equal((await interactive.claim('production','mac-interactive',NOW+6,['compare'])).id,source.id);
  await interactive.complete('production',source.id,source.lease_token,{topics:[]},'',NOW+7,prepare);
  const search=await interactive.claim('production','mac-interactive',NOW+8,['source','search']);
  assert.equal(search.kind,'search');assert.equal(search.id,id(3));
  await interactive.complete('production',search.id,search.lease_token,{candidates:[]},'',NOW+9,prepare);
  assert.equal(await interactive.claim('production','mac-interactive',NOW+10,['source','search']),null);
  const still=await r.claim('production','mac-compare',NOW+11,['compare']);
  assert.equal(still.lease_token,comparison.lease_token);assert.equal(still.attempts,1);assert.equal(still.status,'running');
  // A reclaimed comparison also stays out of the interactive lane.
  assert.equal(await interactive.claim('production','mac-interactive',NOW+LEASE+20,['source','search']),null);
  assert.equal((await r.claim('production','mac-compare',NOW+LEASE+21,['compare'])).attempts,2);
});
test('SQLite lane kinds are validated before SQL and legacy claims still include every kind',async t=>{
  const {repository:r}=await fixture(t);
  await r.enqueue('production',sid(1),'compare',{},null,id(1),NOW);
  for(const kinds of [[],['source','source'],['arbitrary'],"compare",['source','search','compare','source'],["source'); DROP TABLE cm_tasks; --"]]) {
    await assert.rejects(r.claim('production','invalid',NOW,kinds),e=>e.status===400);
  }
  assert.equal((await r.claim('production','legacy',NOW)).kind,'compare');
});
