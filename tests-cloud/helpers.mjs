import {newLease, HttpError} from '../netlify/lib/security.mjs';

export class MemoryRepository {
  sessions = new Map(); tasks = new Map(); artifacts = new Map(); workers = new Map(); garbage = new Map();
  key(scope,id) { return scope + ':' + id; }
  async ensureSession(scope,session) { this.sessions.set(this.key(scope,session.id),session); }
  async workerOnline(scope,now) { return [...this.workers.values()].some(w => w.scope === scope && w.last_seen > now - 120_000); }
  async workerHeartbeat(scope,id,now) { this.workers.set(this.key(scope,id),{scope,id,last_seen:now}); }
  async enqueue(scope,session_id,kind,payload,pdf_key,id,now) {
    if (!this.sessions.has(this.key(scope,session_id))) throw new HttpError(401,'expired');
    const used = [...this.tasks.values()].filter(t => t.scope === scope && t.session_id === session_id && t.kind === kind && t.created_at > now - 3_600_000).length;
    if (used >= {source:20,search:120,compare:5}[kind]) throw new HttpError(429,'rate');
    const task = {scope,id,session_id,kind,payload:structuredClone(payload),pdf_key,status:'queued',attempts:0,created_at:now,expires_at:now + 86_400_000};
    this.tasks.set(this.key(scope,id),task); return task;
  }
  async getTask(scope,id,session,now) {
    const t = this.tasks.get(this.key(scope,id)); return t?.session_id === session && t.expires_at > now ? t : null;
  }
  async getArtifact(scope,id,session,now,kind) {
    const a = this.artifacts.get(this.key(scope,id)); return a?.session_id === session && a.kind === kind && a.expires_at > now ? a : null;
  }
  async updateBeneficiary(scope,id,session,beneficiary,now) {
    const a = await this.getArtifact(scope,id,session,now,'report'); if (!a) throw new HttpError(404,'missing');
    a.value = {...a.value,beneficiary}; this.tasks.get(this.key(scope,id)).result = a.value; return a.value;
  }
  async reservePdf(scope,key,now) { this.garbage.set(this.key(scope,key),{scope,key,not_before:now + 86_400_000}); }
  async cleanup(scope,now) {
    for (const map of [this.tasks,this.artifacts]) for (const [key,value] of map) if (value.scope === scope && value.expires_at <= now) {
      if (value.pdf_key) this.garbage.set(this.key(scope,value.pdf_key),{scope,key:value.pdf_key,not_before:now});
      map.delete(key);
    }
    return [...this.garbage.values()].filter(g => g.scope === scope && g.not_before <= now).map(g => g.key);
  }
  async ackPdfDeletion(scope,key) { this.garbage.delete(this.key(scope,key)); }
  async claim(scope,worker,now,kinds) {
    await this.workerHeartbeat(scope,worker,now);
    const current = [...this.tasks.values()].find(t => t.scope === scope && t.worker_id === worker && t.status === 'running' && t.lease_expires_at > now && t.expires_at > now);
    if (current) return current;
    for (const t of this.tasks.values()) if (t.scope === scope && t.status === 'running' && t.lease_expires_at <= now && t.attempts >= 3) {t.status='error';t.lease_token=null;}
    const t = [...this.tasks.values()].find(t => t.scope === scope && t.expires_at > now && t.attempts < 3 && (t.status === 'queued' || t.status === 'running' && t.lease_expires_at <= now) && (!kinds || kinds.includes(t.kind)));
    if (!t) return null;
    Object.assign(t,{status:'running',worker_id:worker,lease_token:newLease(),lease_expires_at:now + 900_000,attempts:t.attempts+1});return t;
  }
  async leasedTask(scope,id,token,now) {
    const t = this.tasks.get(this.key(scope,id)); return t?.status === 'running' && t.lease_token === token && t.lease_expires_at > now && t.expires_at > now ? t : null;
  }
  async heartbeat(scope,id,token,now) {
    const t = await this.leasedTask(scope,id,token,now);if (!t) throw new HttpError(409,'stale');t.lease_expires_at=Math.min(now+900_000,t.expires_at);await this.workerHeartbeat(scope,t.worker_id,now);return t;
  }
  async complete(scope,id,token,result,error,now,prepare) {
    const t = this.tasks.get(this.key(scope,id));if (!t || t.expires_at <= now) throw new HttpError(404,'expired');
    if (t.lease_token !== token) throw new HttpError(409,'stale');
    if (['done','error'].includes(t.status)) return {id,status:t.status,idempotent:true};
    if (t.status !== 'running' || t.lease_expires_at <= now) throw new HttpError(409,'stale');
    const ready = error ? null : prepare(t,structuredClone(result));
    t.status=error ? 'error' : 'done';t.result=ready;t.error=error || null;t.lease_expires_at=null;
    if (ready && ['source','compare'].includes(t.kind)) this.artifacts.set(this.key(scope,id),{scope,id,session_id:t.session_id,kind:t.kind === 'source' ? 'source' : 'report',value:ready,pdf_key:t.kind === 'source' ? t.pdf_key : null,expires_at:t.expires_at});
    return {id,status:t.status,idempotent:false};
  }
}
export class MemoryPdfs {
  values = new Map(); deleted = [];
  async put(key,value) { this.values.set(key,Buffer.from(value)); }
  async get(key) { return this.values.get(key) || null; }
  async delete(key) { this.deleted.push(key);this.values.delete(key); }
}
