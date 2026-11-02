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
function ui() {
  const documentRoot=new Element('document');parseHTML(documentRoot,fs.readFileSync(new URL('../equivalency-pilot/index.html',import.meta.url),'utf8'));
  const element=id=>documentRoot.querySelector('#'+id);
  const document={getElementById:element,querySelectorAll:selector=>documentRoot.querySelectorAll(selector),createElement:tag=>new Element(tag)};
  const context=vm.createContext({document,console,setTimeout,clearTimeout,URL,Uint8Array,Blob,btoa:globalThis.btoa,crypto:globalThis.crypto,
    history:{replaceState(){}},location:{origin:'http://localhost:8798',search:''},window:{print(){}}});
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
  state.element('reviewed').checked=true;
  await state.element('compareButton').onclick({target:state.element('compareButton')});
  const jobCall=JSON.parse(JSON.stringify(state.context.requests.find(call=>call.route==='/api/jobs')));
  assert.equal(jobCall.body.target_id,choice.course_id);
  assert.equal(jobCall.body.source_id,'incoming-source');
});

test('an input without extracted topics requests completion instead of unrelated retrieval', async () => {
  const state=ui();state.context.requests=[];state.context.emptySource={id:'empty-source',name:'مصدر بلا موضوعات',topics:[],warnings:[]};
  await vm.runInContext('api=async(route,body)=>{requests.push({route,body});throw Error("no retrieval for empty topics");};setSource(emptySource)',state.context);
  assert.equal(state.context.requests.length,0);
  assert.equal(state.element('sourceReview').open,true);
  assert.equal(state.element('candidatePanel').hidden,true);
  assert.ok(state.element('notice').textContent.includes('أكمل مفردات'));
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


test('the public reference is read-only while the submitted course can be corrected',()=>{
 const {context,element,catalog}=ui();context.courseInput=catalog.courses.find(c=>c.topics.length);
 vm.runInContext("referenceEditable=false; editor('sourceTopics',courseInput); setTarget(courseInput.id,true);",context);
 assert.equal(element('targetTopics').querySelector('textarea').readOnly,true);
 assert.equal(element('sourceTopics').querySelector('textarea').readOnly,false);
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
