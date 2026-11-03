import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs/promises';import os from 'node:os';import path from 'node:path';import{execFileSync}from'node:child_process';import{fileURLToPath}from'node:url';
// Synthetic pagination boundary; no private academic input, service or model.
test('curriculum print spacing keeps fourteen original rows in three pages with unchanged fonts and screen layout',{skip:process.env.COURSEMATCH_CHROME_QA!=='1'},async()=>{
 const{chromium}=await import('/Users/majd/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs');
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'coursematch-curriculum-density-'));
 let css=await fs.readFile(new URL('../equivalency-pilot/style.css',import.meta.url),'utf8');
 for(const name of['IBMPlexSansArabic-Regular.ttf','IBMPlexSansArabic-SemiBold.ttf'])css=css.replaceAll('/fonts/'+name,'data:font/ttf;base64,'+(await fs.readFile(new URL('../equivalency-pilot/fonts/'+name,import.meta.url))).toString('base64'));
 const previous=css.replace(/\/\* Compact curriculum evidence in print without changing type sizes or content\. \*\/[\s\S]*?\/\* End compact curriculum print evidence\. \*\//,'');assert.notEqual(previous,css);
 const rows=Array.from({length:14},(_,i)=>`<tr data-target-id="t${i+1}"><td class="target-topic"><span class="topic-order">${i+1}</span><p>ROW-${String(i+1).padStart(2,'0')}<br>Original target topic</p><small>Target page 5</small></td><td class="curriculum-witnesses"><div class="source-witness"><small>Source page 6</small><p class="source-quotation">Literal source witness<br>Preserved exact content<br>Complete original excerpt</p></div><div class="source-witness"><small>Source page 7</small><p class="source-quotation">Another literal witness</p></div></td><td class="matching-cell curriculum-verdict"><span class="match-state strict">Strict threshold</span><p class="match-reason">Whole-target rationale<br>Original evidence retained</p><div class="curriculum-accounting"><span class="curriculum-account strict">Strict counted</span><span class="curriculum-account lenient">Lenient counted</span></div></td></tr>`).join('');
 const html=style=>`<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><style>${style}</style><main><section class="panel" id="reportPanel"><article id="report"><div class="equivalency-report"><header class="report-head" style="height:20mm">SYNTHETIC-REPORT-ID</header><section class="report-block evidence-block"><div class="report-section-heading"><h2>Whole-target evidence</h2></div><div class="report-table-wrap"><table class="details-table curriculum-details-table"><thead><tr><th>Original target</th><th>Source witnesses</th><th>Agreement</th></tr></thead><tbody>${rows}</tbody></table></div></section><div class="report-end">CourseMatch SYNTHETIC-REPORT-ID</div></div></article></section></main></html>`;
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:390,height:844},offline:true}),results=[];
  for(const[version,style]of[['before',previous],['after',css]]){
   await page.emulateMedia({media:'screen'});await page.setContent(html(style));await page.evaluate(()=>document.fonts.ready);
   const screen=await page.evaluate(()=>({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,rows:[...document.querySelectorAll('[data-target-id]')].map(e=>({id:e.dataset.targetId,height:e.getBoundingClientRect().height,text:e.textContent}))}));
   await page.emulateMedia({media:'print'});const font=await page.locator('.source-quotation').first().evaluate(e=>({size:getComputedStyle(e).fontSize,lineHeight:getComputedStyle(e).lineHeight}));
   const file=path.join(directory,version+'.pdf');await page.pdf({path:file,format:'A4',preferCSSPageSize:true,printBackground:true});results.push({screen,font,file});
  }
  const analysis=JSON.parse(execFileSync(fileURLToPath(new URL('../.venv-host/bin/python',import.meta.url)),['-c',`import pymupdf,json,sys
out=[]
for file in sys.argv[1:]:
 d=pymupdf.open(file);pages=[]
 for page in d:
  words=page.get_text('words');body=[w for w in words if w[1]<800];footer=[w for w in words if w[1]>=800]
  pages.append({'text':page.get_text(),'body_bottom':max((w[3]for w in body),default=0),'body_words':len(body),'brand_count':sum('CourseMatch'in w[4]for w in footer),'footer_inside':all(w[3]<page.rect.height and w[2]<page.rect.width for w in footer)})
 out.append({'page_count':len(d),'pages':pages})
print(json.dumps(out))`,...results.map(r=>r.file)],{encoding:'utf8'}));
  assert.equal(analysis[0].page_count,4);assert.equal(analysis[1].page_count,3);assert.deepEqual(results[0].screen,results[1].screen);assert.deepEqual(results[0].font,results[1].font);assert.ok(parseFloat(results[1].font.size)>=9);
  const text=analysis[1].pages.map(p=>p.text).join('\n');assert.match(text,/SYNTHETIC-REPORT-ID/);for(let i=1;i<=14;i++)assert.equal((text.match(new RegExp('ROW-'+String(i).padStart(2,'0'),'g'))||[]).length,1);
  for(const [i,p]of analysis[1].pages.entries()){assert.ok(p.body_words>=(i===0?1:14));assert.equal(p.brand_count,1);assert.ok(p.body_bottom<800);assert.equal(p.footer_inside,true);}
 }finally{await browser.close();await fs.rm(directory,{recursive:true,force:true})}
});
