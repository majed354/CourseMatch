#!/usr/bin/env python3
"""Private outbound worker for the user's always-on Mac.

--lane all preserves the original queue. --lane compare runs only comparisons;
--lane interactive handles source extraction and search without waiting for Qwen.
Specialized lanes append their role to the configured worker ID. The interactive
lane has its own runtime, lock and checkpoints; compare keeps the original lock
so it cannot overlap a legacy all-lane worker during a supervised replacement.
The lane is sent only to the authenticated worker claim endpoint.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.equivalency.engine import RUBRIC_VERSION, EvidenceValidationError, calculate, compare, search, validate_pairs
from scripts.equivalency.grounding import validate_evidence
from scripts.equivalency.reference_quality import audit_incomplete, reference_pending, evidence_calculation, reference_problem, extraction_integrity, uploaded_target_reference
from scripts.equivalency.extract import apply_readings, digest, extract_cached
from scripts.equivalency.server import clean_beneficiary, clean_topics

LOGGER = logging.getLogger('coursematch.worker')
LANES = {'all': ('source','search','compare'), 'interactive': ('source','search'), 'compare': ('compare',)}
TASK_ID = re.compile(r'^[a-f0-9]{32}$')
SESSION_ID = re.compile(r'^[a-f0-9]{64}$')
LEASE_TOKEN = re.compile(r'^[a-f0-9]{48}$')
PDF_LIMIT = 12 * 1024 * 1024
JSON_LIMIT = 16 * 1024 * 1024
ENV_KEYS = {'COURSEMATCH_CLOUD_URL', 'COURSEMATCH_LOCAL_URL', 'COURSEMATCH_WORKER_TOKEN', 'COURSEMATCH_WORKER_ID', 'COURSEMATCH_MODEL'}


class CloudError(Exception):
    """A transport failure whose message never contains response bodies or URLs."""
    def __init__(self, status=0):
        self.status = status
        super().__init__('cloud_request_failed')


class LeaseLost(CloudError):
    pass


class TaskGone(CloudError):
    pass


class TaskInputError(Exception):
    """A fixed, beneficiary-safe explanation; never raw exception interpolation."""


def require_id(value, expression, message='معرف المهمة أو مصدرها غير صالح.'):
    if not isinstance(value, str) or not expression.fullmatch(value):
        raise TaskInputError(message)
    return value


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('private_directory_required')
    path.chmod(0o700)
    return path


def atomic_json(path, value):
    path = Path(path)
    private_directory(path.parent)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def load_env_file(path):
    """Read only our literal key=value file; no shell, eval or variable expansion."""
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, encoding='utf-8') as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
            raise ValueError('worker_env_requires_owner_only_permissions')
        result = {}
        for line in stream:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if not separator or key not in ENV_KEYS or key in result:
                raise ValueError('invalid_worker_env_entry')
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError('invalid_worker_env_quote')
                value = value[1:-1]
            if any(ord(character) < 32 for character in value):
                raise ValueError('invalid_worker_env_value')
            result[key] = value
        return result


@dataclass(frozen=True)
class Config:
    cloud_url: str
    token: str = field(repr=False)
    worker_id: str
    model: str = 'qwen3.8:27b-q8_0'
    local_url: str = ''

    def __post_init__(self):
        parsed = urllib.parse.urlsplit(self.cloud_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('worker_cloud_url_requires_https_origin')
        # This is a single explicit loopback exception, not general HTTP support.
        if not isinstance(self.local_url, str) or self.local_url not in ('', 'http://127.0.0.1:8800'):
            raise ValueError('worker_local_url_requires_fixed_loopback_origin')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,512}', self.token):
            raise ValueError('invalid_worker_token')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', self.worker_id):
            raise ValueError('invalid_worker_id')
        if not self.model or ':cloud' in self.model:
            raise ValueError('worker_model_must_be_local')

    @classmethod
    def from_file(cls, path, environ=None):
        values = load_env_file(path)
        values.update({key: value for key, value in (os.environ if environ is None else environ).items() if key in ENV_KEYS})
        return cls(values.get('COURSEMATCH_CLOUD_URL', '').rstrip('/'), values.get('COURSEMATCH_WORKER_TOKEN', ''), values.get('COURSEMATCH_WORKER_ID', ''), values.get('COURSEMATCH_MODEL', 'qwen3.8:27b-q8_0'), local_url=values.get('COURSEMATCH_LOCAL_URL', ''))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class CloudClient:
    def __init__(self, config, opener=None, lane='all'):
        if lane not in LANES:
            raise ValueError('invalid_worker_lane')
        self.lane = lane
        self.config = config
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, method, route, body=None, *, lease=None, binary=False):
        if not isinstance(route, str) or not route.startswith('/api/worker/') or '?' in route or '#' in route or '..' in route or '\\' in route:
            raise ValueError('invalid_worker_route')
        headers = {'Authorization': 'Bearer ' + self.config.token, 'Accept': 'application/pdf' if binary else 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            headers['Content-Type'] = 'application/json'
        if lease is not None:
            require_id(lease, LEASE_TOKEN)
            headers['X-Lease-Token'] = lease
        destination = self.config.local_url or self.config.cloud_url
        request = urllib.request.Request(destination.rstrip('/') + route, data=data, headers=headers, method=method)
        maximum = PDF_LIMIT if binary else JSON_LIMIT
        try:
            with self.opener.open(request, timeout=60 if binary else 30) as response:
                # An injected opener must not silently defeat the no-redirect rule.
                final_url = response.geturl() if hasattr(response, 'geturl') else request.full_url
                if final_url != request.full_url:
                    raise CloudError(302)
                raw = response.read(maximum + 1)
                if len(raw) > maximum:
                    raise CloudError(413)
                return raw if binary else json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code == 409:
                raise LeaseLost(409) from None
            if error.code in (404, 410):
                raise TaskGone(error.code) from None
            raise CloudError(error.code) from None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, UnicodeError):
            raise CloudError() from None

    def claim(self):
        reply = self.request('POST', '/api/worker/claim', {'worker_id': self.config.worker_id, **({'lane': self.lane} if self.lane != 'all' else {})})
        if not isinstance(reply, dict) or 'task' not in reply:
            raise CloudError()
        return reply['task']

    def idle_heartbeat(self):
        return self.request('POST', '/api/worker/heartbeat', {'worker_id': self.config.worker_id})

    def heartbeat(self, task):
        return self.request('POST', f"/api/worker/tasks/{task['id']}/heartbeat", {'lease_token': task['lease_token']})

    def input_pdf(self, task, *, role='source'):
        if role not in ('source','target'):
            raise ValueError('invalid_pdf_role')
        endpoint = 'input-target-pdf' if role == 'target' else 'input-pdf'
        return self.request('GET', f"/api/worker/tasks/{task['id']}/{endpoint}", lease=task['lease_token'], binary=True)

    def complete(self, task, result=None, error=None):
        body = {'lease_token': task['lease_token'], 'result': result}
        if error is not None:
            body['error'] = error
        return self.request('POST', f"/api/worker/tasks/{task['id']}/complete", body)


def validate_task(task):
    if not isinstance(task, dict) or task.get('kind') not in ('source', 'search', 'compare') or not isinstance(task.get('payload'), dict):
        raise TaskInputError('بيانات المهمة غير صالحة.')
    require_id(task.get('id'), TASK_ID)
    require_id(task.get('session_id'), SESSION_ID)
    require_id(task.get('lease_token'), LEASE_TOKEN)
    if len(json.dumps(task['payload'], ensure_ascii=False).encode()) > JSON_LIMIT:
        raise TaskInputError('حجم بيانات المهمة يتجاوز الحد المسموح.')
    return task


def payload_digest(task):
    return hashlib.sha256(json.dumps([task['kind'], task['session_id'], task['payload']], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Checkpoints:
    def __init__(self, directory, worker_id):
        self.directory = private_directory(directory)
        self.worker_id = worker_id

    def path(self, key):
        return self.directory / (require_id(key, TASK_ID) + '.json')

    def find(self, task, catalog_id):
        path = self.path(task['id'])
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if value.get('session_id') != task['session_id'] or value.get('payload_digest') != payload_digest(task) or value.get('catalog_id') != catalog_id:
            return None
        # Reclaimed work may carry a fresh lease. Reuse the completed calculation,
        # not the old lease, and persist it before attempting acknowledgement.
        value.update(lease_token=task['lease_token'], lease_expires_at=task.get('lease_expires_at'), worker_id=self.worker_id, state='ready')
        atomic_json(path, value)
        return value

    def save(self, task, catalog_id, result, error):
        value = {key: task[key] for key in ('id', 'kind', 'session_id', 'lease_token')}
        value.update(payload=copy.deepcopy(task['payload']), lease_expires_at=task.get('lease_expires_at'), worker_id=self.worker_id, payload_digest=payload_digest(task), catalog_id=catalog_id, result=result, error=error, state='ready', saved_at=time.time())
        atomic_json(self.path(task['id']), value)
        return value

    def mark(self, value, state):
        value = dict(value, state=state)
        atomic_json(self.path(value['id']), value)

    def pending(self):
        for path in sorted(self.directory.glob('*.json')):
            if not TASK_ID.fullmatch(path.stem):
                continue
            try:
                value = json.loads(path.read_text())
                if value.get('worker_id') == self.worker_id and value.get('state') == 'ready':
                    require_id(value.get('id'), TASK_ID)
                    require_id(value.get('session_id'), SESSION_ID)
                    require_id(value.get('lease_token'), LEASE_TOKEN)
                    yield value
            except (OSError, ValueError, TaskInputError):
                LOGGER.warning('checkpoint_unreadable')


class LeaseHeartbeat:
    def __init__(self, cloud, task, interval=20):
        self.cloud, self.task, self.interval = cloud, task, interval
        self.stop_event = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self.run, name='coursematch-lease-heartbeat', daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=65)

    def run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.cloud.heartbeat(self.task)
            except (LeaseLost, TaskGone):
                self.lost.set()
                return
            except CloudError as error:
                LOGGER.warning('lease_heartbeat_retry status=%s', error.status)


SOURCE_FIELDS = {'id','name','code','hours','university','institution','context','context_id','topics','source_sha256','catalog_course_id','upload_pdf','warnings','status','reviewed','source_note','extraction_method','extractor','reading_note','transcription_model','transcription_model_digest','transcription_models','page_proof','excluded_rows','source_text_blocks','fixed_topic_curriculum','source_context','library_only','match_status','reference_review','reference_quality','source_media_kind','original_filename','visual_verification_status','visual_verification','visual_verification_proof','visual_verification_method','visual_verification_cache_key','visual_response_proof','visual_spatial_proof','image_proof','extraction_integrity'}


def public_source(value, *, catalog_pdf=None):
    result = {key: copy.deepcopy(value[key]) for key in SOURCE_FIELDS if key in value}
    if catalog_pdf:
        result['source_pdf'] = catalog_pdf
    for key in ('warnings', 'source_note', 'reading_note'):
        values = result.get(key)
        if values is None:
            continue
        def safe(text):
            text = str(text)
            return 'تحتاج بيانات المصدر إلى مراجعة.' if re.search(r'/Users/|/home/|[A-Za-z]:\\|/private/|/tmp/', text) else text[:1000]
        result[key] = [safe(text) for text in values] if isinstance(values, list) else safe(values)
    return result


class Processor:
    def __init__(self, root=ROOT, runtime=None, model='qwen3.8:27b-q8_0'):
        self.root = Path(root).resolve()
        self.runtime = private_directory(runtime or self.root / 'runtime/cloud-worker').resolve()
        self.model = model
        self.catalog = json.loads((self.root / 'equivalency-pilot/catalog.json').read_text())
        self.courses = {course['id']: course for course in self.catalog['courses']}
        self.sources = {**self.courses, **{course['id']: course for course in self.catalog.get('library_sources', [])}}
        self.verify_catalog()

    def verify_catalog(self):
        for name, expected in self.catalog['source_hashes'].items():
            path = (self.root / name).resolve()
            if not path.is_relative_to(self.root) or not path.is_file() or digest(path) != expected:
                raise TaskInputError('تغيرت مكتبة التوصيفات؛ يلزم تحديث العامل قبل متابعة المعادلة.')

    def course_path(self, course):
        url = course.get('source_pdf')
        if not isinstance(url, str):
            raise TaskInputError('لا يتوافر ملف توصيف للمقرر المحدد.')
        if Path(url).is_absolute() or '..' in Path(url).parts or '\\' in url:
            raise TaskInputError('مرجع التوصيف في المكتبة غير صالح.')
        path = (self.root / url).resolve()
        if not path.is_relative_to(self.root / 'assets') or not path.is_file() or digest(path) != course.get('source_sha256'):
            raise TaskInputError('تغير ملف توصيف المقرر؛ يلزم تحديث المكتبة قبل المقارنة.')
        return path

    def session_directory(self, session_id):
        directory = self.runtime / 'sessions' / require_id(session_id, SESSION_ID)
        if not directory.resolve().is_relative_to(self.runtime):
            raise TaskInputError('مرجع جلسة المستفيد غير صالح.')
        return private_directory(directory)

    def save_pdf(self, task, source_id, cloud, expected=None, *, role='source'):
        source_id = require_id(source_id, TASK_ID)
        directory = private_directory(self.session_directory(task['session_id']) / 'sources')
        path = directory / (source_id + '.pdf')
        if path.is_symlink():
            raise TaskInputError('مرجع ملف التوصيف غير صالح.')
        if path.is_file() and expected and digest(path) == expected:
            return path
        raw = cloud.input_pdf(task, role='target') if role == 'target' else cloud.input_pdf(task)
        if len(raw) > PDF_LIMIT or not raw.startswith(b'%PDF-'):
            raise TaskInputError('يلزم ملف PDF صالح لا يزيد عن 12 ميغابايت.')
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            raise TaskInputError('بصمة التوصيف لا تطابق المصدر المحفوظ.')
        import pymupdf
        try:
            with pymupdf.open(stream=raw, filetype='pdf') as document:
                if document.needs_pass or document.page_count > 40:
                    raise TaskInputError('يلزم توصيف غير محمي بكلمة مرور، ولا يزيد عن 40 صفحة.')
        except TaskInputError:
            raise
        except Exception:
            raise TaskInputError('تعذر فتح ملف التوصيف المرفوع.') from None
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        return path

    def existing_input(self, task, cloud, *, role='source'):
        payload = task['payload']
        if role not in ('source','target'):
            raise TaskInputError('نوع المقرر غير صالح.')
        uploaded_target = role == 'target'
        if uploaded_target and (payload.get('target_mode') != 'uploaded' or 'target_id' in payload):
            raise TaskInputError('نوع المقرر الهدف لا يطابق المهمة.')
        value = payload.get(role)
        label = 'المقرر الهدف' if uploaded_target else 'المقرر المصدر'
        if not isinstance(value, dict):
            raise TaskInputError(label + ' غير موجود في هذه الجلسة.')
        source_id = require_id(value.get('id'), TASK_ID)
        if require_id(payload.get('target_source_id' if uploaded_target else 'source_id'), TASK_ID) != source_id:
            raise TaskInputError('هوية ' + label + ' لا تطابق المهمة.')
        if value.get('session_id', task['session_id']) != task['session_id']:
            raise TaskInputError(label + ' لا ينتمي إلى جلسة المستفيد.')
        if uploaded_target and (not value.get('upload_pdf') or value.get('catalog_course_id') or value.get('source_pdf')):
            raise TaskInputError('يلزم ملف توصيف مرفوع للمقرر الهدف.')
        trusted = None
        if value.get('catalog_course_id'):
            trusted = self.sources.get(value['catalog_course_id'])
            if trusted is None or value.get('source_sha256') != trusted.get('source_sha256'):
                raise TaskInputError('مرجع توصيف المصدر لم يعد مطابقًا للمكتبة.')
            self.course_path(trusted)
        elif value.get('source_pdf'):
            raise TaskInputError('لا يقبل العامل مسارات ملفات من بيانات المستفيد.')
        source = public_source(value, catalog_pdf=trusted.get('source_pdf') if trusted else None)
        # Academic readiness is canonical: a saved/session source must not strip
        # a curator hold or introduce student-supplied approval metadata.
        for field in ('reference_review','reference_quality'):
            source.pop(field,None)
            if trusted and field in trusted: source[field]=copy.deepcopy(trusted[field])
        try:
            source['topics'] = clean_topics(source.get('topics', []), source.get('topics', []))
        except (TypeError, ValueError, KeyError):
            raise TaskInputError('مفردات المقرر المصدر غير صالحة.') from None
        path = None
        if source.get('upload_pdf') or value.get('has_pdf'):
            expected = require_id(source.get('source_sha256'), SESSION_ID, 'بصمة التوصيف المرفوع غير صالحة.')
            path = self.save_pdf(task, source_id, cloud, expected, **({'role':'target'} if uploaded_target else {}))
            source['upload_pdf'] = True
        return source, trusted, path


    def existing_source(self, task, cloud):
        return self.existing_input(task, cloud)

    def comparison_target(self, task, cloud):
        payload = task['payload']
        if payload.get('target_mode') == 'uploaded':
            target, _, path = self.existing_input(task, cloud, role='target')
            expected = [{'id':t['id'],'text':t.get('corrected_text') or t['text']} for t in target['topics']]
            supplied = payload.get('target_topics', expected)
            if supplied != expected:
                raise TaskInputError('مفردات الهدف المرفوع ثابتة وفق التوصيف المحفوظ.')
            return target, path
        if payload.get('target_mode') not in (None,'catalog') or 'target_source_id' in payload:
            raise TaskInputError('نوع المقرر الهدف غير صالح.')
        if payload.get('target_id') not in self.courses:
            raise TaskInputError('المقرر الهدف غير موجود في البرنامج والخطة المحددين.')
        target = copy.deepcopy(self.courses[payload['target_id']])
        self.course_path(target)
        return target, None

    def comparison_hold(self, target, source, *, uploaded=False):
        reference = target
        if uploaded:
            anchor, problem = uploaded_target_reference(target, self.sources.values())
            if problem:
                return {'provider':'reference_pending','pairs':[],'rubric':RUBRIC_VERSION,'reference_review':{'target':problem}}
            if anchor is not None:
                self.course_path(anchor)
                reference = anchor
        return reference_pending(reference, source, RUBRIC_VERSION)

    def process(self, task, cloud):
        validate_task(task)
        self.verify_catalog()
        payload = task['payload']
        if payload.get('catalog_version', self.catalog['catalog_id']) != self.catalog['catalog_id']:
            raise TaskInputError('تغير إصدار مكتبة المقررات؛ أعد تحميل الصفحة قبل المقارنة.')
        directory = self.session_directory(task['session_id'])
        if task['kind'] == 'source':
            if payload.get('catalog_id'):
                original = self.sources.get(payload['catalog_id'])
                if original is None:
                    raise TaskInputError('المقرر المصدر غير موجود في المكتبة.')
                self.course_path(original)
                source = public_source(original, catalog_pdf=original.get('source_pdf'))
                source['catalog_course_id'] = original['id']
            elif payload.get('has_pdf'):
                path = self.save_pdf(task, task['id'], cloud)
                try:
                    from scripts.equivalency.image_input import extract_image_pdf, verify_pdf_topics
                    extraction_cache = private_directory(directory / 'extraction')
                    if payload.get('input_kind') == 'image':
                        source = extract_image_pdf(path, extraction_cache)
                    else:
                        # A byte-identical, visually approved reference already
                        # has stronger evidence than repeating model OCR.
                        approved = next((c for c in self.sources.values()
                                         if c.get('source_sha256') == digest(path)
                                         and c.get('reference_quality', {}).get('state') == 'ready_visual'
                                         and reference_problem(c) is None), None)
                        if approved is not None:
                            self.course_path(approved)
                            source = {'source_sha256':digest(path),'topics':copy.deepcopy(approved['topics']),
                                      'excluded_rows':copy.deepcopy(approved.get('excluded_rows', [])),
                                      'reviewed':False,'warnings':[], 'status':'extracted',
                                      'extraction_method':'reviewed_visual_transcription',
                                      'visual_verification_status':'completed',
                                      'visual_verification':{'method':'verified_reference_sha256','source_sha256':digest(path)}}
                        else:
                            try:
                                source = apply_readings(extract_cached(path, extraction_cache, vision=True), self.root)
                            except Exception:
                                # A vision outage must not erase a usable native
                                # extraction or prevent the bounded verifier.
                                source = apply_readings(extract_cached(path, extraction_cache, vision=False), self.root)
                            source = verify_pdf_topics(path, source, extraction_cache)
                except Exception:
                    raise TaskInputError('تعذرت قراءة محتوى الملف المرفوع.') from None
                source['extraction_integrity'] = extraction_integrity(source)
                source = public_source(source)
                source.update(name=str(payload.get('name') or 'المقرر المرفوع')[:200], upload_pdf=True,
                              source_media_kind='image' if payload.get('input_kind') == 'image' else 'pdf')
                if payload.get('input_kind') == 'image':
                    source['original_filename'] = str(payload.get('original_filename') or '')[:200]
                if source.get('source_sha256') != digest(path):
                    raise TaskInputError('تغير ملف التوصيف أثناء استخراج المفردات.')
            else:
                try:
                    source = {'name': str(payload.get('name') or 'المقرر المدخل')[:200], 'topics': clean_topics(payload.get('topics', [])), 'reviewed': False, 'warnings': [], 'status': 'manual'}
                except (TypeError, ValueError, KeyError):
                    raise TaskInputError('مفردات المقرر المدخل غير صالحة.') from None
            try:
                source['topics'] = clean_topics(source.get('topics', []), source.get('topics', []))
            except (TypeError, ValueError, KeyError):
                raise TaskInputError('مفردات التوصيف غير صالحة أو تتجاوز 100 مفردة؛ راجع تقسيمها.') from None
            source['id'] = task['id']
            self.verify_catalog()
            return source
        source, trusted_source, source_pdf = self.existing_source(task, cloud)
        target_pdf = None
        try:
            if task['kind'] == 'search':
                source['topics'] = clean_topics(payload.get('topics', source['topics']), source['topics'])
                result = search(self.catalog, source, payload.get('version', '47'), context_id=payload.get('context_id'), context_ids=payload.get('context_ids'), all_contexts=payload.get('all_contexts', False), limit=payload.get('limit', 5))
                result = dict(result, candidates=[dict(public_source(candidate, catalog_pdf=candidate.get('source_pdf')), **({'available_contexts': copy.deepcopy(candidate['available_contexts'])} if 'available_contexts' in candidate else {})) for candidate in result['candidates']])
            else:
                if payload.get('reviewed') is not True:
                    raise TaskInputError('يلزم تأكيد مراجعة مفردات المقررين قبل إصدار التقرير.')
                target, target_pdf = self.comparison_target(task, cloud)
                source['topics'] = clean_topics(payload.get('source_topics', source['topics']), source['topics'])
                target['topics'] = clean_topics(payload.get('target_topics', target['topics']), target['topics'])
                beneficiary = clean_beneficiary(payload.get('beneficiary'))
                evidence = self.comparison_hold(target, trusted_source or source, uploaded=payload.get('target_mode') == 'uploaded')
                if evidence is None:
                    try:
                        evidence = compare(target, source, model=self.model, cache_dir=private_directory(directory / 'comparisons'))
                    except EvidenceValidationError as error:
                        evidence = audit_incomplete(error,RUBRIC_VERSION)
                if evidence['pairs']:
                    validate_evidence(evidence,target['topics'],source['topics'])
                result = {'id': task['id'], 'source': source, 'target': public_source(target, catalog_pdf=target.get('source_pdf')), 'evidence': evidence, 'calculation': evidence_calculation(target['topics'], source['topics'], evidence), 'beneficiary': beneficiary, 'created_at': datetime.now(timezone.utc).isoformat(), 'catalog_id': self.catalog['catalog_id'], 'reviewed_by': 'automated_pipeline' if payload.get('review_mode') == 'automatic' else 'user_confirmed_extraction', 'human_reviewed': payload.get('review_mode') != 'automatic'}
                result['target_mode'] = 'uploaded' if payload.get('target_mode') == 'uploaded' else 'catalog'
                if target_pdf is None: self.course_path(target)
        except TaskInputError:
            raise
        except Exception:
            label = 'البحث عن المقررات' if task['kind'] == 'search' else 'تدقيق توافق المفردات'
            raise TaskInputError('تعذر استكمال ' + label + '؛ أعد المحاولة لاحقًا.') from None
        self.verify_catalog()
        if trusted_source:
            self.course_path(trusted_source)
        if target_pdf and digest(target_pdf) != target.get('source_sha256'):
            raise TaskInputError('تغير توصيف المقرر الهدف أثناء معالجة المهمة.')
        if source_pdf and digest(source_pdf) != source.get('source_sha256'):
            raise TaskInputError('تغير التوصيف المرفوع أثناء معالجة المهمة.')
        return result


    def verify_checkpoint(self, value, cloud):
        self.verify_catalog()
        if value.get('catalog_id') != self.catalog['catalog_id']:
            raise TaskInputError('تغير إصدار مكتبة المقررات؛ أعد تحميل الصفحة قبل المقارنة.')
        if value.get('error'):
            return
        if value['kind'] == 'source':
            source = value['result']
            if source.get('catalog_course_id'):
                course = self.sources.get(source['catalog_course_id'])
                if course is None:
                    raise TaskInputError('المقرر المصدر غير موجود في المكتبة.')
                self.course_path(course)
            elif source.get('upload_pdf'):
                self.save_pdf(value, source['id'], cloud, source['source_sha256'])
        else:
            input_source, _, _ = self.existing_source(value, cloud)
            if value['kind'] == 'compare':
                target, target_pdf = self.comparison_target(value, cloud)
                # A completed but unacknowledged task may survive a code/rubric
                # upgrade. File integrity alone does not make its old evidence
                # valid under today's rules. Never regrade a saved report here;
                # reject it safely so the beneficiary can request a new study.
                try:
                    report = value['result']
                    evidence = report['evidence']
                    if (evidence['rubric'] != RUBRIC_VERSION
                            or report['id'] != value['id']
                            or report['catalog_id'] != self.catalog['catalog_id']
                            or report['source']['id'] != value['payload']['source_id']
                            or report['target']['id'] != target['id']):
                        raise ValueError('obsolete_or_mismatched_checkpoint')
                    targets, sources = report['target']['topics'], report['source']['topics']
                    if value['payload'].get('target_mode') == 'uploaded':
                        effective = lambda rows: [(t['id'],t.get('corrected_text') or t['text']) for t in rows]
                        if (report.get('target_mode') != 'uploaded'
                                or report['target'].get('source_sha256') != target.get('source_sha256')
                                or effective(targets) != effective(target['topics'])):
                            raise ValueError('uploaded_target_checkpoint_changed')
                    pairs = evidence['pairs']
                    if not isinstance(pairs, list):
                        raise ValueError('invalid_checkpoint_pairs')
                    if pairs:
                        validate_evidence(evidence, targets, sources)
                    if evidence.get('provider')=='audit_incomplete':
                        effective=lambda rows:[(t['id'],t.get('corrected_text') or t['text']) for t in rows]
                        expected_sources=clean_topics(value['payload'].get('source_topics',input_source['topics']),input_source['topics'])
                        expected_targets=clean_topics(value['payload'].get('target_topics',target['topics']),target['topics'])
                        if (effective(sources)!=effective(expected_sources) or effective(targets)!=effective(expected_targets)
                                or report['source'].get('source_sha256')!=input_source.get('source_sha256')
                                or report['target'].get('source_sha256')!=target.get('source_sha256')):
                            raise ValueError('incomplete_evidence_inputs_changed')
                    # Empty/unresolved topics legitimately return insufficient
                    # with no pairs; calculate owns that deterministic policy.
                    pending=self.comparison_hold(target,self.sources.get(report['source'].get('catalog_course_id')) or report['source'],uploaded=value['payload'].get('target_mode') == 'uploaded')
                    if pending is not None and evidence!=pending:
                        raise ValueError('reference_readiness_changed')
                    if evidence.get('provider')=='reference_pending' and evidence!=pending:
                        raise ValueError('invalid_reference_hold')
                    if evidence_calculation(targets, sources, evidence) != report['calculation']:
                        raise ValueError('checkpoint_calculation_changed')
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise TaskInputError('لم تعد نتيجة المقارنة المحفوظة صالحة للتحقق الحالي؛ أعد المقارنة لإصدار تقرير جديد.') from None


class Worker:
    def __init__(self, config, root=ROOT, runtime=None, cloud=None, processor=None, heartbeat_interval=20, lane='all'):
        if lane not in LANES:
            raise ValueError('invalid_worker_lane')
        self.lane = lane
        if lane != 'all':
            suffix = '-' + lane
            base = config.worker_id
            if len(base) + len(suffix) > 64:
                base = base[:64-len(suffix)-9] + '-' + hashlib.sha256(base.encode()).hexdigest()[:8]
            config = replace(config, worker_id=base + suffix)
        self.config = config
        directory = 'cloud-worker-interactive' if lane == 'interactive' else 'cloud-worker'
        self.runtime = private_directory(runtime or Path(root) / 'runtime' / directory).resolve()
        self.cloud = cloud or CloudClient(config, lane=lane)
        self.processor = processor or Processor(root, self.runtime, config.model)
        self.checkpoints = Checkpoints(self.runtime / 'checkpoints', config.worker_id)
        self.heartbeat_interval = heartbeat_interval
        self.last_idle_health = float('-inf')
        self.last_prune = float('-inf')

    def acknowledge(self, value):
        if hasattr(self.processor, 'verify_checkpoint'):
            try:
                self.processor.verify_checkpoint(value, self.cloud)
            except TaskInputError as rejected:
                value = dict(value, result=None, error=str(rejected))
                atomic_json(self.checkpoints.path(value['id']), value)
        try:
            self.cloud.complete(value, result=value.get('result'), error=value.get('error'))
        except LeaseLost:
            self.checkpoints.mark(value, 'waiting_for_lease')
            LOGGER.warning('completion_lease_lost')
            return False
        except TaskGone:
            self.checkpoints.mark(value, 'expired')
            LOGGER.warning('completion_task_expired')
            return False
        except CloudError as rejected:
            if rejected.status not in (400, 413, 422):
                raise
            value = dict(value, result=None, error='تعذر حفظ نتيجة المهمة؛ أعد المحاولة بعد مراجعة المدخلات.')
            atomic_json(self.checkpoints.path(value['id']), value)
            self.cloud.complete(value, result=None, error=value['error'])
        self.checkpoints.mark(value, 'acknowledged')
        LOGGER.info('task_completed')
        return True

    def recover_pending(self):
        for value in self.checkpoints.pending():
            self.acknowledge(value)

    def process_task(self, task):
        validate_task(task)
        if task['kind'] not in LANES[self.lane]:
            raise TaskInputError('نوع المهمة لا يطابق مسار العامل.')
        with LeaseHeartbeat(self.cloud, task, self.heartbeat_interval) as heartbeat:
            value = self.checkpoints.find(task, self.processor.catalog['catalog_id'])
            if value is None:
                try:
                    result, error = self.processor.process(task, self.cloud), None
                except (CloudError, LeaseLost, TaskGone):
                    raise
                except TaskInputError as rejected:
                    result, error = None, str(rejected)
                except Exception:
                    result, error = None, 'تعذر معالجة المهمة؛ أعد المحاولة لاحقًا.'
                value = self.checkpoints.save(task, self.processor.catalog['catalog_id'], result, error)
            if heartbeat.lost.is_set():
                self.checkpoints.mark(value, 'waiting_for_lease')
                LOGGER.warning('task_waiting_for_new_lease')
                return False
            return self.acknowledge(value)

    def prune_expired(self):
        if time.monotonic() - self.last_prune < 3600:
            return
        cutoff = time.time() - 86_400
        for path in self.checkpoints.directory.glob('*.json'):
            if TASK_ID.fullmatch(path.stem) and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
        sessions = self.runtime / 'sessions'
        if sessions.is_dir() and not sessions.is_symlink():
            for directory in sessions.iterdir():
                if not SESSION_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                    continue
                try:
                    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                except OSError:
                    continue
                try:
                    # Work relative to open directories, never through a symlink.
                    # A recent file must not keep older PDFs/caches indefinitely.
                    for _, directories, files, parent in os.fwalk('.', topdown=False, follow_symlinks=False, dir_fd=descriptor):
                        for name in files:
                            try:
                                details = os.stat(name, dir_fd=parent, follow_symlinks=False)
                                if stat.S_ISREG(details.st_mode) and details.st_mtime < cutoff:
                                    os.unlink(name, dir_fd=parent)
                            except FileNotFoundError:
                                pass
                        for name in directories:
                            try:
                                os.rmdir(name, dir_fd=parent)
                            except OSError:
                                # Nonempty directories and symlinks stay intact.
                                pass
                finally:
                    os.close(descriptor)
                try:
                    directory.rmdir()
                except OSError:
                    pass
        self.last_prune = time.monotonic()

    def run_once(self):
        self.prune_expired()
        self.recover_pending()
        if time.monotonic() - self.last_idle_health > 30:
            self.cloud.idle_heartbeat()
            self.last_idle_health = time.monotonic()
        task = self.cloud.claim()
        if task is None:
            return False
        return self.process_task(task)

    def run(self, stop=None, poll=4):
        stop = stop or threading.Event()
        backoff = poll
        while not stop.is_set():
            try:
                active = self.run_once()
                backoff = poll
                if active:
                    continue
            except (CloudError, TaskInputError) as error:
                LOGGER.warning('cloud_retry type=%s status=%s', type(error).__name__, getattr(error, 'status', 0))
                backoff = min(60, max(poll, backoff * 2))
            stop.wait(backoff)


@contextmanager
def singleton_lock(path):
    path = Path(path)
    private_directory(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another_worker_is_running') from None
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode())
        yield
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description='CourseMatch outbound-only cloud worker')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env.worker')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--lane', choices=tuple(LANES), default='all')
    parser.add_argument('--poll', type=float, default=4)
    arguments = parser.parse_args(argv)
    if not 3 <= arguments.poll <= 5:
        parser.error('--poll must be between 3 and 5 seconds')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        config = Config.from_file(arguments.env_file)
        worker = Worker(config, lane=arguments.lane)
        with singleton_lock(worker.runtime / 'worker.lock'):
            if arguments.once:
                worker.run_once()
            else:
                worker.run(poll=arguments.poll)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        # No raw exception, environment value, request body or local path is logged.
        LOGGER.error('worker_stopped type=%s status=%s', type(error).__name__, getattr(error, 'status', 0))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
