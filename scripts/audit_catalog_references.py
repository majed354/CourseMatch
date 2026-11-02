#!/usr/bin/env python3
"""CPU-only evidence audit of every reference PDF; never writes the catalog."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.equivalency.extract import EXTRACTOR_VERSION, compact, digest, extract_pdf, norm, pure_assessment_text
from scripts.equivalency.recovery import numbered_grid, number_cell_has_ink


def complete_page_evidence(root, record):
    """Do not let a truncated native/old extraction truncate the visual audit."""
    pages = set(record.get('review_pages', []))
    candidate = record.get('native_candidate', {})
    sections = candidate.get('table_evidence', [])
    source = (Path(root) / record['source_pdfs'][0]).resolve()
    if not source.is_relative_to(Path(root).resolve() / 'assets') or digest(source) != record['source_sha256']:
        raise ValueError('source_sha_changed_before_coverage_review')
    with pymupdf.open(source) as document:
        clipped = []
        for topic in candidate.get('topics', []):
            page = topic.get('page')
            box = topic.get('bbox')
            if type(page) is int and 1 <= page <= len(document) and box and (box[1] < 0 or box[3] > document[page-1].rect.height - 1):
                clipped.append({'page': page, 'bbox': box, 'topic_id': topic.get('id')})
        blank = []
        for section in sections:
            for row in section.get('empty_rows', []):
                y0, y1 = row['bbox'][1], row['bbox'][3]
                number_box = (section['topic_column'][1], y0, section['table_bbox'][2], y1)
                if row.get('label') or number_cell_has_ink(document[section['page']-1], number_box):
                    blank.append(dict(row, page=section['page']))
        closed = bool(sections and sections[-1].get('total'))
        if record.get('review_status') != 'verified_visual_existing' and (not closed or candidate.get('additional_topic_sections')):
            start = min(pages) if pages else 1
            pages.update(range(start, len(document) + 1))
            record['coverage_extension_reason'] = 'No complete first-table closure proven, or additional sections: review every following page until the actual table end is visually established.'
        record['clipped_topic_rows'] = clipped
        record['blank_numbered_rows'] = blank
        record['review_pages'] = sorted(page for page in pages if 1 <= page <= len(document))
    if (clipped or blank) and record.get('review_status') not in ('verified_visual_existing', 'no_fixed_topics_existing'):
        record['review_status'] = 'unresolved'
        record['review_basis'] = 'clipped_or_blank_numbered_topic_rows'
        record.setdefault('risk_reasons', []).append('clipped_or_blank_numbered_rows')
        record['risk_priority'] = max(1000, record.get('risk_priority', 0))
    record['page_coverage_provenance'] = 'Full PDF page inventory plus explicit table closure; not bounded by the old topic list.'
    return record


def text_quality(topics):
    """Flags are triage heuristics, never a proof of academically correct text."""
    flags = []
    for topic in topics:
        text = topic.get('corrected_text') or topic.get('text', '')
        if '(cid:' in text or '\ufffd' in text or any(0xE000 <= ord(c) <= 0xF8FF for c in text):
            flags.append({'topic_id': topic.get('id'), 'kind': 'unresolved_glyphs'})
        words = re.findall(r'[\u0621-\u064a]+', norm(text))
        singles = sum(len(word) == 1 for word in words)
        if len(words) >= 6 and singles >= 2 and singles / len(words) > .13:
            flags.append({'topic_id': topic.get('id'), 'kind': 'possibly_fragmented_arabic', 'single_character_words': singles, 'arabic_words': len(words)})
        if len(compact(text)) < 3:
            flags.append({'topic_id': topic.get('id'), 'kind': 'short_unresolved_text'})
    return flags


def classify(candidate, courses):
    curated = [course for course in courses if course.get('reference_review')]
    if curated:
        return 'verified_visual_existing', 'preserve_previously_reviewed_topics'
    if courses and all(course.get('status') == 'no_fixed_topics' for course in courses):
        return 'no_fixed_topics_existing', 'preserve_explicit_no_fixed_topics_status'
    if not candidate.get('topics'):
        return 'unresolved', 'no_trustworthy_topic_table'
    evidence = candidate.get('table_evidence', [])
    quality = text_quality(candidate['topics'])
    if not evidence or candidate.get('warnings') or quality:
        return 'needs_visual', 'structural_or_text_evidence_requires_visual_review'
    if not evidence[-1].get('total') or any(section.get('empty_rows') for section in evidence):
        return 'needs_visual', 'table_closure_or_cells_unresolved'
    for index, section in enumerate(evidence):
        merged = len(section.get('merged_fragment_rows', [])) if 'merged_fragment_rows' in section else bool(section.get('continued_previous_topic'))
        accounted = len(section.get('content_rows', [])) + len(section.get('excluded_rows', [])) + merged
        if section.get('numbered_geometry_rows') != accounted:
            return 'needs_visual', 'not_all_physical_rows_accounted_for'
        if not section.get('total') and (index + 1 == len(evidence) or evidence[index + 1]['page'] != section['page'] + 1):
            return 'needs_visual', 'continuation_page_gap'
    return 'verified_native', 'bounded_number_column_geometry_and_no_detected_text_flags'


def audit_one(task):
    root, sha, relatives, courses = task
    root = Path(root).resolve()
    path = (root / relatives[0]).resolve()
    started = time.monotonic()
    record = {'source_sha256': sha, 'source_pdfs': relatives, 'course_ids': [c['id'] for c in courses],
              'names': sorted({c.get('name', '') for c in courses}),
              'appearances': [{'id': c['id'], 'context': c.get('context'), 'count': len(c.get('topics', [])), 'status': c.get('status')} for c in courses],
              'catalog_counts': sorted({len(c.get('topics', [])) for c in courses}), 'extractor': EXTRACTOR_VERSION}
    try:
        if not path.is_relative_to(root / 'assets') or digest(path) != sha or any(digest(root / relative) != sha for relative in relatives):
            raise ValueError('source_path_or_sha_mismatch')
        with pymupdf.open(path) as document:
            record['pdf_page_count'] = len(document)
            record['native_text_chars_per_page'] = [len(page.get_text()) for page in document]
            record['topic_heading_pages_native'] = [i + 1 for i, page in enumerate(document) if i >= 2
                if any(key in compact(page.get_text()) for key in ('قائمةالموضوعات', 'لائمةالموضوعات', 'موضوعاتالمقرر', 'محتوىالمقرر'))]
        candidate = numbered_grid(path)
        if not candidate['topics']:
            candidate = extract_pdf(path)
        record['native_candidate'] = candidate
        record['native_candidate_count'] = len(candidate['topics'])
        record['review_status'], record['review_basis'] = classify(candidate, courses)
        curated = [course for course in courses if course.get('reference_review')]
        if curated:
            if any(course['reference_review'].get('source_sha256') != sha for course in curated):
                raise ValueError('curated_reference_source_sha_mismatch')
            record['existing_reviews'] = [{'course_id': c['id'], 'reference_review': c['reference_review'], 'topics': c['topics']} for c in curated]
            record['proposed_topics'] = curated[0]['topics']
            record['proposal_action'] = 'keep_existing_curated_reading'
        elif record['review_status'] == 'no_fixed_topics_existing':
            record['proposed_topics'] = []
            record['proposal_action'] = 'skip_no_fixed_topics'
        else:
            record['proposed_topics'] = candidate['topics']
            record['proposal_action'] = 'candidate_only_requires_curator_approval'
        record['proposed_count'] = len(record['proposed_topics'])
        record['text_flags'] = text_quality(record['proposed_topics'])
        record['structural_evidence'] = candidate.get('table_evidence', [])
        sections = record['structural_evidence']
        record['structural_coverage'] = {
            'first_topic_page': min((t['page'] for t in candidate['topics']), default=None),
            'last_topic_page': max((t.get('end_page', t['page']) for t in candidate['topics']), default=None),
            'last_printed_row_label': sections[-1].get('last_row_label') if sections else None,
            'final_total_observed': bool(sections and sections[-1].get('total')),
            'geometry_rows': sum(s['numbered_geometry_rows'] for s in sections),
            'accounted_geometry_rows': sum(len(s['content_rows']) + len(s['excluded_rows']) + len(s.get('empty_rows', [])) + (len(s['merged_fragment_rows']) if 'merged_fragment_rows' in s else bool(s.get('continued_previous_topic'))) for s in sections),
            'empty_rows': sum(len(s.get('empty_rows', [])) for s in sections),
            'unclosed_section_pages': [s['page'] for i, s in enumerate(sections) if not s.get('total') and (i+1 == len(sections) or sections[i+1]['page'] != s['page']+1)],
            'scope': 'physical_table_completeness_only_not_human_academic_review'}
        topic_pages = {t['page'] for t in record['proposed_topics'] if type(t.get('page')) is int}
        topic_pages.update(t['page'] for t in candidate['topics'] if type(t.get('page')) is int)
        for topic in record['proposed_topics']:
            topic_pages.update(fragment['page'] for fragment in topic.get('fragments', []) if type(fragment.get('page')) is int)
        topic_pages.update(record['topic_heading_pages_native'])
        topic_pages.update(section['page'] for section in candidate.get('additional_topic_sections', []))
        record['catalog_topic_pages'] = sorted({t['page'] for course in courses for t in course.get('topics', [])
            if type(t.get('page')) is int and 1 <= t['page'] <= record['pdf_page_count']})
        topic_pages.update(record['catalog_topic_pages'])
        if not topic_pages and record['review_status'] in ('unresolved', 'blocked', 'no_fixed_topics_existing'):
            # A scanned source still needs visible evidence even when no text
            # or header can be located by the native reader.
            topic_pages.update(range(1, record['pdf_page_count'] + 1))
        record['review_pages'] = sorted(topic_pages)
        old = max(record['catalog_counts'], default=0)
        record['denominator_delta'] = record['proposed_count'] - old
        record['risk_reasons'] = []
        record['assessment_rows_in_catalog'] = [{'course_id': c['id'], 'topic': t} for c in courses for t in c.get('topics', [])
            if pure_assessment_text(t.get('corrected_text') or t.get('text', '')) or compact(t.get('corrected_text') or t.get('text', '')) in ('المجموع', 'مجموع', 'الإجمالي')]
        if not curated and record['denominator_delta']:
            record['risk_reasons'].append('denominator_changes')
        if record['text_flags']:
            record['risk_reasons'].append('text_reading_requires_visual_review')
        if record['review_status'] == 'unresolved':
            record['risk_reasons'].append('blocked_topic_extraction')
        if record['review_status'] == 'needs_visual':
            record['risk_reasons'].append('incomplete_native_proof')
        if candidate.get('additional_topic_sections'):
            record['risk_reasons'].append('multiple_topic_sections_not_aggregated')
        if record['assessment_rows_in_catalog']:
            record['risk_reasons'].append('exam_or_total_in_catalog_denominator')
        record['risk_priority'] = (1000 if record['review_status'] == 'unresolved' else 25 if record['review_status'] == 'needs_visual' else 0) + abs(record['denominator_delta']) * 100 + len(record['text_flags']) + 200 * len(record['assessment_rows_in_catalog'])
        # Every preserved curated topic remains available even if native v11 is inferior.
        if curated:
            record['risk_priority'] = 0
    except Exception as error:
        record.update(review_status='blocked', review_basis=type(error).__name__ + ': ' + str(error),
                      risk_priority=10000, risk_reasons=['source_or_extraction_error'], proposed_topics=[], review_pages=[])
    if record.get('review_status') != 'blocked':
        try:
            complete_page_evidence(root, record)
        except Exception as error:
            record.update(review_status='blocked', review_basis='page_coverage_error: ' + type(error).__name__, risk_priority=10000)
    record['seconds'] = round(time.monotonic() - started, 3)
    return record


def inventory(root):
    root = Path(root).resolve()
    catalog = json.loads((root / 'equivalency-pilot/catalog.json').read_text())
    grouped = {}
    for course in catalog['courses'] + catalog.get('library_sources', []):
        if not course.get('source_pdf'):
            continue
        item = grouped.setdefault(course['source_sha256'], {'paths': set(), 'courses': []})
        item['paths'].add(course['source_pdf'])
        item['courses'].append(course)
    tasks = [(str(root), sha, sorted(item['paths']), item['courses']) for sha, item in sorted(grouped.items())]
    return catalog, tasks


def contact_sheets(root, output, records):
    """Readable page thumbnails with machine IDs; source pixels are unchanged."""
    from PIL import Image, ImageDraw
    folder = output / 'contact-sheets'
    folder.mkdir(exist_ok=True)
    sheets, canvas, draw, slot = [], None, None, 0
    for record in records:
        path = Path(root) / record['source_pdfs'][0]
        pages = record.get('review_pages') or record.get('topic_heading_pages_native') or []
        with pymupdf.open(path) as document:
            for number in pages:
                if number < 1 or number > len(document):
                    continue
                if slot == 0:
                    canvas = Image.new('RGB', (1200, 1740), 'white')
                    draw = ImageDraw.Draw(canvas)
                pix = document[number - 1].get_pixmap(matrix=pymupdf.Matrix(.65, .65), alpha=False)
                tile = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                tile.thumbnail((390, 540))
                x, y = (slot % 3) * 400, (slot // 3) * 580
                canvas.paste(tile, (x + (400 - tile.width) // 2, y + 35))
                draw.text((x + 5, y + 4), record['source_sha256'][:12] + ' / p' + str(number), fill='black')
                draw.text((x + 5, y + 18), record['review_status'] + ' / ' + str(record.get('proposed_count', '?')), fill='black')
                record.setdefault('contact_sheet_locations', []).append({'sheet': len(sheets) + 1, 'slot': slot + 1, 'page': number})
                slot += 1
                if slot == 9:
                    name = f'contact-{len(sheets)+1:03}.png'
                    canvas.save(folder / name)
                    sheets.append(name)
                    slot = 0
    if slot:
        name = f'contact-{len(sheets)+1:03}.png'
        canvas.save(folder / name)
        sheets.append(name)
    return sheets


def audit(root=ROOT, output=None, workers=3, render_contacts=True):
    root = Path(root).resolve()
    output = Path(output or root / 'output/full-reference-review-20261102').resolve()
    output.mkdir(parents=True, exist_ok=True)
    records_dir = output / 'records'
    records_dir.mkdir(exist_ok=True)
    catalog, tasks = inventory(root)
    # Freeze the exact parsed input used by every child process. Concurrent
    # curator batches may advance the live catalog while this CPU audit runs.
    (output / 'catalog-input-snapshot.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2))
    code_paths = ['scripts/audit_catalog_references.py', 'scripts/equivalency/extract.py', 'scripts/equivalency/recovery.py', 'scripts/pdf_font_recovery.py']
    code_hashes = {name: digest(root / name) for name in code_paths if (root / name).exists()}
    records = []
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(audit_one, task) for task in tasks]
        for future in as_completed(futures):
            record = future.result()
            record['audit_code_hashes'] = code_hashes
            records.append(record)
            (records_dir / (record['source_sha256'] + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2))
            if len(records) % 25 == 0 or len(records) == len(tasks):
                progress = {'processed': len(records), 'total': len(tasks), 'seconds': round(time.monotonic() - started, 1), 'statuses': dict(collections.Counter(r['review_status'] for r in records))}
                (output / 'progress.json').write_text(json.dumps(progress, indent=2))
                print(json.dumps(progress), flush=True)
    records.sort(key=lambda r: (-r['risk_priority'], r['source_sha256']))
    sheets = contact_sheets(root, output, records) if render_contacts else []
    for record in records:
        (records_dir / (record['source_sha256'] + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2))
    summary = {'catalog_id': catalog['catalog_id'], 'extractor': EXTRACTOR_VERSION, 'pdf_path_count': len({p for task in tasks for p in task[2]}),
               'unique_sha_count': len(tasks), 'catalog_appearances': len(catalog['courses']),
               'appearances_with_topics': sum(bool(c.get('topics')) for c in catalog['courses']),
               'status_counts': dict(collections.Counter(r['review_status'] for r in records)),
               'existing_curated_pdf_count': sum(r['review_status'] == 'verified_visual_existing' for r in records),
               'contact_sheets': sheets, 'seconds': round(time.monotonic() - started, 1),
               'human_review_claim': False, 'catalog_written': False, 'records': [{k: r.get(k) for k in ['source_sha256', 'source_pdfs', 'course_ids', 'names', 'review_status', 'catalog_counts', 'proposed_count', 'denominator_delta', 'risk_priority', 'risk_reasons', 'review_pages', 'contact_sheet_locations']} for r in records]}
    summary['audit_code_hashes'] = code_hashes
    summary['code_changed_during_run'] = any(digest(root / name) != sha for name, sha in code_hashes.items())
    (output / 'index.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    (output / 'risk-queue.json').write_text(json.dumps([r for r in summary['records'] if r['risk_priority']], ensure_ascii=False, indent=2))
    return summary


def refresh_page_coverage(root, source, output, render_contacts=True):
    """Refresh complete-page evidence from all saved records, without inference.

    Native extraction remains attributed to its original code hashes. This
    separate stage never rewrites the baseline audit or its input snapshot.
    """
    root, source, output = Path(root).resolve(), Path(source).resolve(), Path(output).resolve()
    if source == output:
        raise ValueError('coverage_refresh_requires_separate_output_directory')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'records').mkdir(exist_ok=True)
    raw_index = (source / 'index.json').read_bytes()
    summary = json.loads(raw_index)
    records = []
    started = time.monotonic()
    for item in summary['records']:
        record = json.loads((source / 'records' / (item['source_sha256'] + '.json')).read_text())
        record.pop('contact_sheet_locations', None)
        if record['review_status'] != 'blocked':
            record = complete_page_evidence(root, record)
        record['coverage_refresh_code_sha256'] = digest(Path(__file__))
        records.append(record)
    records.sort(key=lambda r: (-r['risk_priority'], r['source_sha256']))
    sheets = contact_sheets(root, output, records) if render_contacts else []
    for record in records:
        (output / 'records' / (record['source_sha256'] + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2))
    keys = ['source_sha256', 'source_pdfs', 'course_ids', 'names', 'review_status', 'catalog_counts', 'proposed_count', 'denominator_delta', 'risk_priority', 'risk_reasons', 'review_pages', 'contact_sheet_locations']
    summary.update(stage='complete_page_coverage_refresh', native_extraction_source_index_sha256=hashlib.sha256(raw_index).hexdigest(),
        coverage_refresh_code_sha256=digest(Path(__file__)), coverage_refresh_seconds=round(time.monotonic()-started, 1),
        contact_sheets=sheets, status_counts=dict(collections.Counter(r['review_status'] for r in records)),
        records=[{k: r.get(k) for k in keys} for r in records])
    (output / 'index.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    (output / 'risk-queue.json').write_text(json.dumps([r for r in summary['records'] if r['risk_priority']], ensure_ascii=False, indent=2))
    if (source / 'catalog-input-snapshot.json').exists():
        (output / 'catalog-input-snapshot.json').write_bytes((source / 'catalog-input-snapshot.json').read_bytes())
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--workers', type=int, choices=(1, 2, 3), default=3)
    parser.add_argument('--no-contact-sheets', action='store_true')
    parser.add_argument('--refresh-from', type=Path, help='Recheck page coverage from a saved complete audit, into a separate --output')
    args = parser.parse_args()
    if args.refresh_from:
        if args.output is None:
            parser.error('--refresh-from requires a separate --output')
        summary = refresh_page_coverage(args.root, args.refresh_from, args.output, not args.no_contact_sheets)
    else:
        summary = audit(args.root, args.output, args.workers, not args.no_contact_sheets)
    print(json.dumps({k: summary[k] for k in ['unique_sha_count', 'status_counts', 'seconds', 'catalog_written']}))


if __name__ == '__main__':
    main()
