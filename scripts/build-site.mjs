import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {projectPublicCatalog} from './public_catalog.mjs';
import {validatedActiveRoutes} from './service_routes.mjs';
const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const output=path.join(root,'dist');
// A static package only. The Python API/model service is a separate runtime;
// uploads, reports, model traces, and local credentials are never packaged.
await fs.rm(output,{recursive:true,force:true});
await fs.mkdir(output,{recursive:true});
for(const name of ['index.html','image-upload.js','app.js','style.css']) await fs.copyFile(path.join(root,'equivalency-pilot',name),path.join(output,name));
const fontAssets=['IBMPlexSansArabic-Regular.ttf','IBMPlexSansArabic-SemiBold.ttf','LICENSE.txt'];
await fs.mkdir(path.join(output,'fonts'),{recursive:true});
for(const name of fontAssets)await fs.copyFile(path.join(root,'equivalency-pilot/fonts',name),path.join(output,'fonts',name));
const catalog=JSON.parse(await fs.readFile(path.join(root,'equivalency-pilot/catalog.json'),'utf8'));
const publicCatalog=projectPublicCatalog(catalog);
await fs.writeFile(path.join(output,'catalog.json'),JSON.stringify(publicCatalog));
let commit;try{commit=execFileSync('git',['rev-parse','HEAD'],{cwd:root,encoding:'utf8'}).trim()}catch{commit='uncommitted'}
await fs.writeFile(path.join(output,'build-info.json'),JSON.stringify({project:'CourseMatch',commit,catalog_id:catalog.catalog_id,built_at:new Date().toISOString()}));
await fs.writeFile(path.join(output,'404.html'),'<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><title>CourseMatch</title><p>الصفحة غير موجودة.</p></html>');
// A live HTTPS tunnel has a generated or explicitly configured address. Preserve only the validated
// supervisor-generated public routes when rebuilding static assets.
try {
  const routes=await fs.readFile(path.join(root,'runtime/service/active-redirects'),'utf8');
  let namedHostname;
  try { namedHostname=JSON.parse(await fs.readFile(path.join(root,'runtime/service/config.json'),'utf8')).named_tunnel_hostname; }
  catch(error) { if(error.code!=='ENOENT')throw error; }
  await fs.writeFile(path.join(output,'_redirects'),validatedActiveRoutes(routes,namedHostname));
} catch(error) {if(error.code!=='ENOENT')throw error;}
let copied=7+fontAssets.length;
for(const name of new Set([...catalog.courses,...catalog.library_sources].map(c=>c.source_pdf).filter(Boolean))) {
  if(!name.startsWith('assets/')||name.includes('..'))throw Error('Unsafe asset path');
  const destination=path.join(output,name);await fs.mkdir(path.dirname(destination),{recursive:true});await fs.copyFile(path.join(root,name),destination);copied++;
}
console.log(`CourseMatch static package: ${copied} files; requires the CourseMatch API at the same origin. Private runtime excluded.`);
