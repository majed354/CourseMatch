import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createApi} from '../netlify/lib/api.mjs';
import {MAX_PDF_BYTES, sessionCodec, contextScope} from '../netlify/lib/security.mjs';
import {MemoryRepository,MemoryPdfs} from './helpers.mjs';

const env = {CONTEXT:'production',COURSEMATCH_SESSION_SECRET:'test-only-session-secret-with-32-characters',COURSEMATCH_WORKER_TOKEN:'test-only-worker-secret-with-32-characters'};
const course = {id:'a'.repeat(20),code:'2001',name:'مدخل إلى علوم القرآن',context_id:'plan47',context:{program:'القرآن وعلومه',degree:'بكالوريوس',version:'47'},source_pdf:'assets/course-specifications/sample.pdf',topics:[{id:'t1',text:'أسباب النزول'}]};
const catalog = {catalog_id:'catalog-fixture',contexts:[{id:'plan47'}],courses:[course],library_sources:[{...course,id:'b'.repeat(20),context_id:null,context:null}]};
function curriculumEvidence(source,target,firstBand='uncertain') {
 const sides=[target.topics,source.topics].map(rows=>rows.map(row=>({id:row.id,text:row.corrected_text || row.text})));
 const parts=band=>band==='strict'?['strict','lenient']:band==='lenient'?['lenient']:band==='uncertain'?['lenient']:[];
 const uncertainty=band=>band==='uncertain'?'decisive':'none';
 const proof=()=>[{part_id:'p1',support:'aligned',source_quotes:[{source_id:source.topics[0].id,quote:source.topics[0].corrected_text||source.topics[0].text}],reason:'شاهد بيانات اختبار.'}];
 return {provider:'curriculum_evidence',version:'full-curriculum-evidence-v2-bands',rubric:'topic-alignment-80-60-v8-full-curriculum-evidence',
   contract:{evidence_version:'curriculum-bundle-evidence-v2-bands'},
   inputs_sha256:createHash('sha256').update(JSON.stringify(sides)).digest('hex'),
   scope_review:{target_ids:target.topics.map(t=>t.id),source_ids:source.topics.map(t=>t.id),complete:true},
   pairs:target.topics.map((topic,i)=>{const band=i===0?firstBand:'weak';return {target_id:topic.id,band,possible_tiers:parts(band),uncertainty:uncertainty(band),facets:proof(),reason:'بيانات عقد اختبار.',
     adjudication:{version:'curriculum-bundle-evidence-v2-bands',state:band==='uncertain'?'uncertain':'confirmed',confirmed_tiers:band==='strict'?['lenient','strict']:band==='lenient'?['lenient']:[],possible_tiers:parts(band),
       review:{target_id:topic.id,verdict:band,possible_tiers:parts(band),uncertainty:uncertainty(band),facets:proof(),reason:'بيانات مراجعة اختبار.'}}}})};
}
function curriculumCalculation(evidence,target) {
 const total=target.topics.length,matches={strict:{},lenient:{}},possible={strict:0,lenient:0};
 for(const p of evidence.pairs)for(const tier of ['strict','lenient']){
  if(p.adjudication.possible_tiers.includes(tier))possible[tier]++;
  if(p.adjudication.confirmed_tiers.includes(tier))matches[tier][p.target_id]=[...new Set(p.adjudication.review.facets.flatMap(f=>f.source_quotes.map(q=>q.source_id)))];
 }
 const ns=Object.keys(matches.strict).length,nl=Object.keys(matches.lenient).length;
 const decision=ns*100>=80*total?'equivalent':nl*100>=80*total?'lenient':possible.lenient*100<80*total?'not_equivalent':'insufficient';
 const pending=decision==='insufficient';
 return {decision,accounting:'original_target_topics',total_topics:total,strict_count:pending?0:ns,lenient_count:pending?0:nl,
   strict_coverage:pending?null:100*ns/total,lenient_coverage:pending?null:100*nl/total,strict_matches:pending?{}:matches.strict,lenient_matches:pending?{}:matches.lenient,
   possible_strict_count:possible.strict,possible_lenient_count:possible.lenient};
}

function setup(options = {}) {
  let now = Date.UTC(2026,10,2,12);
  const repository = options.repository || new MemoryRepository(),pdfs = options.pdfs || new MemoryPdfs();
  const api = createApi({repository,pdfs,catalog:options.catalog || catalog,env:{...env,...options.env},clock:()=>now,logger:{error(){}}});
  const request = (path,{method='GET',data,cookie,token,origin='https://example.net',headers={}} = {}) => api(new Request('https://example.net'+path,{method,headers:{...(data !== undefined ? {'Content-Type':'application/json'} : {}),...(cookie ? {Cookie:cookie} : {}),...(token ? {'X-Equivalency-Token':token} : {}),...(method === 'POST' && origin ? {Origin:origin} : {}),...headers},...(data !== undefined ? {body:JSON.stringify(data)} : {})}));
  const worker = (path,data,extra = {}) => request('/api/worker/'+path,{method:'POST',data,origin:null,headers:{Authorization:'Bearer '+env.COURSEMATCH_WORKER_TOKEN,...extra}});
  const bootstrap = async () => {const response=await request('/api/catalog');return {response,cookie:response.headers.get('set-cookie').split(';')[0],...(await response.json())};};
  const post = (path,data,session) => request(path,{method:'POST',data,cookie:session.cookie,token:session.token});
  async function source(session,{pdf=false,catalogId,visualStatus} = {}) {
    const body = catalogId ? {catalog_id:catalogId} : pdf ? {name:'مصدر اختباري',pdf_base64:Buffer.from('%PDF-1.7\nfixture').toString('base64')} : {name:'مصدر اختباري',topics:course.topics};
    const created = await post('/api/sources',body,session);assert.equal(created.status,202);
    const {operation_id:id} = await created.json();
    const task = (await(await worker('claim',{worker_id:'mac-test'})).json()).task;
    assert.equal(task.id,id);
    const result={id:'ignored-worker-id',name:'مصدر اختباري',topics:course.topics,source_sha256:'fingerprint',input_pdf_key:'must-not-leak'};
    if(visualStatus !== undefined)result.visual_verification_status=visualStatus;
    const complete=await worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result});assert.equal(complete.status,200);
    return (await(await request('/api/operations/'+id,{cookie:session.cookie})).json()).result;
  }
  return {api,repository,pdfs,request,worker,bootstrap,post,source,advance:ms=>{now+=ms;},now:()=>now};
}

test('bootstrap creates a signed 7-day secure session and bound CSRF token',async()=>{
  const s=setup(),a=await s.bootstrap();
  assert.match(a.response.headers.get('set-cookie'),/HttpOnly; Secure; SameSite=Strict/);
  assert.match(a.response.headers.get('set-cookie'),/Max-Age=604800/);
  assert.equal(a.capabilities.max_upload_mb,4);assert.equal(a.capabilities.queued,true);assert.equal(a.capabilities.worker_online,false);
  const again=await s.request('/api/catalog',{cookie:a.cookie});assert.equal((await again.json()).token,a.token);
  assert.equal(again.headers.get('set-cookie'),null);assert.equal(again.headers.get('access-control-allow-origin'),null);
  const decoded=sessionCodec(env.COURSEMATCH_SESSION_SECRET,'production').read(new Request('https://example.net',{headers:{Cookie:a.cookie}}),s.now());
  assert.match(decoded.id,/^[a-f0-9]{64}$/);
  s.advance(604800_001);assert.equal((await s.request('/api/operations/'+'0'.repeat(32),{cookie:a.cookie})).status,401);
});

test('image containers stay private and image mode cannot masquerade as manual input',async()=>{
  const s=setup(),a=await s.bootstrap();
  assert.equal((await s.post('/api/sources',{topics:course.topics,input_kind:'image'},a)).status,400);
  assert.equal((await s.post('/api/sources',{pdf_base64:Buffer.from('not a PDF').toString('base64'),input_kind:'image'},a)).status,400);
  const response=await s.post('/api/sources',{pdf_base64:Buffer.from('%PDF-1.7\nimage container fixture').toString('base64'),input_kind:'image',original_filename:'صورة المقرر.png'},a);
  assert.equal(response.status,202);const body=await response.json();
  const task=s.repository.tasks.get('production:'+body.operation_id)||[...s.repository.tasks.values()][0];
  assert.equal(task.payload.input_kind,'image');assert.equal(task.payload.original_filename,'صورة المقرر.png');
  assert.equal(task.payload.pdf_base64,undefined);assert.ok(task.pdf_key);
  const outsider=await s.bootstrap();assert.equal((await s.request('/api/operations/'+body.operation_id,{cookie:outsider.cookie})).status,404);
});

test('automatic comparison does not require or claim human confirmation',async()=>{
  const s=setup(),a=await s.bootstrap(),source=await s.source(a);
  const response=await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'automatic'},a);
  assert.equal(response.status,202);const {id}=await response.json();
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  assert.equal(task.payload.reviewed,true);assert.equal(task.payload.review_mode,'automatic');
  const result={source,target:course,catalog_id:catalog.catalog_id,calculation:{decision:'insufficient'},reviewed_by:'user_confirmed_extraction',human_reviewed:true};
  assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result})).status,200);
  const report=(await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json()).result;
  assert.equal(report.reviewed_by,'automated_pipeline');assert.equal(report.human_reviewed,false);
  assert.equal((await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'forged',reviewed:true},a)).status,400);
});
test('session tampering, cross-site posts and missing or foreign CSRF are denied',async()=>{
  const s=setup(),a=await s.bootstrap(),b=await s.bootstrap(),body={topics:course.topics};
  for(const options of [{cookie:a.cookie,token:b.token},{cookie:a.cookie,token:a.token,origin:'https://evil.net'},{cookie:a.cookie,token:a.token,origin:null},{cookie:a.cookie.replace('=','=0'),token:a.token},{cookie:a.cookie}]) {
    const response=await s.request('/api/sources',{method:'POST',data:body,...options});assert.ok([401,403].includes(response.status));
  }
  assert.equal((await s.request('/api/catalog',{headers:{'Sec-Fetch-Site':'cross-site'}})).status,403);
});
test('PDF upload persists only a private blob reference and is readable only by its owner',async()=>{
  const s=setup(),a=await s.bootstrap(),b=await s.bootstrap();
  const result=await s.source(a,{pdf:true});assert.equal(result.upload_pdf,true);assert.equal(result.input_pdf_key,undefined);
  const task=[...s.repository.tasks.values()][0];assert.equal(task.payload.pdf_base64,undefined);assert.ok(task.payload.input_pdf_key);assert.ok(s.repository.garbage.size);
  assert.equal((await s.request('/pdf/'+result.id,{cookie:a.cookie})).status,200);
  assert.equal((await s.request('/pdf/'+result.id,{cookie:b.cookie})).status,404);
  assert.equal((await s.request('/api/operations/'+result.id,{cookie:b.cookie})).status,404);
  const pdf=await s.request('/pdf/'+course.id);assert.equal(pdf.status,302);assert.equal(pdf.headers.get('location'),'/'+course.source_pdf);
});
test('decoded PDF limit, invalid base64, invalid bodies and oversized stream requests are rejected',async()=>{
  const s=setup(),a=await s.bootstrap();
  const huge=Buffer.alloc(MAX_PDF_BYTES+1,65);huge.write('%PDF-');
  for(const value of [huge.toString('base64'),'%%%%','AAAA','JVBERi0===']) assert.equal((await s.post('/api/sources',{pdf_base64:value},a)).status,400);
  assert.equal((await s.post('/api/sources',{pdf_base64:Buffer.from('%PDF-1.7').toString('base64'),topics:[]},a)).status,400);
  assert.equal(s.pdfs.values.size,0);
  const response=await s.request('/api/sources',{method:'POST',data:{topics:[]},cookie:a.cookie,token:a.token,headers:{'Content-Length':'6000001'}});assert.equal(response.status,413);
});
test('search and comparison enforce source ownership, catalog targets and reviewed topic bounds',async()=>{
  const s=setup(),a=await s.bootstrap(),b=await s.bootstrap(),src=await s.source(a);
  assert.equal((await s.post('/api/search',{source_id:src.id,all_contexts:true},b)).status,404);
  assert.equal((await s.post('/api/jobs',{source_id:src.id,target_id:catalog.library_sources[0].id,reviewed:true},a)).status,400);
  assert.equal((await s.post('/api/jobs',{source_id:src.id,target_id:course.id,reviewed:false},a)).status,400);
  assert.equal((await s.post('/api/search',{source_id:src.id,all_contexts:true,context_id:'plan47'},a)).status,400);
  assert.equal((await s.post('/api/search',{source_id:src.id,context_ids:['unknown']},a)).status,400);
  const queued=await s.post('/api/search',{source_id:src.id,all_contexts:true,limit:8,source:{id:'injected',topics:[]}},a);assert.equal(queued.status,202);
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  assert.equal(task.payload.source.id,src.id);assert.equal(task.payload.source.name,'مصدر اختباري');assert.equal(task.payload.catalog_version,catalog.catalog_id);assert.equal(task.payload.limit,8);
});
test('worker authentication, lease token and upload download are separated from browser sessions',async()=>{
  const s=setup(),a=await s.bootstrap();
  assert.equal((await s.request('/api/worker/claim',{method:'POST',data:{worker_id:'x'}})).status,403);
  assert.equal((await s.worker('claim',{worker_id:'x'},{Origin:'https://example.net'})).status,403);
  const queued=await s.post('/api/sources',{pdf_base64:Buffer.from('%PDF-1.7\ninput').toString('base64')},a);const id=(await queued.json()).operation_id;
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  const headers={Authorization:'Bearer '+env.COURSEMATCH_WORKER_TOKEN,'X-Lease-Token':task.lease_token};
  assert.equal((await s.request('/api/worker/tasks/'+id+'/input-pdf',{headers})).status,200);
  assert.equal((await s.request('/api/worker/tasks/'+id+'/input-pdf',{headers:{...headers,'X-Lease-Token':'0'.repeat(48)}})).status,409);
  assert.equal((await s.request('/api/worker/tasks/'+id+'/input-pdf',{cookie:a.cookie})).status,403);
});
test('claim reuses an active worker lease, stale completion is rejected, retries stop at three',async()=>{
  const s=setup(),a=await s.bootstrap();await s.post('/api/sources',{topics:course.topics},a);
  const first=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  assert.equal((await(await s.worker('claim',{worker_id:'mac-test'})).json()).task.lease_token,first.lease_token);
  s.advance(900_001);const second=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;assert.notEqual(second.lease_token,first.lease_token);
  assert.equal((await s.worker('tasks/'+first.id+'/complete',{lease_token:first.lease_token,error:'قديم'})).status,409);
  s.advance(900_001);const third=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;assert.ok(third);
  s.advance(900_001);assert.equal((await(await s.worker('claim',{worker_id:'mac-test'})).json()).task,null);
  const row=(await(await s.request('/api/operations/'+first.id,{cookie:a.cookie})).json());assert.equal(row.status,'error');
  assert.equal((await s.worker('tasks/'+third.id+'/complete',{lease_token:third.lease_token,result:{topics:course.topics}})).status,409);
});
test('completion is idempotent and cannot overwrite an acknowledged artifact',async()=>{
  const s=setup(),a=await s.bootstrap();await s.post('/api/sources',{topics:course.topics},a);
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  const body={lease_token:task.lease_token,result:{name:'أول',topics:course.topics}};
  assert.equal((await s.worker('tasks/'+task.id+'/complete',body)).status,200);
  const replay=await s.worker('tasks/'+task.id+'/complete',{...body,result:{name:'تغيير غير مقبول',topics:[]}});
  assert.equal((await replay.json()).idempotent,true);
  assert.equal((await(await s.request('/api/operations/'+task.id,{cookie:a.cookie})).json()).result.name,'أول');
});
test('beneficiary update survives reopening and preserves calculation and evidence',async()=>{
  const s=setup(),a=await s.bootstrap(),b=await s.bootstrap(),src=await s.source(a);
  const queued=await s.post('/api/jobs',{source_id:src.id,target_id:course.id,reviewed:true,beneficiary:{name:'اسم',number:'٠٠١23'}},a);const id=(await queued.json()).id;
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  const result={id:'ignored',source:src,target:course,catalog_id:catalog.catalog_id,evidence:{pairs:[]},calculation:{decision:'insufficient'}};
  assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result})).status,200);
  assert.equal((await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{number:'bad-owner'}},b)).status,404);
  assert.equal((await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{number:123}},a)).status,400);
  const response=await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{name:'تحديث',number:'000123'}},a);assert.equal(response.status,200);
  const reopened=(await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json()).result;assert.equal(reopened.beneficiary.number,'000123');assert.deepEqual(reopened.evidence,result.evidence);assert.deepEqual(reopened.calculation,result.calculation);assert.equal(reopened.id,id);
});
test('rolling comparison quota is five per session and hour and preserves ownership',async()=>{
  const s=setup(),a=await s.bootstrap(),src=await s.source(a);
  const body={source_id:src.id,target_id:course.id,reviewed:true};
  for(let i=0;i<5;i++) assert.equal((await s.post('/api/jobs',body,a)).status,202);
  assert.equal((await s.post('/api/jobs',body,a)).status,429);
  s.advance(3_600_001);assert.equal((await s.post('/api/jobs',body,a)).status,202);
});
test('expired data cannot be read and cleanup removes PDFs with durable retries',async()=>{
  const s=setup(),a=await s.bootstrap(),src=await s.source(a,{pdf:true});const key=[...s.pdfs.values.keys()][0];
  s.advance(86_400_001);
  assert.equal((await s.request('/pdf/'+src.id,{cookie:a.cookie})).status,404);assert.equal((await s.request('/api/operations/'+src.id,{cookie:a.cookie})).status,404);
  const remove=s.pdfs.delete.bind(s.pdfs);s.pdfs.delete=async()=>{throw Error('simulated storage failure');};
  await s.worker('claim',{worker_id:'mac-test'});assert.ok(s.repository.garbage.size);assert.ok(s.pdfs.values.has(key));
  s.pdfs.delete=remove;await s.worker('claim',{worker_id:'mac-test'});assert.equal(s.repository.garbage.size,0);assert.equal(s.pdfs.values.size,0);
});
test('preview sessions, tasks, worker heartbeat and store namespaces are isolated',async()=>{
  const repository=new MemoryRepository(),pdfs=new MemoryPdfs(),prod=setup({repository,pdfs}),preview=setup({repository,pdfs,env:{CONTEXT:'deploy-preview',DEPLOY_ID:'preview1'}});
  const a=await prod.bootstrap(),b=await preview.bootstrap();const source=await prod.source(a,{pdf:true});
  assert.equal((await preview.request('/api/operations/'+source.id,{cookie:a.cookie})).status,401);
  assert.equal((await preview.request('/api/operations/'+source.id,{cookie:b.cookie})).status,404);
  assert.equal((await(await preview.worker('claim',{worker_id:'preview-mac'})).json()).task,null);
  assert.notEqual(contextScope(env),contextScope({...env,CONTEXT:'deploy-preview',DEPLOY_ID:'preview1'}));
});

test('heartbeat renews the current lease and reports the local worker online',async()=>{
  const s=setup(),a=await s.bootstrap();await s.post('/api/sources',{topics:course.topics},a);
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  s.advance(60_000);const renewed=await s.worker('tasks/'+task.id+'/heartbeat',{lease_token:task.lease_token});
  assert.equal(renewed.status,200);assert.equal((await renewed.json()).lease_expires_at,s.now()+900_000);
  assert.equal((await(await s.request('/api/catalog',{cookie:a.cookie})).json()).capabilities.worker_online,true);
  s.advance(900_001);assert.equal((await s.worker('tasks/'+task.id+'/heartbeat',{lease_token:task.lease_token})).status,409);
});
test('search completion constrains candidates to requested catalog contexts and restores public metadata',async()=>{
  const s=setup(),a=await s.bootstrap(),src=await s.source(a);
  await s.post('/api/search',{source_id:src.id,context_id:'plan47'},a);
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  const result={candidates:[{...course,name:'worker-supplied-name',source_pdf:'/private/local/file.pdf',available_contexts:[{course_id:course.id,context:{degree:'invented'}}]}]};
  assert.equal((await s.worker('tasks/'+task.id+'/complete',{lease_token:task.lease_token,result})).status,200);
  const stored=(await(await s.request('/api/operations/'+task.id,{cookie:a.cookie})).json()).result.candidates[0];
  assert.equal(stored.name,course.name);assert.equal(stored.source_pdf,course.source_pdf);assert.deepEqual(stored.available_contexts[0].context,course.context);
  await s.post('/api/search',{source_id:src.id,all_contexts:true},a);const next=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  assert.equal((await s.worker('tasks/'+next.id+'/complete',{lease_token:next.lease_token,result:{candidates:[{id:'f'.repeat(20)}]}})).status,400);
});
test('uploaded source PDF remains available to its leased search task without exposing a blob key',async()=>{
  const s=setup(),a=await s.bootstrap(),src=await s.source(a,{pdf:true});
  await s.post('/api/search',{source_id:src.id,all_contexts:true},a);const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  const response=await s.request('/api/worker/tasks/'+task.id+'/input-pdf',{headers:{Authorization:'Bearer '+env.COURSEMATCH_WORKER_TOKEN,'X-Lease-Token':task.lease_token}});
  assert.equal(response.status,200);assert.match(await response.text(),/^%PDF-/);assert.equal(task.payload.source.input_pdf_key,undefined);
});
test('a failed or unacknowledged blob write still has durable expiry registration',async()=>{
  const s=setup(),a=await s.bootstrap();const put=s.pdfs.put.bind(s.pdfs);
  s.pdfs.put=async(...args)=>{await put(...args);throw Error('simulated lost storage acknowledgement');};
  const response=await s.post('/api/sources',{pdf_base64:Buffer.from('%PDF-1.7\nprivate').toString('base64')},a);
  assert.equal(response.status,503);assert.equal(s.repository.tasks.size,0);assert.equal(s.repository.garbage.size,1);assert.equal(s.pdfs.values.size,1);
  s.advance(86_400_001);await s.worker('claim',{worker_id:'mac-test'});assert.equal(s.pdfs.values.size,0);assert.equal(s.repository.garbage.size,0);
});
test('malformed worker report identity cannot be published to the browser',async()=>{
  const s=setup(),a=await s.bootstrap(),src=await s.source(a);await s.post('/api/jobs',{source_id:src.id,target_id:course.id,reviewed:true},a);
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  const result={source:{...src,id:'0'.repeat(32)},target:course,catalog_id:catalog.catalog_id,calculation:{decision:'equivalent'}};
  assert.equal((await s.worker('tasks/'+task.id+'/complete',{lease_token:task.lease_token,result})).status,400);
  const read=await s.request('/api/jobs/'+task.id,{cookie:a.cookie});assert.equal((await read.json()).status,'running');
  assert.equal(s.repository.artifacts.size,1);
});
test('body streaming limit is applied even without a Content-Length header',async()=>{
  const s=setup(),a=await s.bootstrap();
  const response=await s.api(new Request('https://example.net/api/sources',{method:'POST',headers:{Cookie:a.cookie,Origin:'https://example.net','X-Equivalency-Token':a.token,'Content-Type':'application/json'},body:' '.repeat(6_000_000)}));
  assert.equal(response.status,413);
});

test('student requests cannot drop, add, edit or reorder canonical reference topics',async()=>{
  const reference={...course,topics:[{id:'r1',text:'موضوع أول'},{id:'r2',text:'موضوع ثان'}]};
  const s=setup({catalog:{...catalog,courses:[reference]}}),a=await s.bootstrap(),src=await s.source(a);
  for(const target_topics of [[],reference.topics.slice(0,1),[...reference.topics,{id:'extra',text:'إضافة'}],
    [{id:'r1',text:'تحريف'},reference.topics[1]],[...reference.topics].reverse(),[{id:'altered-id',text:'موضوع أول'},reference.topics[1]]]) {
    const response=await s.post('/api/jobs',{source_id:src.id,target_id:reference.id,reviewed:true,target_topics},a);
    assert.equal(response.status,400);
    assert.equal((await response.json()).error,'مفردات المقرر المرجعي ثابتة وفق التوصيف المعتمد');
  }
  assert.equal([...s.repository.tasks.values()].filter(t=>t.kind==='compare').length,0);
});
test('omitting reference topics queues canonical effective text and keeps uploaded source editable',async()=>{
  const reference={...course,topics:[{id:'r1',text:'وشوطه',corrected_text:'وشروطه',page:5},{id:'r2',text:'نص ثابت'}]};
  const original=structuredClone(reference);
  const s=setup({catalog:{...catalog,courses:[reference]}}),a=await s.bootstrap(),src=await s.source(a);
  assert.equal(a.capabilities.reference_editable,false);
  const editedSource=[{id:'t1',text:'قراءة مصححة من المصدر'},{id:'s2',text:'مفردة أخرى في المصدر'}];
  const response=await s.post('/api/jobs',{source_id:src.id,target_id:reference.id,reviewed:true,source_topics:editedSource,
    target:{context:{program:'سياق مزور'},topics:[]}},a);
  assert.equal(response.status,202);
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  assert.deepEqual(task.payload.target_topics,[{id:'r1',text:'وشروطه'},{id:'r2',text:'نص ثابت'}]);
  assert.deepEqual(task.payload.source_topics,editedSource);
  assert.equal(task.payload.target,undefined);
  assert.equal(task.payload.target_id,reference.id);
  assert.deepEqual(reference,original);
});
test('supplied reference rows must match corrected canonical text and discard all client metadata',async()=>{
  const reference={...course,topics:[{id:'r1',text:'وشوطه',corrected_text:'وشروطه',page:5}]};
  const s=setup({catalog:{...catalog,courses:[reference]}}),a=await s.bootstrap(),src=await s.source(a);
  const body={source_id:src.id,target_id:reference.id,reviewed:true};
  assert.equal((await s.post('/api/jobs',{...body,target_topics:[{id:'r1',text:'وشوطه'}]},a)).status,400);
  assert.equal((await s.post('/api/jobs',{...body,target_topics:[{id:'r1',text:' وشروطه '}]},a)).status,400);
  assert.equal((await s.post('/api/jobs',{...body,target_topics:[{...reference.topics[0],page:999,source_pdf:'/private/forged.pdf'}]},a)).status,202);
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  assert.deepEqual(task.payload.target_topics,[{id:'r1',text:'وشروطه'}]);
});
test('worker completion cannot publish a report with a changed reference denominator',async()=>{
  const s=setup(),a=await s.bootstrap(),src=await s.source(a);
  await s.post('/api/jobs',{source_id:src.id,target_id:course.id,reviewed:true},a);
  const task=(await(await s.worker('claim',{worker_id:'mac-test'})).json()).task;
  const result={source:src,target:{...course,topics:[]},catalog_id:catalog.catalog_id,calculation:{decision:'equivalent'}};
  const response=await s.worker('tasks/'+task.id+'/complete',{lease_token:task.lease_token,result});
  assert.equal(response.status,400);
  assert.equal((await response.json()).error,'مفردات المقرر المرجعي ثابتة وفق التوصيف المعتمد');
  const current=(await(await s.request('/api/jobs/'+task.id,{cookie:a.cookie})).json());
  assert.equal(current.status,'running');assert.equal(current.result,undefined);
  assert.equal(s.repository.artifacts.size,1);
});

test('protected claim lanes filter source/search and comparison while default all stays compatible',async()=>{
  const s=setup(),session=await s.bootstrap(),src=await s.source(session);
  const comparison=await s.post('/api/jobs',{source_id:src.id,target_id:course.id,reviewed:true},session);
  const comparisonId=(await comparison.json()).id;
  const source=await s.post('/api/sources',{topics:course.topics},session),sourceId=(await source.json()).operation_id;
  const search=await s.post('/api/search',{source_id:src.id,all_contexts:true},session),searchId=(await search.json()).operation_id;
  const running=(await(await s.worker('claim',{worker_id:'mac-compare',lane:'compare'})).json()).task;
  assert.equal(running.id,comparisonId);assert.equal(running.kind,'compare');
  const interactive=(await(await s.worker('claim',{worker_id:'mac-interactive',lane:'interactive'})).json()).task;
  assert.equal(interactive.id,sourceId);assert.equal(interactive.kind,'source');
  const repeat=(await(await s.worker('claim',{worker_id:'mac-interactive',lane:'interactive'})).json()).task;
  assert.equal(repeat.lease_token,interactive.lease_token);
  assert.equal((await s.worker('tasks/'+interactive.id+'/complete',{lease_token:interactive.lease_token,result:{topics:course.topics}})).status,200);
  const next=(await(await s.worker('claim',{worker_id:'mac-interactive',lane:'interactive'})).json()).task;
  assert.equal(next.id,searchId);assert.equal(next.kind,'search');
  const same=(await(await s.worker('claim',{worker_id:'mac-compare'})).json()).task;
  assert.equal(same.id,running.id);assert.equal(same.lease_token,running.lease_token);
  for(const lane of ['source','COMPARE','',[],{}])assert.equal((await s.worker('claim',{worker_id:'bad',lane})).status,400);
  assert.equal((await s.request('/api/worker/claim',{method:'POST',data:{worker_id:'browser',lane:'interactive'},cookie:session.cookie,token:session.token})).status,403);
});

test('lost source acknowledgment is recoverable by GET without a second upload or enqueue',async()=>{
  const s=setup(),a=await s.bootstrap(),request_id='e'.repeat(32);
  assert.equal(a.capabilities.request_recovery,true);
  let puts=0;const put=s.pdfs.put.bind(s.pdfs);s.pdfs.put=async(...args)=>{puts++;return put(...args);};
  // Discard the acknowledgment as if its response was lost after acceptance.
  assert.equal((await s.post('/api/sources',{request_id,pdf_base64:Buffer.from('%PDF-1.7\noriginal bytes').toString('base64')},a)).status,202);
  const recovered=await s.request('/api/requests/'+request_id,{cookie:a.cookie});assert.equal(recovered.status,200);
  const body=await recovered.json(),task=[...s.repository.tasks.values()][0];
  assert.deepEqual(body,{id:task.id,status:'queued',kind:'source',operation_id:task.id});assert.notEqual(task.id,request_id);
  assert.equal(task.payload.request_id,request_id);assert.ok(task.pdf_key.includes(task.id));
  const repeated=await s.post('/api/sources',{request_id,pdf_base64:Buffer.from('%PDF-1.7\nreplacement bytes').toString('base64')},a);
  assert.equal(repeated.status,202);assert.equal((await repeated.json()).operation_id,task.id);
  assert.equal(puts,1);assert.equal(s.repository.tasks.size,1);assert.equal(s.pdfs.deleted.length,0);
  assert.equal((await s.pdfs.get(task.pdf_key)).toString(),'%PDF-1.7\noriginal bytes');
  const lease=(await(await s.worker('claim',{worker_id:'recovery-test'})).json()).task;
  assert.equal((await s.worker('tasks/'+task.id+'/complete',{lease_token:lease.lease_token,result:{name:'مصدر',topics:course.topics}})).status,200);
  assert.equal((await(await s.request('/api/requests/'+request_id,{cookie:a.cookie})).json()).status,'done');
  assert.equal((await(await s.request('/api/operations/'+body.operation_id,{cookie:a.cookie})).json()).result.id,task.id);
});

test('search and comparison acknowledgments recover with their existing response IDs',async()=>{
  const s=setup(),a=await s.bootstrap(),src=await s.source(a);
  for(const [route,kind,request_id,extra] of [['search','search','1'.repeat(32),{all_contexts:true}],['jobs','compare','2'.repeat(32),{target_id:course.id,reviewed:true}]]) {
    const data={request_id,source_id:src.id,...extra};
    const first=await s.post('/api/'+route,data,a);assert.equal(first.status,202);const created=await first.json();
    const id=created.id || created.operation_id,before=s.repository.tasks.size;
    const recovered=await(await s.request('/api/requests/'+request_id,{cookie:a.cookie})).json();
    assert.deepEqual(recovered,{id,status:'queued',kind,...(kind==='compare'?{}:{operation_id:id})});
    assert.equal((await s.post('/api/'+route,data,a)).status,202);assert.equal(s.repository.tasks.size,before);
    assert.equal([...s.repository.tasks.values()].find(t=>t.id===id).payload.request_id,request_id);
  }
});

test('invalid recovery IDs and cross-kind reuse reject before writes',async()=>{
  const s=setup(),a=await s.bootstrap();
  for(const route of ['sources','search','jobs']) for(const request_id of [null,42,{},[],true,'','f'.repeat(31),'F'.repeat(32),'g'.repeat(32)]) {
    assert.equal((await s.post('/api/'+route,{request_id,pdf_base64:Buffer.from('%PDF-1.7\nnever written').toString('base64')},a)).status,400);
  }
  assert.equal(s.repository.tasks.size,0);assert.equal(s.repository.garbage.size,0);assert.equal(s.pdfs.values.size,0);
  const request_id='3'.repeat(32);assert.equal((await s.post('/api/sources',{request_id,topics:course.topics},a)).status,202);
  for(const route of ['search','jobs'])assert.equal((await s.post('/api/'+route,{request_id},a)).status,409);
  assert.equal(s.repository.tasks.size,1);assert.equal(s.pdfs.values.size,0);
});

test('request recovery protects session, deployment scope and task expiry without leaking payloads',async()=>{
  const s=setup(),a=await s.bootstrap(),b=await s.bootstrap(),request_id='4'.repeat(32);
  await s.post('/api/sources',{request_id,topics:course.topics},a);
  assert.equal((await s.request('/api/requests/'+request_id,{cookie:b.cookie})).status,404);
  assert.equal((await s.request('/api/requests/'+'5'.repeat(32),{cookie:a.cookie})).status,404);
  const preview=setup({repository:s.repository,env:{CONTEXT:'deploy-preview',DEPLOY_ID:'another-deployment'}}),p=await preview.bootstrap();
  assert.equal((await preview.request('/api/requests/'+request_id,{cookie:p.cookie})).status,404);
  assert.equal((await s.request('/api/requests/'+request_id)).status,401);
  assert.equal((await s.request('/api/requests/'+'Z'.repeat(32),{cookie:a.cookie})).status,404);
  const recovered=await(await s.request('/api/requests/'+request_id,{cookie:a.cookie})).json();
  assert.deepEqual(Object.keys(recovered).sort(),['id','kind','operation_id','status']);
  s.advance(86_400_000);assert.equal((await s.request('/api/requests/'+request_id,{cookie:a.cookie})).status,404);
});

test('legacy requests retain independent random IDs and no recovery payload field',async()=>{
  const s=setup(),a=await s.bootstrap();
  const first=await(await s.post('/api/sources',{topics:course.topics},a)).json();
  const second=await(await s.post('/api/sources',{topics:course.topics},a)).json();
  assert.notEqual(first.operation_id,second.operation_id);assert.equal(s.repository.tasks.size,2);
  for(const task of s.repository.tasks.values())assert.equal(Object.hasOwn(task.payload,'request_id'),false);
});

test('uploaded comparison queues a server-owned target snapshot and restores authoritative report metadata',async()=>{
  const s=setup(),a=await s.bootstrap(),source=await s.source(a),target=await s.source(a,{pdf:true});
  assert.equal(a.capabilities.uploaded_target,true);
  const response=await s.post('/api/jobs',{source_id:source.id,target_source_id:target.id,review_mode:'automatic',target:{id:'forged',topics:[]},beneficiary:{name:'مستفيد',number:'DEMO-02'}},a);
  assert.equal(response.status,202);const {id}=await response.json();
  const task=(await(await s.worker('claim',{worker_id:'uploaded-target'})).json()).task;
  assert.equal(task.id,id);assert.equal(task.payload.target_mode,'uploaded');assert.equal(task.payload.target_source_id,target.id);
  assert.equal(task.payload.target_id,undefined);assert.deepEqual(task.payload.target,target);assert.deepEqual(task.payload.target_topics,course.topics);
  const result={source,target:{...target,name:'اسم مزور',context:{program:'نطاق مزور'},source_pdf:'https://foreign.test/private.pdf',upload_pdf:false,topics:target.topics.map(t=>({...t,page:999}))},target_mode:'catalog',catalog_id:catalog.catalog_id,calculation:{decision:'insufficient'}};
  assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result})).status,200);
  const report=(await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json()).result;
  assert.deepEqual(report.target,target);assert.equal(report.target.id,target.id);assert.equal(report.target_mode,'uploaded');
  assert.equal(report.human_reviewed,false);assert.equal(report.reviewed_by,'automated_pipeline');assert.equal(report.beneficiary.number,'DEMO-02');
  assert.equal(report.payload,undefined);assert.equal(report.target.pdf_key,undefined);
  assert.equal((await s.request('/pdf/'+target.id,{cookie:a.cookie})).status,200);
});

test('jobs require exactly one target selector and forbid uploaded target scope or topic overrides',async()=>{
  const s=setup(),a=await s.bootstrap(),source=await s.source(a),target=await s.source(a,{pdf:true});
  const base={source_id:source.id,review_mode:'automatic'};
  for(const selectors of [{},{target_id:course.id,target_source_id:target.id},{target_source_id:null},{target_source_id:'bad'},{target_id:null}]) {
    assert.equal((await s.post('/api/jobs',{...base,...selectors},a)).status,400);
  }
  for(const extra of [{target_topics:target.topics},{context_id:'plan47'},{context_ids:[]},{all_contexts:false},{version:'47'},{catalog_id:course.id}]) {
    assert.equal((await s.post('/api/jobs',{...base,target_source_id:target.id,...extra},a)).status,400);
  }
  assert.equal([...s.repository.tasks.values()].filter(t=>t.kind==='compare').length,0);
  assert.equal((await s.post('/api/jobs',{...base,target_id:course.id},a)).status,202);
});

test('uploaded target must be a completed owned actual upload, not manual or catalog artifacts',async()=>{
  const s=setup(),a=await s.bootstrap(),b=await s.bootstrap(),source=await s.source(a),manual=await s.source(a),fromCatalog=await s.source(a,{catalogId:course.id}),foreign=await s.source(b,{pdf:true});
  const base={source_id:source.id,review_mode:'automatic'};
  for(const target of [manual,fromCatalog])assert.equal((await s.post('/api/jobs',{...base,target_source_id:target.id},a)).status,400);
  assert.equal((await s.post('/api/jobs',{...base,target_source_id:foreign.id},a)).status,404);
  const queued=await(await s.post('/api/sources',{pdf_base64:Buffer.from('%PDF-1.7\nnot ready').toString('base64')},a)).json();
  assert.equal((await s.post('/api/jobs',{...base,target_source_id:queued.operation_id},a)).status,404);
  assert.equal([...s.repository.tasks.values()].filter(t=>t.kind==='compare').length,0);
});

test('source and uploaded target TTLs are checked independently before accepting a comparison',async()=>{
  const s=setup(),a=await s.bootstrap(),source=await s.source(a),target=await s.source(a,{pdf:true});
  const sourceArtifact=[...s.repository.artifacts.values()].find(t=>t.id===source.id),targetArtifact=[...s.repository.artifacts.values()].find(t=>t.id===target.id);
  const expires=sourceArtifact.expires_at,body={source_id:source.id,target_source_id:target.id,review_mode:'automatic'};
  targetArtifact.expires_at=s.now();assert.equal((await s.post('/api/jobs',body,a)).status,404);targetArtifact.expires_at=expires;
  sourceArtifact.expires_at=s.now();assert.equal((await s.post('/api/jobs',body,a)).status,404);
  assert.equal([...s.repository.tasks.values()].filter(t=>t.kind==='compare').length,0);
});

test('claim fences expired or changed uploaded target evidence without changing the queued snapshot',async()=>{
  for(const mutation of ['expired','topics','sha','id']) {
    const s=setup(),a=await s.bootstrap(),source=await s.source(a),target=await s.source(a,{pdf:true});
    const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_source_id:target.id,review_mode:'automatic'},a)).json();
    const artifact=[...s.repository.artifacts.values()].find(t=>t.id===target.id),task=[...s.repository.tasks.values()].find(t=>t.id===id);
    const frozen=structuredClone(task.payload.target);
    if(mutation==='expired')artifact.expires_at=s.now();
    if(mutation==='topics')artifact.value.topics=[{id:'t1',text:'نص مختلف'}];
    if(mutation==='sha')artifact.value.source_sha256='different';
    if(mutation==='id')artifact.value.id='f'.repeat(32);
    const claimed=await s.worker('claim',{worker_id:'target-fenced'});assert.equal((await claimed.json()).task,null);
    assert.deepEqual(task.payload.target,frozen);
    const state=await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json();assert.equal(state.status,'error');assert.equal(state.result,undefined);
  }
});

test('uploaded target worker completion rejects wrong identity, SHA or denominator before publishing',async()=>{
  const s=setup(),a=await s.bootstrap(),source=await s.source(a),target=await s.source(a,{pdf:true});
  const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_source_id:target.id,review_mode:'automatic'},a)).json();
  const task=(await(await s.worker('claim',{worker_id:'target-proof'})).json()).task;
  const base={source,target,catalog_id:catalog.catalog_id,calculation:{decision:'equivalent'}};
  for(const forged of [{...target,id:course.id},{...target,source_sha256:'different'},{...target,topics:[]},{...target,topics:[{id:'t1',text:'موضوع مختلف'}]}]) {
    assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result:{...base,target:forged}})).status,400);
    assert.equal((await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json()).status,'running');
  }
  assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result:base})).status,200);
});

test('leased target PDF download uses the owned target artifact, never caller paths or public credentials',async()=>{
  const s=setup(),a=await s.bootstrap(),b=await s.bootstrap(),source=await s.source(a),target=await s.source(a,{pdf:true});
  const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_source_id:target.id,review_mode:'automatic',target_pdf_key:'forged/path'},a)).json();
  const task=(await(await s.worker('claim',{worker_id:'target-download'})).json()).task;
  const path='/api/worker/tasks/'+id+'/input-target-pdf',headers={Authorization:'Bearer '+env.COURSEMATCH_WORKER_TOKEN,'X-Lease-Token':task.lease_token};
  const response=await s.request(path,{headers});assert.equal(response.status,200);assert.equal(await response.text(),'%PDF-1.7\nfixture');
  assert.equal((await s.request(path,{headers:{...headers,'X-Lease-Token':'0'.repeat(48)}})).status,409);
  assert.equal((await s.request(path,{cookie:b.cookie})).status,403);
  assert.equal((await s.request('/api/worker/tasks/'+id+'/input-pdf',{headers})).status,404); // source is manual
  assert.equal(task.payload.target_pdf_key,undefined);
  await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,error:'اختبار انتهاء الحجز'});
  assert.equal((await s.request(path,{headers})).status,409);
  const catalogJob=await(await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'automatic'},a)).json();
  const catalogTask=(await(await s.worker('claim',{worker_id:'target-download'})).json()).task;
  assert.equal(catalogTask.id,catalogJob.id);
  assert.equal((await s.request('/api/worker/tasks/'+catalogJob.id+'/input-target-pdf',{headers:{...headers,'X-Lease-Token':catalogTask.lease_token}})).status,404);
});

test('uploaded comparison preserves request recovery without another enqueue or payload exposure',async()=>{
  const s=setup(),a=await s.bootstrap(),source=await s.source(a),target=await s.source(a,{pdf:true}),request_id='9'.repeat(32);
  const accepted=await s.post('/api/jobs',{source_id:source.id,target_source_id:target.id,review_mode:'automatic',request_id},a);assert.equal(accepted.status,202);
  const {id}=await accepted.json(),count=s.repository.tasks.size;
  assert.deepEqual(await(await s.request('/api/requests/'+request_id,{cookie:a.cookie})).json(),{id,status:'queued',kind:'compare'});
  assert.equal((await(await s.post('/api/jobs',{request_id},a)).json()).id,id);assert.equal(s.repository.tasks.size,count);
  const publicTask=await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json();assert.equal(publicTask.payload,undefined);assert.equal(publicTask.target,undefined);
});

test('new final verdicts reject unavailable or uncertain uploaded sources even if worker hides their status',async()=>{
  for(const status of ['unavailable','uncertain'])for(const decision of ['equivalent','lenient','not_equivalent']) {
    const s=setup(),a=await s.bootstrap(),source=await s.source(a,{pdf:true,visualStatus:status});
    const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'automatic'},a)).json();
    const task=(await(await s.worker('claim',{worker_id:'reading-guard'})).json()).task;
    const result={source:{...source,upload_pdf:false,visual_verification_status:'completed'},target:course,catalog_id:catalog.catalog_id,calculation:{decision}};
    const response=await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result});
    assert.equal(response.status,400);assert.equal((await response.json()).error,'تعذر اعتماد هذا الحكم لعدم ثبوت قراءة أحد التوصيفين.');
    const state=await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json();assert.equal(state.status,'running');assert.equal(state.result,undefined);
    assert.equal([...s.repository.artifacts.values()].filter(a=>a.kind==='report').length,0);
  }
});

test('uploaded target reading gate runs after canonical snapshot and permits only insufficient',async()=>{
  for(const status of ['unavailable','uncertain']) {
    const s=setup(),a=await s.bootstrap(),source=await s.source(a),target=await s.source(a,{pdf:true,visualStatus:status});
    const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_source_id:target.id,review_mode:'automatic'},a)).json();
    const task=(await(await s.worker('claim',{worker_id:'target-reading-guard'})).json()).task;
    const base={source,target:{...target,upload_pdf:false,visual_verification_status:'completed'},catalog_id:catalog.catalog_id};
    for(const decision of ['equivalent','lenient','not_equivalent']) {
      assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result:{...base,calculation:{decision}}})).status,400);
    }
    assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result:{...base,calculation:{decision:'insufficient'}}})).status,200);
    const report=(await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json()).result;
    assert.equal(report.calculation.decision,'insufficient');assert.equal(report.target.upload_pdf,true);assert.equal(report.target.visual_verification_status,status);
    assert.equal((await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{name:'مستفيد',number:'SAFE-HELD'}},a)).status,200);
  }
});

test('historical unsafe final reports stay raw in storage but GET and beneficiary update cannot republish them',async()=>{
  for(const role of ['source','target'])for(const status of ['unavailable','uncertain']) {
    const s=setup(),a=await s.bootstrap(),b=await s.bootstrap();
    const source=await s.source(a,{pdf:role==='source',visualStatus:role==='source'?status:undefined});
    const target=role==='target'?await s.source(a,{pdf:true,visualStatus:status}):course;
    const job=await(await s.post('/api/jobs',{source_id:source.id,...(role==='target'?{target_source_id:target.id}:{target_id:target.id}),review_mode:'automatic'},a)).json();
    const task=(await(await s.worker('claim',{worker_id:'historical-reading-guard'})).json()).task;
    const result={source:{...source,warnings:['PRIVATE_WARNING_DO_NOT_DISPLAY'],source_note:'PRIVATE_RAW_READING'},target,catalog_id:catalog.catalog_id,calculation:{decision:'insufficient'}};
    assert.equal((await s.worker('tasks/'+job.id+'/complete',{lease_token:task.lease_token,result})).status,200);
    // Fixture only: represent a report persisted by the pre-guard version.
    const storedTask=[...s.repository.tasks.values()].find(t=>t.id===job.id),artifact=[...s.repository.artifacts.values()].find(a=>a.id===job.id);
    storedTask.result.calculation={decision:'not_equivalent',strict_count:0,lenient_count:0,total_topics:13,strict_coverage:0,lenient_coverage:0};
    const rawTask=structuredClone(storedTask),rawArtifact=structuredClone(artifact);
    const response=await s.request('/api/jobs/'+job.id,{cookie:a.cookie});assert.equal(response.status,200);
    const blocked=await response.json();assert.deepEqual(blocked,{id:job.id,status:'error',message:'تعذر اعتماد هذا الحكم لعدم ثبوت قراءة أحد التوصيفين.',error:'تعذر اعتماد هذا الحكم لعدم ثبوت قراءة أحد التوصيفين.'});
    assert.equal(blocked.result,undefined);assert.equal(JSON.stringify(blocked).includes('PRIVATE_'),false);
    let updates=0;const update=s.repository.updateBeneficiary.bind(s.repository);s.repository.updateBeneficiary=async(...args)=>{updates++;return update(...args);};
    const denied=await s.post('/api/reports/'+job.id+'/beneficiary',{beneficiary:{name:'تعديل',number:'DENIED'}},a);
    assert.equal(denied.status,400);assert.equal((await denied.json()).error,blocked.error);assert.equal(updates,0);
    assert.equal((await s.request('/api/jobs/'+job.id,{cookie:b.cookie})).status,404);
    assert.equal((await s.post('/api/reports/'+job.id+'/beneficiary',{beneficiary:{name:'غير المالك'}},b)).status,404);
    assert.deepEqual(storedTask,rawTask);assert.deepEqual(artifact,rawArtifact);assert.equal(storedTask.status,'done');
    assert.equal(storedTask.result.calculation.decision,'not_equivalent');assert.equal(storedTask.result.calculation.total_topics,13);
  }
});

test('manual, canonical and completed uploads keep their existing final report and beneficiary routes',async()=>{
  for(const kind of ['manual','catalog','completed']) {
    const s=setup(),a=await s.bootstrap();
    const source=await s.source(a,kind==='catalog'?{catalogId:course.id,visualStatus:'unavailable'}:kind==='completed'?{pdf:true,visualStatus:'completed'}:{visualStatus:'uncertain'});
    const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'automatic'},a)).json();
    const task=(await(await s.worker('claim',{worker_id:'safe-reading-report'})).json()).task;
    const result={source,target:{...course,visual_verification_status:'unavailable'},catalog_id:catalog.catalog_id,calculation:{decision:'not_equivalent'}};
    assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result})).status,200);
    const state=await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json();assert.equal(state.status,'done');assert.equal(state.result.calculation.decision,'not_equivalent');
    assert.equal((await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{number:'SAFE-FINAL'}},a)).status,200);
  }
});

const uncertainRejectionNotice='لم تثبت أدلة كافية لاعتماد حكم الرفض.';
const pendingPositiveEvidence=()=>({provider:'ollama_local',pairs:[{target_id:'t1',source_id:'t1',score:null,candidate_only:true,
  reason:'PRIVATE_UNRESOLVED_REASON',adjudication:{state:'uncertain',possible_tiers:['lenient'],review:{reason:'PRIVATE_RAW_REVIEW'}}}]});

test('new definitive rejection with a serious unresolved lenient alternative cannot be published',async()=>{
 for(const possible_tiers of [['lenient'],['strict','lenient']]){
  const s=setup(),a=await s.bootstrap(),source=await s.source(a);
  const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'automatic'},a)).json();
  const task=(await(await s.worker('claim',{worker_id:'uncertain-rejection-guard'})).json()).task;
  const evidence=pendingPositiveEvidence();evidence.pairs[0].adjudication.possible_tiers=possible_tiers;
  const result={source,target:course,catalog_id:catalog.catalog_id,evidence,calculation:{decision:'not_equivalent',strict_coverage:0,lenient_coverage:0}};
  const before=structuredClone(result),response=await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result});
  assert.equal(response.status,400);assert.equal((await response.json()).error,uncertainRejectionNotice);assert.deepEqual(result,before);
  const state=await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json();assert.equal(state.status,'running');assert.equal(state.result,undefined);
  assert.equal([...s.repository.artifacts.values()].filter(r=>r.kind==='report').length,0);
 }
});

test('historical uncertain rejection cannot be read or updated while raw evidence and ownership remain intact',async()=>{
 const s=setup(),a=await s.bootstrap(),b=await s.bootstrap(),source=await s.source(a);
 const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'automatic'},a)).json();
 const task=(await(await s.worker('claim',{worker_id:'historical-negative-guard'})).json()).task;
 const result={source,target:course,catalog_id:catalog.catalog_id,evidence:pendingPositiveEvidence(),calculation:{decision:'insufficient'}};
 assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result})).status,200);
 // Test fixture only: represent the exact pre-guard historical verdict.
 const rawTask=[...s.repository.tasks.values()].find(r=>r.id===id),artifact=[...s.repository.artifacts.values()].find(r=>r.id===id);
 rawTask.result.calculation={decision:'not_equivalent',strict_count:0,lenient_count:0,total_topics:13,strict_coverage:0,lenient_coverage:0};
 artifact.value.calculation=rawTask.result.calculation;
 const savedTask=structuredClone(rawTask),savedArtifact=structuredClone(artifact);
 const response=await s.request('/api/jobs/'+id,{cookie:a.cookie});assert.equal(response.status,200);
 const blocked=await response.json();assert.deepEqual(blocked,{id,status:'error',message:uncertainRejectionNotice,error:uncertainRejectionNotice});
 assert.equal(blocked.result,undefined);assert.equal(JSON.stringify(blocked).includes('PRIVATE_'),false);assert.ok(!blocked.message.includes('قراءة'));
 let updates=0;const update=s.repository.updateBeneficiary.bind(s.repository);s.repository.updateBeneficiary=async(...args)=>{updates++;return update(...args)};
 const denied=await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{name:'تعديل لا يعتمد',number:'DENIED'}},a);
 assert.equal(denied.status,400);assert.equal((await denied.json()).error,uncertainRejectionNotice);assert.equal(updates,0);
 assert.equal((await s.request('/api/jobs/'+id,{cookie:b.cookie})).status,404);assert.equal((await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{number:'FOREIGN'}},b)).status,404);
 assert.deepEqual(rawTask,savedTask);assert.deepEqual(artifact,savedArtifact);assert.equal(rawTask.status,'done');assert.equal(rawTask.result.calculation.decision,'not_equivalent');
});

test('positive verdicts, pending studies and definitive weak or excluded rejections retain public routes',async()=>{
 const cases=[
  {decision:'equivalent',adjudication:{state:'uncertain',possible_tiers:['lenient']}},
  {decision:'lenient',adjudication:{state:'uncertain',possible_tiers:['strict','lenient']}},
  {decision:'insufficient',adjudication:{state:'uncertain',possible_tiers:['lenient']}},
  {decision:'not_equivalent',adjudication:{state:'confirmed',possible_tiers:[],review:{verdict:'weak'}}},
  {decision:'not_equivalent',adjudication:{state:'uncertain',possible_tiers:[],kind:'deterministic_atomic_scope'}},
  {decision:'not_equivalent',adjudication:undefined}
 ];
 for(const fixture of cases){
  const s=setup(),a=await s.bootstrap(),source=await s.source(a),{id}=await(await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'automatic'},a)).json();
  const task=(await(await s.worker('claim',{worker_id:'definitive-evidence-guard'})).json()).task;
  const evidence={pairs:[{target_id:'t1',source_id:'t1',score:40,...(fixture.adjudication?{adjudication:fixture.adjudication}:{})}]};
  const result={source,target:course,catalog_id:catalog.catalog_id,evidence,calculation:{decision:fixture.decision}};
  assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result})).status,200,JSON.stringify(fixture));
  const returned=await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json();assert.equal(returned.status,'done');assert.equal(returned.result.calculation.decision,fixture.decision);assert.deepEqual(returned.result.evidence,evidence);
  const saved=await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{name:'مستفيد محفوظ',number:'SAFE'}},a);assert.equal(saved.status,200);assert.deepEqual((await saved.json()).evidence,evidence);
 }
});

test('bound full-curriculum rejection is allowed despite a nondecisive possible target and stays private',async()=>{
 const target={...course,topics:Array.from({length:5},(_,i)=>({id:'t'+(i+1),text:'موضوع الهدف '+(i+1)}))};
 const s=setup({catalog:{...catalog,courses:[target]}}),a=await s.bootstrap(),b=await s.bootstrap(),source=await s.source(a);
 const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_id:target.id,review_mode:'automatic'},a)).json();
 const task=(await(await s.worker('claim',{worker_id:'full-curriculum-tested'})).json()).task;
 const evidence=curriculumEvidence(source,target);
 const calculation=curriculumCalculation(evidence,target);
 assert.equal(calculation.decision,'not_equivalent');assert.equal(calculation.possible_lenient_count,1);
 const result={source,target,catalog_id:catalog.catalog_id,evidence,calculation};
 assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result})).status,200);
 const returned=await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json();
 assert.equal(returned.status,'done');assert.deepEqual(returned.result.calculation,calculation);
 assert.deepEqual(returned.result.evidence,evidence);
 assert.equal((await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{number:'SAFE-CURRICULUM'}},a)).status,200);
 assert.equal((await s.request('/api/jobs/'+id,{cookie:b.cookie})).status,404);
});

test('curriculum proof cannot be accepted with changed text, incomplete source scope or a relabeled rubric',async()=>{
 for(const mutate of [
   r=>{r.source.topics=[{...r.source.topics[0],text:'محتوى آخر لا يطابق البصمة'}]},
   r=>{r.evidence.scope_review.source_ids=[]},
   r=>{r.evidence.scope_review.target_ids=[]},
   r=>{r.evidence.scope_review.complete=false},
   r=>{r.evidence.rubric='old-rubric'},
   r=>{r.evidence.inputs_sha256='0'.repeat(64)},
   r=>{r.calculation.accounting='source_rows'},
   r=>{r.evidence.pairs[0].score=8},
   r=>{r.evidence.pairs=[]},
   r=>{r.calculation.strict_coverage=0},
   r=>{r.calculation.total_topics++},
   r=>{r.calculation.decision='not_equivalent'},
   r=>{r.calculation.strict_matches.t1=['FOREIGN_SOURCE']},
   r=>{r.evidence.pairs[0].adjudication.confirmed_tiers=['lenient']},
   r=>{r.evidence.pairs[0].adjudication.possible_tiers=[]}
 ]){
  const s=setup(),a=await s.bootstrap(),source=await s.source(a);
  const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'automatic'},a)).json();
  const task=(await(await s.worker('claim',{worker_id:'bound-input-test'})).json()).task;
  const result={source:structuredClone(source),target:course,catalog_id:catalog.catalog_id,evidence:curriculumEvidence(source,course,'strict'),
    calculation:curriculumCalculation(curriculumEvidence(source,course,'strict'),course)};
  mutate(result);
  const response=await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result});
  assert.equal(response.status,400);assert.equal((await response.json()).error,'لم تثبت مطابقة أدلة المحتوى مع المقررين محل المعادلة.');
  assert.equal([...s.repository.artifacts.values()].filter(r=>r.kind==='report').length,0);
 }
});

test('historical curriculum binding failure is hidden without raw mutation or beneficiary revival',async()=>{
 const s=setup(),a=await s.bootstrap(),source=await s.source(a);
 const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_id:course.id,review_mode:'automatic'},a)).json();
 const task=(await(await s.worker('claim',{worker_id:'historical-curriculum-test'})).json()).task;
 const result={source,target:course,catalog_id:catalog.catalog_id,evidence:curriculumEvidence(source,course),
   calculation:curriculumCalculation(curriculumEvidence(source,course),course)};
 assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result})).status,200);
 const rawTask=[...s.repository.tasks.values()].find(r=>r.id===id),rawArtifact=[...s.repository.artifacts.values()].find(r=>r.id===id);
 rawTask.result.evidence.inputs_sha256='incorrect-historical-binding';rawArtifact.value.evidence.inputs_sha256='incorrect-historical-binding';
 const before=JSON.stringify([rawTask,rawArtifact]);
 const blocked=await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json();
 assert.equal(blocked.status,'error');assert.equal(blocked.result,undefined);
 assert.equal((await s.post('/api/reports/'+id+'/beneficiary',{beneficiary:{number:'SHOULD-NOT-REVIVE'}},a)).status,400);
 assert.equal(JSON.stringify([rawTask,rawArtifact]),before);
});

test('publication independently verifies shared tolerance at exactly eighty while strict remains possible',async()=>{
 const target={...course,topics:Array.from({length:5},(_,i)=>({id:'t'+i,text:'مبحث اختبار رمزي '+i}))};
 const s=setup({catalog:{...catalog,courses:[target]}}),a=await s.bootstrap(),source=await s.source(a);
 const {id}=await(await s.post('/api/jobs',{source_id:source.id,target_id:target.id,review_mode:'automatic'},a)).json();
 const task=(await(await s.worker('claim',{worker_id:'nested-threshold-consensus'})).json()).task;
 const evidence=curriculumEvidence(source,target,'strict');
 const strict=structuredClone(evidence.pairs[0]);
 for(let i=1;i<4;i++){evidence.pairs[i]=structuredClone(strict);evidence.pairs[i].target_id='t'+i;evidence.pairs[i].adjudication.review.target_id='t'+i;}
 const partial=evidence.pairs[3];partial.adjudication.state='partial';partial.adjudication.confirmed_tiers=['lenient'];
 Object.assign(partial.adjudication.review,{verdict:'lenient',possible_tiers:['strict','lenient'],uncertainty:'non_decisive'});
 const calculation=curriculumCalculation(evidence,target);
 assert.equal(calculation.strict_count,3);assert.equal(calculation.lenient_count,4);assert.equal(calculation.lenient_coverage,80);
 assert.equal(calculation.possible_strict_count,4);assert.equal(calculation.decision,'lenient');
 const before=JSON.stringify(evidence);
 assert.equal((await s.worker('tasks/'+id+'/complete',{lease_token:task.lease_token,result:{source,target,catalog_id:catalog.catalog_id,evidence,calculation}})).status,200);
 const returned=await(await s.request('/api/jobs/'+id,{cookie:a.cookie})).json();
 assert.equal(returned.status,'done');assert.deepEqual(returned.result.calculation,calculation);
 assert.equal(returned.result.evidence.pairs[3].band,'strict');assert.equal(returned.result.evidence.pairs[3].adjudication.review.verdict,'lenient');
 assert.equal(JSON.stringify(evidence),before);
});
