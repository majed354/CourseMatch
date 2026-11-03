'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const textOf = t => t.corrected_text || t.text;
const pageOf = t => t.page ? String(t.page)+(t.end_page&&t.end_page!==t.page?'–'+t.end_page:'') : '—';
let catalog, token, source, target, currentReport;
let comparisonMode='repository', uploadedTarget=null, modeSequence=0, targetInputSequence=0, comparisonSequence=0;
let searchSequence=0, targetContextConfirmed=false, requestRecovery=false;
let sourceInputSequence=0, maxUploadBytes=12*1024*1024, referenceEditable=true;
const candidateVariants=new Map();
const groupKey = c => c.program+' | '+c.degree;
const selectedContext = () => catalog.contexts.find(c=>c.id===$('version').value);
function notice(message, error=false, busy=false) { $('notice').className='status'+(error?' error':'')+(busy?' busy':''); $('notice').textContent=message; }
async function sendApi(route, body, attempt=0) {
  let r;
  try { r=await fetch(route, body ? {method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Equivalency-Token':token},body:JSON.stringify(body)} : {credentials:'same-origin'}); }
  catch(error) {
    if(body||attempt>=6)throw error;
    notice('تعذر الاتصال مؤقتًا؛ سنواصل متابعة طلبك تلقائيًا.',false,true);
    await new Promise(resolve=>setTimeout(resolve,Math.min(3000*2**attempt,30000)));
    return api(route,undefined,attempt+1);
  }
  if(!body&&[429,500,502,503,504,530].includes(r.status)&&attempt<6) {
    const retry=Number(r.headers?.get?.('retry-after'));
    const delay=Number.isFinite(retry)&&retry>0?Math.min(retry*1000,30000):Math.min(3000*2**attempt,30000);
    notice('الخدمة مشغولة مؤقتًا؛ سنواصل متابعة طلبك تلقائيًا.',false,true);
    await new Promise(resolve=>setTimeout(resolve,delay));
    return api(route,undefined,attempt+1);
  }
  let data;
  try { data=await r.json(); }
  catch(error) {
    if(!r.ok)throw Object.assign(new Error('تعذر تنفيذ الطلب'),{status:r.status});
    if(body||attempt>=6)throw new Error('وصلت استجابة غير مكتملة من الخدمة؛ تعذر قراءة نتيجة الطلب.');
    notice('وصلت استجابة غير مكتملة؛ سنواصل متابعة طلبك تلقائيًا.',false,true);
    await new Promise(resolve=>setTimeout(resolve,Math.min(3000*2**attempt,30000)));
    return api(route,undefined,attempt+1);
  }
  if(!r.ok) throw Object.assign(new Error(data.error || 'تعذر تنفيذ الطلب'),{status:r.status});
  if(r.status===202&&data.operation_id)return waitForOperation(data.operation_id);
  return data;
}
const pendingQueueRequests=new Map();
try {
  const saved=JSON.parse(sessionStorage.getItem('coursematch.pendingRequests')||'[]');
  for(const [key,value] of saved)if(/^[a-f0-9]{64}$/.test(key)&&/^[a-f0-9]{32}$/.test(value.id)&&['/api/sources','/api/search','/api/jobs'].includes(value.route)&&Number.isFinite(value.at)&&Date.now()-value.at<86400000)pendingQueueRequests.set(key,value);
}catch{}
function persistQueueRequests(){try{sessionStorage.setItem('coursematch.pendingRequests',JSON.stringify([...pendingQueueRequests]));}catch{}}
function finishQueueRequest(record){if(record){pendingQueueRequests.delete(record.key);persistQueueRequests();}}
async function queueRequestRecord(route,body){
  const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(route+'\n'+JSON.stringify(body)));
  const key=Array.from(new Uint8Array(bytes),b=>b.toString(16).padStart(2,'0')).join('');
  const saved=pendingQueueRequests.get(key);
  if(saved)return {...saved,key,existing:true};
  const value={id:crypto.randomUUID().replaceAll('-',''),route,at:Date.now()};
  pendingQueueRequests.set(key,value);persistQueueRequests();return {...value,key,existing:false};
}
async function recoverQueueRequest(record){
  let missing=0,last;
  for(let attempt=0;attempt<7;attempt++){
    try{
      const value=await api('/api/requests/'+record.id);
      const expected=record.route==='/api/jobs'?'compare':record.route==='/api/search'?'search':'source';
      if(!/^[a-f0-9]{32}$/.test(value.id)||value.kind!==expected||(expected!=='compare'&&value.operation_id!==value.id))throw Object.assign(new Error('تعذر التحقق من هوية الطلب المستعاد.'),{status:400});
      return value;
    }catch(error){
      last=error;
      if(error.status===404){if(++missing>=3){finishQueueRequest(record);throw new Error('لم يثبت استلام الخادم للطلب؛ لم نكرر إرساله تلقائيًا.');}}
      else if(error.status&&error.status<500&&error.status!==429)throw error;
    }
    notice('انقطع استلام النتيجة مؤقتًا؛ نستعيد طلبك القائم تلقائيًا.',false,true);
    await new Promise(resolve=>setTimeout(resolve,3000));
  }
  throw last||new Error('تعذر استعادة الطلب مؤقتًا؛ حُفظ معرّفه دون تكرار إرساله.');
}
async function waitForOperation(id){
  const started=Date.now();
  while(Date.now()-started<20*60*1000){
    await new Promise(resolve=>setTimeout(resolve,1500));
    const status=await api('/api/operations/'+encodeURIComponent(id));
    if(status.status==='done')return status.result;
    if(status.status==='error')throw Object.assign(new Error(status.error||status.message||'تعذر إكمال الطلب'),{taskFailed:true});
    notice(status.message||'الطلب قيد المعالجة…',false,true);
  }
  throw new Error('استغرق الطلب وقتًا طويلًا؛ حُفظ معرّفه لمتابعته دون إعادة الإرسال.');
}
async function api(route,body,attempt=0){
  if(!body||!requestRecovery||!['/api/sources','/api/search','/api/jobs'].includes(route))return sendApi(route,body,attempt);
  const record=await queueRequestRecord(route,body);
  if(!record.existing){
    try{const value=await sendApi(route,{...body,request_id:record.id},attempt);finishQueueRequest(record);return value;}
    catch(error){
      if(error.taskFailed||(error.status&&error.status<500)){finishQueueRequest(record);throw error;}
    }
  }
  const accepted=await recoverQueueRequest(record);
  try{
    const value=route==='/api/jobs'?accepted:await waitForOperation(accepted.operation_id);
    finishQueueRequest(record);return value;
  }catch(error){if(error.taskFailed)finishQueueRequest(record);throw error;}
}
async function action(button, task) { button.disabled=true; try { await task(); } catch(e) { notice(e.message,true); } finally { button.disabled=false;if(button===$('pairCompareButton'))syncPairState(); } }
function clearReportReference() {
  if(location.search.includes('report='))history.replaceState(null,'',location.pathname||'/');
}
async function waitForReport(id,expected=null) {
  const submitted=expected?.beneficiary||beneficiaryValues();
  const inputRevision=sourceInputSequence,scopeRevision=searchSequence,modeRevision=modeSequence,targetRevision=targetInputSequence;
  history.replaceState(null,'','?report='+encodeURIComponent(id));
  while(true) {
    const job=await api('/api/jobs/'+encodeURIComponent(id));
    if(job.status==='error')throw new Error(job.message||job.error||'تعذر إكمال المقارنة');
    if(job.status==='done') {
      if((expected&&(source?.id!==expected.sourceId||target?.id!==expected.targetId||expected.revision!==comparisonSequence||expected.mode!==comparisonMode))||modeRevision!==modeSequence||targetRevision!==targetInputSequence||
         (!expected&&(inputRevision!==sourceInputSequence||scopeRevision!==searchSequence))) {
        notice('اكتملت المقارنة السابقة؛ يمكنك العودة إليها من رابط التقرير.');return;
      }
      showReportPreservingEdits(job.result,submitted);return;
    }
    notice(job.message||'المقارنة قيد المعالجة…',false,true);
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
}
function courseLabel(c) { return `${c.code} · ${c.name} · ${c.context?c.context.program+' · '+c.context.degree+' · '+c.context.version:'مصدر إضافي بلا خطة محددة'}${c.topics.length?'':c.status==='no_fixed_topics'?' · بلا مفردات ثابتة':c.source_pdf?' · المفردات تحتاج استكمالًا':' · توصيف غير متاح'}`; }
function options(courses) { return '<option value="">اختر المقرر</option>'+courses.map(c=>`<option value="${esc(c.id)}">${esc(courseLabel(c))}</option>`).join(''); }
function sourceLink(c) { const key=c.catalog_course_id || c.id; return c.source_pdf || c.upload_pdf ? `<a href="/pdf/${esc(key)}" target="_blank" rel="noopener">فتح التوصيف</a>` : ''; }
function metadata(c) { return `<p class="muted">${esc(c.topics.length)} مفردة · ${sourceLink(c)} ${c.context ? '· '+esc(c.context.program)+' · '+esc(c.context.degree)+' · '+esc(c.context.plan_type)+' '+esc(c.context.version):''}</p>`; }
function editor(container, course) {
  const readonly=container==='sourceTopics'||container==='uploadedTargetTopics'||(container==='targetTopics'&&!referenceEditable);
  $(container).innerHTML=course.topics.map((t,i)=>`<div class="topic-row" data-id="${esc(t.id)}"><span class="topic-index">${i+1}</span><div><textarea rows="2" ${readonly?'readonly':''} aria-label="المفردة ${i+1}">${esc(textOf(t))}</textarea><div class="topic-foot"><span>${t.page?'صفحة '+t.page:'إدخال يدوي'}</span><button type="button" class="remove-topic" ${readonly?'disabled hidden':''}>حذف الصف</button><details ${readonly?'hidden':''}><summary>النص الأصلي</summary>${esc(t.text)}</details></div></div></div>`).join('')+`<div class="topic-tools" ${readonly?'hidden':''}><button type="button" class="add-topic" ${readonly?'disabled':''}>إضافة مفردة</button></div>${readonly?'<p class="reference-readonly-note">النص وفق توصيف المقرر.</p>':''}`;
  $(container).querySelectorAll('.remove-topic').forEach(b=>b.onclick=()=>b.closest('.topic-row').remove());
  $(container).querySelector('.add-topic').onclick=()=>{
    const div=document.createElement('div');div.className='topic-row';div.dataset.id='manual'+crypto.randomUUID().replaceAll('-','');div.innerHTML='<span class="topic-index">+</span><div><textarea rows="2" aria-label="مفردة إضافية"></textarea><button class="remove-topic" type="button">حذف الصف</button></div>';div.querySelector('button').onclick=()=>div.remove();$(container).insertBefore(div,$(container).lastElementChild);div.querySelector('textarea').focus();
  };
  $(container).oninput=()=>{$('reviewed').checked=false;};
}
function readEditor(container) { return [...$(container).querySelectorAll('.topic-row')].map(row=>({id:row.dataset.id,text:row.querySelector('textarea').value.trim()})); }
async function setSource(data) {
  clearReportReference();
  source=data;target=comparisonMode==='upload_pair'?uploadedTarget:null;currentReport=null;targetContextConfirmed=false;comparisonSequence++;beneficiaryStatus();
  $('sourcePanel').hidden=false;$('sourceTitle').textContent=source.name;$('sourceMeta').innerHTML=metadata(source);editor('sourceTopics',source);
  $('candidatePanel').hidden=true;$('reportPanel').hidden=true;$('reviewed').checked=false;
  if($('sourceReview')) $('sourceReview').open=false;
  if(comparisonMode==='upload_pair')syncPairState();
  if(!source.topics.length){notice('لا تتوافر مفردات مقروءة كافية للبحث عن معادلة.',true);return;}
  if(comparisonMode==='upload_pair'){syncPairState();notice('توصيف المقرر الأصل جاهز.');return;}
  await searchCandidates();
}
function sourceOptions() {
  const group=$('sourceProgram').value;
  const all=[...catalog.courses.filter(c=>c.topics.length),...(catalog.library_sources||[])];
  $('sourceCourse').innerHTML=options(all.filter(c=>!group||(group==='__library__'?c.library_only:c.context&&groupKey(c.context)===group)));
}
function targetOptions() {
  const group=$('program').value,context=selectedContext();
  const courses=catalog.courses.filter(c=>(!group||groupKey(c.context)===group)&&(!context||c.context_id===context.id));
  $('targetCourse').innerHTML=options(courses);
  $('scopeInventory').textContent=group?`${group.replace(' | ',' · ')}${context?' · الخطة '+context.plan_type+' '+context.version:' · جميع الخطط'}: ${courses.filter(c=>c.topics.length).length} توصيفًا متاحًا للمقارنة.`:`البحث تلقائي في جميع برامج الكلية ودرجاتها وخططها: ${courses.filter(c=>c.topics.length).length} توصيفًا متاحًا للمقارنة.`;
}
function clearTarget() {
  clearReportReference();
  searchSequence++;target=null;currentReport=null;targetContextConfirmed=false;beneficiaryStatus();$('targetReview').hidden=true;$('candidatePanel').hidden=true;$('reportPanel').hidden=true;$('candidates').innerHTML='';$('reviewed').checked=false;
}
function planOptions() {
  const plans=catalog.contexts.filter(c=>groupKey(c)===$('program').value).sort((a,b)=>Number(b.version)-Number(a.version));
  $('version').innerHTML='<option value="">جميع الخطط</option>'+plans.map(c=>`<option value="${esc(c.id)}">${esc(c.plan_type)} · ${esc(c.version)}</option>`).join('');
  $('version').disabled=!$('program').value;targetOptions();clearTarget();
}
function initContexts() {
  if(!catalog.contexts) {
    catalog.contexts=[];
    for(const course of catalog.courses) {
      const context={...course.context,id:'legacy'+course.context.version};course.context_id=context.id;
      if(!catalog.contexts.some(c=>c.id===context.id))catalog.contexts.push(context);
    }
  }
  const groups=[...new Set(catalog.contexts.map(groupKey))].sort((a,b)=>a.localeCompare(b,'ar'));
  const groupOptions=groups.map(g=>`<option value="${esc(g)}">${esc(g.replace(' | ',' · '))}</option>`).join('');
  $('program').innerHTML='<option value="">جميع البرامج والدرجات</option>'+groupOptions;
  $('sourceProgram').innerHTML='<option value="">كل البرامج والدرجات</option>'+groupOptions+((catalog.library_sources||[]).length?'<option value="__library__">مصادر إضافية غير مرتبطة بخطة</option>':'');sourceOptions();planOptions();
  $('searchScopeMode').value='all';syncScopeControls();
}
function syncScopeControls() {
  const scoped=!!$('program').value||$('searchScopeMode').value==='program';
  $('searchScopeMode').value=scoped?'program':'all';
  $('scopeProgramFields').hidden=!scoped;
  $('program').required=scoped;
}
function searchScope() {
  const context=selectedContext(),group=$('program').value;
  if($('searchScopeMode').value==='program'&&!group)throw new Error('اختر البرنامج والدرجة المطلوبين، أو اختر البحث في جميع البرامج.');
  if(context) return {context_id:context.id};
  if(group) return {context_ids:catalog.contexts.filter(c=>groupKey(c)===group).map(c=>c.id)};
  return {all_contexts:true};
}
async function searchCandidates() {
  if(!source||comparisonMode!=='repository')return;
  const revision=++searchSequence,sourceId=source.id,modeRevision=modeSequence,scope=searchScope();
  const topics=readEditor('sourceTopics');
  if(!topics.length){notice('لا تتوافر مفردات مقروءة كافية للبحث عن معادلة.',true);return;}
  notice('البحث تلقائيًا عن أقرب المقررات المتاحة…',false,true);
  $('candidatePanel').hidden=false;$('candidates').innerHTML='<div class="search-loading">جارٍ البحث في مفردات المقررات…</div>';
  $('targetReview').hidden=true;$('reportPanel').hidden=true;target=null;currentReport=null;targetContextConfirmed=false;beneficiaryStatus();
  const r=await api('/api/search',{source_id:sourceId,topics,...scope,limit:8});
  if(revision!==searchSequence||source?.id!==sourceId||modeRevision!==modeSequence||comparisonMode!=='repository')return;
  candidateVariants.clear();
  for(const c of r.candidates) {
    const contexts=c.available_contexts?.length?c.available_contexts:[{course_id:c.id,context_id:c.context_id,context:c.context}];
    for(const entry of contexts)candidateVariants.set(entry.course_id,contexts);
  }
  $('candidates').innerHTML=r.candidates.length?r.candidates.map((c,i)=>`<button class="candidate" data-id="${esc(c.id)}"><span class="candidate-rank">المرشح ${i+1}</span><strong>${esc(c.name)}</strong><span>${esc(c.code)} · ${c.topics.length} مفردة</span><span>${esc(c.context.program)} · ${esc(c.context.degree)} · ${esc(c.context.plan_type)} ${esc(c.context.version)}</span>${c.reference_quality&&!['ready_visual','ready_native'].includes(c.reference_quality.state)?'<span class="candidate-reference-pending">بيانات غير مكتملة</span>':''}${c.available_contexts?.length>1?`<span class="candidate-context-count">متاح في ${c.available_contexts.length} سياقات دراسية</span>`:''}</button>`).join(''):'<p>لا توجد توصيفات ذات مفردات متاحة ضمن النطاق المختار. وسّع نطاق البحث.</p>';
  $('candidates').querySelectorAll('button').forEach(b=>b.onclick=()=>setTarget(b.dataset.id));
  $('searchNote').textContent=`عدد المرشحين: ${r.candidates.length} · نطاق البحث: ${r.scope_count} توصيفًا متاحًا. الترتيب لترشيح المقررات؛ لا يمثل نسبة معادلة.`;
  if(r.candidates.length)setTarget(r.candidates[0].id);
  notice(r.candidates.length?'اختر المقرر والبرنامج المطلوبين لإصدار التقرير.':'لا تتوافر مقررات قريبة ضمن النطاق المحدد.');
  $('candidatePanel').scrollIntoView({behavior:'smooth',block:'start'});
}
function setTarget(id,explicitContext=false) {
  if(comparisonMode!=='repository')return;
  const course=catalog.courses.find(c=>c.id===id);if(!course)return;
  clearReportReference();
  target=course;currentReport=null;beneficiaryStatus();$('reportPanel').hidden=true;
  const contexts=candidateVariants.get(id)||[{course_id:id,context_id:course.context_id,context:course.context}];
  targetContextConfirmed=explicitContext||contexts.length===1;
  if($('candidateContextLabel')) $('candidateContextLabel').hidden=contexts.length<2;
  if($('candidateContext')) {
    $('candidateContext').innerHTML=(contexts.length>1?'<option value="">حدد البرنامج والخطة</option>':'')+contexts.map(entry=>`<option value="${esc(entry.course_id)}">${esc(entry.context.program)} · ${esc(entry.context.degree)} · ${esc(entry.context.plan_type)} ${esc(entry.context.version)}</option>`).join('');
    $('candidateContext').value=targetContextConfirmed?id:'';
  }
  $('targetCourse').value=id;$('targetReview').hidden=false;$('targetTitle').textContent=target.name+' · '+target.code;$('targetMeta').innerHTML=metadata(target);editor('targetTopics',target);$('reviewed').checked=false;
  if($('targetTopicsReview'))$('targetTopicsReview').open=false;
  $('compareButton').disabled=!targetContextConfirmed;
  document.querySelectorAll('.candidate').forEach(b=>b.classList.toggle('selected',b.dataset.id===id||(candidateVariants.get(b.dataset.id)||[]).some(entry=>entry.course_id===id)));
}
function download(name, content, mime) { const url=URL.createObjectURL(new Blob([content],{type:mime}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000); }
function beneficiaryValues() {
  return {name:($('beneficiaryName')?.value||'').trim(),number:($('beneficiaryNumber')?.value||'').trim()};
}
function beneficiaryIsDirty() {
  const entered=beneficiaryValues(),saved=currentReport?.beneficiary||{};
  return !!currentReport&&(entered.name!==(saved.name||'')||entered.number!==(saved.number||''));
}
function beneficiaryStatus() {
  const dirty=beneficiaryIsDirty(),state=$('beneficiarySaveState');
  if(state) state.className='muted'+(dirty?' beneficiary-dirty':'');
  if(state) state.textContent=dirty?'تغييرات غير محفوظة؛ احفظها لتحديث التقرير.':currentReport?'بيانات المستفيد محفوظة في هذا التقرير.':'تُضاف البيانات إلى التقرير عند إصداره.';
}
function showReportPreservingEdits(report,submitted) {
  const latest=beneficiaryValues();
  showReport(report);
  if(latest.name!==submitted.name||latest.number!==submitted.number) {
    if($('beneficiaryName')) $('beneficiaryName').value=latest.name;
    if($('beneficiaryNumber')) $('beneficiaryNumber').value=latest.number;
    beneficiaryStatus();
  }
}
async function saveBeneficiary() {
  if(!currentReport){beneficiaryStatus();notice('ستظهر بيانات المستفيد في التقرير بعد إصدار المقارنة.');return;}
  if(beneficiaryIsDirty()) {
    const submitted=beneficiaryValues(),reportId=currentReport.id;
    const report=await api('/api/reports/'+reportId+'/beneficiary',{beneficiary:submitted});
    if(currentReport?.id!==reportId) throw new Error('تغير التقرير أثناء الحفظ؛ افتح التقرير المطلوب قبل التصدير.');
    showReportPreservingEdits(report,submitted);
    if(beneficiaryIsDirty()) throw new Error('تغيرت بيانات المستفيد أثناء الحفظ؛ احفظ التعديل الجديد قبل تصدير التقرير.');
  }
  beneficiaryStatus();
}
function academicReason(pair,evidence) {
  if(evidence.provider==='exact_text'||/توحيد المسافات|حذف المسافات|المسافات والتشكيل/.test(pair.reason||''))return 'تتطابق المفردتان في الموضوع والنطاق.';
  return (pair.adjudication?.state==='uncertain'?pair.adjudication.review?.reason||pair.reason:pair.reason)||'توافق موضوعي ضمن نطاق المفردتين.';
}
// Curriculum evidence counts original targets; quotations are supporting evidence, not weighted topics.
function curriculumReportRows(targets,pairs,sources,calc,insufficient,exact) {
  const evaluations=new Map(pairs.map(pair=>[pair.target_id,pair]));
  return targets.map((t,i)=>{
    const pair=evaluations.get(t.id),tiers=pair?.adjudication?.confirmed_tiers;
    const confirmed=!!pair&&(exact||(Array.isArray(tiers)?tiers.length>0||pair.adjudication.state==='confirmed':pair.adjudication?.state==='confirmed'));
    const review=pair?.adjudication?.review;
    const explanation=pair?.adjudication?.state==='partial'&&pair.band==='lenient'?pair:review;
    const facets=explanation?.facets||(!confirmed?pair?.facets:[])||[];
    const quotations=new Map();
    const add=(id,quote)=>{
      if(!sources.has(id)||typeof quote!=='string'||!quote.trim())return;
      if(!quotations.has(id))quotations.set(id,new Set());
      quotations.get(id).add(quote);
    };
    if(exact){for(const id of pair?.source_ids||[])if(sources.has(id))add(id,textOf(sources.get(id)));}
    else {for(const facet of facets)for(const q of facet.source_quotes||[])add(q.source_id,q.quote);}
    const witnesses=[...quotations].map(([id,quotes])=>{
      const s=sources.get(id);
      return `<div class="source-witness"><small>المصدر · ص ${esc(pageOf(s))}</small>${[...quotes].map(quote=>`<p class="source-quotation">${esc(quote)}</p>`).join('')}</div>`;
    }).join('');
    const reason=confirmed?(explanation?.reason||pair.reason||'توافق موضوعي ضمن نطاق مفردة الهدف.'):'لم تثبت أدلة كافية لحسم توافق هذه المفردة.';
    const band=Array.isArray(tiers)?(tiers.includes('strict')?'strict':tiers.includes('lenient')?'lenient':pair?.band==='weak'?'weak':null):pair?.band||(Number.isFinite(pair?.score)?(pair.score>=80?'strict':pair.score>=60?'lenient':'weak'):null);
    const gradeLabel=confirmed?(exact&&Number.isFinite(pair.score)?`<bdi dir="ltr">${esc(pair.score)}%</bdi> · توافق المفردة`:band==='strict'?'توافق دقيق · <bdi dir="ltr">80%</bdi> فأعلى':band==='lenient'?'توافق مع التسامح · <bdi dir="ltr">60%</bdi> فأعلى':band==='weak'?'دون <bdi dir="ltr">60%</bdi>':null):null;
    const grade=gradeLabel?`<span class="match-state ${band==='strict'?'strict':band==='lenient'?'lenient':'uncounted'}">${gradeLabel}</span>`:'<span class="match-state uncounted">غير محسوم</span>';
    const accounting=['strict','lenient'].map(mode=>{
      const counted=confirmed&&Array.isArray(calc[mode+'_matches']?.[t.id])&&calc[mode+'_matches'][t.id].length>0;
      return `<span class="curriculum-account ${!insufficient&&counted?mode:'uncounted'}">${mode==='strict'?'الدقيق':'المتسامح'}: ${insufficient?'لم يُحسم':counted?'محتسبة':'غير محتسبة'}</span>`;
    }).join('');
    return `<tr data-target-id="${esc(t.id)}"><td class="target-topic"><span class="topic-order">${i+1}</span><p>${esc(textOf(t))}</p><small>الهدف · ص ${esc(pageOf(t))}</small>${textOf(t)==='أخرى'?'<span class="tag">موضوع غير محدد في المصدر</span>':''}</td><td class="curriculum-witnesses">${witnesses?(!confirmed?'<span class="witness-pending">شواهد غير محسومة</span>':'')+witnesses:'<span class="match-empty">لم تثبت شواهد مصدرية كافية لهذه المفردة.</span>'}</td><td class="matching-cell curriculum-verdict">${grade}<p class="match-reason">${esc(reason)}</p><div class="curriculum-accounting">${accounting}</div></td></tr>`;
  }).join('');
}
function showReport(r) {
  currentReport=r;
  if($('beneficiaryName')) $('beneficiaryName').value=r.beneficiary?.name||'';
  if($('beneficiaryNumber')) $('beneficiaryNumber').value=r.beneficiary?.number||'';
  beneficiaryStatus();
  const pairReport=r.target_mode==='uploaded',reportTitle=pairReport?'تقرير مقارنة المقررات':'تقرير معادلة المقررات';
  const calc=r.calculation,evidence=r.evidence||{},pairs=evidence.pairs||[];
  const curriculumReport=['curriculum_evidence','curriculum_exact'].includes(evidence.provider);
  const decisions={equivalent:'يمكن معادلته',lenient:'يمكن مع التسامح',not_equivalent:'لا يمكن معادلته',insufficient:'تعذر الحسم'};
  const decision=Object.hasOwn(decisions,calc.decision)?calc.decision:'insufficient';
  const insufficient=decision==='insufficient';
  const total=calc.total_topics,required=Math.ceil(total*80/100);
  const ready=['equivalent','lenient','not_equivalent'].includes(decision)&&total>0&&Number.isFinite(calc.strict_coverage)&&Number.isFinite(calc.lenient_coverage);
  const percent=value=>Number.isFinite(value)?(Math.floor(value*100)/100).toLocaleString('en-US',{minimumFractionDigits:1,maximumFractionDigits:2})+'%':'—';
  const sources=new Map(r.source.topics.map(t=>[t.id,t]));
  const count=mode=>calc[mode+'_count']||0;
  const coverage=mode=>Number.isFinite(calc[mode+'_coverage'])?Math.max(0,Math.min(100,calc[mode+'_coverage'])):null;
  const names={strict:'الحساب الدقيق',lenient:'الحساب المتسامح'},threshold={strict:80,lenient:60};
  const coverageCard=mode=>{
    const value=coverage(mode),pending=insufficient||value===null;
    const label=pending?names[mode]+': التغطية لم تحسب':`${names[mode]}: تغطية ${percent(value)}، ${count(mode)} من ${total} مفردة`;
    return `<section class="coverage-card ${mode}" data-mode="${mode}" data-count="${pending?'':count(mode)}" data-total="${total}" data-coverage="${pending?'':value}"><div class="coverage-caption"><span class="report-kicker">توافق المفردة ≥${threshold[mode]}%</span><h3>${names[mode]}</h3></div>${pending?'<div class="coverage-pending"><b>—</b><span>لم تُحسب التغطية</span></div>':`<div class="coverage-visual"><svg class="coverage-gauge" viewBox="0 0 104 104" role="img" aria-label="${esc(label)}"><circle class="gauge-track" cx="52" cy="52" r="43" pathLength="100"/><circle class="gauge-value" cx="52" cy="52" r="43" pathLength="100" stroke-dasharray="${value} ${100-value}" transform="rotate(-90 52 52)"/></svg><div class="coverage-value"><b dir="ltr">${percent(value)}</b><span>من مفردات الهدف</span></div></div>`}<p class="coverage-count">${pending?'لا تتوافر بيانات كافية لحساب التغطية':`<strong>${count(mode)}</strong> من <strong>${total}</strong> مفردة محتسبة`}</p><span class="coverage-threshold">حد تغطية المقرر: 80%</span></section>`;
  };
  const bar=mode=>{const value=coverage(mode);return `<div class="coverage-bar-row" data-mode="${mode}"><div class="coverage-bar-label"><b>${names[mode]}</b><span>${count(mode)} محتسبة · ${Math.max(0,total-count(mode))} غير محتسبة</span></div><div class="coverage-track" role="img" aria-label="${esc(names[mode]+': '+percent(value)+'، حد القبول 80%')}"><span class="coverage-bar-fill ${mode}" style="width:${value}%"></span><span class="threshold-marker" style="left:80%"></span></div><div class="coverage-scale"><span>0%</span><span>حد القبول 80%</span><span>100%</span></div></div>`;};
  const sourceCard=(c,label,n)=>`<section class="course-card"><div class="course-card-top"><span class="report-section-number">${n}</span><span class="report-kicker">${label}</span>${c.code?'<bdi class="course-code" dir="ltr">'+esc(c.code)+'</bdi>':''}</div><h3>${esc(c.name)}</h3><p class="course-scope">${!pairReport&&c.context?esc(c.context.program)+' · '+esc(c.context.degree)+'<br>الخطة '+esc(c.context.plan_type||'')+' '+esc(c.context.version):esc(c.university||c.institution||(pairReport?'مقرر مرفوع للمقارنة':'مقرر مصدر المعادلة'))}</p><dl class="course-facts"><div><dt>الساعات المعتمدة</dt><dd>${esc(c.hours??'غير محددة')}</dd></div><div><dt>المفردات</dt><dd>${c.topics.length}</dd></div></dl>${sourceLink(c)?'<div class="course-source-link">'+sourceLink(c)+'</div>':''}</section>`;
  const matchedCell=(t,mode)=>{
    const sid=calc[mode+'_matches']?.[t.id],pair=sid?pairs.find(p=>p.target_id===t.id&&p.source_id===sid):null,s=pair?sources.get(pair.source_id):null;
    if(!pair||!s) {
      const nearest=pairs.filter(p=>p.target_id===t.id&&sources.has(p.source_id)).sort((a,b)=>b.score-a.score)[0];
      if(!nearest)return '<td class="matching-cell"><span class="match-state uncounted">غير محتسبة</span><span class="match-empty">لا مقابلة محتسبة في هذا الحساب</span></td>';
      const alternative=sources.get(nearest.source_id);
      const pending=nearest.adjudication?.state==='uncertain';
      const grade=pending?'قيد الاستكمال':esc(nearest.score)+'%';
      const explanation=pending?'لم يثبت نطاق هذه المقابلة بما يكفي لاحتسابها.':nearest.score<threshold[mode]?'دون حد التوافق المطلوب في هذا الحساب.':'لا تُحتسب المفردة المصدرية نفسها لأكثر من مفردة هدف في الحساب الواحد.';
      return `<td class="matching-cell"><span class="match-state uncounted">غير محتسبة · <bdi dir="ltr">${grade}</bdi></span><p>${esc(textOf(alternative))}</p><small>أقرب مقابلة · المصدر ص ${esc(pageOf(alternative))}</small><p class="match-reason">${esc(academicReason(nearest,evidence))}<br>${esc(explanation)}</p></td>`;
    }
    return `<td class="matching-cell"><span class="match-state ${mode}">محتسبة · <bdi dir="ltr">${esc(pair.score)}%</bdi></span><p>${esc(textOf(s))}</p><small>المصدر · ص ${esc(pageOf(s))}</small><p class="match-reason">${esc(academicReason(pair,evidence))}</p></td>`;
  };
  const rows=curriculumReport?curriculumReportRows(r.target.topics,pairs,sources,calc,insufficient,evidence.provider==='curriculum_exact'):r.target.topics.map((t,i)=>{
    const topic=`<td class="target-topic"><span class="topic-order">${i+1}</span><p>${esc(textOf(t))}</p><small>الهدف · ص ${esc(pageOf(t))}</small>${textOf(t)==='أخرى'?'<span class="tag">موضوع غير محدد في المصدر</span>':''}</td>`;
    if(insufficient) {
      const alternatives=pairs.filter(p=>p.target_id===t.id&&sources.has(p.source_id));
      const confirmed=calc.rejection_review?null:alternatives.filter(p=>p.adjudication?.state==='confirmed').sort((a,b)=>b.score-a.score)[0];
      const p=confirmed||alternatives[0],s=p?sources.get(p.source_id):null;
      const state=confirmed?'نطاق المقابلة مثبت':p?'نطاق المقابلة غير محسوم':'المعلومات غير كافية للحكم على هذه المفردة.';
      const reason=calc.rejection_review?'لم تثبت أدلة كافية لحسم توافق هذه المفردة.':p?academicReason(p,evidence):'';
      return `<tr>${topic}<td class="matching-cell"><span class="match-state uncounted">${esc(state)}</span>${s?'<p>'+esc(textOf(s))+'</p><small>المصدر · ص '+esc(pageOf(s))+'</small><p class="match-reason">'+esc(reason)+'</p>':''}</td></tr>`;
    }
    const strict=!!calc.strict_matches?.[t.id],lenient=!!calc.lenient_matches?.[t.id];
    const note=strict&&lenient?'مستوفية لحد التوافق الدقيق والمتسامح.':strict?'مستوفية في المطابقة الدقيقة.':lenient?'مستوفية في المطابقة المتسامحة فقط.':'لا تدخل ضمن المفردات المتوافقة في النتيجة الإجمالية.';

    return `<tr>${topic}${matchedCell(t,'strict')}${matchedCell(t,'lenient')}<td class="matching-note">${esc(note)}</td></tr>`;
  }).join('');

  const date=r.created_at?new Date(r.created_at):null;
  const dated=date&&!Number.isNaN(date.getTime())?date.toLocaleDateString('ar-SA-u-ca-gregory-nu-latn',{year:'numeric',month:'long',day:'numeric',timeZone:'Asia/Riyadh'}):'—';
  const summary=insufficient?(calc.invalid_verse_scopes?'تعذر الحسم لعدم اتساق نطاق الآيات المذكور مع حدود السورة.':calc.reference_review?'تعذر الحسم لعدم ثبوت نص بعض مفردات المقررين.':(calc.evidence_review||calc.rejection_review)?'تعذر الحسم لعدم كفاية أدلة التوافق بين مفردات المقررين.':calc.adjudication_review?'تعذر الحسم لعدم ثبوت توافق نطاق بعض مفردات المقررين.':calc.segmentation_review?'تعذر الحسم لعدم اتساق تقسيم الموضوعات بين المقررين.':/غير محدد/.test(calc.reason||'')?'تعذر الحسم لوجود موضوعات غير محددة ضمن بيانات المقرر.':'تعذر الحسم لعدم اكتمال البيانات.'):`تتطلب المعادلة تغطية ${required} من ${total} مفردة على الأقل. بلغ الاحتساب الدقيق ${count('strict')}، والمتسامح ${count('lenient')} مفردة.`;
  const stamp=`<div class="report-stamp ${ready?'approved':'pending'}" role="img" aria-label="${ready?'معتمد — تقرير المعادلة':'قيد الاستكمال — تقرير المعادلة'}"><span>تقرير المعادلة</span><strong>${ready?'معتمد':'قيد الاستكمال'}</strong><small>منصة المعادلات</small></div>`;
  $('report').innerHTML=`<div class="equivalency-report" data-decision="${decision}"><header class="report-head"><div class="report-brand"><span class="report-mark" aria-hidden="true">م</span><div><span class="report-kicker">CourseMatch · منصة المعادلات</span><p>${pairReport?'المقرر الهدف: '+esc(r.target.name):'الجامعة المستهدفة: جامعة الطائف<br>'+esc(r.target.context?.program||'دراسة توافق المقررات')+' · '+esc(r.target.context?.degree||'')}</p></div><span class="report-edition">${pairReport?'مقارنة المقررات<br>الدراسية':'دراسة معادلة<br>المقررات الدراسية'}</span></div><h1>${reportTitle}</h1><div class="report-reference"><span>تاريخ التقرير: ${esc(dated)}</span><span>رقم التقرير <bdi dir="ltr">${esc(r.id)}</bdi></span></div></header><section class="beneficiary-card" aria-label="بيانات المستفيد"><span class="report-section-number" aria-hidden="true">01</span><div><span class="report-kicker">اسم المستفيد</span><strong>${esc(r.beneficiary?.name||'غير مسجل')}</strong></div><div><span class="report-kicker">رقم المستفيد</span><strong><bdi dir="auto">${esc(r.beneficiary?.number||'غير مسجل')}</bdi></strong></div></section><section class="report-hero ${decision}">${stamp}<div><span class="report-kicker">نتيجة دراسة المفردات</span><h2>${esc(decisions[decision])}</h2><p>${esc(summary)}</p></div></section><section class="report-block"><div class="report-section-heading"><span class="report-section-number">02</span><div><h2>المقرران محل المعادلة</h2><p>المقارنة من محتوى المصدر إلى المقرر الهدف المحدد</p></div></div><div class="source-grid">${sourceCard(r.source,pairReport?'المقرر الأصل':'المقرر المصدر','أ')}${sourceCard(r.target,'المقرر الهدف','ب')}</div></section><section class="report-block"><div class="report-section-heading"><span class="report-section-number">03</span><div><h2>ملخص تغطية المفردات</h2><p>عدد مفردات الهدف المحتسبة ÷ جميع مفردات الهدف × 100</p></div></div><div class="report-summary">${coverageCard('strict')}${coverageCard('lenient')}</div>${insufficient?'<div class="pending-explanation">النتيجة قيد الاستكمال لعدم كفاية المعلومات.</div>':`<div class="distribution-chart"><div class="distribution-heading"><h3>المحتسب والمتبقي من مفردات الهدف</h3><span>الخط المنقّط: حد القبول</span></div>${bar('strict')}${bar('lenient')}</div>`}<p class="report-note calculation-note">حد تغطية المقرر 80%؛ التوافق الدقيق للمفردة 80% والمتسامح 60%.</p></section><section class="report-block evidence-block"><div class="report-section-heading"><span class="report-section-number">04</span><div><h2>${insufficient?'نطاق المفردات المتاح':'تفصيل التوافق الموضوعي'}</h2><p>${insufficient?'بيانات المقررين وأسباب تعذر الحسم':'المفردات المقابلة وأسباب التوافق الموضوعي'}</p></div></div><div class="report-table-wrap"><table class="details-table${curriculumReport?' curriculum-details-table':''}"><thead><tr><th scope="col">مفردة المقرر الهدف</th>${curriculumReport?'<th scope="col">شواهد المصدر</th><th scope="col">التوافق والاحتساب</th>':insufficient?'<th scope="col">حالة المقارنة</th>':'<th scope="col">الحساب الدقيق · ≥80%</th><th scope="col">الحساب المتسامح · ≥60%</th><th scope="col">النتيجة</th>'}</tr></thead><tbody>${rows||`<tr><td colspan="${curriculumReport?3:insufficient?2:4}">لا تتوافر مفردات موضوعية كافية لإصدار الحكم.</td></tr>`}</tbody></table></div></section><div class="report-end"><span>CourseMatch · ${reportTitle}</span><bdi dir="ltr">${esc(r.id)}</bdi></div></div>`;
  $('reportPanel').hidden=false;$('reportPanel').scrollIntoView({behavior:'smooth'});notice('اكتمل التقرير. يمكنك تنزيله أو طباعته.');
}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(t=>t.classList.toggle('active',t===b));for(const name of ['upload','catalog','manual']) $(name+'Tab').hidden=name!==b.dataset.tab;});
async function uploadPayload(file,name,role='source') {
  if(!file)throw new Error('اختر توصيف PDF أو صورة أولًا');
  const image=CourseImageUpload.isImage(file);
  if(!image&&!CourseImageUpload.isPdf(file))throw new Error('اختر PDF أو صورة JPG أو PNG أو WebP.');
  if(!image&&file.size>maxUploadBytes)throw new Error('الحد الأقصى لملف PDF '+(maxUploadBytes/1024/1024)+' ميغابايت');
  const bytes=image?(await CourseImageUpload.prepare(file,{maxOutputBytes:maxUploadBytes})).bytes:new Uint8Array(await file.arrayBuffer());
  let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
  return {pdf_base64:btoa(binary),name:name||file.name,intake_role:role,...(image?{input_kind:'image',original_filename:file.name}:{})};
}
function invalidateComparison() {
  comparisonSequence++;currentReport=null;clearReportReference();$('reportPanel').hidden=true;beneficiaryStatus();
}
function syncPairState() {
  const pair=comparisonMode==='upload_pair';$('pairComparisonPanel').hidden=!pair;
  if(!pair)return;
  target=uploadedTarget;
  const ready=!!source?.topics.length&&!!uploadedTarget?.topics.length;
  $('pairCompareButton').disabled=!ready;
  $('pairStatus').textContent=ready?'المقرران جاهزان للمقارنة.':!source?.topics.length?'أضف توصيف المقرر الأصل.':'أضف توصيف المقرر الهدف.';
}
async function setComparisonMode(mode) {
  if(!['repository','upload_pair'].includes(mode))return;
  comparisonMode=mode;modeSequence++;searchSequence++;invalidateComparison();target=null;targetContextConfirmed=false;
  $('comparisonMode').value=mode;$('setupGrid').dataset.comparisonMode=mode;
  const pair=mode==='upload_pair';$('searchScopePanel').hidden=pair;$('targetUploadPanel').hidden=!pair;$('candidatePanel').hidden=true;$('targetReview').hidden=true;
  $('inputHeading').textContent=pair?'المقرر الأصل':'المقرر الذي درسته';
  $('extractButton').textContent=pair?'استخدام التوصيف':'البحث عن المعادلة';$('loadManual').textContent=pair?'استخدام المفردات':'البحث عن المعادلة';
  $('footerBrand').textContent=pair?'CourseMatch · مقارنة المقررات':'CourseMatch · مقررات جامعة الطائف';
  $('siteScopeBadge').textContent=pair?'مقارنة مباشرة':'جامعة الطائف';$('heroTitle').textContent=pair?'مقرران، وتقرير مقارنة.':'مقررك، وأقرب معادلة له.';$('heroCopy').textContent=pair?'ارفع التوصيفين واحصل على تقرير المقارنة.':'ارفع التوصيف، واختر المقرر الأقرب، واحصل على تقريرك.';
  syncPairState();$('notice').className='';$('notice').textContent='';
  if(!pair&&source?.topics.length)await searchCandidates();
}
$('comparisonMode').onchange=e=>action(e.target,()=>setComparisonMode(e.target.value));
$('pdfFile').onchange=()=>{sourceInputSequence++;searchSequence++;source=null;target=null;$('sourcePanel').hidden=true;$('candidatePanel').hidden=true;invalidateComparison();syncPairState();};
$('targetPdfFile').onchange=()=>{targetInputSequence++;uploadedTarget=null;$('uploadedTargetPanel').hidden=true;invalidateComparison();syncPairState();};
$('extractButton').onclick=e=>action(e.target,async()=>{
  const inputRevision=++sourceInputSequence,modeRevision=modeSequence;
  notice('قراءة المفردات والتحقق من التوصيف…',false,true);
  const payload=await uploadPayload($('pdfFile').files[0],$('uploadName').value);
  if(inputRevision!==sourceInputSequence||modeRevision!==modeSequence)return;
  const received=await api('/api/sources',payload);
  if(inputRevision===sourceInputSequence&&modeRevision===modeSequence)await setSource(received);
});
$('targetExtractButton').onclick=e=>action(e.target,async()=>{
  if(comparisonMode!=='upload_pair')return;
  const revision=++targetInputSequence,modeRevision=modeSequence;
  uploadedTarget=null;invalidateComparison();syncPairState();$('uploadedTargetPanel').hidden=true;
  notice('قراءة المفردات والتحقق من التوصيف…',false,true);
  const payload=await uploadPayload($('targetPdfFile').files[0],$('targetUploadName').value,'target');
  if(revision!==targetInputSequence||modeRevision!==modeSequence)return;
  const received=await api('/api/sources',payload);
  if(revision!==targetInputSequence||modeRevision!==modeSequence||comparisonMode!=='upload_pair')return;
  uploadedTarget=received;$('uploadedTargetTitle').textContent=received.name;$('uploadedTargetMeta').innerHTML=metadata(received);editor('uploadedTargetTopics',received);$('uploadedTargetPanel').hidden=false;
  syncPairState();notice(received.topics.length?'توصيف المقرر الهدف جاهز.':'لا تتوافر مفردات مقروءة كافية للمقارنة.',!received.topics.length);
});
$('loadCourse').onclick=e=>action(e.target,async()=>{if(!$('sourceCourse').value)throw new Error('اختر المقرر المصدر');const inputRevision=++sourceInputSequence,modeRevision=modeSequence;const received=await api('/api/sources',{catalog_id:$('sourceCourse').value});if(inputRevision===sourceInputSequence&&modeRevision===modeSequence)await setSource(received);});
$('loadManual').onclick=e=>action(e.target,async()=>{const topics=$('manualTopics').value.split('\n').map(s=>s.trim()).filter(Boolean).map((text,i)=>({id:'t'+(i+1),text}));if(!topics.length)throw new Error('أدخل المفردات أولًا');const inputRevision=++sourceInputSequence,modeRevision=modeSequence;const received=await api('/api/sources',{name:$('manualName').value,topics});if(inputRevision===sourceInputSequence&&modeRevision===modeSequence)await setSource(received);});
$('sourceProgram').onchange=sourceOptions;
$('searchScopeMode').onchange=e=>action(e.target,async()=>{
  if(e.target.value==='all'){$('program').value='';planOptions();syncScopeControls();if(source)await searchCandidates();}
  else {syncScopeControls();clearTarget();notice('حدد البرنامج والدرجة المطلوبين، ويمكنك اختيار خطة محددة.');}
});
$('program').onchange=e=>action(e.target,async()=>{$('searchScopeMode').value=e.target.value?'program':'all';syncScopeControls();planOptions();if(source)await searchCandidates();});
$('version').onchange=e=>action(e.target,async()=>{targetOptions();clearTarget();if(source)await searchCandidates();});
$('directTargetButton').onclick=()=>{targetOptions();$('candidatePanel').hidden=false;$('directTargetChoice').open=true;$('candidatePanel').scrollIntoView({behavior:'smooth'});};
$('searchButton').onclick=e=>action(e.target,searchCandidates);
$('targetCourse').onchange=()=>setTarget($('targetCourse').value,true);
if($('candidateContext'))$('candidateContext').onchange=()=>{if($('candidateContext').value)setTarget($('candidateContext').value,true);else{targetContextConfirmed=false;$('compareButton').disabled=true;}};
async function submitComparison() {
  if(!source)throw new Error('أضف توصيف المقرر الأصل.');
  const pair=comparisonMode==='upload_pair';
  if(!target)throw new Error('أضف المقرر الهدف.');
  if(pair&&(!source.topics.length||!uploadedTarget?.topics.length))throw new Error('لا تتوافر مفردات مقروءة كافية للمقارنة.');
  if(!pair&&!targetContextConfirmed)throw new Error('حدد برنامج المقرر الهدف وخطته أولًا');
  const expected={sourceId:source.id,targetId:target.id,beneficiary:beneficiaryValues(),mode:comparisonMode,revision:comparisonSequence};
  const body=pair?{source_id:source.id,target_source_id:uploadedTarget.id,review_mode:'automatic',beneficiary:expected.beneficiary}:{source_id:source.id,target_id:target.id,source_topics:readEditor('sourceTopics'),target_topics:referenceEditable?readEditor('targetTopics'):undefined,review_mode:'automatic',beneficiary:expected.beneficiary};
  const job=await api('/api/jobs',body);
  if(expected.revision!==comparisonSequence||expected.mode!==comparisonMode||source?.id!==expected.sourceId||target?.id!==expected.targetId)return;
  notice('المقارنة قيد المعالجة…',false,true);await waitForReport(job.id,expected);
}
$('compareButton').onclick=e=>action(e.target,submitComparison);
$('pairCompareButton').onclick=e=>action(e.target,submitComparison);
for(const id of ['beneficiaryName','beneficiaryNumber']) if($(id)) $(id).oninput=beneficiaryStatus;
if($('saveBeneficiary')) $('saveBeneficiary').onclick=e=>action(e.target,saveBeneficiary);
if($('editBeneficiary')) $('editBeneficiary').onclick=()=>{$('beneficiaryName')?.closest('.beneficiary-panel')?.scrollIntoView({behavior:'smooth',block:'center'});$('beneficiaryName')?.focus({preventScroll:true});};
$('printButton').onclick=e=>action(e.target,async()=>{await saveBeneficiary();window.print();});
$('jsonButton').onclick=e=>action(e.target,async()=>{await saveBeneficiary();download('equivalency-'+currentReport.id+'.json',JSON.stringify(currentReport,null,2),'application/json');});
let reportCSSPromise;
async function standaloneReportCSS() {
  if(!reportCSSPromise) reportCSSPromise=(async()=>{
    const response=await fetch('/style.css');if(response.ok===false)throw new Error('تعذر تحميل تنسيق التقرير.');
    let css=await response.text();
    for(const file of ['IBMPlexSansArabic-Regular.ttf','IBMPlexSansArabic-SemiBold.ttf']) {
      const path='/fonts/'+file;if(!css.includes(path))continue;
      const font=await fetch(path);if(!font.ok)throw new Error('تعذر تحميل خط التقرير.');
      const bytes=new Uint8Array(await font.arrayBuffer());let binary='';
      for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
      css=css.split(path).join('data:font/ttf;base64,'+btoa(binary));
    }
    return css;
  })().catch(error=>{reportCSSPromise=undefined;throw error;});
  return reportCSSPromise;
}
$('htmlButton').onclick=e=>action(e.target,async()=>{
  await saveBeneficiary();const id=currentReport.id,css=await standaloneReportCSS();
  if(currentReport.id!==id)throw new Error('تغير التقرير؛ أعد تنزيل التقرير الحالي.');
  await saveBeneficiary();
  const reportHTML=$('report').innerHTML.replace(/href="(\/pdf\/[^" ]+)"/g,(_,path)=>'href="'+esc(location.origin+path)+'"');
  const html='<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(currentReport.target_mode==='uploaded'?'تقرير مقارنة المقررات':'تقرير معادلة المقررات')+'</title><style>'+css+'</style><main><section class="panel" id="reportPanel"><article id="report">'+reportHTML+'</article></section></main></html>';
  download('equivalency-'+id+'.html',html,'text/html');
});
(async()=>{
  try {
    const id=new URLSearchParams(location.search).get('report');
    const response=await api('/api/catalog');token=response.token;
    catalog=response.catalog||(await(await fetch(response.catalog_url,{credentials:'same-origin'})).json());
    referenceEditable=response.capabilities?.reference_editable!==false;
    requestRecovery=response.capabilities?.request_recovery===true;
    if(response.capabilities?.max_upload_mb){
      maxUploadBytes=response.capabilities.max_upload_mb*1024*1024;
      for(const id of ['uploadLimitHint','targetUploadLimitHint'])if($(id))$(id).textContent='PDF حتى '+response.capabilities.max_upload_mb+' ميغابايت · صورة حتى 12 ميغابايت';
    }
    initContexts();const available=catalog.courses.filter(c=>c.topics.length).length;
    $('inventory').textContent=`الفهرس: ${catalog.courses.length} ظهورًا في ${catalog.contexts.length} سياقًا دراسيًا، ${available} بمفردات مستخرجة.`;
    targetOptions();
    if(id)await waitForReport(id);
    else if(requestRecovery){
      const saved=[...pendingQueueRequests].filter(([,v])=>v.route==='/api/jobs').sort((a,b)=>b[1].at-a[1].at)[0];
      if(saved){const record={key:saved[0],...saved[1]},accepted=await recoverQueueRequest(record);finishQueueRequest(record);await waitForReport(accepted.id);}
    }
  }catch(error){notice(error.message,true);}
})();
