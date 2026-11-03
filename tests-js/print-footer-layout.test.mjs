import test from'node:test';import assert from'node:assert/strict';import fs from'node:fs/promises';import os from'node:os';import path from'node:path';import{execFileSync}from'node:child_process';import{fileURLToPath}from'node:url';
// Synthetic pagination boundary independent of private academic inputs; no API/model calls.
test('printing retains all thirteen rows without a footer-only page and preserves screen layout',{skip:process.env.COURSEMATCH_CHROME_QA!=='1'},async()=>{
 const{chromium}=await import('/Users/majd/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs');const directory=await fs.mkdtemp(path.join(os.tmpdir(),'coursematch-footer-'));
 let css=await fs.readFile(new URL('../equivalency-pilot/style.css',import.meta.url),'utf8');for(const f of['IBMPlexSansArabic-Regular.ttf','IBMPlexSansArabic-SemiBold.ttf'])css=css.replaceAll('/fonts/'+f,'data:font/ttf;base64,'+(await fs.readFile(new URL('../equivalency-pilot/fonts/'+f,import.meta.url))).toString('base64'));
 const previous=css.replace(/\/\* Page-margin branding avoids an orphan footer;[\s\S]*?\/\* End reserved print footer\. \*\//,'');assert.notEqual(previous,css);
 const html=style=>'<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><style>'+style+'</style><main><section id="reportPanel" class="panel"><article id="report"><div class="equivalency-report"><p style="height:4mm;margin:0;font-size:8px">SYNTHETIC-REPORT-ID</p><table class="details-table" style="height:260mm;margin:0"><tbody>'+Array.from({length:13},(_,i)=>'<tr><td>ROW-'+String(i+1).padStart(2,'0')+' substantive content for synthetic layout verification</td></tr>').join('')+'</tbody></table><div class="report-end"><span>CourseMatch · منصة المعادلات</span><bdi>SYNTHETIC-REPORT-ID</bdi></div></div></article></section></main></html>';
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});try{
  const page=await browser.newPage({viewport:{width:390,height:844},offline:true});const results=[];
  for(const[version,style]of[['before',previous],['after',css]]){await page.emulateMedia({media:'screen'});await page.setContent(html(style));await page.evaluate(()=>document.fonts.ready);const screen=await page.evaluate(()=>({height:document.documentElement.scrollHeight,width:document.documentElement.scrollWidth,footer:document.querySelector('.report-end').getBoundingClientRect().toJSON()}));await page.emulateMedia({media:'print'});const file=path.join(directory,version+'.pdf');await page.pdf({path:file,format:'A4',preferCSSPageSize:true,printBackground:true});results.push({version,screen,file});}
  const analysis=JSON.parse(execFileSync(fileURLToPath(new URL('../.venv-host/bin/python',import.meta.url)),['-c',`import pymupdf,json,sys;out=[]
for p in sys.argv[1:]:
 d=pymupdf.open(p);pages=[]
 for page in d:
  words=page.get_text('words');footer=[w for w in words if w[1]>800];body=[w for w in words if w[1]<800];brands=[w for w in footer if 'CourseMatch' in w[4]];counters=[w for w in footer if 'CourseMatch' not in w[4] and w[0]>200];pages.append({'text':page.get_text(),'body_words':len(body),'body_bottom':max((w[3]for w in body),default=0),'footer_brand_count':len(brands),'footer_inside_page':all(0<=w[0]<w[2]<=page.rect.width and 0<=w[1]<w[3]<=page.rect.height for w in footer),'footer_counter_gap':min((w[0]for w in counters),default=999)-max((w[2]for w in footer if w[2]<200),default=0)})
 out.append({'page_count':len(d),'pages':pages})
print(json.dumps(out))`,...results.map(x=>x.file)],{encoding:'utf8'}));
  assert.equal(analysis[0].page_count,2);assert.equal(analysis[1].page_count,1);assert.deepEqual(results[0].screen,results[1].screen);assert.match(analysis[1].pages[0].text,/SYNTHETIC-REPORT-ID/);
  const text=analysis[1].pages.map(x=>x.text).join('\n');for(let n=1;n<=13;n++)assert.equal((text.match(new RegExp('ROW-'+String(n).padStart(2,'0'),'g'))||[]).length,1);
  for(const p of analysis[1].pages){assert.ok(p.body_words>13);assert.equal(p.footer_brand_count,1);assert.equal(p.footer_inside_page,true);assert.ok(p.body_bottom<800);assert.ok(p.footer_counter_gap>30);}
 }finally{await browser.close();await fs.rm(directory,{recursive:true,force:true})}
});
