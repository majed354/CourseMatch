#!/usr/bin/env python3
"""Validate a curator's PDF transcription batch; write the catalog only with --apply."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
SHA256 = re.compile(r'^[a-f0-9]{64}$')
MAX_TOPICS = 100
MAX_TEXT = 4000


class CatalogReviewError(ValueError):
    pass


def canonical_sha256(value):
    """Use the catalog verifier's exact JSON encoding, including default separators."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def catalog_sha256(catalog):
    return canonical_sha256({key: value for key, value in catalog.items() if key != 'catalog_id'})


def require(condition, message):
    if not condition:
        raise CatalogReviewError(message)


def valid_string(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def source_path(root, value):
    require(isinstance(value, str) and '\\' not in value, 'invalid_source_pdf_path')
    path = Path(value)
    require(not path.is_absolute() and path.parts and path.parts[0] == 'assets' and '..' not in path.parts and path.suffix.lower() == '.pdf', 'invalid_source_pdf_path')
    resolved = (root / path).resolve()
    require(resolved.is_relative_to(root / 'assets') and resolved.is_relative_to(root) and resolved.is_file(), 'source_pdf_outside_assets_or_missing')
    return resolved


def review_catalog_topics(root, manifest_path, *, apply=False):
    root = Path(root).resolve()
    manifest = Path(manifest_path)
    manifest = (manifest if manifest.is_absolute() else root / manifest).resolve()
    require(manifest.is_relative_to(root / 'review-batches') and manifest.is_file(), 'manifest_requires_review_batches_path')
    manifest_relative = manifest.relative_to(root).as_posix()
    manifest_bytes = manifest.read_bytes()
    batch = json.loads(manifest_bytes)
    require(isinstance(batch, dict), 'invalid_review_manifest')
    require(valid_string(batch.get('batch_id'), 128), 'invalid_batch_id')

    catalog_path = root / 'equivalency-pilot/catalog.json'
    require(not catalog_path.is_symlink() and catalog_path.resolve().is_relative_to(root), 'invalid_catalog_path')
    catalog_bytes = catalog_path.read_bytes()
    catalog = json.loads(catalog_bytes)
    require(isinstance(catalog, dict) and isinstance(catalog.get('source_hashes'), dict), 'invalid_catalog')
    require(catalog_sha256(catalog) == catalog.get('catalog_id'), 'catalog_id_mismatch')
    require(batch.get('catalog_base_id') == catalog['catalog_id'], 'stale_catalog_base_id')
    require(isinstance(batch.get('entries'), list) and 1 <= len(batch['entries']) <= 1000, 'invalid_review_entries')
    # Existing provenance remains bound; a later batch uses a new manifest file.
    for relative, expected in catalog['source_hashes'].items():
        require(isinstance(relative, str) and not Path(relative).is_absolute() and '..' not in Path(relative).parts, 'invalid_existing_source_binding')
        path = (root / relative).resolve()
        require(path.is_relative_to(root) and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'existing_source_hash_mismatch')

    courses = catalog.get('courses')
    require(isinstance(courses, list), 'invalid_catalog_courses')
    by_id = {}
    for course in courses:
        require(isinstance(course, dict) and valid_string(course.get('id'), 128) and course['id'] not in by_id, 'duplicate_or_invalid_catalog_course_id')
        require(isinstance(course.get('topics'), list), 'invalid_catalog_course_topics')
        by_id[course['id']] = course

    seen, prepared, pdf_bytes_by_path = set(), [], {}
    for entry in batch['entries']:
        require(isinstance(entry, dict) and entry.get('review_kind') == 'visual_source_transcription', 'invalid_review_kind')
        ids = entry.get('course_ids')
        require(isinstance(ids, list) and 1 <= len(ids) <= 1000, 'explicit_course_ids_required')
        for course_id in ids:
            require(isinstance(course_id, str) and course_id in by_id, 'unknown_course_id')
            require(course_id not in seen, 'duplicate_course_id_in_batch')
            seen.add(course_id)
        path = source_path(root, entry.get('source_pdf'))
        expected_sha = entry.get('source_sha256')
        require(isinstance(expected_sha, str) and SHA256.fullmatch(expected_sha), 'invalid_source_sha256')
        previous_sha = entry.get('previous_topics_sha256')
        require(isinstance(previous_sha, str) and SHA256.fullmatch(previous_sha), 'invalid_previous_topics_sha256')
        raw = path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == expected_sha, 'source_pdf_sha256_mismatch')
        pdf_bytes_by_path[path] = raw
        with pymupdf.open(stream=raw, filetype='pdf') as document:
            require(not document.needs_pass and document.page_count > 0, 'unreadable_source_pdf')
            page_count = document.page_count
        pages = entry.get('review_pages')
        require(isinstance(pages, list) and 1 <= len(pages) <= page_count and all(type(p) is int and 1 <= p <= page_count for p in pages), 'invalid_review_pages')
        require(len(set(pages)) == len(pages), 'duplicate_review_page')
        topics = entry.get('topics')
        require(isinstance(topics, list) and 1 <= len(topics) <= MAX_TOPICS, 'invalid_reviewed_topic_count')
        topic_ids = set()
        for topic in topics:
            require(isinstance(topic, dict) and valid_string(topic.get('id'), 128), 'invalid_reviewed_topic_id')
            require(topic['id'] not in topic_ids, 'duplicate_reviewed_topic_id')
            topic_ids.add(topic['id'])
            require(valid_string(topic.get('text'), MAX_TEXT), 'invalid_reviewed_topic_text')
            if 'corrected_text' in topic:
                require(valid_string(topic['corrected_text'], MAX_TEXT), 'invalid_corrected_topic_text')
            require(type(topic.get('page')) is int and 1 <= topic['page'] <= page_count and topic['page'] in pages, 'invalid_reviewed_topic_page')
            if 'end_page' in topic:
                require(type(topic['end_page']) is int and topic['page'] <= topic['end_page'] <= page_count and topic['end_page'] in pages, 'invalid_reviewed_topic_end_page')
            if 'fragments' in topic:
                require(isinstance(topic['fragments'], list) and len(topic['fragments']) <= page_count, 'invalid_topic_fragments')
                require(all(isinstance(fragment, dict) and type(fragment.get('page')) is int and fragment['page'] in pages for fragment in topic['fragments']), 'invalid_topic_fragment_page')
        canonical_sha256(topics)  # Reject non-JSON numeric values in optional metadata.
        for course_id in ids:
            course = by_id[course_id]
            require(course.get('source_pdf') == entry['source_pdf'] and source_path(root, course['source_pdf']) == path and course.get('source_sha256') == expected_sha, 'course_pdf_scope_mismatch')
            require(canonical_sha256(course.get('topics')) == previous_sha, 'stale_previous_topics_sha256')
            prepared.append((course_id, entry))

    updated = copy.deepcopy(catalog)
    targets = {course['id']: course for course in updated['courses']}
    changes = []
    for course_id, entry in prepared:
        course = targets[course_id]
        changes.append({'course_id': course_id, 'old_count': len(course['topics']), 'new_count': len(entry['topics'])})
        if 'original_extracted_topics' not in course:
            course['original_extracted_topics'] = copy.deepcopy(course['topics'])
        course['topics'] = copy.deepcopy(entry['topics'])
        course['reference_review'] = {'batch_id': batch['batch_id'], 'review_pages': copy.deepcopy(entry['review_pages']), 'source_sha256': entry['source_sha256'], 'kind': entry['review_kind']}
        course['extraction_method'] = 'reviewed_visual_transcription'
        course['reviewed'] = False
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    updated['source_hashes'][manifest_relative] = manifest_sha
    updated['catalog_id'] = catalog_sha256(updated)
    proof = {'mode': 'apply' if apply else 'dry_run', 'batch_id': batch['batch_id'], 'catalog_base_id': catalog['catalog_id'], 'catalog_id': updated['catalog_id'], 'manifest': manifest_relative, 'manifest_sha256': manifest_sha, 'changes': changes}
    if apply:
        require(catalog_path.read_bytes() == catalog_bytes, 'catalog_changed_during_review')
        require(manifest.read_bytes() == manifest_bytes, 'manifest_changed_during_review')
        require(all(path.read_bytes() == raw for path, raw in pdf_bytes_by_path.items()), 'source_pdf_changed_during_review')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=catalog_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(updated, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(catalog_path.stat().st_mode & 0o777)
            temporary.replace(catalog_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return proof


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', help='Approved manifest within review-batches/')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--apply', action='store_true', help='Atomically apply the fully validated batch (default: dry-run)')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(review_catalog_topics(args.root, args.manifest, apply=args.apply), ensure_ascii=False))
    except (ValueError, OSError, pymupdf.FileDataError, RuntimeError) as error:
        print('catalog_topic_review_failed: ' + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
