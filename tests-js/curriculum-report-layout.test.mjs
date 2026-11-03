import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';

// Interface-only fixtures, no academic result or model/API service is being tested.
test('curriculum evidence is readable at 390px, on desktop and in print media, with offline HTML', {skip:process.env.COURSEMATCH_CHROME_QA!=='1'},async()=>{
 const {chromium}=await import('/Users/majd/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs');
 const qa=path.resolve('runtime/service/qa/curriculum-report-layout-20261104');await fs.mkdir(qa,{recursive:true,mode:0o700});
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const report={id:'local-interface-fixture-only',source:{id:'source',name:'المقرر المصدر — اختبار عرض',topics:[{id:'s1',text:'تعريف العلم ونشأته وأهميته ومصنفاته.',page:6,end_page:7},{id:'s2',text:'مناهج المصنفين وتطبيقاتها.',page:8}],hours:2},target:{id:'target',name:'المقرر الهدف — اختبار عرض',topics:[{id:'t1',text:'تعريف العلم ونشأته.',page:4},{id:'t2',text:'أهمية العلم ومصنفاته ومناهج المصنفين.',page:5}],hours:2,context:{program:'برنامج اختبار الواجهة',degree:'بكالوريوس',version:'39'}},calculation:{decision:'equivalent',total_topics:2,strict_count:2,lenient_count:2,strict_coverage:100,lenient_coverage:100,strict_matches:{t1:['s1'],t2:['s1','s2']},lenient_matches:{t1:['s1'],t2:['s1','s2']}},evidence:{provider:'curriculum_evidence',pairs:[]}};
 const facet=(source_id,quote)=>({part_id:'p1',support:'aligned',source_quotes:[{source_id,quote}],reason:'دليل اختبار عرض.'});
 report.evidence.pairs=report.target.topics.map((t,i)=>({target_id:t.id,score:90,reason:'PRIVATE_PROPOSAL_REASON',facets:[],adjudication:{state:'confirmed',review:{reason:'تغطي شواهد المصدر نطاق المفردة كاملًا.',facets:i?[facet('s1','أهميته ومصنفاته.'),facet('s1','أهميته ومصنفاته.'),facet('s2','مناهج المصنفين وتطبيقاتها.')]:[facet('s1','تعريف العلم ونشأته.')]}}}));
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'}),posts=[];
  await page.route('**/*',async route=>{
   const req=route.request(),pathname=new URL(req.url()).pathname;if(req.method()==='POST')posts.push(pathname);
   if(pathname==='/api/catalog')return route.fulfill({contentType:'application/json',body:JSON.stringify({token:'local-fixture-only',catalog:{courses:[],contexts:[]},capabilities:{reference_editable:false}})});
   const file={'/':'index.html','/app.js':'app.js','/image-upload.js':'image-upload.js','/style.css':'style.css','/fonts/IBMPlexSansArabic-Regular.ttf':'fonts/IBMPlexSansArabic-Regular.ttf','/fonts/IBMPlexSansArabic-SemiBold.ttf':'fonts/IBMPlexSansArabic-SemiBold.ttf'}[pathname];
   assert.ok(file,'No external connection allowed: '+pathname);
   return route.fulfill({contentType:pathname.endsWith('.js')?'text/javascript':pathname.endsWith('.css')?'text/css':pathname.endsWith('.ttf')?'font/ttf':'text/html',body:await fs.readFile(new URL('../equivalency-pilot/'+file,import.meta.url))});
  });
  await page.goto('https://coursematch.local/');await page.waitForFunction(()=>typeof catalog!=='undefined'&&catalog);
  await page.evaluate(input=>showReport(input),report);await page.evaluate(()=>document.fonts.ready);
  assert.equal(await page.locator('[data-target-id]').count(),2);assert.equal(await page.locator('.curriculum-details-table thead th').count(),3);assert.equal(await page.locator('[data-target-id="t2"] .source-quotation').count(),2);
  await page.locator('#report').screenshot({path:path.join(qa,'confirmed-1440.png')});
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  const wrap=await page.locator('.report-table-wrap').evaluate(e=>({width:e.clientWidth,scroll:e.scrollWidth}));assert.ok(wrap.scroll<=wrap.width,'Mobile evidence cards fit without horizontal scrolling');
  const cells=await page.locator('[data-target-id="t2"] td').evaluateAll(es=>es.map(e=>({top:e.getBoundingClientRect().top,bottom:e.getBoundingClientRect().bottom,width:e.getBoundingClientRect().width})));assert.ok(cells[1].top>=cells[0].bottom&&cells[2].top>=cells[1].bottom);assert.ok(cells.every(c=>c.width>250));
  await page.locator('.report-table-wrap').evaluate(e=>e.scrollLeft=0);await page.locator('#report').screenshot({path:path.join(qa,'confirmed-390.png')});
  const dl=page.waitForEvent('download');await page.locator('#htmlButton').click();const htmlPath=path.join(qa,'curriculum-interface-fixture.html');await(await dl).saveAs(htmlPath);
  const html=await fs.readFile(htmlPath,'utf8');assert.match(html,/data:font\/ttf;base64,/);assert.ok(!html.includes('PRIVATE_PROPOSAL_REASON'));
  const offline=await browser.newContext({offline:true,viewport:{width:390,height:844}}),exported=await offline.newPage();await exported.goto('file://'+htmlPath);await exported.evaluate(()=>document.fonts.ready);
  assert.equal(await exported.locator('[data-target-id]').count(),2);assert.equal(await exported.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await exported.evaluate(()=>document.fonts.check('12px "IBM Plex Sans Arabic"')),true);await offline.close();
  await page.setViewportSize({width:794,height:1123});await page.emulateMedia({media:'print'});
  const print=await page.locator('.curriculum-details-table').evaluate(e=>({width:e.getBoundingClientRect().width,minWidth:getComputedStyle(e).minWidth,cols:[...e.querySelectorAll('thead th')].map(c=>c.getBoundingClientRect().width)}));
  assert.equal(print.minWidth,'0px');assert.ok(print.cols.every(w=>w>100));assert.ok(print.width<=794);await page.locator('#report').screenshot({path:path.join(qa,'confirmed-print-media.png')});
  report.calculation={...report.calculation,decision:'insufficient',strict_coverage:null,lenient_coverage:null,strict_count:0,lenient_count:0,strict_matches:{},lenient_matches:{}};for(const p of report.evidence.pairs){p.score=0;p.adjudication.state='uncertain';}
  await page.emulateMedia({media:'screen'});await page.setViewportSize({width:390,height:844});await page.evaluate(input=>showReport(input),report);
  assert.equal(await page.locator('.report-stamp.approved').count(),0);assert.equal(await page.locator('.report-stamp.pending').count(),1);assert.equal(await page.locator('.curriculum-verdict').filter({hasText:'0%'}).count(),0);assert.equal(await page.locator('[data-target-id]').count(),2);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.locator('#report').screenshot({path:path.join(qa,'pending-390.png')});assert.deepEqual(posts,[]);
 }finally{await browser.close();for(const name of await fs.readdir(qa))await fs.chmod(path.join(qa,name),0o600)}
});
