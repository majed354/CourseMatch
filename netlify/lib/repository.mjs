import {HttpError, newLease} from './security.mjs';

const one = result => result.rows[0] || null;
export class PostgresRepository {
  constructor(pool) { this.pool = pool; }
  async transaction(callback) {
    const client = await this.pool.connect();
    try { await client.query('BEGIN'); const result = await callback(client); await client.query('COMMIT'); return result; }
    catch (error) { await client.query('ROLLBACK'); throw error; }
    finally { client.release(); }
  }
  async ensureSession(scope, session, now) {
    await this.pool.query(`INSERT INTO cm_sessions(scope,id,created_at,expires_at) VALUES($1,$2,$3,$4) ON CONFLICT (scope,id) DO NOTHING`,
      [scope,session.id,new Date(now),new Date(session.expires * 1000)]);
  }
  async workerOnline(scope, now) {
    return !!one(await this.pool.query('SELECT id FROM cm_workers WHERE scope=$1 AND last_seen>$2 LIMIT 1', [scope,new Date(now - 120_000)]));
  }
  async workerHeartbeat(scope, worker, now) {
    await this.pool.query(`INSERT INTO cm_workers(scope,id,last_seen) VALUES($1,$2,$3) ON CONFLICT (scope,id) DO UPDATE SET last_seen=excluded.last_seen`, [scope,worker,new Date(now)]);
  }
  async enqueue(scope, session, kind, payload, pdfKey, id, now) {
    const maximum = {source:20,search:120,compare:5}[kind];
    return this.transaction(async client => {
      const sessionRow = one(await client.query('SELECT id FROM cm_sessions WHERE scope=$1 AND id=$2 AND expires_at>$3 FOR UPDATE', [scope,session,new Date(now)]));
      if (!sessionRow) throw new HttpError(401, 'انتهت الجلسة؛ أعد فتح الصفحة');
      // Session-row lock makes this rolling-hour quota and insertion atomic.
      const usage = one(await client.query(`SELECT count(*)::int AS used FROM cm_tasks WHERE scope=$1 AND session_id=$2 AND kind=$3 AND created_at>$4`, [scope,session,kind,new Date(now - 3_600_000)]));
      if (usage.used >= maximum) throw new HttpError(429, 'بلغت الحد المتاح لهذه الطلبات؛ حاول لاحقًا');
      return one(await client.query(`INSERT INTO cm_tasks(scope,id,session_id,kind,payload,pdf_key,created_at,updated_at,expires_at)
        VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$7,$8) RETURNING *`,
      [scope,id,session,kind,JSON.stringify(payload),pdfKey,new Date(now),new Date(now + 86_400_000)]));
    });
  }
  async getTask(scope, id, session, now) {
    return one(await this.pool.query('SELECT * FROM cm_tasks WHERE scope=$1 AND id=$2 AND session_id=$3 AND expires_at>$4', [scope,id,session,new Date(now)]));
  }
  async getArtifact(scope, id, session, now, kind) {
    return one(await this.pool.query('SELECT * FROM cm_artifacts WHERE scope=$1 AND id=$2 AND session_id=$3 AND expires_at>$4 AND kind=$5', [scope,id,session,new Date(now),kind]));
  }
  async updateBeneficiary(scope, id, session, value, now) {
    return this.transaction(async client => {
      const row = one(await client.query(`UPDATE cm_artifacts SET value=jsonb_set(value,'{beneficiary}',$4::jsonb,true)
        WHERE scope=$1 AND id=$2 AND session_id=$3 AND kind='report' AND expires_at>$5 RETURNING value`, [scope,id,session,JSON.stringify(value),new Date(now)]));
      if (!row) throw new HttpError(404, 'التقرير غير موجود أو انتهت صلاحيته');
      await client.query(`UPDATE cm_tasks SET result=$4::jsonb,updated_at=$5 WHERE scope=$1 AND id=$2 AND session_id=$3 AND status='done'`, [scope,id,session,JSON.stringify(row.value),new Date(now)]);
      return row.value;
    });
  }
  async cleanup(scope, now) {
    return this.transaction(async client => {
      await client.query(`INSERT INTO cm_pdf_gc(scope,pdf_key) SELECT scope,pdf_key FROM cm_tasks WHERE scope=$1 AND expires_at<=$2 AND pdf_key IS NOT NULL
        UNION SELECT scope,pdf_key FROM cm_artifacts WHERE scope=$1 AND expires_at<=$2 AND pdf_key IS NOT NULL ON CONFLICT DO NOTHING`, [scope,new Date(now)]);
      await client.query('DELETE FROM cm_artifacts WHERE scope=$1 AND expires_at<=$2', [scope,new Date(now)]);
      await client.query('DELETE FROM cm_tasks WHERE scope=$1 AND expires_at<=$2', [scope,new Date(now)]);
      await client.query(`DELETE FROM cm_sessions s WHERE scope=$1 AND expires_at<=$2 AND NOT EXISTS(SELECT 1 FROM cm_tasks t WHERE t.scope=s.scope AND t.session_id=s.id)
        AND NOT EXISTS(SELECT 1 FROM cm_artifacts a WHERE a.scope=s.scope AND a.session_id=s.id)`, [scope,new Date(now)]);
      return (await client.query('SELECT pdf_key FROM cm_pdf_gc WHERE scope=$1 AND not_before<=$2 ORDER BY created_at LIMIT 100', [scope,new Date(now)])).rows.map(row => row.pdf_key);
    });
  }
  async reservePdf(scope, key, now) {
    // Register deletion before the object write, so a timeout between storage
    // and task insertion cannot leave an untracked private upload forever.
    await this.pool.query(`INSERT INTO cm_pdf_gc(scope,pdf_key,not_before) VALUES($1,$2,$3) ON CONFLICT DO NOTHING`, [scope,key,new Date(now + 86_400_000)]);
  }
  async ackPdfDeletion(scope, key) { await this.pool.query('DELETE FROM cm_pdf_gc WHERE scope=$1 AND pdf_key=$2', [scope,key]); }
  async claim(scope, worker, now) {
    return this.transaction(async client => {
      // Lock this worker row first, preventing a repeated claim from leasing
      // several tasks when the acknowledgment of its first claim was lost.
      await client.query(`INSERT INTO cm_workers(scope,id,last_seen) VALUES($1,$2,$3) ON CONFLICT (scope,id) DO UPDATE SET last_seen=excluded.last_seen`, [scope,worker,new Date(now)]);
      const current = one(await client.query(`SELECT * FROM cm_tasks WHERE scope=$1 AND worker_id=$2 AND status='running' AND lease_expires_at>$3 AND expires_at>$3 ORDER BY created_at LIMIT 1 FOR UPDATE`, [scope,worker,new Date(now)]));
      if (current) return current;
      await client.query(`UPDATE cm_tasks SET status='error',error='تعذر إكمال الطلب بعد ثلاث محاولات',lease_token=NULL,lease_expires_at=NULL,updated_at=$2
        WHERE scope=$1 AND status='running' AND lease_expires_at<=$2 AND expires_at>$2 AND attempts>=3`, [scope,new Date(now)]);
      const row = one(await client.query(`SELECT * FROM cm_tasks WHERE scope=$1 AND expires_at>$2 AND attempts<3
        AND (status='queued' OR (status='running' AND lease_expires_at<=$2)) ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED`, [scope,new Date(now)]));
      if (!row) return null;
      return one(await client.query(`UPDATE cm_tasks SET status='running',attempts=attempts+1,worker_id=$3,lease_token=$4,lease_expires_at=LEAST($5,expires_at),updated_at=$6
        WHERE scope=$1 AND id=$2 RETURNING *`, [scope,row.id,worker,newLease(),new Date(now + 900_000),new Date(now)]));
    });
  }
  async leasedTask(scope, id, token, now) {
    return one(await this.pool.query(`SELECT * FROM cm_tasks WHERE scope=$1 AND id=$2 AND status='running' AND lease_token=$3 AND lease_expires_at>$4 AND expires_at>$4`, [scope,id,token,new Date(now)]));
  }
  async heartbeat(scope, id, token, now) {
    const row = one(await this.pool.query(`UPDATE cm_tasks SET lease_expires_at=LEAST($5,expires_at),updated_at=$4
      WHERE scope=$1 AND id=$2 AND status='running' AND lease_token=$3 AND lease_expires_at>$4 AND expires_at>$4 RETURNING *`, [scope,id,token,new Date(now),new Date(now + 900_000)]));
    if (!row) throw new HttpError(409, 'انتهى حجز المهمة أو تغير');
    await this.workerHeartbeat(scope,row.worker_id,now);
    return row;
  }
  async complete(scope, id, token, result, error, now, prepare) {
    return this.transaction(async client => {
      const row = one(await client.query('SELECT * FROM cm_tasks WHERE scope=$1 AND id=$2 FOR UPDATE', [scope,id]));
      if (!row || new Date(row.expires_at).getTime() <= now) throw new HttpError(404, 'المهمة غير موجودة أو انتهت صلاحيتها');
      if (row.lease_token !== token) throw new HttpError(409, 'تغير حجز المهمة');
      if (['done','error'].includes(row.status)) return {id,status:row.status,idempotent:true};
      if (row.status !== 'running' || new Date(row.lease_expires_at).getTime() <= now) throw new HttpError(409, 'انتهى حجز المهمة');
      const ready = error ? null : prepare(row,result);
      if (ready && ['source','compare'].includes(row.kind)) {
        await client.query(`INSERT INTO cm_artifacts(scope,id,session_id,kind,value,pdf_key,created_at,expires_at) VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8)
          ON CONFLICT (scope,id) DO NOTHING`, [scope,id,row.session_id,row.kind === 'source' ? 'source' : 'report',JSON.stringify(ready),row.kind === 'source' ? row.pdf_key : null,new Date(now),row.expires_at]);
      }
      await client.query(`UPDATE cm_tasks SET status=$3,result=$4::jsonb,error=$5,lease_expires_at=NULL,updated_at=$6 WHERE scope=$1 AND id=$2`, [scope,id,error ? 'error' : 'done',JSON.stringify(ready),error || null,new Date(now)]);
      return {id,status:error ? 'error' : 'done',idempotent:false};
    });
  }
}
