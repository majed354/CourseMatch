import http from 'node:http';
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomBytes} from 'node:crypto';
import {createApi} from '../netlify/lib/api.mjs';
import {MAX_BODY_BYTES} from '../netlify/lib/security.mjs';
import {SqliteRepository} from '../netlify/lib/sqlite-repository.mjs';

const ROOT=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
export async function privateDirectory(directory) {
  await fs.mkdir(directory,{recursive:true,mode:0o700});
  const entry=await fs.lstat(directory);
  if (!entry.isDirectory() || entry.isSymbolicLink() || entry.uid!==process.getuid()) throw Error('private_directory_required');
  await fs.chmod(directory,0o700);
}
export class LocalPdfs {
  constructor(directory) { this.directory=directory; }
  filename(key) {
    if(typeof key!=='string'|| !/^[a-f0-9]{20}\/[a-f0-9]{64}\/[a-f0-9]{32}\.pdf$/.test(key)) throw Error('invalid_pdf_key');
    return path.join(this.directory,key.replaceAll('/','_'));
  }
  async put(key,value) {
    const name=this.filename(key), temporary=name+'.'+randomBytes(12).toString('hex')+'.tmp';
    try {
      await fs.writeFile(temporary,value,{mode:0o600,flag:'wx'});
      await fs.link(temporary,name);
    } finally { await fs.unlink(temporary).catch(()=>{}); }
  }
  async get(key) {
    let file;
    try {
      file=await fs.open(this.filename(key),constants.O_RDONLY|constants.O_NOFOLLOW);
      const stat=await file.stat();
      if(!stat.isFile()||stat.uid!==process.getuid()||stat.size>4*1024*1024) throw Error('invalid_private_pdf');
      return await file.readFile();
    } catch(error) { if(error.code==='ENOENT')return null;throw error; }
    finally { await file?.close(); }
  }
  async delete(key) { await fs.unlink(this.filename(key)).catch(error=>{if(error.code!=='ENOENT')throw error;}); }
}
export async function readServiceEnv(filename) {
  const file=await fs.open(filename,constants.O_RDONLY|constants.O_NOFOLLOW);
  try {
    const stat=await file.stat();
    if(!stat.isFile()||stat.uid!==process.getuid()||(stat.mode&0o077))throw Error('private_service_env_required');
    const env={CONTEXT:'production'};
    const allowed=new Set(['COURSEMATCH_PUBLIC_ORIGIN','COURSEMATCH_SESSION_SECRET','COURSEMATCH_WORKER_TOKEN']);
    for(const line of (await file.readFile('utf8')).split(/\r?\n/)) {
      if(!line.trim()||line.trimStart().startsWith('#'))continue;
      const split=line.indexOf('='),key=line.slice(0,split),value=line.slice(split+1);
      if(split<1||!allowed.has(key)||env[key]!==undefined||/[\x00-\x20]/.test(value))throw Error('invalid_service_env');
      env[key]=value;
    }
    const origin=new URL(env.COURSEMATCH_PUBLIC_ORIGIN);
    if(origin.protocol!=='https:'||origin.origin!==env.COURSEMATCH_PUBLIC_ORIGIN)throw Error('invalid_public_origin');
    if(!/^[A-Za-z0-9_-]{32,512}$/.test(env.COURSEMATCH_SESSION_SECRET||'')||!/^[A-Za-z0-9_-]{32,512}$/.test(env.COURSEMATCH_WORKER_TOKEN||''))throw Error('invalid_service_secret');
    return env;
  } finally {await file.close();}
}
export function createLocalServer({api,origin,port=8800,clock=()=>Date.now()}) {
  const publicHost=new URL(origin).host,localHost='127.0.0.1:'+port;
  const buckets=new Map();
  return http.createServer({maxHeaderSize:16*1024,requestTimeout:30_000,headersTimeout:15_000},async(req,res)=>{
    const sendBuffered=(status,headers,body)=>{
      const bytes=Buffer.isBuffer(body)?body:Buffer.from(body);
      const framed=Object.fromEntries(Object.entries(headers).filter(([key])=>!['content-length','transfer-encoding'].includes(key.toLowerCase())));
      res.writeHead(status,{...framed,'Content-Length':bytes.length});
      res.end(bytes);
    };
    const reject=(status)=>sendBuffered(status,{'Content-Type':'application/json','Cache-Control':'no-store','X-Content-Type-Options':'nosniff',...(status===429?{'Retry-After':'30'}:{})},JSON.stringify({error:status===429?'طلبات كثيرة؛ حاول لاحقًا':'الطلب غير مسموح'}));
    try {
      if(![publicHost,localHost].includes(req.headers.host)||!req.url.startsWith('/')||req.url.startsWith('//'))return reject(400);
      const url=new URL(req.url,origin);
      if(url.origin!==origin)return reject(400);
      if(url.pathname==='/healthz' && req.method==='GET') {
        return sendBuffered(200,{'Content-Type':'application/json','Cache-Control':'no-store'},'{"ok":true,"service":"CourseMatch"}');
      }
      if(!url.pathname.startsWith('/api/')&&!/^\/pdf\/[a-f0-9]{20,32}$/.test(url.pathname))return reject(404);
      const worker=url.pathname.startsWith('/api/worker/');
      if(worker&&(req.headers.host!==localHost||req.headers.origin))return reject(403);
      // Forwarded addresses are not trusted as identities. Limit the total
      // public entry point as well as each signed session in the repository.
      if(!worker) {
        const now=clock(),key=req.method==='GET'&&url.pathname!=='/api/catalog'?'reads':'writes',maximum=key==='reads'?1200:120,bucket=buckets.get(key);
        if(!bucket||bucket.until<=now)buckets.set(key,{until:now+60_000,used:1});
        else if(++bucket.used>maximum)return reject(429);
      }
      const length=Number(req.headers['content-length']||0);
      if(!Number.isFinite(length)||length>MAX_BODY_BYTES)return reject(413);
      const chunks=[];let size=0;
      for await(const chunk of req) {size+=chunk.length;if(size>MAX_BODY_BYTES)return reject(413);chunks.push(chunk);}
      const headers=new Headers();
      for(const [key,value] of Object.entries(req.headers))if(value!==undefined&&!['host','connection','transfer-encoding','content-length'].includes(key))headers.set(key,Array.isArray(value)?value.join(', '):value);
      const response=await api(new Request(url,{method:req.method,headers,...(chunks.length?{body:Buffer.concat(chunks)}:{})}));
      // Finish reading the Response before committing headers. Explicit byte
      // lengths avoid an inherited/chunked frame and keep body errors recoverable.
      const bytes=Buffer.from(await response.arrayBuffer());
      sendBuffered(response.status,Object.fromEntries(response.headers),bytes);
    } catch(error) {console.error('local_service_request_failed',error?.name||'Error');if(!res.headersSent)reject(503);else res.end();}
  });
}
async function main() {
  process.umask(0o077);
  const runtime=path.join(ROOT,'runtime/service');await privateDirectory(runtime);await privateDirectory(path.join(runtime,'pdfs'));
  const env=await readServiceEnv(path.join(ROOT,'.env.service'));
  const catalog=JSON.parse(await fs.readFile(path.join(ROOT,'equivalency-pilot/catalog.json'),'utf8'));
  const repository=new SqliteRepository(path.join(runtime,'queue.sqlite'));
  const api=createApi({repository,pdfs:new LocalPdfs(path.join(runtime,'pdfs')),catalog,env});
  const server=createLocalServer({api,origin:env.COURSEMATCH_PUBLIC_ORIGIN});
  server.listen(8800,'127.0.0.1',()=>console.log('coursematch_service_listening port=8800'));
  const stop=()=>{server.close(()=>{repository.close();process.exit(0);});setTimeout(()=>process.exit(1),5000).unref();};
  process.on('SIGTERM',stop);process.on('SIGINT',stop);
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(error=>{console.error('local_service_stopped',error?.name||'Error');process.exitCode=1;});
