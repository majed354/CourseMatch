import {createHash} from 'node:crypto';
import {HttpError, newId, isId, plain, equal, sessionCodec, contextScope, requireOrigin, requireWorker, readJson,
  string, beneficiary, topics, decodePdf} from './security.mjs';

const messages = {queued:'بانتظار المحرك',running:'جارٍ إعداد النتيجة',done:'اكتمل الطلب',error:'تعذر إكمال الطلب'};
const outputHeaders = {'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
  'Referrer-Policy':'no-referrer','Vary':'Cookie','Cross-Origin-Resource-Policy':'same-origin'};
const hidden = new Set(['input_pdf_key','pdf_key','worker_token','session_secret','local_path']);
function publicValue(value) {
  if (Array.isArray(value)) return value.map(publicValue);
  if (!plain(value)) return value;
  return Object.fromEntries(Object.entries(value).filter(([key]) => !hidden.has(key)).map(([key,item]) => [key,publicValue(item)]));
}
function leasedToken(value) {
  if (typeof value !== 'string' || !/^[a-f0-9]{48}$/.test(value)) throw new HttpError(400, 'رمز حجز المهمة غير صالح');
  return value;
}

export function createApi({repository, pdfs, catalog, env = {}, clock = () => Date.now(), logger = console}) {
  const scope = contextScope(env);
  const storePrefix = createHash('sha256').update(scope).digest('hex').slice(0,20);
  const sources = new Map([...(catalog.courses || []),...(catalog.library_sources || [])].map(course => [course.id,course]));
  const targets = new Map((catalog.courses || []).map(course => [course.id,course]));
  const contexts = new Set((catalog.contexts || []).map(context => context.id));
  const respond = (value, status = 200, headers = {}) => new Response(JSON.stringify(value), {status,headers:{...outputHeaders,...headers}});
  function referenceTopics(course, supplied) {
    // The bundled catalog is the trusted local reference imported from the
    // published specification. Students can review their uploaded source, but
    // cannot change the target denominator, order, identities or content.
    const canonical = course.topics.map(topic => ({id:topic.id,text:topic.corrected_text || topic.text}));
    if (supplied !== undefined && (!Array.isArray(supplied) || supplied.length !== canonical.length
        || supplied.some((row,i) => !plain(row) || row.id !== canonical[i].id || (row.corrected_text || row.text) !== canonical[i].text))) {
      throw new HttpError(400,'مفردات المقرر المرجعي ثابتة وفق التوصيف المعتمد');
    }
    // Always queue fresh server-owned copies, never caller-provided metadata.
    return canonical;
  }
  function searchOptions(data) {
    const all = data.all_contexts ?? false;
    const single = data.context_id;
    const many = data.context_ids;
    if (typeof all !== 'boolean' || Number(all) + Number(single !== undefined) + Number(many !== undefined) > 1) throw new HttpError(400,'نطاق البحث غير صالح');
    if (single !== undefined && (typeof single !== 'string' || !contexts.has(single))) throw new HttpError(400,'الخطة غير موجودة');
    if (many !== undefined && (!Array.isArray(many) || !many.length || many.length > contexts.size || many.some(id => typeof id !== 'string' || !contexts.has(id)))) throw new HttpError(400,'الخطط غير صالحة');
    const limit = data.limit ?? 5;
    if (!Number.isInteger(limit) || limit < 1 || limit > 10) throw new HttpError(400,'عدد المرشحين غير صالح');
    return {all_contexts:all,...(single !== undefined ? {context_id:single} : {}),...(many !== undefined ? {context_ids:[...new Set(many)]} : {}),
      version:string(data.version ?? '47','الخطة',20),limit};
  }
  async function ownedSource(id, session, now) {
    if (!isId(id)) throw new HttpError(400,'معرف المصدر غير صالح');
    const source = await repository.getArtifact(scope,id,session,now,'source');
    if (!source) throw new HttpError(404,'المصدر غير موجود أو انتهت صلاحيته');
    return source;
  }
  function prepareResult(task, result) {
    if (!plain(result)) throw new HttpError(400,'نتيجة المحرك غير صالحة');
    const ready = publicValue(result);
    if (task.kind === 'source') {
      if (!Array.isArray(ready.topics) || ready.topics.length > 100) throw new HttpError(400,'مفردات المصدر غير صالحة');
      topics(ready.topics);
      ready.id = task.id;
      ready.upload_pdf = !!task.pdf_key;
      if(task.pdf_key) ready.source_media_kind = task.payload.input_kind === 'image' ? 'image' : 'pdf';
      if (task.payload.catalog_id) {
        const original = sources.get(task.payload.catalog_id);
        ready.catalog_course_id = original.id;
        ready.source_pdf = original.source_pdf;
      } else { delete ready.source_pdf; delete ready.catalog_course_id; }
    } else if (task.kind === 'search') {
      if (!Array.isArray(ready.candidates) || ready.candidates.length > 10 || ready.candidates.some(candidate => !plain(candidate) || !targets.has(candidate.id))) throw new HttpError(400,'مرشحات المحرك غير صالحة');
      for (const candidate of ready.candidates) {
        const original = targets.get(candidate.id);
        if (candidate.available_contexts !== undefined && (!Array.isArray(candidate.available_contexts) || candidate.available_contexts.some(c => !plain(c) || !targets.has(c.course_id)))) throw new HttpError(400,'سياقات المرشح غير صالحة');
        const allowed = course => task.payload.all_contexts || (task.payload.context_id ? course.context_id === task.payload.context_id
          : task.payload.context_ids ? task.payload.context_ids.includes(course.context_id)
          : course.context?.program === 'القرآن وعلومه' && course.context?.degree === 'بكالوريوس' && String(course.context?.version) === task.payload.version);
        const identity = course => JSON.stringify([course.code,course.name,course.source_sha256,course.topics?.map(t => t.corrected_text || t.text)]);
        if (!allowed(original) || candidate.available_contexts?.some(c => !allowed(targets.get(c.course_id)) || identity(targets.get(c.course_id)) !== identity(original))) throw new HttpError(400,'المقرر المرشح خارج نطاق البحث أو يجمع مصادر مختلفة');
        // Canonical identities and PDF URLs always come from the bundled
        // catalog, regardless of which metadata the worker returned.
        Object.assign(candidate,{id:original.id,code:original.code,name:original.name,context_id:original.context_id,context:original.context,source_pdf:original.source_pdf});
        if (candidate.available_contexts) candidate.available_contexts = candidate.available_contexts.map(c => {
          const course = targets.get(c.course_id);
          return {course_id:course.id,context_id:course.context_id,context:course.context};
        });
      }
    } else if (task.kind === 'compare') {
      const original = targets.get(task.payload.target_id);
      if (!plain(ready.source) || ready.source.id !== task.payload.source_id || !plain(ready.target) || ready.target.id !== original.id
          || (original.source_sha256 && ready.target.source_sha256 !== original.source_sha256)
          || ready.catalog_id !== catalog.catalog_id || !plain(ready.calculation) || !['equivalent','lenient','not_equivalent','insufficient'].includes(ready.calculation.decision)) {
        throw new HttpError(400,'هوية تقرير المقارنة لا تطابق الطلب');
      }
      if (!Array.isArray(ready.target.topics)) throw new HttpError(400,'مفردات تقرير المرجع غير صالحة');
      referenceTopics(original,ready.target.topics);
      ready.id = task.id;
      ready.reviewed_by = task.payload.review_mode === 'automatic' ? 'automated_pipeline' : 'user_confirmed_extraction';
      ready.human_reviewed = task.payload.review_mode !== 'automatic';
      Object.assign(ready.target,{id:original.id,code:original.code,name:original.name,context_id:original.context_id,context:original.context,source_pdf:original.source_pdf});
      ready.beneficiary = beneficiary(task.payload.beneficiary);
    }
    return ready;
  }
  async function cleanup(now) {
    const keys = await repository.cleanup(scope,now);
    for (const key of keys) {
      try { await pdfs.delete(key); await repository.ackPdfDeletion(scope,key); }
      catch { logger.error('coursematch_pdf_cleanup_pending'); }
    }
  }
  async function worker(request, pathname, now) {
    requireWorker(request,env.COURSEMATCH_WORKER_TOKEN);
    const taskPath = pathname.match(/^\/api\/worker\/tasks\/([a-f0-9]{32})\/(input-pdf|heartbeat|complete)$/);
    if (taskPath && taskPath[2] === 'input-pdf') {
      if (request.method !== 'GET') throw new HttpError(405,'طريقة الطلب غير مسموحة');
      const token = leasedToken(request.headers.get('x-lease-token'));
      const task = await repository.leasedTask(scope,taskPath[1],token,now);
      if (!task) throw new HttpError(409,'انتهى حجز المهمة أو تغير');
      let key = task.pdf_key;
      if (!key && ['search','compare'].includes(task.kind)) key = (await ownedSource(task.payload.source_id,task.session_id,now)).pdf_key;
      if (!key) throw new HttpError(404,'لا يوجد PDF مرفوع لهذا المصدر');
      const binary = await pdfs.get(key);
      if (!binary) throw new HttpError(404,'ملف المصدر غير متاح');
      return new Response(binary,{headers:{...outputHeaders,'Content-Type':'application/pdf','Content-Disposition':'inline; filename="course.pdf"'}});
    }
    if (request.method !== 'POST') throw new HttpError(405,'طريقة الطلب غير مسموحة');
    const data = await readJson(request);
    if (pathname === '/api/worker/heartbeat' || pathname === '/api/worker/claim') {
      const workerId = string(data.worker_id,'معرف المحرك',100);
      if (!workerId || !/^[a-zA-Z0-9_.:-]+$/.test(workerId)) throw new HttpError(400,'معرف المحرك غير صالح');
      if (pathname.endsWith('/heartbeat')) { await repository.workerHeartbeat(scope,workerId,now); return respond({ok:true}); }
      const lane = data.lane ?? 'all';
      const lanes = {all:undefined,interactive:['source','search'],compare:['compare']};
      if (typeof lane !== 'string' || !Object.hasOwn(lanes,lane)) throw new HttpError(400,'مسار العامل غير صالح');
      await cleanup(now);
      const task = lane === 'all' ? await repository.claim(scope,workerId,now) : await repository.claim(scope,workerId,now,lanes[lane]);
      if (!task) return respond({task:null});
      const payload = {...task.payload};
      if (['search','compare'].includes(task.kind)) {
        try { payload.source = (await ownedSource(payload.source_id,task.session_id,now)).value; }
        catch (error) {
          await repository.complete(scope,task.id,task.lease_token,null,'انتهت صلاحية المصدر؛ أعد إدخاله',now,prepareResult);
          return respond({task:null});
        }
      }
      if (task.pdf_key) payload.has_pdf = true;
      return respond({task:{id:task.id,kind:task.kind,payload,session_id:task.session_id,lease_token:task.lease_token,lease_expires_at:task.lease_expires_at}});
    }
    if (taskPath) {
      const token = leasedToken(data.lease_token);
      if (taskPath[2] === 'heartbeat') {
        const task = await repository.heartbeat(scope,taskPath[1],token,now);
        return respond({ok:true,lease_expires_at:task.lease_expires_at});
      }
      const error = data.error === undefined ? '' : string(data.error,'وصف الخطأ',500);
      const completed = await repository.complete(scope,taskPath[1],token,data.result,error,now,prepareResult);
      return respond(completed);
    }
    throw new HttpError(404,'المسار غير موجود');
  }
  return async request => {
    try {
      const pathname = new URL(request.url).pathname, now = clock();
      if (pathname.startsWith('/api/worker/')) return await worker(request,pathname,now);
      if (!['GET','POST'].includes(request.method)) throw new HttpError(405,'طريقة الطلب غير مسموحة');
      const pdfPath = pathname.match(/^\/pdf\/([a-f0-9]{20,32})$/);
      if (request.method === 'GET' && pdfPath && sources.has(pdfPath[1])) {
        const url = sources.get(pdfPath[1]).source_pdf;
        if (!url || !/^assets\/(course-specifications|course-uploads)\//.test(url) || url.includes('..')) throw new HttpError(404,'التوصيف غير متاح');
        return new Response(null,{status:302,headers:{...outputHeaders,Location:'/' + url}});
      }
      const codec = sessionCodec(env.COURSEMATCH_SESSION_SECRET,scope);
      let session = codec.read(request,now);
      if (pathname === '/api/catalog' && request.method === 'GET') {
        if (request.headers.get('sec-fetch-site') === 'cross-site' || (request.headers.has('origin') && request.headers.get('origin') !== new URL(request.url).origin)) throw new HttpError(403,'مصدر الطلب غير مسموح');
        let cookie;
        if (!session) { session = codec.create(now); cookie = codec.cookie(session); }
        await repository.ensureSession(scope,session,now);
        const online = await repository.workerOnline(scope,now);
        return respond({catalog_url:'/catalog.json',token:codec.csrf(session),capabilities:{queued:true,max_upload_mb:4,image_upload:true,pdf_visual_verification:true,worker_online:online,reference_editable:false}},200,cookie ? {'Set-Cookie':cookie} : {});
      }
      if (!session) throw new HttpError(401,'افتح المنصة لبدء جلسة صالحة');
      if (request.method === 'POST') {
        requireOrigin(request);
        if (!equal(request.headers.get('x-equivalency-token'),codec.csrf(session))) throw new HttpError(403,'رمز الطلب غير صالح');
      }
      const operationPath = pathname.match(/^\/api\/(operations|jobs)\/([a-f0-9]{32})$/);
      if (request.method === 'GET' && operationPath) {
        const row = await repository.getTask(scope,operationPath[2],session.id,now);
        if (!row || (operationPath[1] === 'jobs') !== (row.kind === 'compare')) throw new HttpError(404,'الطلب غير موجود أو انتهت صلاحيته');
        return respond({id:row.id,status:row.status,message:row.error || messages[row.status],...(row.result ? {result:publicValue(row.result)} : {}),...(row.error ? {error:row.error} : {})});
      }
      if (request.method === 'GET' && pdfPath) {
        const row = await ownedSource(pdfPath[1],session.id,now);
        if (!row.pdf_key) throw new HttpError(404,'لا يوجد ملف مرفوع لهذا المصدر');
        const binary = await pdfs.get(row.pdf_key);
        if (!binary) throw new HttpError(404,'ملف المصدر غير متاح');
        return new Response(binary,{headers:{...outputHeaders,'Content-Type':'application/pdf','Content-Disposition':'inline; filename="course.pdf"'}});
      }
      if (request.method !== 'POST') throw new HttpError(404,'المسار غير موجود');
      const data = await readJson(request);
      const beneficiaryPath = pathname.match(/^\/api\/reports\/([a-f0-9]{32})\/beneficiary$/);
      if (beneficiaryPath) return respond(publicValue(await repository.updateBeneficiary(scope,beneficiaryPath[1],session.id,beneficiary(data.beneficiary),now)));
      if (!['/api/sources','/api/search','/api/jobs'].includes(pathname)) throw new HttpError(404,'المسار غير موجود');
      const id = newId(); let kind, payload, pdfKey = null, pdf;
      if (pathname === '/api/sources') {
        kind = 'source';
        const modes = Number(data.catalog_id !== undefined) + Number(data.pdf_base64 !== undefined) + Number(data.topics !== undefined);
        if (modes !== 1) throw new HttpError(400,'اختر مصدرًا واحدًا للمقرر');
        if (data.input_kind !== undefined && (data.input_kind !== 'image' || data.pdf_base64 === undefined)) throw new HttpError(400,'نوع الملف المرفوع غير صالح');
        payload = {name:string(data.name,'اسم المقرر',200,{optional:true})};
        if (data.catalog_id !== undefined) {
          if (typeof data.catalog_id !== 'string' || !sources.has(data.catalog_id)) throw new HttpError(400,'المقرر المصدر غير موجود');
          payload.catalog_id = data.catalog_id;
        } else if (data.pdf_base64 !== undefined) {
          pdf = decodePdf(data.pdf_base64);
          pdfKey = `${storePrefix}/${session.id}/${id}.pdf`;
          payload.has_pdf = true;
          payload.input_pdf_key = pdfKey;
          if(data.input_kind==='image') {
            payload.input_kind='image';
            payload.original_filename=string(data.original_filename,'اسم الملف',200,{optional:true});
          }
        } else payload.topics = topics(data.topics);
      } else {
        const source = await ownedSource(data.source_id,session.id,now);
        if (pathname === '/api/search') {
          kind = 'search';
          payload = {source_id:source.id,...searchOptions(data)};
          const edited = topics(data.topics,true); if (edited !== undefined) payload.topics = edited;
        } else {
          kind = 'compare';
          if (typeof data.target_id !== 'string' || !targets.has(data.target_id)) throw new HttpError(400,'المقرر الهدف غير موجود في الخطة');
          if (data.review_mode !== undefined && data.review_mode !== 'automatic') throw new HttpError(400,'نمط التحقق غير صالح');
          if (data.review_mode !== 'automatic' && data.reviewed !== true) throw new HttpError(400,'أكد مراجعة مفردات المقررين قبل المقارنة');
          payload = {source_id:source.id,target_id:data.target_id,reviewed:true,review_mode:data.review_mode === 'automatic' ? 'automatic' : 'user_confirmed',beneficiary:beneficiary(data.beneficiary)};
          payload.target_topics = referenceTopics(targets.get(data.target_id),data.target_topics);
          const editedSource = topics(data.source_topics,true); if (editedSource !== undefined) payload.source_topics = editedSource;
        }
      }
      payload.catalog_version = catalog.catalog_id;
      if (pdf) {
        await repository.reservePdf(scope,pdfKey,now);
        await pdfs.put(pdfKey,pdf,{expires_at:new Date(now + 86_400_000).toISOString(),session_id:session.id});
      }
      try { await repository.enqueue(scope,session.id,kind,payload,pdfKey,id,now); }
      catch (error) { if (pdfKey) { try { await pdfs.delete(pdfKey); await repository.ackPdfDeletion(scope,pdfKey); } catch { logger.error('coursematch_upload_rollback_pending'); } } throw error; }
      return respond(kind === 'compare' ? {id,status:'queued',message:messages.queued} : {operation_id:id,status:'queued'},202);
    } catch (error) {
      if (!(error instanceof HttpError)) logger.error('coursematch_api_unavailable',error?.name || 'Error');
      return respond({error:error instanceof HttpError ? error.message : 'الخدمة غير متاحة مؤقتًا؛ حاول لاحقًا'},error instanceof HttpError ? error.status : 503);
    }
  };
}
