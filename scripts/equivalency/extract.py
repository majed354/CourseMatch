"""Extract topic rows without asking a language model to invent missing text."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unicodedata
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pdfplumber

from scripts.extract_course_outcomes import logical_region_text

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = "القرآن وعلومه"
EXTRACTOR_VERSION = "topic-grid-v14"
ASSESSMENT_LABEL_PATTERN = (
    r'(?:(?:ا\s*ل\s*)?[اإ]\s*خ\s*ت\s*ب\s*ا\s*ر\s*(?:النصف(?:ي)?|نصف(?:ي)?|الفصل(?:ي)?|فصل(?:ي)?|النهائ(?:ي)?|نهائ(?:ي)?|النيائي|الدوري|دوري)'
    r'|(?:التقويم|التقييم)\s*(?:الأول|الثاني)'
    r'|تقييم\s*البحوث)'
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def norm(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u200e\u200f\u202a-\u202e\u2066-\u2069ـ]", "", value)
    return " ".join(value.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).split())


def compact(value):
    return re.sub(r"[\W_]", "", norm(value))


def mixed_assessment_reading(text):
    """An exam label may share a physical row with substantive curriculum."""
    cleaned, count = re.subn(ASSESSMENT_LABEL_PATTERN, '', text)
    if not count:
        return None
    cleaned = cleaned.strip(' +،:.-')
    if cleaned == text or len(compact(cleaned)) < 8:
        return None
    if compact(cleaned) in ('مراجعة', 'مراجعةعامة', 'مراجعةالمقرر', 'الاختبارالنهائي'):
        return None
    return cleaned


def pure_assessment_text(text):
    value = norm(text).strip(' .،:;-▪■')
    residual, exams = re.subn(ASSESSMENT_LABEL_PATTERN, '', value)
    residual, reviews = re.subn(r'(?:مراجعة|راجعة)\s*(?:عامة|المقرر|وتقييم)?', '', residual)
    return bool(exams or reviews) and not compact(residual)


def _prefer_numbered_grid(topics, grid):
    if not grid.get('topics'):
        return False
    if grid.get('additional_topic_sections') or len(grid['topics']) > len(topics):
        return True
    contaminated = any(pure_assessment_text(t.get('corrected_text') or t['text'])
        or compact(t.get('corrected_text') or t['text']) in ('المجموع', 'مجموع', 'الإجمالي') for t in topics)
    closed = bool(grid.get('table_evidence') and grid['table_evidence'][-1].get('total'))
    return bool(contaminated and closed)


def explicit_unit_topics(path):
    """Recover numbered units when a vector table fragments each printed line.

    Only explicit unit headings inside a verified topic column create rows;
    sub-bullets, exam matrices and teaching outcomes never create new units.
    """
    topics, active = [], None
    with pdfplumber.open(path) as document:
        for number, page in enumerate(document.pages, 1):
            for table in sorted(page.find_tables(), key=lambda t:t.bbox[1]):
                if table.bbox[2]-table.bbox[0] < page.width*.55:
                    continue
                header = next((c for row in table.rows[:4] for c in row.cells if c
                    and page.width*.38 < c[2]-c[0] < (table.bbox[2]-table.bbox[0])*.9
                    and compact(logical_region_text(page,c)) == 'قائمةالموضوعات'), None)
                if header:
                    active = {'x0':header[0], 'x1':header[2], 'left':table.bbox[0], 'page':number}
                    top = header[3]
                elif active and number == active['page']+1 and table.bbox[1] < 160 and abs(table.bbox[0]-active['left']) < 10:
                    top = table.bbox[1]
                else:
                    continue
                x0,x1=active['x0'],active['x1']
                cells=[c for row in table.rows for c in row.cells if c and c[1]>=top-1
                       and abs(c[0]-x0)<18 and abs(c[2]-x1)<18 and c[2]-c[0]>page.width*.38]
                anchors=[]
                for c in cells:
                    value=norm(logical_region_text(page,c))
                    if re.match(r'^الوحدة\s+\S',value) and not any(abs(c[1]-y)<2 for y in anchors):
                        anchors.append(c[1])
                anchors.sort()
                if not anchors:
                    active=None
                    continue
                totals=[c[1] for row in table.rows for c in row.cells if c and c[1]>anchors[-1]
                        and compact(logical_region_text(page,c)) == 'المجموع']
                bottom=min(totals) if totals else table.bbox[3]
                for y,end in zip(anchors,anchors[1:]+[bottom]):
                    box=[x0+.4,y+.2,x1-.4,end-.2]
                    text=norm(logical_region_text(page,box))
                    hours=norm(logical_region_text(page,(table.bbox[0]+.3,y+.2,x0-.3,end-.2)))
                    topics.append({'id':f't{len(topics)+1}','text':text,'page':number,'bbox':box,
                                   'hours_raw':hours,'row_basis':'explicit_printed_unit_heading'})
                active['page']=number
            page.close()
    if len(topics)<2:
        return None
    warnings=[]
    if any(not re.fullmatch(r'\d+(?:[.٫]\d+)?',t['hours_raw']) for t in topics):
        warnings.append('بعض ساعات الوحدات غير محسومة؛ راجع المصدر')
    return {'source_sha256':digest(path),'extractor':EXTRACTOR_VERSION,'topics':topics,
            'excluded_rows':[],'warnings':warnings,'reviewed':False,
            'status':'needs_review' if warnings else 'extracted_needs_review',
            'extraction_method':'explicit_unit_headings'}


def extract_pdf(path):
    """Read bounded topic cells; keep original rows, locations and excluded rows.

    Merged hour cells define physical rows. This avoids treating wrapped lines
    as different topics and stitches continuation tables on the following page.
    Unreadable or missing tables remain explicit, never model-completed.
    """
    path = Path(path)
    topics, excluded, warnings, sections = [], [], [], []
    active = None
    with pdfplumber.open(path) as document:
        if len(document.pages) > 40:
            raise ValueError("التجربة تقبل توصيف مقرر واحد لا يزيد عن 40 صفحة")
        for page_number, page in enumerate(document.pages, 1):
            tables = sorted(page.find_tables(), key=lambda t: (t.bbox[1], -(t.bbox[2] - t.bbox[0])))
            consumed = []
            for table in tables:
                if table.bbox[2] - table.bbox[0] < page.width * .55:
                    continue
                if any(abs(table.bbox[1] - y) < 2 for y in consumed):
                    continue
                header = None
                for row in table.rows[:3]:
                    for cell in row.cells:
                        if not cell or not page.width * .38 < cell[2] - cell[0] < (table.bbox[2] - table.bbox[0]) * .88:
                            continue
                        header_text = logical_region_text(page, cell)
                        if len(header_text) > 70:
                            continue
                        text = compact(header_text)
                        if any(h in text for h in ("الموضوعات", "موضوعات", "المضات", "الموضات", "الموضوع", "محتوىالمقرر")):
                            header = cell
                            break
                    if header:
                        break
                if header:
                    continuation = False
                    active = {"x0": header[0], "x1": header[2], "table_x0": table.bbox[0], "last_page": page_number}
                    body_top = header[3]
                    sections.append(page_number)
                elif active and page_number == active["last_page"] + 1 and table.bbox[1] < 160 and abs(table.bbox[0] - active["table_x0"]) < 10:
                    continuation = True
                    # Only the first table on the immediate next page can continue.
                    body_top = table.bbox[1]
                    # Re-exported tables can move their inner content borders
                    # slightly on the next page. Re-anchor only a wide actual
                    # content cell near both former topic-column boundaries.
                    anchors = [c for row in table.rows[:2] for c in row.cells if c
                               and c[1] <= body_top + 2 and c[2] - c[0] > page.width * .38
                               and abs(c[0] - active['x0']) <= 18 and abs(c[2] - active['x1']) <= 18]
                    if anchors:
                        anchor = min(anchors, key=lambda c: abs(c[0] - active['x0']) + abs(c[2] - active['x1']))
                        active.update(x0=anchor[0], x1=anchor[2])
                    first = compact(logical_region_text(page, (active["x0"], body_top, active["x1"], min(body_top + 25, table.bbox[3]))))
                    if first in ("أنشطةالتقييم", "المراجع", "استراتيجياتالتدريس", "المرافق"):
                        active = None
                        continue
                else:
                    continue
                x0, x1 = active["x0"], active["x1"]
                spans = []
                for row in table.rows:
                    for c in row.cells:
                        if c and c[2] <= x0 + 1 and c[2] - c[0] > page.width * .07 and c[1] >= body_top - 1 and c[3] - c[1] > 7:
                            spans.append(c)
                # Some PDFs retain only the right-hand row-number cell while
                # the hour/content borders are broken. A bounded integer cell
                # in the known numbering column gives that row's real y-range;
                # read its hours from the existing left column, without making
                # up a row or borrowing text from another table.
                for row in table.rows:
                    for c in row.cells:
                        if (not c or c[0] < x1 - 1 or c[2] > table.bbox[2] + 1
                                or c[2] - c[0] > page.width * .09 or c[1] < body_top - 1 or c[3] - c[1] <= 7):
                            continue
                        number = norm(logical_region_text(page, c))
                        if not re.fullmatch(r"[0-9]{1,2}", number) or not 1 <= int(number) <= 99:
                            continue
                        if any(c[1] >= other[1] - 1 and c[3] <= other[3] + 1 for other in spans):
                            continue
                        hours_cell = (table.bbox[0], c[1], x0, c[3])
                        hours = norm(logical_region_text(page, hours_cell))
                        if re.fullmatch(r"[0-9٠-٩۰-۹]+(?:[.٫][0-9٠-٩۰-۹]+)?", hours) or (hours and len(hours) <= 40 and "سا" in compact(hours)):
                            spans.append(hours_cell)
                # Drop nested duplicates produced by borders and merged cells.
                rows = []
                for c in sorted(spans, key=lambda c: (c[1], -(c[3] - c[1]))):
                    if any(c[1] >= r[1] - 1 and c[3] <= r[3] + 1 for r in rows):
                        continue
                    rows.append(c)
                for c in rows:
                    bbox = [round(x0 + .6, 2), round(c[1] + .4, 2), round(x1 - .6, 2), round(c[3] - .4, 2)]
                    text = norm(logical_region_text(page, bbox))
                    hours_raw = norm(logical_region_text(page, c))
                    key = compact(text)
                    if not key:
                        continue
                    if key in ("المجموع", "مجموع", "اع", "وع", "المجمو", "اجمالي", "الإجمالي"):
                        active = None
                        break
                    if any(k in key for k in ("قائمةالم", "قاالم", "قاهم", "التدريسيةالمتوقعة")):
                        continue
                    record = {"text": text, "page": page_number, "bbox": bbox, "hours_raw": hours_raw}
                    mixed = mixed_assessment_reading(text)
                    if not mixed and len(text) < 85 and any(k in key for k in ("مراجعة", "راجعة", "اختبارنهائي", "الاختبارالنهائي", "اختبارفصلي", "الاختبارالفصلي", "اختبارنصفي", "الاختبارالنصفي")):
                        excluded.append({**record, "reason": "مراجعة أو اختبار؛ ليس مفردة موضوعية"})
                        continue
                    missing_hours_topic = False
                    if not hours_raw and continuation and topics and abs(c[1] - body_top) < 2 and len(key) >= 3:
                        if re.match(r"^[\W\d_]*الوحدة\b", text):
                            # An explicit unit heading starts a new physical
                            # topic even when the source left its hours blank.
                            missing_hours_topic = True
                            record['hours_status'] = 'missing_in_source'
                            warnings.append(f"صف موضوع مستقل بلا ساعات مطبوعة في الصفحة {page_number}؛ راجع تقسيم المفردات")
                        else:
                            previous = topics[-1]
                            previous.setdefault("fragments", [{"page": previous["page"], "bbox": previous["bbox"]}]).append({"page": page_number, "bbox": bbox})
                            previous["text"] += " " + text
                            previous["end_page"] = page_number
                            continue
                    # A references/equipment table on the next page is not a
                    # topic continuation. Its left cells contain prose, not hours.
                    numeric_hours = bool(re.fullmatch(r"[0-9٠-٩۰-۹]+(?:[.٫][0-9٠-٩۰-۹]+)?", hours_raw))
                    if not missing_hours_topic and (not hours_raw or not (numeric_hours or "سا" in compact(hours_raw)) or len(hours_raw) > 40):
                        active = None
                        break
                    if not mixed and len(text) < 85 and any(k in key for k in ("مراجعة", "راجعة", "اختبارنهائي", "الاختبارالنهائي", "اختبارفصلي", "الاختبارالفصلي", "اختبارنصفي", "الاختبارالنصفي")):
                        excluded.append({**record, "reason": "مراجعة أو اختبار؛ ليس مفردة موضوعية"})
                    elif len(key) < 3:
                        warnings.append(f"نص قصير غير محسوم في الصفحة {page_number}: {text}")
                    else:
                        corrected = mixed or text
                        if "البيع" in text:
                            corrected = corrected.replace("وشوطه", "وشروطه").replace("والشوط", "والشروط").replace("المرادبه", "المراد به")
                        if "الإجارة" in text and "بالشكة" in text:
                            corrected = corrected.replace("بالشكة", "بالشركة").replace("والمساقة", "والمساقاة")
                        correction = {} if corrected == text else {"corrected_text": corrected,
                            "correction_method": "contextual_inference", "correction_note": "استعادة حروف/مسافات من سياق أبواب فقه المعاملات بتفويض المستخدم؛ يبقى الأصل محفوظًا"}
                        if mixed:
                            correction.update(correction_method='mixed_assessment_subject',
                                correction_note='حُفظ صف المصدر المختلط؛ القراءة الموضوعية تفصل عبارة الاختبار عن المحتوى الفعلي')
                        topics.append({"id": f"t{len(topics) + 1}", **record, **correction, **({"mixed_assessment_row": True} if mixed else {})})
                if active:
                    active["last_page"] = page_number
                consumed.append(table.bbox[1])
            page.close()
    if topics:
        # A nonzero result is not proof of completeness. The independent vector
        # numbering column can retain full rows even when fragmented hour cells
        # made the older reader stop after the first row or page continuation.
        from .recovery import numbered_grid
        grid = numbered_grid(path)
        if _prefer_numbered_grid(topics, grid):
            grid['extractor'] = EXTRACTOR_VERSION
            grid['replaced_partial_extraction'] = {'topic_count': len(topics), 'topics': topics}
            return grid
    if not topics:
        # Existing physical-grid recovery preserves merged rows and page
        # fragments. It takes priority over the narrower unit-heading fallback.
        from .recovery import strict_grid, numbered_grid
        recovered_grid = strict_grid(path)
        numbered = numbered_grid(path)
        if numbered.get('topics') and numbered.get('additional_topic_sections'):
            numbered['extractor'] = EXTRACTOR_VERSION
            return numbered
        units = explicit_unit_topics(path)
        if units and len(units['topics']) > max(len(recovered_grid.get('topics', [])), len(numbered.get('topics', []))):
            return units
        if _prefer_numbered_grid(recovered_grid.get('topics', []), numbered):
            numbered['extractor'] = EXTRACTOR_VERSION
            numbered['replaced_partial_extraction'] = {'topic_count': len(recovered_grid.get('topics', [])), 'topics': recovered_grid.get('topics', [])}
            return numbered
        if recovered_grid.get('topics'):
            return recovered_grid
        recovered = units
        if recovered:
            return recovered
        warnings.append("لم يستخرج جدول موضوعات موثوق؛ يلزم فحص الصفحة أو إدخال المفردات يدويًا")
    # Contextual readings are a separate layer, never a replacement of PDF text.
    for topic in topics:
        text = topic.get("corrected_text", topic["text"])
        corrected = re.sub(r"\(cid:(?:13|127)\)", "", text)
        corrected = corrected.replace("\uf048", "صلى الله عليه وسلم").replace("\uf076", "").replace("\uf0b7", "")
        corrected = corrected.replace("المدلƒس", "المدلس").replace("المر سل الخفي", "المرسل الخفي")
        if len(corrected) > 85:
            corrected = re.sub(r"^\s*الاختبار النصفي\s*", "", corrected)
        if "(cid:" in text or re.search(r"[A-Z]{2,}", text) or any(c in text for c in ("\uf076", "\uf0b7")):
            corrected = re.sub(r"(?<=[\u0621-\u064a])\s*[NBTz]+\s*(?=[\u0621-\u064a])", "", corrected)
            corrected = corrected.replace("أصو هم", "أصولهم").replace("أهل ا السنة", "أهل السنة").replace("عند ا الصوفية", "عند الصوفية")
            corrected = corrected.replace("المع ˆل", "المعلل")
        corrected = " ".join(corrected.split())
        if corrected != topic["text"]:
            topic.update(corrected_text=corrected, correction_method=topic.get('correction_method', 'contextual_inference'),
                         correction_note=topic.get('correction_note', 'قراءة مستكملة من سياق الموضوع؛ أزيلت رموز امتداد/تشكيل تالفة وحفظ النص الخام ومواضع صفحاته'))
    if any("(cid:" in t.get("corrected_text", t["text"]) or "\ufffd" in t.get("corrected_text", t["text"]) for t in topics):
        warnings.append("يوجد نص غير مقروء في مفردات مستخرجة")
    return {"source_sha256": digest(path), "extractor": EXTRACTOR_VERSION,
            "topics": topics, "excluded_rows": excluded, "warnings": warnings,
            "status": "extracted_needs_review" if topics and not warnings else "needs_review",
            "reviewed": False}


def apply_readings(extracted, root=ROOT):
    path = root / "equivalency-pilot/topic-readings.json"
    if not path.exists():
        return extracted
    readings = json.loads(path.read_text())
    for table in readings.get('topic_tables',[]):
        if table['source_sha256']==extracted.get('source_sha256'):
            extracted=dict(extracted,topics=table['topics'],extraction_method=table['method'],reading_note=table['note'],
                           warnings=[table['note']],reviewed=False,status='extracted_needs_review')
            if table.get('no_fixed_topics'):
                extracted.update(fixed_topic_curriculum=False,source_text_blocks=table.get('source_text_blocks',[]),status='no_fixed_topics')
    # Both the PDF and the exact raw row must agree before applying a reading.
    for topic in extracted["topics"]:
        raw_hash = hashlib.sha256(topic["text"].encode()).hexdigest()
        for reading in readings.get("readings", []):
            if reading["source_sha256"] == extracted.get("source_sha256") and reading["raw_text_sha256"] == raw_hash:
                topic.update(corrected_text=reading["corrected_text"], correction_method=reading["method"], correction_note=reading["note"])
    return extracted


def effective_data(root=ROOT):
    # Reuse the actual overlay, including identity guards and excluded scopes.
    code = "const fs=require('fs');const path=require('path');const r=process.argv[1];const data=JSON.parse(fs.readFileSync(path.join(r,'data.json')));const edits=JSON.parse(fs.readFileSync(path.join(r,'site-edits.json')));process.stdout.write(JSON.stringify(require(path.join(r,'assets/js/site-edits.js')).apply(data,edits)));"
    return json.loads(subprocess.check_output(["node", "-e", code, str(root)]))


def matches(scope, context):
    return all(not norm(scope.get(k)) or norm(scope[k]) == norm(context.get(k)) for k in ("program", "degree", "plan_type", "version"))


def select_details(data, course, context):
    raw = data.get("course_details", {}).get(course["code"], {})
    variants = raw.get("variants", [raw])
    eligible = [v for v in variants if norm(v.get("title")) == norm(course["name"])
                and not any(matches(s, context) for s in v.get("excluded_scopes", []))]
    scopes = lambda v: v.get("scopes", [v["scope"]] if v.get("scope") else [])
    return next((v for v in eligible if any(matches(s, context) for s in scopes(v))),
                next((v for v in eligible if not scopes(v)), None))


def context_id(context):
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def extract_cached(path, cache_dir, vision=False):
    sha = digest(path)
    cached = Path(cache_dir) / f"{EXTRACTOR_VERSION}-{sha}.json"
    if cached.exists():
        extracted = json.loads(cached.read_text())
    else:
        try:
            extracted = extract_pdf(path)
        except Exception as e:
            extracted = {"source_sha256": sha, "topics": [], "status": "needs_review", "reviewed": False, "warnings": [str(e)]}
        cached.write_text(json.dumps(extracted, ensure_ascii=False, indent=2))
    if not extracted['topics']:
        from .recovery import VERSION, strict_grid, save_json, vision_topics, sanitize_vision
        recovered = Path(cache_dir) / f'{VERSION}-{sha}.json'
        if not recovered.exists():
            save_json(recovered,strict_grid(path))
        visions = sorted(Path(cache_dir).glob(f'{VERSION}-vision3-{sha}-*.json'), key=lambda p:p.stat().st_mtime)
        candidate = visions[-1] if visions else recovered if recovered.exists() else None
        if candidate:
            reading = json.loads(candidate.read_text())
            if 'vision3' in candidate.name:
                reading=sanitize_vision(reading,path,cache_dir)
            if reading.get('source_sha256') == sha and reading.get('topics'):
                extracted = reading
        if not extracted['topics'] and vision:
            reading=vision_topics(path,cache_dir)
            if reading and reading.get('topics'):
                extracted=reading
    return extracted


def build_catalog(output, root=ROOT, workers=3):
    data = effective_data(root)
    cache_dir = root / "runtime/equivalency/extraction"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Extract each physical source once. Processes only read PDFs and write
    # distinct private hash-cache files; the coordinator is the sole catalog writer.
    sources = {}
    for p in data["programs"]:
        context = {"program": p["name"], "degree": p["degree"], "plan_type": p["plan_type"], "version": str(p["version"])}
        for course in p["courses"]:
            details = select_details(data, course, context)
            if details and details.get("pdf_url"):
                path = (root / details["pdf_url"]).resolve()
                if path.is_relative_to((root / "assets").resolve()) and path.is_file():
                    sha = digest(path)
                    if not (cache_dir / f"{EXTRACTOR_VERSION}-{sha}.json").exists():
                        sources[sha] = str(path)
    for details in data.get('course_details',{}).values():
        for variant in details.get('variants',[details]):
            if variant.get('pdf_url'):
                path=(root/variant['pdf_url']).resolve()
                if path.is_relative_to((root/'assets').resolve()) and path.is_file():
                    sha=digest(path)
                    if not (cache_dir/f'{EXTRACTOR_VERSION}-{sha}.json').exists():
                        sources[sha]=str(path)
    if sources:
        print(f"Extracting {len(sources)} additional source PDFs with {workers} workers", flush=True)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(extract_cached, path, str(cache_dir)): path for path in sources.values()}
            for i, future in enumerate(as_completed(futures), 1):
                result = future.result()
                print(f"PDF {i}/{len(futures)} {Path(futures[future]).name}: {len(result['topics'])} topics", flush=True)
    courses = []
    contexts = []
    for p in data["programs"]:
        context = {"program": p["name"], "degree": p["degree"], "plan_type": p["plan_type"], "version": str(p["version"])}
        cid = context_id(context)
        contexts.append({"id": cid, **context})
        for course in p["courses"]:
            details = select_details(data, course, context)
            item = {"id": hashlib.sha256(json.dumps([context, course["code"], course["name"]], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20],
                    "code": course["code"], "name": course["name"], "hours": course.get("hours"), "context": context, "context_id": cid,
                    "topics": [], "status": "missing_pdf", "reviewed": False, "warnings": []}
            if details and details.get("pdf_url"):
                url = details["pdf_url"]
                path = (root / url).resolve()
                if not path.is_relative_to((root / "assets").resolve()) or not path.is_file():
                    item["warnings"].append("المصدر ليس ملفًا محليًا متاحًا؛ لا يُجلب رابط خارجي تلقائيًا")
                else:
                    sha = digest(path)
                    extracted = extract_cached(path, cache_dir)
                    item.update(apply_readings(json.loads(json.dumps(extracted)), root), source_pdf=url, match_status=details.get("match_status"), source_note=details.get("match_note", ""))
                    unknown = [t["id"] for t in item["topics"] if norm(t.get("corrected_text", t["text"])) in ("أخرى", "اخرى", "غير محدد") or 'لايوجدموضوع' in compact(t.get('corrected_text',t['text']))]
                    if unknown:
                        item["warnings"].append("مفردة غير محددة في المصدر: " + ", ".join(unknown) + "؛ لا تُستكمل بالتخمين")
                        item["status"] = "needs_review"
                    if any('لايوجدموضوع' in compact(t.get('corrected_text',t['text'])) for t in item['topics']) or compact(course['name'])=='الرسالة':
                        item.update(fixed_topic_curriculum=False,source_text_blocks=item['topics'],topics=[],status='no_fixed_topics')
                        item['warnings'].append('المصدر لا يحدد مفردات موضوعية ثابتة؛ لا يحسب له حكم معادلة مفردات تلقائي')
            courses.append(item)
            print(f"{context['version']} {item['code']}: {len(item['topics'])} topics; {item['status']}", flush=True)
    source_hashes = {name: digest(root / name) for name in ("data.json", "site-edits.json")}
    if (root / "equivalency-pilot/topic-readings.json").exists():
        source_hashes["equivalency-pilot/topic-readings.json"] = digest(root / "equivalency-pilot/topic-readings.json")
    for context in contexts:
        selected = [c for c in courses if c["context_id"] == context["id"]]
        context.update(total=len(selected), with_pdf=sum(bool(c.get("source_pdf")) for c in selected), with_topics=sum(bool(c["topics"]) for c in selected))
    library=[]
    selected_paths={c.get('source_pdf') for c in courses}
    for code, details in data.get('course_details',{}).items():
        for variant in details.get('variants',[details]):
            url=variant.get('pdf_url')
            if not url or url in selected_paths:
                continue
            path=(root/url).resolve()
            if not path.is_relative_to((root/'assets').resolve()) or not path.is_file():
                continue
            identity=['library',code,variant.get('title'),url]
            item={'id':hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()[:20],
                  'code':code,'name':variant.get('title',code),'hours':None,'context':None,'context_id':None,
                  'library_only':True,'source_context':variant.get('source_context'),
                  'source_pdf':url,'source_note':variant.get('match_note','')}
            item.update(apply_readings(extract_cached(path,cache_dir),root))
            item['warnings'].append('مصدر إضافي في الفهرس، غير مرتبط بخطة محددة؛ يستخدم كمصدر للمقارنة فقط')
            library.append(item)
    catalog = {"schema_version": 2, "scope": "college", "college": "كلية الشريعة والأنظمة", "contexts": contexts, "extractor": EXTRACTOR_VERSION,
               "source_hashes": source_hashes, "courses": courses, "library_sources":library,
               "policy": {"coverage_threshold": 80, "strict_topic_threshold": 80, "lenient_topic_threshold": 60, "weighting": "topic_count", "direction": "uploaded_to_target"}}
    catalog["catalog_id"] = hashlib.sha256(json.dumps(catalog, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(output).with_suffix('.building.json')
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    return catalog
