import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {LocalPdfs,privateDirectory,readServiceEnv,createLocalServer} from '../scripts/local-service.mjs';

test('private PDF store rejects paths and symlinks, preserves uploads without overwriting',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'cm-pdfs-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
  await privateDirectory(root);assert.equal((await fs.stat(root)).mode&0o777,0o700);
  const pdfs=new LocalPdfs(root),key='a'.repeat(20)+'/'+'b'.repeat(64)+'/'+'c'.repeat(32)+'.pdf';
  assert.throws(()=>pdfs.filename('../outside'));assert.throws(()=>pdfs.filename('/etc/passwd'));
  await pdfs.put(key,Buffer.from('%PDF-1.7'));
  assert.equal((await pdfs.get(key)).toString(),'%PDF-1.7');
  assert.equal((await fs.stat(pdfs.filename(key))).mode&0o777,0o600);
  await assert.rejects(pdfs.put(key,Buffer.from('overwrite')),{code:'EEXIST'});
  await pdfs.delete(key);assert.equal(await pdfs.get(key),null);
  const outside=path.join(root,'outside');await fs.writeFile(outside,'secret');await fs.symlink(outside,pdfs.filename(key));
  await assert.rejects(pdfs.get(key),{code:'ELOOP'});await pdfs.delete(key);assert.equal(await fs.readFile(outside,'utf8'),'secret');
});
test('service configuration must be owner-only, literal, complete and HTTPS',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'cm-env-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const filename=path.join(root,'.env.service');
  const value='COURSEMATCH_PUBLIC_ORIGIN=https://example.net\nCOURSEMATCH_SESSION_SECRET='+ 's'.repeat(40)+'\nCOURSEMATCH_WORKER_TOKEN='+'w'.repeat(40)+'\n';
  await fs.writeFile(filename,value,{mode:0o600});assert.equal((await readServiceEnv(filename)).CONTEXT,'production');
  await fs.chmod(filename,0o644);await assert.rejects(readServiceEnv(filename),/private_service_env_required/);await fs.chmod(filename,0o600);
  for(const bad of [value.replace('https:','http:'),value+'UNKNOWN=anything\n',value+'COURSEMATCH_SESSION_SECRET=duplicate\n',value.replace('https://example.net','https://example.net/private')]) {
    await fs.writeFile(filename,bad);await assert.rejects(readServiceEnv(filename));
  }
});
test('HTTP adapter fixes the public origin and blocks worker access through the public Host',async t=>{
  const requests=[];const server=createLocalServer({origin:'https://example.net',api:async request=>{requests.push(request);return Response.json({ok:true},{headers:{'Set-Cookie':'signed=fixture; Secure; HttpOnly'}});}});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));t.after(()=>new Promise(resolve=>server.close(resolve)));
  const port=server.address().port;
  const call=(route,headers={},method='GET',body)=>new Promise((resolve,reject)=>{
    const req=http.request({hostname:'127.0.0.1',port,path:route,method,headers:{Host:'example.net',...headers}},res=>{const chunks=[];res.on('data',c=>chunks.push(c));res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body:Buffer.concat(chunks).toString()}));});
    req.on('error',reject);req.end(body);
  });
  assert.equal((await call('/healthz')).status,200);
  assert.equal((await call('/api/catalog',{Host:'evil.net'})).status,400);
  assert.equal((await call('//evil.net/api/catalog')).status,400);
  assert.equal((await call('/api/worker/claim')).status,403);
  assert.equal((await call('/api/worker/claim',{Host:'127.0.0.1:8800',Origin:'https://example.net'})).status,403);
  const worker=await call('/api/worker/heartbeat',{Host:'127.0.0.1:8800','Content-Type':'application/json'},'POST','{}');assert.equal(worker.status,200);
  const result=await call('/api/catalog',{'X-Forwarded-Host':'evil.net','X-Forwarded-Proto':'http'});assert.equal(result.status,200);
  assert.match(result.headers['set-cookie'][0],/Secure; HttpOnly/);
  assert.equal(requests.at(-1).url,'https://example.net/api/catalog');
  assert.equal((await call('/runtime/service/queue.sqlite')).status,404);
  assert.equal((await call('/api/sources',{'Content-Length':'6000001'},'POST')).status,413);
});
test('public entry point has a bounded aggregate request rate while the local worker remains available',async t=>{
  const server=createLocalServer({origin:'https://example.net',clock:()=>1000,api:async()=>Response.json({ok:true})});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));t.after(()=>new Promise(resolve=>server.close(resolve)));
  const call=(route,host='example.net',method='GET')=>new Promise((resolve,reject)=>{
    const request=http.request({hostname:'127.0.0.1',port:server.address().port,path:route,method,headers:{Host:host}},response=>{response.resume();response.on('end',()=>resolve(response.statusCode));});request.on('error',reject);request.end();
  });
  for(let i=0;i<120;i++)assert.equal(await call('/api/sources','example.net','POST'),200);
  assert.equal(await call('/api/sources','example.net','POST'),429);
  assert.equal(await call('/api/catalog'),429);
  // Exhausting submissions must not interrupt existing beneficiaries polling.
  for(let i=0;i<1200;i++)assert.equal(await call('/api/jobs/'+'a'.repeat(32)),200);
  assert.equal(await call('/api/catalog'),429);
  assert.equal(await call('/api/worker/claim','127.0.0.1:8800'),200);
});
