import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {PostgresRepository} from '../netlify/lib/repository.mjs';

function pool(answer = () => ({rows:[]})) {
  const calls=[];let releases=0;
  const query=async(text,values)=>{calls.push({text,values});return answer(text,values);};
  return {calls,query,connect:async()=>({query,release(){releases++;}}),get releases(){return releases;}};
}
test('Postgres ownership reads parameterize session, scope and expiration',async()=>{
  const p=pool(),r=new PostgresRepository(p);await r.getTask('production','task','owner',1000);await r.getArtifact('preview','artifact','owner',1000,'source');
  for(const call of p.calls) {assert.match(call.text,/session_id=\$3/);assert.match(call.text,/expires_at>\$4/);assert.equal(call.values[2],'owner');assert.ok(call.values[3] instanceof Date);}
  assert.equal(p.calls[0].values[0],'production');assert.equal(p.calls[1].values[0],'preview');
});
test('queue claim uses a worker lock, bounded attempts and atomic SKIP LOCKED selection',async()=>{
  const p=pool(),r=new PostgresRepository(p);assert.equal(await r.claim('preview','worker',1000),null);
  assert.equal(p.calls[0].text,'BEGIN');assert.equal(p.calls.at(-1).text,'COMMIT');assert.equal(p.releases,1);
  const claim=p.calls.find(c=>/SKIP LOCKED/.test(c.text));assert.match(claim.text,/attempts<3/);assert.match(claim.text,/scope=\$1/);assert.equal(claim.values[0],'preview');
  assert.ok(p.calls.find(c=>/INSERT INTO cm_workers/.test(c.text)));assert.ok(p.calls.find(c=>/attempts>=3/.test(c.text)));
});
test('rolling rate check holds the session lock and rejects a sixth comparison transaction',async()=>{
  const p=pool(text=>({rows:/SELECT id FROM cm_sessions/.test(text)?[{id:'session'}]:/count\(\*\)/.test(text)?[{used:5}]:[]}));
  const r=new PostgresRepository(p);await assert.rejects(r.enqueue('production','session','compare',{},null,'id',5_000_000),error=>error.status===429);
  assert.ok(p.calls.find(c=>/cm_sessions.*FOR UPDATE/.test(c.text)));assert.ok(!p.calls.find(c=>/INSERT INTO cm_tasks/.test(c.text)));
  assert.equal(p.calls.at(-1).text,'ROLLBACK');assert.equal(p.releases,1);
});
test('stale completion rolls back and cannot write an artifact',async()=>{
  const p=pool(text=>({rows:/SELECT \*/.test(text)?[{expires_at:new Date(100000),lease_token:'new',status:'running'}]:[]}));
  const r=new PostgresRepository(p);await assert.rejects(r.complete('production','id','old',{},'',1000,()=>({})),error=>error.status===409);
  assert.equal(p.calls.at(-1).text,'ROLLBACK');assert.ok(!p.calls.find(c=>/cm_artifacts/.test(c.text)));assert.equal(p.releases,1);
});
test('migration enforces bounded task attempts, ownership and dedicated PDF garbage collection',async()=>{
  const sql=await readFile(new URL('../reference/managed-database/20261102124000_create_coursematch_queue.sql',import.meta.url),'utf8');
  assert.match(sql,/attempts BETWEEN 0 AND 3/);assert.match(sql,/FOREIGN KEY \(scope, session_id\)/);assert.match(sql,/CREATE TABLE cm_artifacts/);assert.match(sql,/CREATE TABLE cm_pdf_gc/);assert.match(sql,/not_before timestamptz/);
});
test('artifact and completion writes share a transaction and roll back together',async()=>{
  const task={id:'id',session_id:'owner',kind:'source',expires_at:new Date(100000),lease_token:'token',status:'running',lease_expires_at:new Date(100000),pdf_key:'key'};
  const p=pool(text=>{
    if(/SELECT \*/.test(text))return {rows:[task]};
    if(/UPDATE cm_tasks SET status=\$3/.test(text))throw Error('simulated task write failure');
    return {rows:[]};
  });
  const r=new PostgresRepository(p);await assert.rejects(r.complete('production','id','token',{},'',1000,()=>({topics:[]})),/simulated/);
  assert.ok(p.calls.find(c=>/INSERT INTO cm_artifacts/.test(c.text)));assert.equal(p.calls.at(-1).text,'ROLLBACK');assert.equal(p.releases,1);
});
