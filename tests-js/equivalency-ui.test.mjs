import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// The small DOM adapter parses the real markup and executes its actual callbacks.
// It intentionally supplies only DOM behavior used by this standalone interface.
function decodeHTML(value) {
  return String(value).replace(/&(?:amp|lt|gt|quot|#39|#10|#x([0-9a-f]+)|#([0-9]+));/gi,(entity,hex,decimal)=>{
    if(hex||decimal)return String.fromCodePoint(parseInt(hex||decimal,hex?16:10));
    return ({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'",'&#10;':'\n'})[entity]??entity;
  });
}
class Element {
  constructor(tag='div',attributes={}) {
    this.tagName=tag.toLowerCase();this.attributes=attributes;this.children=[];this.parentElement=null;
    this.dataset={};this.className=attributes.class||'';this.hidden='hidden' in attributes;this.disabled='disabled' in attributes;
    this.readOnly='readonly' in attributes;this.open='open' in attributes;this.checked='checked' in attributes;this.value=attributes.value||'';this.files=[];
    for(const [name,value] of Object.entries(attributes))if(name.startsWith('data-'))this.dataset[name.slice(5).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase())]=value;
    this.classList={toggle:(name,force)=>{const values=new Set(this.className.split(/\s+/).filter(Boolean));const add=force??!values.has(name);if(add)values.add(name);else values.delete(name);this.className=[...values].join(' ');return add;}};
  }
  appendChild(child) {child.parentElement=this;this.children.push(child);return child;}
  insertBefore(child,before) {child.parentElement=this;const index=this.children.indexOf(before);if(index<0)this.children.push(child);else this.children.splice(index,0,child);return child;}
  remove() {if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(child=>child!==this);}
  get lastElementChild() {return this.children.filter(child=>child instanceof Element).at(-1)||null;}
  matches(selector) {
    if(selector.startsWith('.'))return this.className.split(/\s+/).includes(selector.slice(1));
    if(selector.startsWith('#'))return this.attributes.id===selector.slice(1);
    if(selector.startsWith('['))return selector.slice(1,-1) in this.attributes;
    return this.tagName===selector;
  }
  querySelectorAll(selector) {const found=[];const visit=node=>{for(const child of node.children){if(!(child instanceof Element))continue;if(child.matches(selector))found.push(child);visit(child);}};visit(this);return found;}
  querySelector(selector) {return this.querySelectorAll(selector)[0]||null;}
  closest(selector) {for(let node=this;node;node=node.parentElement)if(node.matches(selector))return node;return null;}
  get options() {return this.querySelectorAll('option').map(option=>({value:option.value,text:option.textContent}));}
  get textContent() {return this.children.map(child=>child instanceof Element?child.textContent:child).join('');}
  set textContent(value) {this.children=[String(value)];}
  set innerHTML(value) {this.html=String(value);this.children=[];parseHTML(this,this.html);if(this.tagName==='select')this.value=this.options[0]?.value||'';}
  get innerHTML() {return this.html||'';}
  scrollIntoView() {}
  focus() {this.focused=true;}
}
function parseHTML(root,html) {
  const stack=[root],voids=new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
  for(const token of html.match(/<!--[\s\S]*?-->|<[^>]+>|[^<]+/g)||[]) {
    if(token.startsWith('<!'))continue;
    if(token.startsWith('</')) {const tag=token.slice(2,-1).trim().toLowerCase();for(let i=stack.length-1;i>0;i--)if(stack[i].tagName===tag){stack.length=i;break;}continue;}
    const parent=stack.at(-1);
    if(!token.startsWith('<')) {const text=decodeHTML(token);parent.children.push(text);if(parent.tagName==='textarea')parent.value+=text;continue;}
    const match=token.match(/^<([\w-]+)([\s\S]*?)\/?>$/);if(!match)continue;
    const attributes={};for(const item of match[2].matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g))attributes[item[1]]=decodeHTML(item[2]??item[3]??item[4]??'');
    const node=parent.appendChild(new Element(match[1],attributes));
    if(!voids.has(node.tagName)&&!token.endsWith('/>'))stack.push(node);
  }
}
function ui({sessionStorage}={}) {
  const documentRoot=new Element('document');parseHTML(documentRoot,fs.readFileSync(new URL('../equivalency-pilot/index.html',import.meta.url),'utf8'));
  const element=id=>documentRoot.querySelector('#'+id);
  const document={getElementById:element,querySelectorAll:selector=>documentRoot.querySelectorAll(selector),createElement:tag=>new Element(tag)};
  const context=vm.createContext({document,console,setTimeout,clearTimeout,URL,URLSearchParams,Uint8Array,Blob,TextEncoder,sessionStorage,btoa:globalThis.btoa,crypto:globalThis.crypto,
    history:{replaceState(){}},location:{origin:'http://localhost:8798',search:''},window:{print(){}}});
  vm.runInContext(fs.readFileSync(new URL('../equivalency-pilot/image-upload.js',import.meta.url),'utf8'),context);
  const source=fs.readFileSync(new URL('../equivalency-pilot/app.js',import.meta.url),'utf8');
  const init=source.lastIndexOf('(async()=>{');assert.ok(init>0,'the app initialization boundary exists');
  vm.runInContext(source.slice(0,init),context);
  const catalog=JSON.parse(fs.readFileSync(new URL('../equivalency-pilot/catalog.json',import.meta.url)));
  context.inputCatalog=catalog;vm.runInContext('catalog = inputCatalog; initContexts();',context);
  return {context,element,catalog};
}

function reportFixture({decision='equivalent', strict=4, lenient=5, total=5, beneficiary}={}) {
  const labels={equivalent:'يمكن معادلته',lenient:'يمكن مع التسامح',not_equivalent:'لا يمكن معادلته',insufficient:'المعلومات لا تكفي'};
  const targetTopics=Array.from({length:total},(_,i)=>({id:'t'+i,text:'مفردة الهدف '+i,page:5}));
  const sourceTopics=targetTopics.map((_,i)=>({id:'s'+i,text:'مفردة المصدر '+i,page:6}));
  const insufficient=decision==='insufficient';
  const pairs=insufficient?[]:targetTopics.map((t,i)=>({target_id:t.id,source_id:sourceTopics[i].id,score:i<strict?85:i<lenient?65:40,reason:'تعليل الدليل '+i}));
  return {
    id:'test-report',beneficiary,source:{id:'source',name:'المقرر المصدر',topics:sourceTopics,hours:2},
    target:{id:'target',name:'المقرر الهدف',topics:targetTopics,hours:2,context:{program:'الأنظمة',degree:'بكالوريوس',version:'47',plan_type:'جديدة'}},
    calculation:{decision,label:labels[decision],total_topics:total,coverage_threshold:80,strict_topic_threshold:80,lenient_topic_threshold:60,
      strict_count:insufficient?0:strict,lenient_count:insufficient?0:lenient,
      strict_coverage:insufficient?null:100*strict/total,lenient_coverage:insufficient?null:100*lenient/total,
      strict_matches:insufficient?{}:Object.fromEntries(targetTopics.slice(0,strict).map((t,i)=>[t.id,'s'+i])),
      lenient_matches:insufficient?{}:Object.fromEntries(targetTopics.slice(0,lenient).map((t,i)=>[t.id,'s'+i])),
      ...(insufficient?{reason:'موضوع غير محدد؛ يلزم استكمال المصدر'}:{})},
    evidence:{pairs,provider:'test_fixture',rubric:'test'},notice:'بيانات اختبار فقط',catalog_id:'test-catalog',reviewed_by:'user_confirmed_extraction'
  };
}

function renderedReport(input) {
  const state=ui();
  state.context.reportInput=input;
  vm.runInContext('showReport(reportInput)',state.context);
  return {...state,html:state.element('report').innerHTML};
}

test('selectors start with every available program and allow every exact context', () => {
  const {context,element,catalog} = ui();
  assert.equal(element('program').value,'');
  assert.equal(element('version').value,'');
  assert.equal(element('version').disabled,true);
  assert.equal(element('targetCourse').options.length,catalog.courses.length+1);
  for(const scope of catalog.contexts) {
    element('program').value=scope.program+' | '+scope.degree;
    vm.runInContext('planOptions()',context);
    assert.ok(element('version').options.some(o=>o.value===scope.id));
    element('version').value=scope.id;
    vm.runInContext('targetOptions()',context);
    const expected=catalog.courses.filter(c=>c.context_id===scope.id).map(c=>c.id);
    assert.deepEqual(element('targetCourse').options.map(o=>o.value).filter(Boolean),expected);
  }
});

test('source filter distinguishes master and doctorate with the same program name', () => {
  const {context,element,catalog} = ui();
  element('sourceProgram').value='القراءات | دكتوراه';
  vm.runInContext('sourceOptions()',context);
  const expected=catalog.courses.filter(c=>c.context.program==='القراءات'&&c.context.degree==='دكتوراه'&&c.topics.length).map(c=>c.id);
  assert.deepEqual(element('sourceCourse').options.map(o=>o.value).filter(Boolean),expected);
});

test('changing a target program clears the prior candidate and report state', () => {
  const {context,element} = ui();
  element('candidatePanel').hidden=false;element('reportPanel').hidden=false;element('reviewed').checked=true;
  vm.runInContext('clearTarget()',context);
  assert.equal(element('candidatePanel').hidden,true);
  assert.equal(element('reportPanel').hidden,true);
  assert.equal(element('reviewed').checked,false);
});

test('unscoped library documents are source choices and never plan targets', () => {
  const {context,element,catalog} = ui();
  element('sourceProgram').value='__library__';
  vm.runInContext('sourceOptions()',context);
  assert.deepEqual(element('sourceCourse').options.map(o=>o.value).filter(Boolean),(catalog.library_sources||[]).map(c=>c.id));
  const libraryIds=new Set((catalog.library_sources||[]).map(c=>c.id));
  assert.ok(element('targetCourse').options.every(o=>!libraryIds.has(o.value)));
});

test('formal report identifies the target program without publishing normalization procedures', () => {
  const {context,element} = ui();
  context.reportInput={id:'test',source:{id:'s',name:'مصدر',topics:[]},target:{id:'t',name:'هدف',topics:[],context:{program:'الأنظمة',degree:'بكالوريوس',version:'47'},extraction_method:'reviewed_visual_normalization'},
    calculation:{decision:'insufficient',label:'المعلومات لا تكفي',total_topics:0,strict_matches:{},lenient_matches:{},strict_count:0,lenient_count:0},evidence:{pairs:[]},notice:'اختبار فقط'};
  vm.runInContext('showReport(reportInput)',context);
  assert.ok(element('report').innerHTML.includes('الأنظمة'));
  assert.ok(element('report').innerHTML.includes('بكالوريوس'));
  assert.ok(!element('report').innerHTML.includes('ليست نقلًا حرفيًا'));
  assert.ok(!element('report').innerHTML.includes('reading-disclosure'));
});

test('report displays all decision outcomes with independent strict and lenient graphics', () => {
  for (const [decision,strict,lenient] of [['equivalent',4,5],['lenient',3,4],['not_equivalent',2,3]]) {
    const {html}=renderedReport(reportFixture({decision,strict,lenient}));
    assert.ok(html.includes(`data-decision="${decision}"`));
    assert.ok(html.includes('coverage-card'));
    assert.equal((html.match(/class="coverage-gauge"/g)||[]).length,2);
    assert.equal((html.match(/class="coverage-bar-row"/g)||[]).length,2);
    for (const [mode,count] of [['strict',strict],['lenient',lenient]]) {
      assert.ok(html.includes(`data-mode="${mode}" data-count="${count}" data-total="5" data-coverage="${count*20}"`));
      assert.ok(html.includes(`stroke-dasharray="${count*20} ${100-count*20}"`));
      assert.ok(html.includes(`class="coverage-bar-fill ${mode}" style="width:${count*20}%"`));
    }
    assert.ok(html.includes('80%'));
  }
});

test('insufficient evidence is shown as pending rather than zero percent or rejection', () => {
  const {html}=renderedReport(reportFixture({decision:'insufficient'}));
  assert.ok(html.includes('data-decision="insufficient"'));
  assert.ok(html.includes('coverage-pending'));
  assert.match(html,/موضوع.*غير محدد/);
  assert.ok(!html.includes('class="coverage-gauge"'));
  assert.ok(!html.includes('class="coverage-bar-fill"'));
  assert.ok(html.includes('data-mode="strict" data-count="" data-total="5" data-coverage=""'));
  assert.ok(!html.includes('0.0%'));
  assert.ok(!html.includes('لا يمكن معادلته'));
});

test('beneficiary and evidence are escaped while textual beneficiary numbers retain zeroes and Arabic digits', () => {
  const input=reportFixture({beneficiary:{name:'<img src=x onerror=alert(1)>',number:'٠٠١23&"'}});
  input.source.name='<script>source</script>';
  input.target.topics[0].text='<svg onload=alert(2)>';
  input.evidence.pairs[0].reason='<iframe src=evil>';
  const {html}=renderedReport(input);
  assert.ok(html.includes('beneficiary-card'));
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(html.includes('٠٠١23&amp;&quot;'));
  assert.ok(html.includes('&lt;script&gt;source&lt;/script&gt;'));
  assert.ok(html.includes('&lt;svg onload=alert(2)&gt;'));
  assert.ok(html.includes('&lt;iframe src=evil&gt;'));
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('<img src=x'));
  assert.ok(!html.includes('<svg onload'));
  assert.ok(!html.includes('<iframe'));
});

test('legacy report without beneficiary still renders its decision and source evidence', () => {
  const input=reportFixture();delete input.beneficiary;
  const {html}=renderedReport(input);
  assert.ok(html.includes(input.calculation.label));
  assert.ok(html.includes('تعليل الدليل 0'));
  assert.ok(!html.includes('undefined'));
  assert.ok(!html.includes('NaN'));
});

test('different maximum matchings preserve both source assignments and explanations', () => {
  const input=reportFixture({decision:'lenient',strict:1,lenient:2,total:2});
  input.calculation.strict_matches={t0:'s0'};
  input.calculation.lenient_matches={t0:'s1',t1:'s0'};
  input.evidence.pairs=[
    {target_id:'t0',source_id:'s0',score:85,reason:'المقابلة الدقيقة المستقلة'},
    {target_id:'t0',source_id:'s1',score:65,reason:'المقابلة المتسامحة البديلة'},
    {target_id:'t1',source_id:'s0',score:65,reason:'الهدف الثاني في الحساب المتسامح'}
  ];
  const {html}=renderedReport(input);
  assert.ok(html.includes('المقابلة الدقيقة المستقلة'));
  assert.ok(html.includes('المقابلة المتسامحة البديلة'));
  assert.ok(html.includes('الهدف الثاني في الحساب المتسامح'));
  assert.ok(html.includes('50.0%'));
  assert.ok(html.includes('100.0%'));
});

test('report keeps a 79.5 percent boundary result below the acceptance threshold', () => {
  const {html}=renderedReport(reportFixture({decision:'not_equivalent',strict:159,lenient:159,total:200}));
  assert.ok(html.includes('data-decision="not_equivalent"'));
  assert.ok(html.includes('79.5%'));
  assert.ok(html.includes('لا يمكن معادلته'));
  assert.ok(!html.includes('80.0%'));
});

test('beneficiary can be saved into an existing report without starting a comparison', async () => {
  const input=reportFixture();
  const {context,element}=renderedReport(input);
  element('beneficiaryName').value='مستفيد تجريبي';
  element('beneficiaryNumber').value='٠٠١234';
  context.updatedReport={...input,beneficiary:{name:'مستفيد تجريبي',number:'٠٠١234'}};
  context.calls=[];
  await vm.runInContext('api=async(route,body)=>{calls.push({route,body});return updatedReport;}; saveBeneficiary()',context);
  assert.deepEqual(JSON.parse(JSON.stringify(context.calls)),[{
    route:'/api/reports/test-report/beneficiary',body:{beneficiary:{name:'مستفيد تجريبي',number:'٠٠١234'}}
  }]);
  assert.ok(element('report').innerHTML.includes('مستفيد تجريبي'));
  assert.ok(element('report').innerHTML.includes('٠٠١234'));
  assert.equal(vm.runInContext('beneficiaryIsDirty()',context),false);
  await vm.runInContext('saveBeneficiary()',context);
  assert.equal(context.calls.length,1,'an unchanged beneficiary should not trigger another request');
});

test('starting a new target context keeps beneficiary draft without updating a hidden earlier report', async () => {
  const input=reportFixture({beneficiary:{name:'اسم التقرير السابق',number:'001'}});
  const {context,element}=renderedReport(input);
  vm.runInContext('clearTarget()',context);
  element('beneficiaryName').value='مسودة المستفيد الجديد';
  context.calls=[];
  await vm.runInContext('api=async(route,body)=>{calls.push({route,body});throw Error("must not edit hidden report");}; saveBeneficiary()',context);
  assert.equal(context.calls.length,0);
  assert.equal(element('beneficiaryName').value,'مسودة المستفيد الجديد');
  assert.equal(element('beneficiaryNumber').value,'001');
  assert.equal(element('reportPanel').hidden,true);
});

test('typing while beneficiary save is pending preserves the latest draft and prevents exporting stale details', async () => {
  const input=reportFixture();
  const {context,element}=renderedReport(input);
  element('beneficiaryName').value='الاسم المرسل';
  element('beneficiaryNumber').value='001';
  const pending=vm.runInContext('api=()=>new Promise(resolve=>{globalThis.finishRequest=resolve;}); saveBeneficiary()',context);
  element('beneficiaryName').value='الاسم الأحدث أثناء الحفظ';
  element('beneficiaryNumber').value='٠٠٢';
  context.finishRequest({...input,beneficiary:{name:'الاسم المرسل',number:'001'}});
  await assert.rejects(pending,/تغيرت بيانات المستفيد أثناء الحفظ/);
  assert.equal(element('beneficiaryName').value,'الاسم الأحدث أثناء الحفظ');
  assert.equal(element('beneficiaryNumber').value,'٠٠٢');
  assert.equal(vm.runInContext('beneficiaryIsDirty()',context),true);
  assert.ok(element('beneficiarySaveState').textContent.includes('غير محفوظة'));
});

test('a late beneficiary save cannot reopen an earlier report after the target context changes', async () => {
  const input=reportFixture();
  const {context,element}=renderedReport(input);
  element('beneficiaryName').value='بيانات التقرير السابق';
  const pending=vm.runInContext('api=()=>new Promise(resolve=>{globalThis.finishRequest=resolve;}); saveBeneficiary()',context);
  vm.runInContext('clearTarget()',context);
  element('beneficiaryName').value='مسودة السياق الجديد';
  context.finishRequest({...input,beneficiary:{name:'بيانات التقرير السابق',number:''}});
  await assert.rejects(pending,/تغير التقرير أثناء الحفظ/);
  assert.equal(element('reportPanel').hidden,true);
  assert.equal(element('beneficiaryName').value,'مسودة السياق الجديد');
});

function installSearchAPI(state,candidates) {
  state.context.requests=[];state.context.testCandidates=candidates;
  vm.runInContext(`api=async(route,body)=>{
    requests.push({route,body});
    if(route==='/api/sources'){
      if(body.catalog_id)return {...catalog.courses.find(course=>course.id===body.catalog_id),id:'incoming-source'};
      if(body.pdf_base64)return {...uploadExtractedSource,id:'incoming-source',name:body.name};
      return {id:'incoming-source',name:body.name,topics:body.topics,warnings:[],status:'manual'};
    }
    if(route==='/api/search')return {candidates:testCandidates,scope_count:732,unique_count:431,method:'test_retrieval'};
    throw Error('Unexpected endpoint: '+route);
  };`,state.context);
}
async function enterManualSource(state) {
  state.element('manualName').value='مقرر من جامعة أخرى';
  state.element('manualTopics').value='تعريف العلم ونشأته\nأقسام العلم ومبادئه';
  await state.element('loadManual').onclick({target:state.element('loadManual')});
}
function repeatedCandidate(catalog) {
  const groups=new Map();
  for(const course of catalog.courses.filter(c=>c.topics.length&&c.source_sha256)) {
    const key=JSON.stringify([course.code,course.name,course.source_sha256,course.topics]);
    const group=groups.get(key)||[];group.push(course);groups.set(key,group);
  }
  const group=[...groups.values()].find(courses=>courses.length>1).slice(0,2);
  return {...group[0],available_contexts:group.map(course=>({course_id:course.id,context_id:course.context_id,context:course.context}))};
}

test('entering a course invokes retrieval automatically across all contexts without starting a comparison', async () => {
  const state=ui(),candidate=state.catalog.courses.find(course=>course.topics.length);
  installSearchAPI(state,[candidate]);await enterManualSource(state);
  const calls=JSON.parse(JSON.stringify(state.context.requests));
  assert.deepEqual(calls.map(call=>call.route),['/api/sources','/api/search']);
  assert.equal(calls[1].body.source_id,'incoming-source');
  assert.equal(calls[1].body.all_contexts,true);
  assert.equal(calls[1].body.context_id,undefined);
  assert.deepEqual(calls[1].body.topics.map(topic=>topic.text),['تعريف العلم ونشأته','أقسام العلم ومبادئه']);
  assert.equal(state.element('candidatePanel').hidden,false);
  assert.equal(state.element('targetReview').hidden,false);
  assert.equal(state.element('sourceReview').open,false);
  assert.equal(state.element('targetTopicsReview').open,false);
  assert.equal(state.element('reviewed').checked,false);
  assert.equal(state.element('reportPanel').hidden,true);
  assert.equal(vm.runInContext('target.id',state.context),candidate.id);
});

test('search filters send a program-degree context union or one explicit plan', async () => {
  const state=ui(),scope=state.catalog.contexts.find(c=>state.catalog.contexts.filter(other=>other.program===c.program&&other.degree===c.degree).length>1);
  const group=scope.program+' | '+scope.degree;
  const candidate=state.catalog.courses.find(course=>course.context_id===scope.id&&course.topics.length);
  installSearchAPI(state,[candidate]);await enterManualSource(state);
  state.element('program').value=group;
  await state.element('program').onchange({target:state.element('program')});
  let payload=JSON.parse(JSON.stringify(state.context.requests.at(-1).body));
  assert.deepEqual(payload.context_ids,state.catalog.contexts.filter(c=>c.program===scope.program&&c.degree===scope.degree).map(c=>c.id));
  assert.equal(payload.all_contexts,undefined);
  assert.equal(payload.context_id,undefined);
  assert.equal(state.element('version').disabled,false);
  state.element('version').value=scope.id;
  await state.element('version').onchange({target:state.element('version')});
  payload=JSON.parse(JSON.stringify(state.context.requests.at(-1).body));
  assert.equal(payload.context_id,scope.id);
  assert.equal(payload.context_ids,undefined);
  assert.equal(payload.all_contexts,undefined);
});

test('a shared candidate requires an explicit target context and submits that original course identity', async () => {
  const state=ui(),candidate=repeatedCandidate(state.catalog),choice=candidate.available_contexts[1];
  installSearchAPI(state,[candidate]);await enterManualSource(state);
  assert.equal(state.element('candidateContextLabel').hidden,false);
  assert.equal(state.element('candidateContext').value,'');
  assert.equal(state.element('compareButton').disabled,true);
  assert.equal(vm.runInContext('targetContextConfirmed',state.context),false);
  assert.deepEqual(state.element('candidateContext').options.map(option=>option.value),['',...candidate.available_contexts.map(context=>context.course_id)]);
  state.element('candidateContext').value=choice.course_id;state.element('candidateContext').onchange();
  assert.equal(state.element('compareButton').disabled,false);
  assert.equal(vm.runInContext('target.id',state.context),choice.course_id);
  assert.equal(vm.runInContext('targetContextConfirmed',state.context),true);
  assert.ok(state.element('targetMeta').innerHTML.includes(choice.context.program));
  assert.ok(state.element('targetMeta').innerHTML.includes(choice.context.version));
  assert.equal(state.element('reviewed').hidden,true);
  assert.equal(state.element('reviewed').checked,false);
  await state.element('compareButton').onclick({target:state.element('compareButton')});
  const jobCall=JSON.parse(JSON.stringify(state.context.requests.find(call=>call.route==='/api/jobs')));
  assert.equal(jobCall.body.target_id,choice.course_id);
  assert.equal(jobCall.body.source_id,'incoming-source');
  assert.equal(jobCall.body.review_mode,'automatic');
  assert.equal(jobCall.body.reviewed,undefined);
});

test('an input without readable topics reports insufficient data without asking for a review', async () => {
  const state=ui();state.context.requests=[];state.context.emptySource={id:'empty-source',name:'مصدر بلا موضوعات',topics:[],warnings:[]};
  await vm.runInContext('api=async(route,body)=>{requests.push({route,body});throw Error("no retrieval for empty topics");};setSource(emptySource)',state.context);
  assert.equal(state.context.requests.length,0);
  assert.equal(state.element('sourceReview').open,false);
  assert.equal(state.element('candidatePanel').hidden,true);
  assert.ok(state.element('notice').textContent.includes('لا تتوافر مفردات مقروءة كافية'));
});

test('a late retrieval response cannot replace candidates for a newer source', async () => {
  const state=ui(),choices=state.catalog.courses.filter(course=>course.topics.length).slice(0,2);
  state.context.oldSource={id:'old-source',name:'المصدر السابق',topics:[{id:'s1',text:'مفردة المصدر السابق'}],warnings:[]};
  state.context.newSource={id:'new-source',name:'المصدر الحالي',topics:[{id:'s2',text:'مفردة المصدر الحالي'}],warnings:[]};
  state.context.latestCandidate=choices[1];
  const pending=vm.runInContext(`api=(route,body)=>body.source_id==='old-source'?new Promise(resolve=>{globalThis.finishRetrieval=resolve;}):Promise.resolve({candidates:[latestCandidate],scope_count:732,method:'test_retrieval'});setSource(oldSource)`,state.context);
  await vm.runInContext('setSource(newSource)',state.context);
  state.context.finishRetrieval({candidates:[choices[0]],scope_count:732,method:'test_retrieval'});await pending;
  assert.equal(vm.runInContext('source.id',state.context),'new-source');
  assert.equal(vm.runInContext('target.id',state.context),choices[1].id);
  assert.equal(state.element('candidates').querySelector('button').dataset.id,choices[1].id);
});

test('completed decision reports have the report seal while incomplete reports remain pending', () => {
  for(const [decision,strict,lenient] of [['equivalent',4,5],['lenient',3,4],['not_equivalent',2,3]]) {
    const {html}=renderedReport(reportFixture({decision,strict,lenient}));
    assert.ok(html.includes('class="report-stamp approved"'));
    assert.ok(html.includes('<strong>معتمد</strong>'));
    assert.ok(html.includes('تقرير المعادلة'));
    assert.ok(!html.includes('قيد الاستكمال'));
    assert.ok(!html.includes('اعتماد مجلس'));
  }
  const {html}=renderedReport(reportFixture({decision:'insufficient'}));
  assert.ok(html.includes('class="report-stamp pending"'));
  assert.ok(html.includes('قيد الاستكمال'));
  assert.ok(!html.includes('class="report-stamp approved"'));
  assert.ok(!html.includes('<strong>معتمد</strong>'));
});

test('formal presentation keeps academic evidence and external university while preserving the full audit JSON', () => {
  const input=reportFixture({beneficiary:{name:'مستفيد تجريبي',number:'٠٠١234'}});
  input.source.university='جامعة المصدر الخارجية';
  input.source.source_sha256='private-source-fingerprint';
  input.source.transcription_model='private-transcription-model';
  input.source.topics[0]={...input.source.topics[0],text:'النص الخام الأصلي',corrected_text:'المفردة الأكاديمية المراجعة',correction_method:'private-normalization',correction_note:'حذف المسافات وتوحيد الحروف'};
  input.target.source_sha256='private-target-fingerprint';
  input.target.extraction_method='reviewed_visual_normalization';
  input.evidence.model='private-comparison-model';input.evidence.model_digest='private-model-digest';
  input.notice='private-processing-notice';input.catalog_id='private-catalog-fingerprint';
  const before=JSON.stringify(input),{context,html}=renderedReport(input);
  assert.ok(html.includes('جامعة المصدر الخارجية'));
  assert.ok(html.includes('جامعة الطائف'));
  assert.ok(html.includes('المفردة الأكاديمية المراجعة'));
  assert.ok(html.includes('تعليل الدليل 0'));
  assert.ok(html.includes('تقرير معادلة المقررات'));
  for(const hidden of ['النص الخام الأصلي','حذف المسافات','private-source-fingerprint','private-target-fingerprint','private-transcription-model','private-comparison-model','private-model-digest','private-processing-notice','private-catalog-fingerprint','نسخة تجريبية','القراءات المصححة','بصمات التحقق','محرك المقارنة'])assert.ok(!html.includes(hidden),hidden+' must remain outside the beneficiary-facing document');
  assert.equal(JSON.stringify(input),before,'presentation cannot rewrite the saved evidence');
  assert.equal(vm.runInContext('currentReport.source.topics[0].correction_method',context),'private-normalization');
});

test('legacy whitespace-normalization explanations become academic reasons without changing their saved text', () => {
  const input=reportFixture();input.evidence.pairs[0].reason='تطابق نص المفردة كاملًا بعد توحيد المسافات والتشكيل';
  const {html}=renderedReport(input);
  assert.ok(html.includes('تتطابق المفردتان في الموضوع والنطاق.'));
  assert.ok(!html.includes('توحيد المسافات'));
  assert.equal(input.evidence.pairs[0].reason,'تطابق نص المفردة كاملًا بعد توحيد المسافات والتشكيل');
});


test('an uploaded PDF proceeds directly from extraction to automatic candidate retrieval', async () => {
  const state=ui(),candidate=state.catalog.courses.find(course=>course.topics.length);
  installSearchAPI(state,[candidate]);state.context.uploadExtractedSource={...candidate,context:null};
  const raw=new TextEncoder().encode('%PDF-1.7 test request payload');
  state.element('pdfFile').files=[{name:'source.pdf',size:raw.length,arrayBuffer:async()=>raw.buffer}];
  state.element('uploadName').value='المقرر الخارجي المرفوع';
  await state.element('extractButton').onclick({target:state.element('extractButton')});
  const calls=JSON.parse(JSON.stringify(state.context.requests));
  assert.deepEqual(calls.map(call=>call.route),['/api/sources','/api/search']);
  assert.equal(calls[0].body.name,'المقرر الخارجي المرفوع');
  assert.equal(calls[0].body.input_kind,undefined);
  assert.equal(calls[0].body.original_filename,undefined);
  assert.equal(Buffer.from(calls[0].body.pdf_base64,'base64').toString(),new TextDecoder().decode(raw));
  assert.equal(calls[1].body.all_contexts,true);
  assert.equal(calls[1].body.topics.length,candidate.topics.length);
  assert.equal(state.element('candidatePanel').hidden,false);
});

test('loading a catalog source also searches automatically without a separate search click', async () => {
  const state=ui(),candidate=state.catalog.courses.find(course=>course.topics.length);
  installSearchAPI(state,[candidate]);state.element('sourceCourse').value=candidate.id;
  await state.element('loadCourse').onclick({target:state.element('loadCourse')});
  const calls=JSON.parse(JSON.stringify(state.context.requests));
  assert.deepEqual(calls.map(call=>call.route),['/api/sources','/api/search']);
  assert.equal(calls[0].body.catalog_id,candidate.id);
  assert.equal(calls[1].body.all_contexts,true);
  assert.equal(calls[1].body.topics.length,candidate.topics.length);
});

test('queued cloud source operations are polled and return the completed source to the same caller', async()=>{
  const {context}=ui();context.calls=[];context.setTimeout=callback=>{callback();return 0;};
  context.responses=[{status:202,data:{operation_id:'queued-source'}},{status:200,data:{status:'running'}},{status:200,data:{status:'done',result:{id:'completed-source',topics:[]}}}];
  vm.runInContext('fetch=async(route,options)=>{calls.push({route,options});const reply=responses.shift();if(!reply)throw Error("unexpected poll");return {ok:true,status:reply.status,json:async()=>reply.data};};',context);
  const result=await vm.runInContext('api("/api/sources",{name:"مقرر",topics:[]})',context);
  assert.equal(result.id,'completed-source');
  assert.deepEqual(context.calls.map(c=>c.route),['/api/sources','/api/operations/queued-source','/api/operations/queued-source']);
  assert.equal(context.calls[0].options.credentials,'same-origin');
});

test('failed queued cloud operations stop rather than showing a fabricated source', async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};
  context.responses=[{status:202,data:{operation_id:'bad-source'}},{status:200,data:{status:'error',error:'تعذر قراءة التوصيف'}}];
  vm.runInContext('fetch=async()=>{const reply=responses.shift();return {ok:true,status:reply.status,json:async()=>reply.data};};',context);
  await assert.rejects(vm.runInContext('api("/api/sources",{name:"مقرر",topics:[]})',context),/تعذر قراءة التوصيف/);
});

test('a pending report link resumes after reload and retains its saved beneficiary',async()=>{
  const {context,element}=ui();context.setTimeout=callback=>{callback();return 0;};context.reportInput=reportFixture({beneficiary:{name:'صاحب التقرير',number:'٠٠١'}});
  context.routes=[];context.urls=[];
  vm.runInContext('history.replaceState=(_state,_title,url)=>urls.push(url); let polls=0; api=async route=>{routes.push(route);return ++polls===1?{status:"running"}:{status:"done",result:reportInput};};',context);
  await vm.runInContext('waitForReport("pending-report")',context);
  assert.deepEqual(context.urls,['?report=pending-report']);
  assert.equal(context.routes.length,2);
  assert.ok(element('report').innerHTML.includes('صاحب التقرير'));
  assert.ok(element('report').innerHTML.includes('٠٠١'));
});

test('an older source extraction cannot overwrite a later catalog choice',async()=>{
  const {context,element,catalog}=ui();const course=catalog.courses.find(c=>c.topics.length);
  context.newSource={...course,id:'new-source'};context.oldSource={...course,id:'old-source'};
  element('manualName').value='إدخال قديم';element('manualTopics').value='موضوع الإدخال القديم';element('sourceCourse').value=course.id;
  vm.runInContext('api=async(route,body)=>{if(route==="/api/sources")return body.catalog_id?newSource:new Promise(resolve=>globalThis.finishOldSource=resolve);if(route==="/api/search")return {candidates:[],scope_count:1};throw Error("unexpected route");};',context);
  const pending=element('loadManual').onclick({target:element('loadManual')});
  await element('loadCourse').onclick({target:element('loadCourse')});
  assert.equal(vm.runInContext('source.id',context),'new-source');
  context.finishOldSource(context.oldSource);await pending;
  assert.equal(vm.runInContext('source.id',context),'new-source');
});


test('extracted source and public reference are optional read-only details in automatic mode',()=>{
 const {context,element,catalog}=ui();context.courseInput=catalog.courses.find(c=>c.topics.length);
 vm.runInContext("referenceEditable=false; editor('sourceTopics',courseInput); setTarget(courseInput.id,true);",context);
 assert.equal(element('targetTopics').querySelector('textarea').readOnly,true);
 assert.equal(element('sourceTopics').querySelector('textarea').readOnly,true);
 assert.equal(element('targetTopics').querySelector('.add-topic').disabled,true);
 assert.equal(element('targetTopics').querySelector('.remove-topic').disabled,true);
});

test('polling retries temporary overload without repeating a submitted comparison',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=[];
  vm.runInContext('fetch=async(route,options)=>{calls.push({route,options});return calls.length===1?{ok:false,status:429,headers:{get:()=>"30"},json:async()=>({error:"busy"})}:{ok:true,status:200,json:async()=>({status:"running"})};};',context);
  assert.equal((await vm.runInContext('api("/api/jobs/abc")',context)).status,'running');assert.equal(context.calls.length,2);
  context.calls=[];
  await assert.rejects(vm.runInContext('api("/api/jobs",{source_id:"s",target_id:"t"})',context),/busy/);
  assert.equal(context.calls.length,1);
  context.calls=[];
  vm.runInContext('fetch=async(route,options)=>{calls.push({route,options});return calls.length===1?{ok:false,status:530,headers:{get:()=>null},json:async()=>({error:"tunnel propagation"})}:{ok:true,status:200,json:async()=>({status:"done"})};};',context);
  assert.equal((await vm.runInContext('api("/api/operations/abc")',context)).status,'done');assert.equal(context.calls.length,2);
  context.calls=[];await assert.rejects(vm.runInContext('api("/api/sources",{name:"مقرر"})',context),/tunnel propagation/);assert.equal(context.calls.length,1);
  context.calls=[];vm.runInContext('fetch=async()=>{calls.push(1);return {ok:false,status:530,headers:{get:()=>null},json:async()=>({error:"still unavailable"})};};',context);
  await assert.rejects(vm.runInContext('api("/api/operations/abc")',context),/still unavailable/);assert.equal(context.calls.length,7);
});

test('a brief lost connection resumes GET polling and never resubmits a POST',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=0;
  vm.runInContext('fetch=async()=>{if(++calls===1)throw Error("connection lost");return {ok:true,status:200,json:async()=>({status:"done"})};};',context);
  assert.equal((await vm.runInContext('api("/api/operations/abc")',context)).status,'done');assert.equal(context.calls,2);
  context.calls=0;await assert.rejects(vm.runInContext('api("/api/sources",{name:"مقرر"})',context),/connection lost/);assert.equal(context.calls,1);
});

test('a successful GET with truncated JSON is read again within the retry budget',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=0;
  vm.runInContext('fetch=async()=>{const attempt=++calls;return {ok:true,status:200,json:async()=>{if(attempt===1)throw new SyntaxError("Unexpected end of JSON");return {status:"done"};}};};',context);
  assert.equal((await vm.runInContext('api("/api/jobs/abc")',context)).status,'done');
  assert.equal(context.calls,2);
});

test('a successful POST with truncated JSON fails clearly without resubmission',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=0;
  vm.runInContext('fetch=async()=>{calls++;return {ok:true,status:200,json:async()=>{throw new SyntaxError("Unexpected end of JSON");}};};',context);
  await assert.rejects(vm.runInContext('api("/api/jobs",{source_id:"s",target_id:"t"})',context),/استجابة غير مكتملة/);
  assert.equal(context.calls,1);
});

test('a permanently truncated successful GET stops after seven total calls',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=0;
  vm.runInContext('fetch=async()=>{calls++;return {ok:true,status:200,json:async()=>{throw new SyntaxError("Unexpected end of JSON");}};};',context);
  await assert.rejects(vm.runInContext('api("/api/catalog")',context),/استجابة غير مكتملة/);
  assert.equal(context.calls,7);
});

test('HTML access errors remain generic and are never retried',async()=>{
  for(const status of [404,403]) {
    const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=0;context.errorStatus=status;
    vm.runInContext('fetch=async()=>{calls++;return {ok:false,status:errorStatus,json:async()=>{throw new SyntaxError("HTML response");}};};',context);
    await assert.rejects(vm.runInContext('api("/api/jobs/missing")',context),/تعذر تنفيذ الطلب/);
    assert.equal(context.calls,1);
  }
});

test('search scope is selectable before upload for one program or all programs',async()=>{
  const {context,element}=ui();
  assert.equal(element('searchScopeMode').value,'all');assert.equal(element('scopeProgramFields').hidden,true);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(searchScope())',context)),{all_contexts:true});
  element('searchScopeMode').value='program';await element('searchScopeMode').onchange({target:element('searchScopeMode')});
  assert.equal(element('scopeProgramFields').hidden,false);assert.throws(()=>vm.runInContext('searchScope()',context),/اختر البرنامج/);
  element('program').value=element('program').options.find(o=>o.value).value;await element('program').onchange({target:element('program')});
  const scoped=JSON.parse(vm.runInContext('JSON.stringify(searchScope())',context));assert.ok(scoped.context_ids.length);assert.equal(scoped.all_contexts,undefined);
  element('searchScopeMode').value='all';await element('searchScopeMode').onchange({target:element('searchScopeMode')});
  assert.equal(element('program').value,'');assert.equal(element('version').value,'');assert.equal(element('scopeProgramFields').hidden,true);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(searchScope())',context)),{all_contexts:true});
});

test('rejected reports explain the closest weak match without counting it',()=>{
  const input=reportFixture({decision:'not_equivalent',strict:0,lenient:0,total:2});
  input.evidence.pairs[0].score=25;input.evidence.pairs[0].reason='يقتصر المصدر على الطهارة ولا يشمل موضوع الزكاة.';
  const before=JSON.stringify(input.calculation);const {html}=renderedReport(input);
  assert.ok(html.includes('يقتصر المصدر على الطهارة ولا يشمل موضوع الزكاة.'));assert.ok(html.includes('25%'));
  assert.ok(html.includes('أقرب مقابلة'));assert.ok(html.includes('دون حد التوافق'));assert.equal(JSON.stringify(input.calculation),before);
});

test('standalone report embeds both local fonts and reuses the self-contained stylesheet',async()=>{
  const {context}=ui(),calls=[];
  const css="@font-face{src:url('/fonts/IBMPlexSansArabic-Regular.ttf')}@font-face{src:url('/fonts/IBMPlexSansArabic-SemiBold.ttf')}";
  const bytes=new Uint8Array(210000).fill(71);
  context.fetch=async path=>{calls.push(path);return path==='/style.css'?{ok:true,text:async()=>css}:{ok:true,arrayBuffer:async()=>bytes.buffer};};
  const embedded=await vm.runInContext('standaloneReportCSS()',context);
  assert.ok(!embedded.includes('/fonts/'));assert.equal((embedded.match(/data:font\/ttf;base64,/g)||[]).length,2);
  assert.equal(embedded.includes(Buffer.from(bytes).toString('base64')),true);
  assert.equal(await vm.runInContext('standaloneReportCSS()',context),embedded);assert.equal(calls.length,3);
});

test('failed report font download can be retried and never silently exports a broken stylesheet',async()=>{
  const {context}=ui();let failed=true;
  context.fetch=async path=>path==='/style.css'?{ok:true,text:async()=>"url('/fonts/IBMPlexSansArabic-Regular.ttf')"}:
    failed?{ok:false}:{ok:true,arrayBuffer:async()=>new Uint8Array([1,2,3]).buffer};
  await assert.rejects(vm.runInContext('standaloneReportCSS()',context),/خط التقرير/);
  failed=false;assert.match(await vm.runInContext('standaloneReportCSS()',context),/data:font\/ttf;base64,AQID/);
});

test('unconfirmed compound matches are shown without a misleading definitive grade',()=>{
  const report=reportFixture({decision:'not_equivalent',strict:2,lenient:2});
  report.evidence.pairs[3].score=85;report.evidence.pairs[3].adjudication={state:'uncertain'};
  const {html}=renderedReport(report);
  assert.match(html,/لم يثبت نطاق هذه المقابلة/);
  assert.match(html,/غير محتسبة · <bdi dir="ltr">قيد الاستكمال/);
});

test('image upload normalizes privately into the existing PDF endpoint before automatic search',async()=>{
 const state=ui(),candidate=state.catalog.courses.find(c=>c.topics.length);installSearchAPI(state,[candidate]);state.context.uploadExtractedSource={...candidate,id:'image-source',context:null,upload_pdf:true};vm.runInContext('maxUploadBytes=4*1024*1024',state.context);
 const prepared=new TextEncoder().encode('%PDF-1.4 converted image'),calls=[];
 state.context.CourseImageUpload={...state.context.CourseImageUpload,prepare:async(file,options)=>{calls.push({name:file.name,maxOutputBytes:options.maxOutputBytes});return{bytes:prepared};}};
 state.element('pdfFile').files=[{name:'جدول المفردات.png',type:'image/png',size:6*1024*1024,arrayBuffer(){throw Error('must use image normalizer');}}];
 await state.element('extractButton').onclick({target:state.element('extractButton')});
 const requests=JSON.parse(JSON.stringify(state.context.requests));assert.deepEqual(requests.map(r=>r.route),['/api/sources','/api/search']);assert.equal(calls.length,1);assert.equal(calls[0].maxOutputBytes,4*1024*1024);assert.equal(requests[0].body.input_kind,'image');assert.equal(requests[0].body.original_filename,'جدول المفردات.png');assert.equal(Buffer.from(requests[0].body.pdf_base64,'base64').toString(),new TextDecoder().decode(prepared));assert.equal(requests[0].body.image_base64,undefined);assert.equal(state.element('sourceReview').open,false);assert.equal(state.element('reviewed').hidden,true);
});

test('image conversion failure does not submit an upload or a comparison',async()=>{
 const state=ui();state.context.requests=[];state.context.CourseImageUpload={...state.context.CourseImageUpload,prepare:async()=>{throw Error('الصورة كبيرة بعد تهيئتها.');}};state.context.fetch=async()=>{throw Error('no HTTP call allowed');};state.element('pdfFile').files=[{name:'photo.jpg',type:'image/jpeg',size:100}];await state.element('extractButton').onclick({target:state.element('extractButton')});assert.match(state.element('notice').textContent,/كبيرة بعد تهيئتها/);assert.equal(state.element('extractButton').disabled,false);assert.equal(state.context.requests.length,0);
});

test('finishing an old image conversion cannot submit it after a newer source was selected',async()=>{
 const state=ui(),candidate=state.catalog.courses.find(c=>c.topics.length);installSearchAPI(state,[candidate]);let finish;
 state.context.CourseImageUpload={...state.context.CourseImageUpload,prepare:()=>new Promise(resolve=>{finish=resolve;})};state.element('pdfFile').files=[{name:'old.png',type:'image/png',size:100}];const pending=state.element('extractButton').onclick({target:state.element('extractButton')});await enterManualSource(state);finish({bytes:new Uint8Array([37,80,68,70])});await pending;
 const posts=JSON.parse(JSON.stringify(state.context.requests));assert.equal(posts.filter(p=>p.route==='/api/sources').length,1);assert.ok(posts.find(p=>p.route==='/api/sources').body.topics);assert.ok(posts.every(p=>p.body?.input_kind!=='image'));assert.equal(vm.runInContext('source.id',state.context),'incoming-source');
});

test('automatic issuance does not require human attestation even when a source contains an unclear marker',async()=>{
 const state=ui(),candidate=state.catalog.courses.find(c=>c.topics.length);installSearchAPI(state,[candidate]);state.context.markerSource={id:'unclear-source',name:'مصدر الصورة',topics:[{id:'t1',text:'مفردة [غير واضح]'}],warnings:['راجع وتصحيح تفاصيل تقنية']};await vm.runInContext('setSource(markerSource)',state.context);
 assert.equal(state.element('sourceReview').open,false);assert.equal(state.element('reviewed').checked,false);assert.ok(!state.element('sourceMeta').innerHTML.includes('راجع'));await state.element('compareButton').onclick({target:state.element('compareButton')});const request=JSON.parse(JSON.stringify(state.context.requests.find(r=>r.route==='/api/jobs')));assert.equal(request.body.review_mode,'automatic');assert.equal(request.body.reviewed,undefined);assert.equal(request.body.source_topics[0].text,'مفردة [غير واضح]');assert.ok(state.context.markerSource.warnings[0].includes('راجع'));
});

test('pending report states the data limitation formally without assigning the beneficiary a review task',()=>{
 for(const flag of['reference_review','adjudication_review','segmentation_review','invalid_verse_scopes']){const report=reportFixture({decision:'insufficient'});report.calculation[flag]=true;const {html}=renderedReport(report);assert.ok(html.includes('تعذر الحسم'));assert.ok(html.includes('نطاق المفردات المتاح'));assert.ok(html.includes('بيانات المقررين وأسباب تعذر الحسم'));assert.ok(!html.includes('قبل المقارنة'));assert.ok(!/يلزم|راجع|أكد مراجعة|راجعت/.test(html));assert.ok(html.includes('report-stamp pending'));assert.ok(!html.includes('report-stamp approved'));}
});

test('markup loads the local converter before the app and exposes only supported upload families',()=>{
 const html=fs.readFileSync(new URL('../equivalency-pilot/index.html',import.meta.url),'utf8');assert.ok(html.indexOf('src="/image-upload.js"')<html.indexOf('src="/app.js"'));assert.match(html,/accept="application\/pdf,\.pdf,image\/jpeg,\.jpg,\.jpeg,image\/png,\.png,image\/webp,\.webp"/);assert.ok(!html.includes('راجعت مفردات'));assert.ok(!html.includes('مراجعة مفردات'));
});

test('an ambiguous queued POST recovers its accepted search through GET without another POST',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=[];
  vm.runInContext(`requestRecovery=true;fetch=async(route,options)=>{
    calls.push({route,options});
    if(options?.method==='POST')return {ok:false,status:502,json:async()=>{throw SyntaxError('upstream HTML');}};
    if(route.startsWith('/api/requests/'))return {ok:true,status:200,json:async()=>({id:'a'.repeat(32),operation_id:'a'.repeat(32),kind:'search',status:'done'})};
    return {ok:true,status:200,json:async()=>({status:'done',result:{candidates:[{id:'real-candidate'}]}})};
  };`,context);
  const result=await vm.runInContext('api("/api/search",{source_id:"source",limit:8})',context);
  assert.equal(result.candidates[0].id,'real-candidate');assert.equal(context.calls.filter(c=>c.options?.method==='POST').length,1);
  const submitted=JSON.parse(context.calls[0].options.body);assert.match(submitted.request_id,/^[a-f0-9]{32}$/);
  assert.equal(context.calls[1].route,'/api/requests/'+submitted.request_id);assert.equal(vm.runInContext('pendingQueueRequests.size',context),0);
});

test('a lost comparison acknowledgement resumes the existing job without submitting it twice',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=[];
  vm.runInContext(`requestRecovery=true;fetch=async(route,options)=>{calls.push({route,options});if(options?.method==='POST')throw TypeError('lost response');return {ok:true,status:200,json:async()=>({id:'b'.repeat(32),kind:'compare',status:'running'})};};`,context);
  assert.equal((await vm.runInContext('api("/api/jobs",{source_id:"source",target_id:"target",review_mode:"automatic"})',context)).id,'b'.repeat(32));
  assert.equal(context.calls.length,2);assert.equal(context.calls.filter(c=>c.options?.method==='POST').length,1);
});

test('a truncated successful POST acknowledgement is recovered by its client request identifier',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=[];
  vm.runInContext(`requestRecovery=true;fetch=async(route,options)=>{calls.push({route,options});if(options?.method==='POST')return {ok:true,status:202,json:async()=>{throw SyntaxError('truncated');}};return {ok:true,status:200,json:async()=>({id:'c'.repeat(32),kind:'compare',status:'queued'})};};`,context);
  assert.equal((await vm.runInContext('api("/api/jobs",{source_id:"source",target_id:"target"})',context)).status,'queued');
  assert.equal(context.calls.filter(c=>c.options?.method==='POST').length,1);
});

test('a definitive validation rejection never starts recovery or keeps a pending request',async()=>{
  const {context}=ui();context.calls=[];
  vm.runInContext('requestRecovery=true;fetch=async(route,options)=>{calls.push({route,options});return {ok:false,status:400,json:async()=>({error:"invalid source"})};};',context);
  await assert.rejects(vm.runInContext('api("/api/sources",{topics:[]})',context),/invalid source/);
  assert.equal(context.calls.length,1);assert.equal(vm.runInContext('pendingQueueRequests.size',context),0);
});

test('an unconfirmed request remains resumable with only identifiers and hashes stored in the browser',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=[];context.storage=new Map();
  vm.runInContext(`sessionStorage={getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)};requestRecovery=true;fetch=async(route,options)=>{calls.push({route,options});if(options?.method==='POST')throw TypeError('lost response');return {ok:false,status:503,json:async()=>({error:'transport unavailable'})};};`,context);
  await assert.rejects(vm.runInContext('api("/api/sources",{name:"PRIVATE SOURCE",pdf_base64:"PRIVATE DOCUMENT BYTES"})',context),/transport unavailable/);
  assert.equal(context.calls.filter(c=>c.options?.method==='POST').length,1);assert.equal(vm.runInContext('pendingQueueRequests.size',context),1);
  const saved=context.storage.get('coursematch.pendingRequests');assert.ok(!saved.includes('PRIVATE'));assert.ok(!saved.includes('pdf_base64'));
  context.calls=[];
  vm.runInContext(`fetch=async(route,options)=>{calls.push({route,options});return {ok:true,status:200,json:async()=>route.startsWith('/api/requests/')?{id:'d'.repeat(32),operation_id:'d'.repeat(32),kind:'source',status:'done'}:{status:'done',result:{id:'completed-source',topics:[]}}};};`,context);
  assert.equal((await vm.runInContext('api("/api/sources",{name:"PRIVATE SOURCE",pdf_base64:"PRIVATE DOCUMENT BYTES"})',context)).id,'completed-source');
  assert.equal(context.calls.filter(c=>c.options?.method==='POST').length,0);
});


test('bootstrap resumes an accepted comparison from stored identifiers after reload without any POST',async()=>{
  const nonce='f'.repeat(32),job='a'.repeat(32),saved=new Map([['coursematch.pendingRequests',JSON.stringify([['b'.repeat(64),{id:nonce,route:'/api/jobs',at:Date.now()}]])]]);
  const {context,element}=ui({sessionStorage:{getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value)}});
  context.calls=[];context.reportInput={...reportFixture(),id:job};
  vm.runInContext(`fetch=async(route,options)=>{calls.push({route,options});return {ok:true,status:200,json:async()=>route==='/api/catalog'?{catalog:inputCatalog,token:'test',capabilities:{request_recovery:true}}:route.startsWith('/api/requests/')?{id:'${job}',kind:'compare',status:'done'}:{status:'done',result:reportInput}};};`,context);
  const text=fs.readFileSync(new URL('../equivalency-pilot/app.js',import.meta.url),'utf8');
  await vm.runInContext(text.slice(text.lastIndexOf('(async()=>{')),context);
  assert.equal(context.calls.filter(c=>c.options?.method==='POST').length,0);
  assert.equal(context.calls.filter(c=>c.route==='/api/requests/'+nonce).length,1);
  assert.equal(vm.runInContext('currentReport.id',context),job);assert.ok(element('report').innerHTML.includes('تقرير معادلة المقررات'));
  assert.equal(saved.get('coursematch.pendingRequests'),'[]');
});


test('a definitive POST 429 clears the pending nonce and permits a later explicit attempt',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=[];
  vm.runInContext(`requestRecovery=true;fetch=async(route,options)=>{calls.push({route,options});return calls.length===1?{ok:false,status:429,json:async()=>({error:'queue full'})}:{ok:true,status:202,json:async()=>({id:'a'.repeat(32),status:'queued'})};};`,context);
  await assert.rejects(vm.runInContext('api("/api/jobs",{source_id:"source",target_id:"target"})',context),/queue full/);
  assert.equal(context.calls.length,1);assert.equal(vm.runInContext('pendingQueueRequests.size',context),0);
  assert.equal((await vm.runInContext('api("/api/jobs",{source_id:"source",target_id:"target"})',context)).id,'a'.repeat(32));
  assert.equal(context.calls.length,2);assert.ok(context.calls.every(c=>c.options?.method==='POST'));
  assert.notEqual(JSON.parse(context.calls[0].options.body).request_id,JSON.parse(context.calls[1].options.body).request_id);
});

test('three authenticated missing-request responses stop recovery without automatically posting again',async()=>{
  const {context}=ui();context.setTimeout=callback=>{callback();return 0;};context.calls=[];
  vm.runInContext(`requestRecovery=true;fetch=async(route,options)=>{calls.push({route,options});if(options?.method==='POST')throw TypeError('lost acknowledgement');return {ok:false,status:404,json:async()=>({error:'not accepted'})};};`,context);
  await assert.rejects(vm.runInContext('api("/api/search",{source_id:"source",limit:8})',context),/لم يثبت استلام/);
  assert.equal(context.calls.length,4);assert.equal(context.calls.filter(c=>c.options?.method==='POST').length,1);
  assert.equal(vm.runInContext('pendingQueueRequests.size',context),0);
});


function uploadedCourse(id='uploaded-target') {return {id,name:'مقرر مرفوع '+id,upload_pdf:true,topics:[{id:'t1',text:'مفردة أكاديمية من التوصيف',page:1}]};}
function pdfFile(name='course.pdf'){return {name,type:'application/pdf',size:8,arrayBuffer:async()=>new TextEncoder().encode('%PDF-1.4').buffer};}

test('pair mode reuses the current source without retrieval and returning to repository resumes automatic search',async()=>{
 const {context,element}=ui();context.sourceInput=uploadedCourse('original');context.requests=[];
 await vm.runInContext(`api=async(route,body)=>{requests.push({route,body});return {candidates:[],scope_count:10}};setComparisonMode('upload_pair');setSource(sourceInput)`,context);
 assert.equal(context.requests.length,0);assert.equal(element('searchScopePanel').hidden,true);assert.equal(element('targetUploadPanel').hidden,false);assert.equal(element('candidatePanel').hidden,true);assert.equal(element('pairCompareButton').disabled,true);
 await vm.runInContext("setComparisonMode('repository')",context);
 assert.deepEqual(context.requests.map(x=>x.route),['/api/search']);assert.equal(context.requests[0].body.all_contexts,true);assert.equal(element('targetUploadPanel').hidden,true);assert.equal(element('pairComparisonPanel').hidden,true);assert.equal(element('searchScopePanel').hidden,false);
});

test('uploaded target uses the same private PDF or image intake, read-only topics and no search',async()=>{
 for(const image of[false,true]){
  const {context,element}=ui();context.original=uploadedCourse('original');context.targetInput=uploadedCourse();context.requests=[];
  await vm.runInContext("api=async(route,body)=>{requests.push({route,body});return targetInput};setComparisonMode('upload_pair');setSource(original)",context);
  element('targetPdfFile').files=[image?{name:'target.png',type:'image/png',size:10}:pdfFile()];
  if(image)context.CourseImageUpload={...context.CourseImageUpload,prepare:async()=>({bytes:new Uint8Array([37,80,68,70])})};
  await element('targetExtractButton').onclick({target:element('targetExtractButton')});
  assert.deepEqual(context.requests.map(x=>x.route),['/api/sources']);assert.equal(context.requests[0].body.input_kind,image?'image':undefined);assert.equal(context.requests[0].body.original_filename,image?'target.png':undefined);assert.ok(context.requests[0].body.pdf_base64);assert.equal(element('pairCompareButton').disabled,false);assert.equal(element('uploadedTargetTopics').querySelector('textarea').readOnly,true);assert.equal(element('uploadedTargetPanel').hidden,false);assert.match(element('uploadedTargetMeta').innerHTML,/\/pdf\/uploaded-target/);
 }
});

test('pair comparison sends only owner artifact IDs, automatic mode and beneficiary',async()=>{
 const {context,element}=ui();context.original=uploadedCourse('original');context.targetInput=uploadedCourse();context.requests=[];
 await vm.runInContext("setComparisonMode('upload_pair');uploadedTarget=targetInput;setSource(original);api=async(route,body)=>{requests.push({route,body});return {id:'pair-job'}};waitForReport=async()=>{}",context);
 element('beneficiaryName').value='مستفيد المقارنة';element('beneficiaryNumber').value='00042';await element('pairCompareButton').onclick({target:element('pairCompareButton')});
 const call=JSON.parse(JSON.stringify(context.requests[0]));assert.equal(call.route,'/api/jobs');assert.deepEqual(call.body,{source_id:'original',target_source_id:'uploaded-target',review_mode:'automatic',beneficiary:{name:'مستفيد المقارنة',number:'00042'}});assert.equal(element('reviewed').checked,false);
});

test('switching mode discards an in-flight repository search instead of opening a stale candidate',async()=>{
 const {context,element,catalog}=ui();context.original=uploadedCourse('original');context.candidate=catalog.courses.find(c=>c.topics.length);
 const pending=vm.runInContext("api=()=>new Promise(resolve=>globalThis.finishSearch=resolve);setSource(original)",context);
 await vm.runInContext("setComparisonMode('upload_pair')",context);context.finishSearch({candidates:[context.candidate],scope_count:20});await pending;
 assert.equal(element('candidatePanel').hidden,true);assert.equal(vm.runInContext('target',context),null);assert.equal(element('reportPanel').hidden,true);
});

test('a late target extraction is discarded after its file changes or after mode changes away and back',async()=>{
 for(const change of['file','mode']){
  const {context,element}=ui();context.targetInput=uploadedCourse();await vm.runInContext("setComparisonMode('upload_pair');api=()=>new Promise(resolve=>globalThis.finishTarget=resolve)",context);element('targetPdfFile').files=[pdfFile()];
  const pending=element('targetExtractButton').onclick({target:element('targetExtractButton')});await new Promise(resolve=>setImmediate(resolve));
  if(change==='file')element('targetPdfFile').onchange();else await vm.runInContext("setComparisonMode('repository');setComparisonMode('upload_pair')",context);
  context.finishTarget(context.targetInput);await pending;assert.equal(vm.runInContext('uploadedTarget',context),null);assert.equal(element('uploadedTargetPanel').hidden,true);assert.equal(element('pairCompareButton').disabled,true);
 }
});

test('switching mode discards a late original extraction without launching search in the new mode',async()=>{
 const {context,element}=ui();context.original=uploadedCourse('old');context.calls=[];element('pdfFile').files=[pdfFile()];
 vm.runInContext("api=(route,body)=>{calls.push(route);return new Promise(resolve=>globalThis.finishOriginal=resolve)}",context);
 const pending=element('extractButton').onclick({target:element('extractButton')});await new Promise(resolve=>setImmediate(resolve));await vm.runInContext("setComparisonMode('upload_pair')",context);context.finishOriginal(context.original);await pending;
 assert.equal(vm.runInContext('source',context),undefined);assert.equal(element('sourcePanel').hidden,true);assert.deepEqual([...context.calls],['/api/sources']);
});

test('changing either uploaded file closes the old report and disables issuance until both sources are ready',async()=>{
 for(const input of['pdfFile','targetPdfFile']){
  const {context,element}=ui();context.original=uploadedCourse('original');context.targetInput=uploadedCourse();context.oldReport=reportFixture();
  await vm.runInContext("setComparisonMode('upload_pair');uploadedTarget=targetInput;setSource(original);showReport(oldReport)",context);element(input).onchange();
  assert.equal(element('reportPanel').hidden,true);assert.equal(vm.runInContext('currentReport',context),null);assert.equal(element('pairCompareButton').disabled,true);
 }
});

test('a late pair job acknowledgement cannot reopen a report after a target file change',async()=>{
 const {context,element}=ui();context.original=uploadedCourse('original');context.targetInput=uploadedCourse();context.polls=[];
 await vm.runInContext("setComparisonMode('upload_pair');uploadedTarget=targetInput;setSource(original);api=()=>new Promise(resolve=>globalThis.finishJob=resolve);waitForReport=async id=>polls.push(id)",context);
 const pending=element('pairCompareButton').onclick({target:element('pairCompareButton')});element('targetPdfFile').onchange();context.finishJob({id:'stale-job'});await pending;
 assert.equal(context.polls.length,0);assert.equal(element('reportPanel').hidden,true);assert.equal(element('pairCompareButton').disabled,true);
});

test('a late completed pair report cannot reopen after changing mode away and back',async()=>{
 const {context,element}=ui();context.original=uploadedCourse('original');context.targetInput=uploadedCourse();context.reportInput=reportFixture();
 await vm.runInContext("setComparisonMode('upload_pair');uploadedTarget=targetInput;setSource(original);api=()=>new Promise(resolve=>globalThis.finishPoll=resolve)",context);
 const pending=vm.runInContext("waitForReport('old-job',{sourceId:source.id,targetId:target.id,mode:comparisonMode,revision:comparisonSequence,beneficiary:{}})",context);
 await vm.runInContext("source=null;setComparisonMode('repository');source=original;setComparisonMode('upload_pair')",context);context.finishPoll({status:'done',result:context.reportInput});await pending;assert.equal(element('reportPanel').hidden,true);
});

test('uploaded report has neutral target identity, private links and unchanged academic evidence',()=>{
 const input=reportFixture();input.target_mode='uploaded';input.source.upload_pdf=true;input.target.upload_pdf=true;input.target.university='جامعة الهدف';const {html}=renderedReport(input);
 assert.match(html,/<h1>تقرير مقارنة المقررات<\/h1>/);assert.ok(html.includes('المقرر الأصل'));assert.ok(html.includes('جامعة الهدف'));assert.ok(html.includes('/pdf/source'));assert.ok(html.includes('/pdf/target'));assert.ok(!html.includes('جامعة الطائف'));assert.ok(!html.includes('الأنظمة'));assert.ok(!html.includes('الخطة'));assert.ok(html.includes('تعليل الدليل 0'));assert.ok(html.includes('report-stamp approved'));assert.equal(input.target.context.program,'الأنظمة');
 delete input.target.university;const neutral=renderedReport(input).html;assert.equal((neutral.match(/مقرر مرفوع للمقارنة/g)||[]).length,2);assert.ok(!neutral.includes('مقرر مصدر المعادلة'));
});


test('identical concurrent original and target uploads retain separate recovery nonces and source identities',async()=>{
 const {context,element}=ui();const posts=[],releases=[];context.setTimeout=callback=>{callback();return 0};
 context.fetch=async(route,options)=>{
  assert.equal(route,'/api/sources','pair intake must not search or recover the other role');assert.equal(options.method,'POST');const body=JSON.parse(options.body);posts.push(body);
  await new Promise(resolve=>releases.push(resolve));return {status:200,ok:true,json:async()=>uploadedCourse(body.intake_role==='source'?'original-artifact':'target-artifact')};
 };
 await vm.runInContext("requestRecovery=true;setComparisonMode('upload_pair')",context);
 const sameFile=pdfFile('same.pdf');element('pdfFile').files=[sameFile];element('targetPdfFile').files=[sameFile];element('uploadName').value='نفس الاسم';element('targetUploadName').value='نفس الاسم';
 const sourcePending=element('extractButton').onclick({target:element('extractButton')}),targetPending=element('targetExtractButton').onclick({target:element('targetExtractButton')});
 for(let n=0;n<30&&posts.length<2;n++)await new Promise(resolve=>setImmediate(resolve));
 assert.equal(posts.length,2);assert.deepEqual(posts.map(x=>x.intake_role).sort(),['source','target']);assert.equal(new Set(posts.map(x=>x.request_id)).size,2);for(const p of posts)assert.match(p.request_id,/^[a-f0-9]{32}$/);
 const neutral=p=>({...p,intake_role:undefined,request_id:undefined});assert.deepEqual(neutral(posts[0]),neutral(posts[1]));releases.forEach(resolve=>resolve());await Promise.all([sourcePending,targetPending]);
 assert.equal(vm.runInContext('source.id',context),'original-artifact');assert.equal(vm.runInContext('uploadedTarget.id',context),'target-artifact');assert.equal(vm.runInContext('pendingQueueRequests.size',context),0);assert.equal(element('pairCompareButton').disabled,false);
});

test('repository uploads retain the source role and automatic retrieval contract',async()=>{
 const state=ui(),candidate=state.catalog.courses.find(c=>c.topics.length);installSearchAPI(state,[candidate]);state.context.uploadExtractedSource={...candidate,context:null};state.element('pdfFile').files=[pdfFile()];await state.element('extractButton').onclick({target:state.element('extractButton')});
 const calls=JSON.parse(JSON.stringify(state.context.requests));assert.deepEqual(calls.map(x=>x.route),['/api/sources','/api/search']);assert.equal(calls[0].body.intake_role,'source');assert.equal(calls[0].body.input_kind,undefined);assert.equal(calls[1].body.all_contexts,true);assert.equal(calls[1].body.limit,8);
});

test('report polling recovers one transient GET 500 HTML or JSON and renders the same completed job',async()=>{
  for(const errorFormat of ['html','json']) {
    const {context,element}=ui();context.setTimeout=callback=>{callback();return 0;};
    const id='a'.repeat(32);context.jobId=id;context.errorFormat=errorFormat;context.calls=[];context.urls=[];context.errorBodyReads=0;
    context.completedReport={...reportFixture({beneficiary:{name:'مستفيد الاسترداد',number:'٠٠١500'}}),id};
    vm.runInContext(`history.replaceState=(_state,_title,url)=>urls.push(url);
      fetch=async(route,options)=>{
        calls.push({route,options});
        if(calls.length===1)return {ok:false,status:500,headers:{get:()=>null},json:async()=>{
          errorBodyReads++;if(errorFormat==='html')throw SyntaxError('HTML500');
          return {error:'temporary 500',id:'f'.repeat(32),operation_id:'f'.repeat(32),status:'done'};
        }};
        return {ok:true,status:200,json:async()=>({id:jobId,status:'done',result:completedReport})};
      };`,context);
    await vm.runInContext('waitForReport(jobId)',context);
    assert.equal(context.calls.length,2);assert.equal(context.errorBodyReads,0);
    assert.deepEqual(context.calls.map(call=>call.route),['/api/jobs/'+id,'/api/jobs/'+id]);
    assert.ok(context.calls.every(call=>!call.options.method&&call.options.credentials==='same-origin'&&!call.options.body));
    assert.deepEqual(context.urls,['?report='+id]);assert.equal(vm.runInContext('currentReport.id',context),id);
    assert.equal(element('reportPanel').hidden,false);assert.ok(element('report').innerHTML.includes('مستفيد الاسترداد'));
    assert.ok(element('report').innerHTML.includes('٠٠١500'));assert.equal(vm.runInContext('pendingQueueRequests.size',context),0);
  }
});

test('permanent GET 500 HTML or JSON stops after seven attempts without creating or resubmitting a job',async()=>{
  for(const errorFormat of ['html','json']) {
    const {context,element}=ui();context.setTimeout=callback=>{callback();return 0;};
    context.jobId='b'.repeat(32);context.errorFormat=errorFormat;context.calls=[];
    vm.runInContext(`fetch=async(route,options)=>{calls.push({route,options});return {ok:false,status:500,headers:{get:()=>null},json:async()=>{
      if(errorFormat==='html')throw SyntaxError('HTML500');return {error:'permanent fixture500'};
    }};};`,context);
    await assert.rejects(vm.runInContext('waitForReport(jobId)',context),/تعذر تنفيذ الطلب|permanent fixture500/);
    assert.equal(context.calls.length,7);assert.ok(context.calls.every(call=>call.route==='/api/jobs/'+context.jobId));
    assert.ok(context.calls.every(call=>!call.options.method&&!call.options.body&&call.options.credentials==='same-origin'));
    assert.equal(element('reportPanel').hidden,true);assert.equal(vm.runInContext('currentReport==null',context),true);
    assert.equal(vm.runInContext('pendingQueueRequests.size',context),0);
  }
});

test('adding GET 500 resilience never retries a POST source, search or comparison automatically',async()=>{
  for(const route of ['/api/sources','/api/search','/api/jobs'])for(const errorFormat of ['html','json']) {
    const {context}=ui();context.setTimeout=callback=>{callback();return 0;};
    context.route=route;context.errorFormat=errorFormat;context.calls=[];context.bodyInput={source_id:'c'.repeat(32),target_id:'d'.repeat(20)};
    vm.runInContext(`requestRecovery=false;token='fixture-csrf';fetch=async(route,options)=>{calls.push({route,options});return {ok:false,status:500,json:async()=>{
      if(errorFormat==='html')throw SyntaxError('HTML500');return {error:'POST fixture500'};
    }};};`,context);
    await assert.rejects(vm.runInContext('api(route,bodyInput)',context),/تعذر تنفيذ الطلب|POST fixture500/);
    assert.equal(context.calls.length,1);assert.equal(context.calls[0].route,route);
    assert.equal(context.calls[0].options.method,'POST');assert.equal(context.calls[0].options.credentials,'same-origin');
    assert.equal(context.calls[0].options.headers['X-Equivalency-Token'],'fixture-csrf');
    assert.deepEqual(JSON.parse(context.calls[0].options.body),context.bodyInput);
    assert.equal(vm.runInContext('pendingQueueRequests.size',context),0);
  }
});

test('an accepted POST with lost 500 acknowledgment recovers only through GET and keeps one nonce and one job',async()=>{
  const {context,element}=ui();context.setTimeout=callback=>{callback();return 0;};
  const id='e'.repeat(32);context.jobId=id;context.calls=[];context.jobReads=0;
  context.completedReport={...reportFixture({beneficiary:{name:'مستفيد الطلب الأصلي',number:'DEMO-ONE-POST'}}),id};
  context.bodyInput={source_id:'1'.repeat(32),target_id:'2'.repeat(20),review_mode:'automatic'};
  vm.runInContext(`requestRecovery=true;token='fixture-csrf';fetch=async(route,options)=>{
    calls.push({route,options});
    if(options?.method==='POST')return {ok:false,status:500,json:async()=>{throw SyntaxError('lost accepted acknowledgment');}};
    if(route.startsWith('/api/requests/'))return {ok:true,status:200,json:async()=>({id:jobId,status:'running',kind:'compare'})};
    if(route==='/api/jobs/'+jobId&&++jobReads===1)return {ok:false,status:500,headers:{get:()=>null},json:async()=>{throw SyntaxError('temporary polling HTML500');}};
    if(route==='/api/jobs/'+jobId)return {ok:true,status:200,json:async()=>({id:jobId,status:'done',result:completedReport})};
    throw Error('unexpected route '+route);
  };`,context);
  const accepted=await vm.runInContext('api("/api/jobs",bodyInput)',context);assert.equal(accepted.id,id);
  await vm.runInContext('waitForReport(jobId)',context);
  const posts=context.calls.filter(call=>call.options?.method==='POST');assert.equal(posts.length,1);
  const submitted=JSON.parse(posts[0].options.body);assert.match(submitted.request_id,/^[a-f0-9]{32}$/);
  assert.equal(submitted.source_id,context.bodyInput.source_id);assert.equal(submitted.target_id,context.bodyInput.target_id);
  assert.equal(posts[0].options.headers['X-Equivalency-Token'],'fixture-csrf');
  assert.deepEqual(context.calls.map(call=>call.route),['/api/jobs','/api/requests/'+submitted.request_id,'/api/jobs/'+id,'/api/jobs/'+id]);
  assert.ok(context.calls.slice(1).every(call=>!call.options.method&&!call.options.body&&call.options.credentials==='same-origin'));
  assert.equal(vm.runInContext('currentReport.id',context),id);assert.equal(vm.runInContext('pendingQueueRequests.size',context),0);
  assert.ok(element('report').innerHTML.includes('DEMO-ONE-POST'));
});

test('incomplete evidence audit renders a formal pending report without zero coverage or private engine details',()=>{
  const input=reportFixture({decision:'insufficient',total:13});
  input.id='6'.repeat(32);
  input.calculation={...input.calculation,strict_coverage:null,lenient_coverage:null,
    evidence_review:{stage:'facet_review',target_ids:['t1']},
    reason:'PRIVATE_RAW_VALIDATION_ERROR uncertain_empty_bounds'};
  input.evidence={provider:'audit_incomplete',pairs:[],rubric:'PRIVATE_RUBRIC_ID',
    model:'qwen3.8:27b-q8_0',reviewer_model:'gemma4:e4b',
    evidence_review:{stage:'facet_review',target_ids:['t1']},
    raw_error:'PRIVATE_RAW_VALIDATION_ERROR uncertain_empty_bounds'};
  const before=JSON.stringify(input),{html,element}=renderedReport(input);
  assert.equal(element('reportPanel').hidden,false);
  assert.ok(html.includes('data-decision="insufficient"'));assert.ok(html.includes('تعذر الحسم'));
  assert.ok(html.includes('لعدم كفاية أدلة التوافق بين مفردات المقررين'));
  assert.ok(html.includes('report-stamp pending'));assert.ok(!html.includes('report-stamp approved'));
  assert.ok(html.includes('coverage-pending'));assert.ok(html.includes('لم تُحسب التغطية'));
  for(const mode of ['strict','lenient'])assert.ok(html.includes(`data-mode="${mode}" data-count="" data-total="13" data-coverage=""`));
  assert.ok(!html.includes('coverage-gauge'));assert.ok(!html.includes('coverage-bar-fill'));
  assert.doesNotMatch(html,/(?:^|[^\d٠-٩])(?:0(?:[.,]0+)?|٠(?:[٫,]٠+)?)\s*%/u);
  for(const privateValue of ['audit_incomplete','facet_review','qwen3.8','gemma4','PRIVATE_RAW_VALIDATION_ERROR','uncertain_empty_bounds','PRIVATE_RUBRIC_ID'])assert.ok(!html.includes(privateValue),privateValue+' must remain outside the formal report');
  assert.equal(JSON.stringify(input),before,'rendering must not convert the preserved audit failure into scores or modify its evidence');
});

test('unresolved semantic alternatives cannot appear as a rejection or zero coverage in the report',()=>{
  const input=reportFixture({decision:'insufficient',total:13});
  input.evidence.pairs=[{target_id:'t0',source_id:'s0',score:20,adjudication:{state:'confirmed'},reason:'PRIVATE_UNVERIFIED_ABSENCE_ASSERTION'}];
  input.calculation={...input.calculation,strict_count:0,lenient_count:0,strict_coverage:null,lenient_coverage:null,
    rejection_review:{pending_target_ids:['t1','t6']},reason:'PRIVATE_CANDIDATE_GRAPH_DIAGNOSTIC'};
  const before=JSON.stringify(input),{html}=renderedReport(input);
  assert.ok(html.includes('تعذر الحسم لعدم كفاية أدلة التوافق بين مفردات المقررين.'));
  assert.ok(html.includes('report-stamp pending'));assert.ok(!html.includes('report-stamp approved'));
  assert.ok(html.includes('لم تُحسب التغطية'));assert.ok(!html.includes('coverage-gauge'));
  assert.ok(!html.includes('نطاق المقابلة مثبت'));
  assert.ok(html.includes('لم تثبت أدلة كافية لحسم توافق هذه المفردة.'));
  assert.ok(!html.includes('PRIVATE_UNVERIFIED_ABSENCE_ASSERTION'));
  assert.ok(!html.includes('PRIVATE_CANDIDATE_GRAPH_DIAGNOSTIC'));
  assert.doesNotMatch(html,/(?:^|[^\d٠-٩])(?:0(?:[.,]0+)?|٠(?:[٫,]٠+)?)\s*%/u);
  assert.equal(JSON.stringify(input),before);
});

function curriculumFixture({pending=false,exact=false}={}) {
 const report=reportFixture({decision:pending?'insufficient':'equivalent',total:2,strict:2,lenient:2});
 report.source.topics=[{id:'s0',text:'تعريف العلم ونشأته. أهميته ومصنفاته.',page:6,end_page:7},{id:'s1',text:'أبرز المصنفات ومناهجها.',page:8}];
 report.target.topics=[{id:'t0',text:'تعريف العلم ونشأته.',page:4},{id:'t1',text:'أهمية العلم ومصنفاته.',page:5}];
 const facet=(id,quote)=>({part_id:'p1',support:'aligned',source_quotes:[{source_id:id,quote}],reason:'شاهد موضوعي.'});
 report.evidence={provider:exact?'curriculum_exact':'curriculum_evidence',pairs:report.target.topics.map((t,i)=>exact?
  {target_id:t.id,source_ids:i?['s0','s1']:['s0'],score:100,reason:'تثبت النصوص نطاق مفردة الهدف.'}:
  {target_id:t.id,score:pending?0:90,reason:'PRIVATE_PROPOSAL_REASON',facets:[facet('s0','PRIVATE_PROPOSAL_QUOTE')],adjudication:{state:pending?'uncertain':'confirmed',possible_tiers:['strict','lenient'],review:{reason:pending?'PRIVATE_UNCONFIRMED_ABSENCE':'تغطي الشواهد نطاق المفردة كاملًا.',facets:i?[facet('s0','أهميته ومصنفاته.'),facet('s0','أهميته ومصنفاته.'),facet('s1','أبرز المصنفات ومناهجها.')]:[facet('s0','تعريف العلم ونشأته.')]}}}),model:'PRIVATE_MODEL',prompt:'PRIVATE_PROMPT',cache:'PRIVATE_CACHE'};
 report.calculation.strict_matches=pending?{}:{t0:['s0'],t1:['s0','s1']};report.calculation.lenient_matches=pending?{}:{t0:['s0'],t1:['s0','s1']};
 report.calculation.accounting='original_target_topics';
 return report;
}

test('whole-curriculum report counts each original target once and reuses one source only as distinct evidence',()=>{
 const input=curriculumFixture(),before=JSON.stringify(input),{element}=renderedReport(input),table=element('report').querySelector('.curriculum-details-table');
 const rows=table.querySelectorAll('[data-target-id]');assert.deepEqual(rows.map(r=>r.dataset.targetId),['t0','t1']);assert.equal(table.querySelectorAll('th').length,3);
 assert.ok(rows.every(r=>r.children.length===3));assert.match(rows[0].children[1].textContent,/تعريف العلم ونشأته/);assert.match(rows[1].children[1].textContent,/أهميته ومصنفاته/);
 assert.ok(rows.every(r=>r.children[2].textContent.includes('الدقيق: محتسبة')&&r.children[2].textContent.includes('المتسامح: محتسبة')));
 assert.equal(element('report').querySelectorAll('.coverage-card')[0].dataset.total,'2');assert.equal(JSON.stringify(input),before);
});

test('multiple source witnesses are shown once with page spans using independent reviewed quotations',()=>{
 const {element,html}=renderedReport(curriculumFixture()),row=element('report').querySelectorAll('[data-target-id]')[1],witnesses=row.querySelectorAll('.source-witness');
 assert.equal(witnesses.length,2);assert.equal(row.querySelectorAll('.source-quotation').length,2);
 assert.match(witnesses[0].textContent,/ص 6–7/);assert.match(witnesses[1].textContent,/ص 8/);
 assert.equal((row.textContent.match(/أهميته ومصنفاته\./g)||[]).length,1);assert.match(row.textContent,/تغطي الشواهد نطاق المفردة كاملًا/);
 for(const secret of ['PRIVATE_PROPOSAL_REASON','PRIVATE_PROPOSAL_QUOTE','PRIVATE_MODEL','PRIVATE_PROMPT','PRIVATE_CACHE'])assert.ok(!html.includes(secret));
});

test('curriculum accounting uses saved unique target matches rather than scores or quote counts',()=>{
 const input=curriculumFixture();input.calculation.decision='lenient';input.calculation.strict_matches={t0:['s0']};input.calculation.strict_count=1;input.calculation.strict_coverage=50;
 const {element}=renderedReport(input),row=element('report').querySelectorAll('[data-target-id]')[1];
 assert.match(row.children[2].textContent,/توافق دقيق · 80% فأعلى/);assert.ok(!row.children[2].textContent.includes('90%'));assert.match(row.children[2].textContent,/الدقيق: غير محتسبة/);assert.match(row.children[2].textContent,/المتسامح: محتسبة/);
 assert.equal(element('report').querySelectorAll('.coverage-card')[0].dataset.count,'1');
});

test('pending curriculum evidence stays visible without a zero grade, definitive absence or approval',()=>{
 const input=curriculumFixture({pending:true}),before=JSON.stringify(input),{html,element}=renderedReport(input),rows=element('report').querySelectorAll('[data-target-id]');
 assert.equal(rows.length,2);assert.equal(rows[1].querySelectorAll('.source-witness').length,2);assert.match(rows[1].textContent,/شواهد غير محسومة/);
 assert.ok(rows.every(r=>r.children[2].textContent.includes('غير محسوم')&&r.children[2].textContent.includes('الدقيق: لم يُحسم')));
 assert.match(html,/report-stamp pending/);assert.ok(!html.includes('report-stamp approved'));assert.ok(!html.includes('PRIVATE_UNCONFIRMED_ABSENCE'));assert.doesNotMatch(html,/(?:^|[^\d])0(?:\.0+)?\s*%/u);
 assert.equal(JSON.stringify(input),before);
});

test('exact curriculum reports display every contributing source and omit internal scope proof',()=>{
 const input=curriculumFixture({exact:true});input.evidence.pairs[1].scope_proof={stage:'PRIVATE_SCOPE_PROOF'};
 const {html,element}=renderedReport(input),rows=element('report').querySelectorAll('[data-target-id]');
 assert.equal(rows[0].querySelectorAll('.source-witness').length,1);assert.equal(rows[1].querySelectorAll('.source-witness').length,2);
 assert.match(rows[1].textContent,/أبرز المصنفات ومناهجها/);assert.match(rows[1].textContent,/100%/);assert.ok(!html.includes('PRIVATE_SCOPE_PROOF'));
});

test('curriculum quotations are escaped and deduplicated by source and exact quote rather than text alone',()=>{
 const input=curriculumFixture(),review=input.evidence.pairs[1].adjudication.review;
 const quote='<script>PRIVATE_UNTRUSTED_MARKUP</script>';review.facets=[{source_quotes:[{source_id:'s0',quote},{source_id:'s0',quote},{source_id:'s1',quote}]}];
 const {html,element}=renderedReport(input),row=element('report').querySelectorAll('[data-target-id]')[1];
 assert.equal(row.querySelectorAll('.source-quotation').length,2);assert.ok(!html.includes('<script>PRIVATE_UNTRUSTED_MARKUP'));assert.match(html,/&lt;script&gt;PRIVATE_UNTRUSTED_MARKUP/);
});

test('curriculum HTML and JSON exports preserve all targets and raw private evidence only in JSON',async()=>{
 const input=curriculumFixture();input.evidence.pairs[0].score=95;const {element,context}=renderedReport(input);context.exports=[];
 context.fetch=async route=>route==='/style.css'?{ok:true,text:async()=>fs.readFileSync(new URL('../equivalency-pilot/style.css',import.meta.url),'utf8')}:{ok:true,arrayBuffer:async()=>new Uint8Array([1,2,3]).buffer};
 vm.runInContext('download=(name,text,type)=>exports.push({name,text,type})',context);
 await element('htmlButton').onclick({target:element('htmlButton')});await element('jsonButton').onclick({target:element('jsonButton')});
 const output=JSON.parse(JSON.stringify(context.exports)),html=output.find(o=>o.type==='text/html').text,json=JSON.parse(output.find(o=>o.type==='application/json').text);
 assert.equal((html.match(/data-target-id=/g)||[]).length,2);assert.match(html,/curriculum-details-table/);assert.match(html,/data:font\/ttf;base64,/);assert.ok(!html.includes('PRIVATE_PROMPT'));assert.equal(json.evidence.prompt,'PRIVATE_PROMPT');assert.ok(!html.includes('95%'));assert.match(html,/توافق دقيق · <bdi dir="ltr">80%<\/bdi> فأعلى/);assert.ok(html.includes('تعريف العلم ونشأته.'));assert.ok(html.includes('تغطي الشواهد نطاق المفردة كاملًا.'));assert.equal(json.evidence.pairs[0].score,95);assert.deepEqual(json,JSON.parse(JSON.stringify(input)));
});


test('semantic agreement displays threshold categories while exact scope keeps its calculated percentage',()=>{
 for(const [score,label] of [[59,'دون 60%'],[60,'توافق مع التسامح · 60% فأعلى'],[79,'توافق مع التسامح · 60% فأعلى'],[80,'توافق دقيق · 80% فأعلى'],[95,'توافق دقيق · 80% فأعلى']]){
  const input=curriculumFixture();input.evidence.pairs[0].score=score;const before=JSON.stringify(input),{element}=renderedReport(input);
  assert.equal(element('report').querySelector('.curriculum-verdict').querySelector('.match-state').textContent,label);assert.equal(JSON.stringify(input),before);
 }
 const input=curriculumFixture({exact:true});input.evidence.pairs[0].score=95;
 assert.match(renderedReport(input).element('report').querySelector('.curriculum-verdict').textContent,/95%/);
});

test('categorical curriculum evidence is rendered and exported without inventing a point score',async()=>{
 const input=curriculumFixture();
 for(const pair of input.evidence.pairs){delete pair.score;pair.band='lenient';pair.adjudication.review.verdict='lenient';}
 input.calculation={...input.calculation,decision:'lenient',strict_count:0,strict_coverage:0,strict_matches:{}};
 const before=JSON.stringify(input),{element,context}=renderedReport(input);
 const rows=element('report').querySelectorAll('[data-target-id]');
 assert.ok(rows.every(row=>row.children[2].textContent.includes('توافق مع التسامح · 60% فأعلى')));
 assert.ok(rows.every(row=>!row.children[2].textContent.includes('100%')));
 context.exports=[];vm.runInContext('download=(name,text,type)=>exports.push({name,text,type})',context);
 await element('jsonButton').onclick({target:element('jsonButton')});
 const exported=JSON.parse(context.exports[0].text);
 assert.equal(JSON.stringify(exported),before);
 assert.ok(exported.evidence.pairs.every(pair=>pair.band==='lenient'&&!Object.hasOwn(pair,'score')));
 assert.equal(JSON.stringify(input),before);
});

test('shared lenient threshold remains visible when readers differ about strict agreement',()=>{
 const input=curriculumFixture();
 for(const pair of input.evidence.pairs){delete pair.score;pair.band='strict';pair.adjudication.confirmed_tiers=['lenient','strict'];}
 const pair=input.evidence.pairs[0];pair.adjudication.state='partial';pair.adjudication.confirmed_tiers=['lenient'];
 pair.adjudication.review.verdict='lenient';pair.adjudication.review.reason='صلة معتبرة مع نقص محدد غير حاسم لحد التسامح.';
 input.calculation={...input.calculation,decision:'lenient',strict_count:1,strict_coverage:50,strict_matches:{t1:['s0','s1']}};
 const before=JSON.stringify(input),first=renderedReport(input).element('report').querySelectorAll('[data-target-id]')[0];
 assert.match(first.textContent,/توافق مع التسامح · 60% فأعلى/);assert.match(first.textContent,/الدقيق: غير محتسبة/);
 assert.match(first.textContent,/المتسامح: محتسبة/);assert.match(first.textContent,/نقص محدد/);assert.equal(JSON.stringify(input),before);
 pair.band='lenient';pair.reason='ثبت حد التسامح مع تفصيل إضافي لم يثبت.';
 pair.facets=[{source_quotes:[{source_id:'s0',quote:'تعريف العلم ونشأته.'}]}];pair.adjudication.review.verdict='strict';
 const second=renderedReport(input).element('report').querySelectorAll('[data-target-id]')[0];
 assert.match(second.textContent,/ثبت حد التسامح/);assert.match(second.textContent,/توافق مع التسامح · 60% فأعلى/);
});
