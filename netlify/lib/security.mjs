import {createHmac, randomBytes, timingSafeEqual} from 'node:crypto';

export const COOKIE_NAME = '__Host-coursematch';
export const SESSION_SECONDS = 7 * 24 * 60 * 60;
export const MAX_PDF_BYTES = 4 * 1024 * 1024;
export const MAX_BODY_BYTES = 6_000_000 - 4096;

export class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
export const newId = () => randomBytes(16).toString('hex');
export const newLease = () => randomBytes(24).toString('hex');
export const isId = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
export const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
export function equal(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const aa = Buffer.from(a), bb = Buffer.from(b);
  return aa.length === bb.length && timingSafeEqual(aa, bb);
}
function mac(secret, purpose, value) {
  return createHmac('sha256', secret).update(purpose + '\0' + value).digest('base64url');
}
export function contextScope(env) {
  const context = env.CONTEXT || 'local';
  if (context === 'production') return 'production';
  if (context === 'deploy-preview') return 'preview-' + (env.DEPLOY_ID || 'unavailable');
  if (context === 'branch-deploy') return 'branch-' + (env.BRANCH || 'unavailable');
  return 'local-' + context;
}
export function sessionCodec(secret, scope) {
  if (typeof secret !== 'string' || secret.length < 32) throw new HttpError(503, 'إعداد الجلسات غير مكتمل');
  return {
    create(now) {
      const id = randomBytes(32).toString('hex');
      const expires = Math.floor(now / 1000) + SESSION_SECONDS;
      const body = `${id}.${expires}`;
      return {id, expires, value: body + '.' + mac(secret, 'session:' + scope, body)};
    },
    read(request, now) {
      const cookie = request.headers.get('cookie') || '';
      const pair = cookie.split(';').map(v => v.trim()).find(v => v.startsWith(COOKIE_NAME + '='));
      const value = pair?.slice(COOKIE_NAME.length + 1) || '';
      const parts = value.split('.');
      if (parts.length !== 3 || !/^[a-f0-9]{64}$/.test(parts[0]) || !/^\d{10,11}$/.test(parts[1])) return null;
      const expires = Number(parts[1]);
      if (expires <= Math.floor(now / 1000) || expires > Math.floor(now / 1000) + SESSION_SECONDS) return null;
      if (!equal(parts[2], mac(secret, 'session:' + scope, parts[0] + '.' + parts[1]))) return null;
      return {id:parts[0], expires, value};
    },
    csrf(session) { return mac(secret, 'csrf:' + scope, session.id + '.' + session.expires); },
    cookie(session) { return `${COOKIE_NAME}=${session.value}; Path=/; Max-Age=${SESSION_SECONDS}; HttpOnly; Secure; SameSite=Strict`; }
  };
}
export function requireOrigin(request) {
  const origin = request.headers.get('origin');
  if (origin !== new URL(request.url).origin || request.headers.get('sec-fetch-site') === 'cross-site') {
    throw new HttpError(403, 'مصدر الطلب غير مسموح');
  }
}
export function requireWorker(request, secret) {
  if (typeof secret !== 'string' || secret.length < 32) throw new HttpError(503, 'إعداد المحرك غير مكتمل');
  if (request.headers.has('origin') || !equal(request.headers.get('authorization'), 'Bearer ' + secret)) {
    throw new HttpError(403, 'الوصول إلى المحرك غير مصرح');
  }
}
export async function readJson(request) {
  if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) throw new HttpError(415, 'يلزم طلب JSON');
  const length = Number(request.headers.get('content-length') || 0);
  if (length > MAX_BODY_BYTES) throw new HttpError(413, 'حجم الطلب يتجاوز الحد المتاح');
  const reader = request.body?.getReader();
  if (!reader) throw new HttpError(400, 'الطلب فارغ');
  const chunks = []; let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) { await reader.cancel(); throw new HttpError(413, 'حجم الطلب يتجاوز الحد المتاح'); }
    chunks.push(value);
  }
  try {
    const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!plain(result)) throw Error('object required');
    return result;
  } catch { throw new HttpError(400, 'صيغة الطلب غير صالحة'); }
}
export function string(value, name, maximum, {optional = false} = {}) {
  if (optional && value === undefined) return '';
  if (typeof value !== 'string' || value.length > maximum || /[\x00-\x1f\x7f]/.test(value)) throw new HttpError(400, `${name} غير صالح`);
  return value.trim();
}
export function beneficiary(value = {}) {
  if (!plain(value)) throw new HttpError(400, 'بيانات المستفيد غير صالحة');
  return {name:string(value.name, 'اسم المستفيد', 160, {optional:true}), number:string(value.number, 'رقم المستفيد', 60, {optional:true})};
}
export function topics(value, optional = false) {
  if (value === undefined && optional) return undefined;
  if (!Array.isArray(value) || value.length > 100) throw new HttpError(400, 'عدد المفردات غير صالح');
  const ids = new Set();
  return value.map((row, i) => {
    if (!plain(row)) throw new HttpError(400, 'مفردة غير صالحة');
    const id = string(row.id ?? 't' + (i + 1), 'معرف المفردة', 100);
    const raw = row.corrected_text || row.text;
    if (typeof raw !== 'string' || raw.length > 4000 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(raw)) throw new HttpError(400,'نص المفردة غير صالح');
    const text = raw.trim();
    if (!id || !text || ids.has(id)) throw new HttpError(400, 'مفردة فارغة أو مكررة');
    ids.add(id);
    return {id, text};
  });
}
export function decodePdf(value) {
  if (typeof value !== 'string' || !value || value.length > Math.ceil(MAX_PDF_BYTES / 3) * 4
      || value.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new HttpError(400, 'ملف PDF غير صالح أو أكبر من 4 ميغابايت');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.length > MAX_PDF_BYTES || bytes.subarray(0, 5).toString() !== '%PDF-' || bytes.toString('base64') !== value) throw new HttpError(400, 'يلزم PDF صالح حتى 4 ميغابايت');
  return bytes;
}
