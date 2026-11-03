import {DatabaseSync} from 'node:sqlite';
import {mkdirSync, chmodSync, existsSync, lstatSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {HttpError, newLease} from './security.mjs';

const HOUR = 3_600_000, DAY = 86_400_000, LEASE = 900_000;
const QUOTAS = {source:20,search:120,compare:5};
const SCHEMA = `
CREATE TABLE IF NOT EXISTS cm_sessions (
  scope TEXT NOT NULL, id TEXT NOT NULL CHECK(length(id)=64 AND id NOT GLOB '*[^0-9a-f]*'),
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY(scope,id)
) STRICT;
CREATE TABLE IF NOT EXISTS cm_tasks (
  scope TEXT NOT NULL, id TEXT NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
  session_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('source','search','compare')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','done','error')),
  payload TEXT NOT NULL CHECK(json_valid(payload)), result TEXT CHECK(result IS NULL OR json_valid(result)),
  error TEXT, pdf_key TEXT, attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
  worker_id TEXT, lease_token TEXT, lease_expires_at INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  PRIMARY KEY(scope,id), FOREIGN KEY(scope,session_id) REFERENCES cm_sessions(scope,id)
) STRICT;
CREATE INDEX IF NOT EXISTS cm_tasks_claim ON cm_tasks(scope,status,created_at);
CREATE INDEX IF NOT EXISTS cm_tasks_claim_lane ON cm_tasks(scope,kind,status,created_at);
CREATE INDEX IF NOT EXISTS cm_tasks_owner ON cm_tasks(scope,session_id,expires_at);
CREATE INDEX IF NOT EXISTS cm_tasks_rate ON cm_tasks(scope,session_id,kind,created_at);
CREATE TABLE IF NOT EXISTS cm_artifacts (
  scope TEXT NOT NULL, id TEXT NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
  session_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('source','report')),
  value TEXT NOT NULL CHECK(json_valid(value)), pdf_key TEXT,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  PRIMARY KEY(scope,id), FOREIGN KEY(scope,session_id) REFERENCES cm_sessions(scope,id)
) STRICT;
CREATE INDEX IF NOT EXISTS cm_artifacts_owner ON cm_artifacts(scope,session_id,expires_at);
CREATE TABLE IF NOT EXISTS cm_workers (
  scope TEXT NOT NULL, id TEXT NOT NULL, last_seen INTEGER NOT NULL, PRIMARY KEY(scope,id)
) STRICT;
CREATE TABLE IF NOT EXISTS cm_pdf_gc (
  scope TEXT NOT NULL, pdf_key TEXT NOT NULL, created_at INTEGER NOT NULL, not_before INTEGER NOT NULL,
  PRIMARY KEY(scope,pdf_key)
) STRICT;
PRAGMA user_version=1;
`;

/** Durable local queue; same interface as PostgresRepository, without hosted DB. */
export class SQLiteRepository {
  constructor(path, {busyTimeout = 10_000} = {}) {
    if (!Number.isInteger(busyTimeout) || busyTimeout < 1 || busyTimeout > 60_000) throw new TypeError('Invalid SQLite busy timeout');
    this.path = path === ':memory:' ? path : resolve(path);
    if (this.path !== ':memory:') {
      mkdirSync(dirname(this.path),{recursive:true,mode:0o700});
      if (existsSync(this.path) && lstatSync(this.path).isSymbolicLink()) throw new Error('SQLite path must not be a symbolic link');
    }
    this.db = new DatabaseSync(this.path,{enableForeignKeyConstraints:true});
    try {
      if (this.path !== ':memory:') chmodSync(this.path,0o600);
      this.db.exec(`PRAGMA busy_timeout=${busyTimeout}; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA trusted_schema=OFF;`);
      const version = this.db.prepare('PRAGMA user_version').get().user_version;
      if (version > 1) throw new Error('Unsupported SQLite schema version');
      this.db.exec(SCHEMA);
      this.closed = false;
    } catch (error) { this.db.close(); throw error; }
  }
  close() { if (!this.closed) { this.db.close(); this.closed = true; } }
  transaction(callback) {
    if (callback?.constructor?.name === 'AsyncFunction') throw new TypeError('SQLite transaction callback must be synchronous');
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = callback();
      if (result && typeof result.then === 'function') throw new TypeError('SQLite transaction callback must be synchronous');
      this.db.exec('COMMIT'); return result;
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  row(row) {
    if (!row) return null;
    const result = {...row};
    for (const key of ['payload','result','value']) if (typeof result[key] === 'string') result[key] = JSON.parse(result[key]);
    for (const key of ['created_at','updated_at','expires_at','lease_expires_at','last_seen']) if (result[key] !== undefined && result[key] !== null) result[key] = new Date(result[key]);
    return result;
  }
  async ensureSession(scope, session, now) {
    this.db.prepare('INSERT INTO cm_sessions(scope,id,created_at,expires_at) VALUES(?,?,?,?) ON CONFLICT(scope,id) DO NOTHING')
      .run(scope,session.id,now,session.expires * 1000);
  }
  async workerOnline(scope, now) {
    return !!this.db.prepare('SELECT id FROM cm_workers WHERE scope=? AND last_seen>? LIMIT 1').get(scope,now - 120_000);
  }
  workerSeen(scope, worker, now) {
    this.db.prepare('INSERT INTO cm_workers(scope,id,last_seen) VALUES(?,?,?) ON CONFLICT(scope,id) DO UPDATE SET last_seen=excluded.last_seen').run(scope,worker,now);
  }
  async workerHeartbeat(scope, worker, now) { this.workerSeen(scope,worker,now); }
  async enqueue(scope, session, kind, payload, pdfKey, id, now) {
    if (!Object.hasOwn(QUOTAS,kind)) throw new HttpError(400,'نوع الطلب غير صالح');
    return this.transaction(() => {
      if (!this.db.prepare('SELECT id FROM cm_sessions WHERE scope=? AND id=? AND expires_at>?').get(scope,session,now)) throw new HttpError(401,'انتهت الجلسة؛ أعد فتح الصفحة');
      // Bound the public pilot queue across all sessions in this isolated
      // environment, including clients that keep creating fresh cookies.
      const pending = this.db.prepare("SELECT count(*) AS total FROM cm_tasks WHERE scope=? AND expires_at>? AND status IN ('queued','running')").get(scope,now).total;
      if (pending >= 100) throw new HttpError(429,'قائمة الطلبات ممتلئة مؤقتًا؛ حاول لاحقًا');
      const used = this.db.prepare('SELECT count(*) AS used FROM cm_tasks WHERE scope=? AND session_id=? AND kind=? AND created_at>?').get(scope,session,kind,now - HOUR).used;
      if (used >= QUOTAS[kind]) throw new HttpError(429,'بلغت الحد المتاح لهذه الطلبات؛ حاول لاحقًا');
      this.db.prepare('INSERT INTO cm_tasks(scope,id,session_id,kind,payload,pdf_key,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)')
        .run(scope,id,session,kind,JSON.stringify(payload),pdfKey,now,now,now + DAY);
      return this.row(this.db.prepare('SELECT * FROM cm_tasks WHERE scope=? AND id=?').get(scope,id));
    });
  }
  async getTask(scope, id, session, now) {
    return this.row(this.db.prepare('SELECT * FROM cm_tasks WHERE scope=? AND id=? AND session_id=? AND expires_at>?').get(scope,id,session,now));
  }
  async findTaskByRequestId(scope, requestId, session, now) {
    return this.row(this.db.prepare("SELECT * FROM cm_tasks WHERE scope=? AND session_id=? AND expires_at>? AND json_extract(payload,'$.request_id')=? ORDER BY created_at DESC,id DESC LIMIT 1")
      .get(scope,session,now,requestId));
  }
  async getArtifact(scope, id, session, now, kind) {
    return this.row(this.db.prepare('SELECT * FROM cm_artifacts WHERE scope=? AND id=? AND session_id=? AND expires_at>? AND kind=?').get(scope,id,session,now,kind));
  }
  async updateBeneficiary(scope, id, session, value, now) {
    return this.transaction(() => {
      const row = this.row(this.db.prepare("SELECT * FROM cm_artifacts WHERE scope=? AND id=? AND session_id=? AND kind='report' AND expires_at>?").get(scope,id,session,now));
      if (!row) throw new HttpError(404,'التقرير غير موجود أو انتهت صلاحيته');
      const updated = {...row.value,beneficiary:value}, json = JSON.stringify(updated);
      this.db.prepare('UPDATE cm_artifacts SET value=? WHERE scope=? AND id=? AND session_id=?').run(json,scope,id,session);
      this.db.prepare("UPDATE cm_tasks SET result=?,updated_at=? WHERE scope=? AND id=? AND session_id=? AND status='done'").run(json,now,scope,id,session);
      return updated;
    });
  }
  async cleanup(scope, now) {
    return this.transaction(() => {
      const keys = this.db.prepare(`SELECT pdf_key FROM cm_tasks WHERE scope=? AND expires_at<=? AND pdf_key IS NOT NULL
        UNION SELECT pdf_key FROM cm_artifacts WHERE scope=? AND expires_at<=? AND pdf_key IS NOT NULL`).all(scope,now,scope,now);
      const reserve = this.db.prepare('INSERT INTO cm_pdf_gc(scope,pdf_key,created_at,not_before) VALUES(?,?,?,?) ON CONFLICT(scope,pdf_key) DO UPDATE SET not_before=min(cm_pdf_gc.not_before,excluded.not_before)');
      for (const {pdf_key} of keys) reserve.run(scope,pdf_key,now,now);
      this.db.prepare('DELETE FROM cm_artifacts WHERE scope=? AND expires_at<=?').run(scope,now);
      this.db.prepare('DELETE FROM cm_tasks WHERE scope=? AND expires_at<=?').run(scope,now);
      this.db.prepare(`DELETE FROM cm_sessions WHERE scope=? AND expires_at<=?
        AND NOT EXISTS(SELECT 1 FROM cm_tasks t WHERE t.scope=cm_sessions.scope AND t.session_id=cm_sessions.id)
        AND NOT EXISTS(SELECT 1 FROM cm_artifacts a WHERE a.scope=cm_sessions.scope AND a.session_id=cm_sessions.id)`).run(scope,now);
      return this.db.prepare('SELECT pdf_key FROM cm_pdf_gc WHERE scope=? AND not_before<=? ORDER BY created_at,pdf_key LIMIT 100').all(scope,now).map(row => row.pdf_key);
    });
  }
  async reservePdf(scope, key, now) {
    this.db.prepare('INSERT INTO cm_pdf_gc(scope,pdf_key,created_at,not_before) VALUES(?,?,?,?) ON CONFLICT(scope,pdf_key) DO NOTHING').run(scope,key,now,now + DAY);
  }
  async ackPdfDeletion(scope, key) { this.db.prepare('DELETE FROM cm_pdf_gc WHERE scope=? AND pdf_key=?').run(scope,key); }
  // Optional kinds are server-selected worker lanes, not browser input. Keep
  // active-lease reuse unfiltered: changing a worker role must not give that
  // same identity a second simultaneous task. Candidates are filtered inside
  // the same write transaction that assigns their lease.
  async claim(scope, worker, now, kinds) {
    if (kinds !== undefined && (!Array.isArray(kinds) || !kinds.length || kinds.length > 3 || new Set(kinds).size !== kinds.length || kinds.some(kind => !Object.hasOwn(QUOTAS,kind)))) throw new HttpError(400,'مسار العامل غير صالح');
    const filter = kinds === undefined ? '' : ` AND kind IN (${kinds.map(() => '?').join(',')})`;
    return this.transaction(() => {
      this.workerSeen(scope,worker,now);
      const current = this.db.prepare("SELECT * FROM cm_tasks WHERE scope=? AND worker_id=? AND status='running' AND lease_expires_at>? AND expires_at>? ORDER BY created_at,id LIMIT 1").get(scope,worker,now,now);
      if (current) return this.row(current);
      this.db.prepare(`UPDATE cm_tasks SET status='error',error='تعذر إكمال الطلب بعد ثلاث محاولات',lease_token=NULL,lease_expires_at=NULL,updated_at=?
        WHERE scope=? AND status='running' AND lease_expires_at<=? AND expires_at>? AND attempts>=3`).run(now,scope,now,now);
      const candidate = this.db.prepare(`SELECT * FROM cm_tasks WHERE scope=? AND expires_at>? AND attempts<3
        AND (status='queued' OR (status='running' AND lease_expires_at<=?))${filter} ORDER BY created_at,id LIMIT 1`).get(scope,now,now,...(kinds || []));
      if (!candidate) return null;
      this.db.prepare("UPDATE cm_tasks SET status='running',attempts=attempts+1,worker_id=?,lease_token=?,lease_expires_at=min(?,expires_at),updated_at=? WHERE scope=? AND id=?")
        .run(worker,newLease(),now + LEASE,now,scope,candidate.id);
      return this.row(this.db.prepare('SELECT * FROM cm_tasks WHERE scope=? AND id=?').get(scope,candidate.id));
    });
  }
  async leasedTask(scope, id, token, now) {
    return this.row(this.db.prepare("SELECT * FROM cm_tasks WHERE scope=? AND id=? AND status='running' AND lease_token=? AND lease_expires_at>? AND expires_at>?").get(scope,id,token,now,now));
  }
  async heartbeat(scope, id, token, now) {
    return this.transaction(() => {
      const row = this.db.prepare(`UPDATE cm_tasks SET lease_expires_at=min(?,expires_at),updated_at=?
        WHERE scope=? AND id=? AND status='running' AND lease_token=? AND lease_expires_at>? AND expires_at>? RETURNING *`)
        .get(now + LEASE,now,scope,id,token,now,now);
      if (!row) throw new HttpError(409,'انتهى حجز المهمة أو تغير');
      this.workerSeen(scope,row.worker_id,now);
      return this.row(row);
    });
  }
  async complete(scope, id, token, result, error, now, prepare) {
    if (!error && prepare?.constructor?.name === 'AsyncFunction') throw new TypeError('SQLite completion preparation must be synchronous');
    return this.transaction(() => {
      const row = this.row(this.db.prepare('SELECT * FROM cm_tasks WHERE scope=? AND id=?').get(scope,id));
      if (!row || row.expires_at.getTime() <= now) throw new HttpError(404,'المهمة غير موجودة أو انتهت صلاحيتها');
      if (row.lease_token !== token) throw new HttpError(409,'تغير حجز المهمة');
      if (['done','error'].includes(row.status)) return {id,status:row.status,idempotent:true};
      if (row.status !== 'running' || !row.lease_expires_at || row.lease_expires_at.getTime() <= now) throw new HttpError(409,'انتهى حجز المهمة');
      const ready = error ? null : prepare(row,result);
      if (ready && typeof ready.then === 'function') throw new TypeError('SQLite completion preparation must be synchronous');
      if (ready && ['source','compare'].includes(row.kind)) {
        this.db.prepare('INSERT INTO cm_artifacts(scope,id,session_id,kind,value,pdf_key,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope,id) DO NOTHING')
          .run(scope,id,row.session_id,row.kind === 'source' ? 'source' : 'report',JSON.stringify(ready),row.kind === 'source' ? row.pdf_key : null,now,row.expires_at.getTime());
      }
      this.db.prepare('UPDATE cm_tasks SET status=?,result=?,error=?,lease_expires_at=NULL,updated_at=? WHERE scope=? AND id=?')
        .run(error ? 'error' : 'done',JSON.stringify(ready),error || null,now,scope,id);
      return {id,status:error ? 'error' : 'done',idempotent:false};
    });
  }
}

export {SQLiteRepository as SqliteRepository};
