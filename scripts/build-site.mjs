import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const output=path.join(root,'dist');
// A static package only. The Python API/model service is a separate runtime;
// uploads, reports, model traces, and local credentials are never packaged.
await fs.rm(output,{recursive:true,force:true});
await fs.mkdir(output,{recursive:true});
for(const name of ['index.html','app.js','style.css']) await fs.copyFile(path.join(root,'equivalency-pilot',name),path.join(output,name));
const catalog=JSON.parse(await fs.readFile(path.join(root,'equivalency-pilot/catalog.json'),'utf8'));
await fs.copyFile(path.join(root,'equivalency-pilot/catalog.json'),path.join(output,'catalog.json'));
let commit;try{commit=execFileSync('git',['rev-parse','HEAD'],{cwd:root,encoding:'utf8'}).trim()}catch{commit='uncommitted'}
await fs.writeFile(path.join(output,'build-info.json'),JSON.stringify({project:'CourseMatch',commit,catalog_id:catalog.catalog_id,built_at:new Date().toISOString()}));
let copied=5;
for(const name of new Set([...catalog.courses,...catalog.library_sources].map(c=>c.source_pdf).filter(Boolean))) {
  if(!name.startsWith('assets/')||name.includes('..'))throw Error('Unsafe asset path');
  const destination=path.join(output,name);await fs.mkdir(path.dirname(destination),{recursive:true});await fs.copyFile(path.join(root,name),destination);copied++;
}
console.log(`CourseMatch static package: ${copied} files; requires the CourseMatch API at the same origin. Private runtime excluded.`);
