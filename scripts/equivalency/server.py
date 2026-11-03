"""Loopback-only pilot. Uploaded files and model traces never enter the site build."""
from __future__ import annotations

import base64
import json
import secrets
import threading
import traceback
import tempfile
import pymupdf
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .engine import RUBRIC_VERSION, calculate, compare, search, topic_text
from .reference_quality import reference_pending, evidence_calculation
from .extract import ROOT, digest, extract_pdf, extract_cached, apply_readings


def clean_beneficiary(value=None):
    """Optional display metadata; identifiers remain strings, including zeroes."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("بيانات المستفيد يجب أن تكون اسمًا ورقمًا نصيين")
    beneficiary = {}
    for key, label, maximum in (("name", "اسم المستفيد", 160), ("number", "رقم المستفيد", 60)):
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) > maximum:
            raise ValueError(f"{label} يجب أن يكون نصًا لا يزيد عن {maximum} حرفًا")
        if any(ord(character) < 32 or ord(character) == 127 for character in text):
            raise ValueError(f"{label} يحتوي على محارف غير مقبولة")
        beneficiary[key] = text.strip()
    return beneficiary


def clean_topics(rows, original=None):
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("عدد المفردات يجب أن يكون بين 1 و100")
    old = {t["id"]: t for t in original or []}
    topics, ids = [], set()
    for i, row in enumerate(rows, 1):
        tid = str(row.get("id", f"t{i}"))
        text = str(row.get("corrected_text") or row.get("text", "")).strip()
        if not text or len(text) > 4000 or tid in ids:
            raise ValueError("نص مفردة فارغ أو طويل أو معرف مكرر")
        if tid in old:
            item = dict(old[tid])
            if text == item.get("corrected_text"):
                pass  # Preserve the provenance of an unchanged contextual reading.
            elif text != item["text"]:
                item.update(corrected_text=text, correction_method="user_review", correction_note="تصحيح أو استكمال في شاشة مراجعة الاستخراج")
            else:
                item.pop("corrected_text", None)
        else:
            item = {"id": tid, "text": text, "page": None, "origin": "manual"}
        topics.append(item)
        ids.add(tid)
    return topics


class Pilot:
    def __init__(self, root=ROOT, model="qwen3.8:27b-q8_0"):
        self.root, self.model = Path(root), model
        self.catalog = json.loads((self.root / "equivalency-pilot/catalog.json").read_text())
        self.courses = {c["id"]: c for c in self.catalog["courses"]}
        self.sources = {**self.courses, **{c['id']:c for c in self.catalog.get('library_sources',[])}}
        self.runtime = self.root / "runtime/equivalency"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.verify_catalog()

    def verify_catalog(self):
        for name, sha in self.catalog["source_hashes"].items():
            if digest(self.root / name) != sha:
                raise ValueError("تغيرت بيانات الموقع؛ أعد بناء فهرس المفردات قبل إصدار أحكام")

    def verify_course(self, course):
        self.verify_catalog()
        if course.get("source_pdf") and digest(self.root / course["source_pdf"]) != course["source_sha256"]:
            raise ValueError("تغير ملف التوصيف؛ أعد بناء الفهرس")

    def path(self, category, key, suffix="json"):
        if not isinstance(key, str) or not key.isalnum() or len(key) != 32:
            raise ValueError("معرف غير صالح")
        path = self.runtime / category / (key + "." + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def source(self, key):
        return json.loads(self.path("sources", key).read_text())

    def save_report(self, report):
        path = self.path("reports", report["id"])
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            temporary = Path(stream.name)
        temporary.replace(path)

    def update_beneficiary(self, key, data):
        beneficiary = clean_beneficiary(data.get("beneficiary"))
        path = self.path("reports", key)
        job = self.jobs.get(key)
        if job and job["status"] != "done":
            raise ValueError("انتظر اكتمال التقرير قبل تحديث بيانات المستفيد")
        if not path.is_file():
            raise ValueError("التقرير المكتمل غير موجود")
        # Load the saved report as authority, including legacy reports without
        # beneficiary metadata. Topic evidence and calculations stay untouched.
        report = json.loads(path.read_text())
        report["beneficiary"] = beneficiary
        self.save_report(report)
        if job:
            job["result"] = report
        return report

    def create_source(self, data):
        key = secrets.token_hex(16)
        if data.get("catalog_id"):
            source = dict(self.sources[data["catalog_id"]])
            self.verify_course(source)
            source["catalog_course_id"] = source["id"]
            if not source['topics'] and source.get('source_pdf') and source.get('fixed_topic_curriculum',True):
                cache=self.runtime/'extraction';cache.mkdir(parents=True,exist_ok=True)
                source.update(apply_readings(extract_cached(self.root/source['source_pdf'],cache,vision=True),self.root))
        elif data.get("pdf_base64"):
            raw = base64.b64decode(data["pdf_base64"], validate=True)
            if len(raw) > 12 * 1024 * 1024 or not raw.startswith(b"%PDF-"):
                raise ValueError("يلزم PDF صالح لا يزيد عن 12 ميغابايت")
            pdf = self.path("sources", key, "pdf")
            pdf.write_bytes(raw)
            with pymupdf.open(pdf) as document:
                if document.page_count > 40:
                    raise ValueError("التجربة تقبل توصيف مقرر واحد لا يزيد عن 40 صفحة")
            cache=self.runtime/'extraction'
            cache.mkdir(parents=True,exist_ok=True)
            from .image_input import extract_image_pdf, verify_pdf_topics
            if data.get('input_kind') == 'image':
                source = extract_image_pdf(pdf,cache)
                source['source_media_kind'] = 'image'
            else:
                source = apply_readings(extract_cached(pdf,cache,vision=True), self.root)
                source = verify_pdf_topics(pdf,source,cache)
            source.update(name=str(data.get("name") or "المقرر المرفوع")[:200], upload_pdf=True)
        else:
            source = {"name": str(data.get("name") or "المقرر المدخل")[:200], "topics": clean_topics(data.get("topics", [])),
                      "reviewed": False, "warnings": [], "status": "manual", "source_note": "مفردات أدخلها المستخدم؛ ليست مستخرجة من PDF"}
        source["id"] = key
        self.path("sources", key).write_text(json.dumps(source, ensure_ascii=False, indent=2))
        return source

    def job(self, data):
        beneficiary = clean_beneficiary(data.get("beneficiary"))
        source = self.source(data["source_id"])
        target = dict(self.courses[data["target_id"]])
        self.verify_course(target)
        if source.get("catalog_course_id"):
            self.verify_course(self.sources[source["catalog_course_id"]])
        if data.get("review_mode") != 'automatic' and data.get("reviewed") is not True:
            raise ValueError("راجع مفردات المقررين وأكد صحة النص قبل المقارنة")
        source["topics"] = clean_topics(data.get("source_topics", source["topics"]), source["topics"])
        target["topics"] = clean_topics(data.get("target_topics", target["topics"]), target["topics"])
        key = secrets.token_hex(16)
        self.jobs[key] = {"id": key, "status": "queued", "message": "بانتظار محرك المقارنة المحلي"}

        def run():
            try:
                self.jobs[key].update(status="running", message="تدقيق توافق المفردات بالنموذج المحلي")
                evidence = reference_pending(self.courses[data['target_id']], (self.sources[source['catalog_course_id']] if source.get('catalog_course_id') else source), RUBRIC_VERSION) or compare(target, source, model=self.model)
                result = {"id": key, "source": source, "target": target, "evidence": evidence,
                          "beneficiary": beneficiary, "created_at": datetime.now(timezone.utc).isoformat(),
                          "calculation": evidence_calculation(target["topics"], source["topics"], evidence),
                          "catalog_id": self.catalog["catalog_id"], "reviewed_by": "automated_pipeline" if data.get('review_mode') == 'automatic' else "user_confirmed_extraction", "human_reviewed": data.get('review_mode') != 'automatic',
                          "notice": "نتيجة تجريبية للمفردات وفق سياسة 80/80/60؛ درجات توافق المفردات تقدير آلي يحتاج اعتماد المختص. الساعات واللوائح خارج هذا الحكم."}
                self.verify_course(target)
                self.save_report(result)
                self.jobs[key].update(status="done", message="اكتملت المقارنة", result=result)
            except Exception as e:
                self.jobs[key].update(status="error", message=str(e))
                traceback.print_exc()
        self.executor.submit(run)
        return self.jobs[key]


def serve(root=ROOT, port=8799, model="qwen3.8:27b-q8_0"):
    pilot = Pilot(root, model)

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, content, mime="application/json; charset=utf-8"):
            if not isinstance(content, bytes):
                content = json.dumps(content, ensure_ascii=False).encode() if mime.startswith("application/json") else content.encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-src 'self'; object-src 'none'")
            self.end_headers()
            self.wfile.write(content)

        def valid_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {"error": "مضيف غير مصرح"})
            route = urlparse(self.path).path
            try:
                if route in ("/", "/image-upload.js", "/app.js", "/style.css"):
                    name = "index.html" if route == "/" else route[1:]
                    mime = {"index.html": "text/html", "image-upload.js":"text/javascript", "app.js": "text/javascript", "style.css": "text/css"}[name] + "; charset=utf-8"
                    return self.send(200, (pilot.root / "equivalency-pilot" / name).read_bytes(), mime)
                if route == "/api/catalog":
                    pilot.verify_catalog()
                    return self.send(200, {"catalog": pilot.catalog, "token": pilot.token, "model": pilot.model})
                if route.startswith("/api/jobs/"):
                    key = route.rsplit("/", 1)[-1]
                    job = pilot.jobs.get(key)
                    if not job and pilot.path("reports", key).exists():
                        job = {"id": key, "status": "done", "result": json.loads(pilot.path("reports", key).read_text())}
                    return self.send(200 if job else 404, job or {"error": "المهمة غير موجودة"})
                if route.startswith("/pdf/"):
                    key = route.rsplit("/", 1)[-1]
                    if key in pilot.sources:
                        course = pilot.sources[key]
                        pilot.verify_course(course)
                        path = pilot.root / course["source_pdf"]
                    else:
                        source = pilot.source(key)
                        path = pilot.root / source["source_pdf"] if source.get("source_pdf") else pilot.path("sources", key, "pdf")
                    return self.send(200, path.read_bytes(), "application/pdf")
                return self.send(404, {"error": "غير موجود"})
            except Exception as e:
                return self.send(400, {"error": str(e)})

        def do_POST(self):
            if not self.valid_host() or self.headers.get("X-Equivalency-Token") != pilot.token:
                return self.send(403, {"error": "طلب غير مصرح"})
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return self.send(403, {"error": "مصدر الطلب غير مصرح"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 17 * 1024 * 1024:
                    return self.send(413, {"error": "حجم الطلب غير مقبول"})
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("يلزم طلب بصيغة JSON object")
                route = urlparse(self.path).path
                if route == "/api/sources":
                    return self.send(200, pilot.create_source(data))
                if route == "/api/search":
                    pilot.verify_catalog()
                    source = pilot.source(data["source_id"])
                    source["topics"] = clean_topics(data.get("topics", source["topics"]), source["topics"])
                    return self.send(200, search(pilot.catalog, source, data.get("version", "47"),
                                                 context_id=data.get("context_id"), all_contexts=data.get("all_contexts", False),
                                                 context_ids=data.get("context_ids"), limit=data.get("limit", 5)))
                if route == "/api/jobs":
                    return self.send(202, pilot.job(data))
                parts = route.split("/")
                if len(parts) == 5 and parts[1:3] == ["api", "reports"] and parts[4] == "beneficiary":
                    return self.send(200, pilot.update_beneficiary(parts[3], data))
                return self.send(404, {"error": "غير موجود"})
            except Exception as e:
                return self.send(400, {"error": str(e)})

    print(f"Course equivalency pilot: http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
