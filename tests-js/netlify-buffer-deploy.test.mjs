import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import {pathToFileURL} from 'node:url';
import {Readable} from 'node:stream';
import {createHash} from 'node:crypto';
import {DEFAULT_CLI_ROOT,MAX_UPLOAD_BYTES,pinnedCliRoot,bufferFileUploads,attachBufferUploadHook,uploadBuffer,runOfficialDeploy} from '../scripts/netlify-buffer-deploy.mjs';
const hasPinned=fs.existsSync(path.join(DEFAULT_CLI_ROOT,'package.json'));
const digest=value=>createHash('sha256').update(value).digest('hex');

test('pinned root fails closed for foreign package or version',async()=>{
 const tmp=await fsp.mkdtemp(path.join(os.tmpdir(),'coursematch-cli-pin-'));
 try {for(const pkg of [{name:'foreign',version:'27.16.1'},{name:'netlify-cli',version:'27.17.0'}]){await fsp.writeFile(path.join(tmp,'package.json'),JSON.stringify(pkg));await assert.rejects(pinnedCliRoot(tmp),/version_mismatch/);}}
 finally{await fsp.rm(tmp,{recursive:true,force:true})}
 await assert.rejects(runOfficialDeploy(['node','wrapper','api']),/requires_deploy/);
});

test('buffering preserves strings, binary bytes, empty files and stream errors',async()=>{
 for(const value of ['', 'عربي',Buffer.from([0,255,1]),new Uint8Array([3,4])])assert.deepEqual(await uploadBuffer(()=>value),Buffer.from(value));
 assert.deepEqual(await uploadBuffer(()=>Readable.from([Buffer.from([0,255]),Buffer.from([1])])),Buffer.from([0,255,1]));
 await assert.rejects(uploadBuffer({bad:true}),/unsupported/);
 await assert.rejects(uploadBuffer(Readable.from((async function*(){yield Buffer.from('partial');throw Error('read failed')})())),/read failed/);
});

test('32 MiB bound accepts the maximum and rejects oversized files before sending',async()=>{
 assert.equal((await uploadBuffer(Buffer.alloc(MAX_UPLOAD_BYTES))).length,MAX_UPLOAD_BYTES);
 await assert.rejects(uploadBuffer(Buffer.alloc(MAX_UPLOAD_BYTES+1)),/exceeds_32mb/);
 const input=Readable.from([Buffer.alloc(MAX_UPLOAD_BYTES),Buffer.from('x')]);
 await assert.rejects(uploadBuffer(input),/exceeds_32mb/);assert.equal(input.destroyed,true);
});

test('API hook changes only file uploads, retains options and replayable body',async()=>{
 let captured;const fn=()=>{};const api={uploadDeployFile:async(params,options)=>{captured={params,options};return 'ok'},uploadDeployFunction:fn};
 bufferFileUploads(api);const wrapped=api.uploadDeployFile;bufferFileUploads(api);assert.equal(api.uploadDeployFile,wrapped);
 const options={signal:'sentinel'};assert.equal(await api.uploadDeployFile({body:()=>Readable.from(['bytes']),deployId:'same',path:'_headers'},options),'ok');
 assert.equal(captured.params.path,'_headers');assert.equal(captured.params.deployId,'same');assert.equal(captured.options,options);assert.deepEqual(captured.params.body(),Buffer.from('bytes'));assert.equal(api.uploadDeployFunction,fn);
 assert.throws(()=>attachBufferUploadHook({commands:[]}),/hook_unavailable/);
});

test('official Commander preAction init precedes our instance hook',{skip:!hasPinned},async()=>{
 const root=await pinnedCliRoot();const {createMainCommand}=await import(pathToFileURL(path.join(root,'dist/commands/main.js')).href);
 const program=createMainCommand();const api={uploadDeployFile:async()=>{}};const original=api.uploadDeployFile;let called=false;
 // Replace only fixture initialization/action: the actual registered CLI
 // preAction hooks and Commander execution order are exercised without network.
 program.init=async command=>{command.netlify={api}};
 const deploy=program.commands.find(c=>c.name()==='deploy');deploy.action(()=>{assert.notEqual(api.uploadDeployFile,original);called=true});
 attachBufferUploadHook(program);await program.parseAsync(['node','fixture','deploy','--no-build']);assert.equal(called,true);
});

test('official uploadFiles and API send identical files with Content-Length and unchanged 404/redirect/header bytes',{skip:!hasPinned},async()=>{
 const cli=await pinnedCliRoot();const moduleFiles=['dist/utils/deploy/upload-files.js','dist/commands/deploy/deploy.js','dist/utils/deploy/deploy-site.js'];
 const before=await Promise.all(moduleFiles.map(async name=>digest(await fsp.readFile(path.join(cli,name)))));
 const {default:uploadFiles}=await import(pathToFileURL(path.join(cli,moduleFiles[0])).href);
 const {NetlifyAPI}=await import(pathToFileURL(path.join(cli,'../@netlify/api/lib/index.js')).href);
 const received=[];const server=http.createServer(async(req,res)=>{const chunks=[];for await(const chunk of req)chunks.push(chunk);received.push({url:req.url,headers:req.headers,body:Buffer.concat(chunks)});res.setHeader('content-type','application/json');if(req.url.endsWith('/missing.txt')){res.statusCode=404;res.end('{"message":"fixture missing"}')}else res.end('{"ok":true}')});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const tmp=await fsp.mkdtemp(path.join(os.tmpdir(),'coursematch-buffer-upload-'));
 try {
  const api=bufferFileUploads(new NetlifyAPI('fixture-only',{scheme:'http',host:'127.0.0.1:'+server.address().port,pathPrefix:'/api/v1',userAgent:'buffer-fixture'}));
  const files=new Map([['catalog.json',Buffer.from(JSON.stringify({text:'محتوى عربي',padding:'x'.repeat(5_500_000)}))],['404.html',Buffer.from('<!doctype html><p>الصفحة غير موجودة</p>')],['_redirects',Buffer.from('/api/worker/* /404.html 404!\n/api/* https://fixture.trycloudflare.com/api/:splat 200\n')],['_headers',Buffer.from('/*\n  X-Content-Type-Options: nosniff\n')],['empty.txt',Buffer.alloc(0)],['binary.pdf',Buffer.from([37,80,68,70,0,255])]]);
  const list=[];for(const [name,bytes] of files){const filepath=path.join(tmp,name);await fsp.writeFile(filepath,bytes);list.push({assetType:'file',normalizedPath:name,filepath})}
  await uploadFiles(api,'fixture-deploy',list,{concurrentUpload:2,maxRetry:1,statusCb(){}});
  for(const request of received){const name=decodeURI(request.url.split('/files/')[1]);assert.deepEqual(request.body,files.get(name));assert.equal(Number(request.headers['content-length']),request.body.length);assert.equal(request.headers['transfer-encoding'],undefined);assert.equal(request.headers.authorization,'Bearer fixture-only');assert.equal(request.headers['content-type'],'application/octet-stream')}
  assert.equal(received.length,files.size);
  await assert.rejects(api.uploadDeployFile({body:()=>Buffer.from('same error body'),deployId:'fixture-deploy',path:'missing.txt'}),error=>error.status===404);
  assert.equal(received.at(-1).headers['content-length'],String(Buffer.byteLength('same error body')));
  assert.deepEqual(await Promise.all(moduleFiles.map(async name=>digest(await fsp.readFile(path.join(cli,name))))),before);
 }finally{await new Promise(resolve=>server.close(resolve));await fsp.rm(tmp,{recursive:true,force:true})}
});
