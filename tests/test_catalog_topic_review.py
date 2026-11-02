"""Curator batches are validated completely before any canonical catalog write."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pymupdf

from scripts.review_catalog_topics import CatalogReviewError, canonical_sha256, catalog_sha256, review_catalog_topics


class CatalogTopicReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ('assets', 'review-batches', 'equivalency-pilot'):
            (self.root / directory).mkdir()
        self.pdf = self.root / 'assets/source.pdf'
        with pymupdf.open() as document:
            document.new_page().insert_text((30, 40), 'source page one')
            document.new_page().insert_text((30, 40), 'source page two')
            self.pdf.write_bytes(document.tobytes())
        self.pdf_bytes = self.pdf.read_bytes()
        self.sha = hashlib.sha256(self.pdf_bytes).hexdigest()
        self.import_manifest = self.root / 'review-batches/source-import.json'
        self.import_manifest.write_text('{"original_import":"unchanged"}\n')
        (self.root / 'data.json').write_text('{"original_data":true}\n')
        self.original = [{'id': 't1', 'text': 'نص مستخرج ناقص', 'page': 1}]
        self.courses = [{'id': key, 'name': 'المقرر', 'context': {'program': key}, 'topics': copy.deepcopy(self.original), 'source_pdf': 'assets/source.pdf', 'source_sha256': self.sha, 'reviewed': False} for key in ('course-1', 'course-2')]
        self.catalog = {'schema_version': 1, 'courses': self.courses, 'contexts': [], 'source_hashes': {'data.json': hashlib.sha256((self.root / 'data.json').read_bytes()).hexdigest(), 'review-batches/source-import.json': hashlib.sha256(self.import_manifest.read_bytes()).hexdigest()}}
        self.catalog_path = self.root / 'equivalency-pilot/catalog.json'
        self.save_catalog()
        self.entry = {'course_ids': ['course-1'], 'source_pdf': 'assets/source.pdf', 'source_sha256': self.sha, 'previous_topics_sha256': canonical_sha256(self.original), 'topics': [{'id': 't1', 'text': 'موضوع أول صحيح', 'page': 1}, {'id': 't2', 'text': 'موضوع ثان مسترد', 'page': 2}], 'review_pages': [1, 2], 'review_kind': 'visual_source_transcription'}
        self.batch = {'batch_id': 'review-test', 'catalog_base_id': self.catalog['catalog_id'], 'entries': [self.entry]}
        self.manifest = self.root / 'review-batches/corrected-topics.json'
        self.save_batch()

    def save_catalog(self):
        self.catalog['catalog_id'] = catalog_sha256(self.catalog)
        self.catalog_path.write_text(json.dumps(self.catalog, ensure_ascii=False))

    def save_batch(self):
        self.manifest.write_text(json.dumps(self.batch, ensure_ascii=False, indent=2))

    def reject(self, message):
        self.save_batch()
        before = self.catalog_path.read_bytes()
        with self.assertRaisesRegex(CatalogReviewError, message):
            review_catalog_topics(self.root, self.manifest, apply=True)
        self.assertEqual(self.catalog_path.read_bytes(), before)
        self.assertEqual(self.pdf.read_bytes(), self.pdf_bytes)

    def test_apply_corrects_denominator_and_binds_provenance_without_modifying_inputs(self):
        before_data = (self.root / 'data.json').read_bytes()
        before_import = self.import_manifest.read_bytes()
        proof = review_catalog_topics(self.root, self.manifest, apply=True)
        after = json.loads(self.catalog_path.read_text())
        corrected, untargeted = after['courses']
        self.assertEqual(len(corrected['topics']), 2)
        self.assertEqual(corrected['original_extracted_topics'], self.original)
        self.assertEqual(untargeted, self.courses[1])
        self.assertFalse(corrected['reviewed'])
        self.assertEqual(corrected['extraction_method'], 'reviewed_visual_transcription')
        self.assertEqual(corrected['reference_review'], {'batch_id': 'review-test', 'review_pages': [1, 2], 'source_sha256': self.sha, 'kind': 'visual_source_transcription'})
        self.assertEqual(after['catalog_id'], catalog_sha256(after))
        self.assertEqual(after['source_hashes']['review-batches/corrected-topics.json'], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(proof['changes'], [{'course_id': 'course-1', 'old_count': 1, 'new_count': 2}])
        self.assertEqual(self.pdf.read_bytes(), self.pdf_bytes)
        self.assertEqual((self.root / 'data.json').read_bytes(), before_data)
        self.assertEqual(self.import_manifest.read_bytes(), before_import)

    def test_default_dry_run_does_not_change_any_file(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        proof = review_catalog_topics(self.root, 'review-batches/corrected-topics.json')
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(proof['mode'], 'dry_run')
        self.assertNotEqual(proof['catalog_id'], self.catalog['catalog_id'])

    def test_hashes_match_the_existing_verifier_encoding_with_arabic_text(self):
        expected_topics = hashlib.sha256(json.dumps(self.original, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(canonical_sha256(self.original), expected_topics)
        clean_catalog = {k: v for k, v in self.catalog.items() if k != 'catalog_id'}
        expected_catalog = hashlib.sha256(json.dumps(clean_catalog, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(catalog_sha256(self.catalog), expected_catalog)

    def test_multiple_course_contexts_are_updated_only_when_explicitly_named(self):
        self.entry['course_ids'] = ['course-1', 'course-2']
        self.save_batch()
        proof = review_catalog_topics(self.root, self.manifest, apply=True)
        after = json.loads(self.catalog_path.read_text())
        self.assertEqual(len(proof['changes']), 2)
        self.assertTrue(all(course['topics'] == self.entry['topics'] for course in after['courses']))
        self.assertEqual([course['context'] for course in after['courses']], [course['context'] for course in self.courses])

    def test_reapply_is_rejected_as_stale_baseline(self):
        review_catalog_topics(self.root, self.manifest, apply=True)
        self.reject('stale_catalog_base_id')

    def test_later_approved_batch_preserves_the_first_raw_extraction(self):
        review_catalog_topics(self.root, self.manifest, apply=True)
        after = json.loads(self.catalog_path.read_text())
        self.manifest = self.root / 'review-batches/second-review.json'
        self.batch['batch_id'] = 'second-review'
        self.batch['catalog_base_id'] = after['catalog_id']
        self.entry['previous_topics_sha256'] = canonical_sha256(after['courses'][0]['topics'])
        self.entry['topics'][0]['text'] = 'قراءة ثانية مصححة'
        self.save_batch()
        review_catalog_topics(self.root, self.manifest, apply=True)
        final = json.loads(self.catalog_path.read_text())
        self.assertEqual(final['courses'][0]['original_extracted_topics'], self.original)
        self.assertEqual(final['courses'][0]['reference_review']['batch_id'], 'second-review')

    def test_stale_catalog_id_and_stale_topic_list_are_rejected(self):
        self.batch['catalog_base_id'] = '0' * 64
        self.reject('stale_catalog_base_id')
        self.batch['catalog_base_id'] = self.catalog['catalog_id']
        self.entry['previous_topics_sha256'] = '0' * 64
        self.reject('stale_previous_topics_sha256')

    def test_corrupt_catalog_digest_and_existing_source_bindings_are_rejected(self):
        self.catalog['courses'][0]['name'] = 'unbound change'
        self.catalog_path.write_text(json.dumps(self.catalog))
        self.reject('catalog_id_mismatch')
        self.save_catalog()
        self.batch['catalog_base_id'] = self.catalog['catalog_id']
        (self.root / 'data.json').write_text('changed data')
        self.reject('existing_source_hash_mismatch')

    def test_pdf_byte_digest_and_catalog_scope_must_both_match(self):
        self.entry['source_sha256'] = '0' * 64
        self.reject('source_pdf_sha256_mismatch')
        self.entry['source_sha256'] = self.sha
        self.catalog['courses'][0]['source_sha256'] = 'f' * 64
        self.save_catalog()
        self.batch['catalog_base_id'] = self.catalog['catalog_id']
        self.reject('course_pdf_scope_mismatch')

    def test_course_cannot_be_selected_through_a_different_pdf_path(self):
        (self.root / 'assets/identical-copy.pdf').write_bytes(self.pdf_bytes)
        self.entry['source_pdf'] = 'assets/identical-copy.pdf'
        self.reject('course_pdf_scope_mismatch')

    def test_asset_paths_reject_absolute_traversal_foreign_and_symlink_escape(self):
        for path in ('/etc/passwd.pdf', 'assets/../source.pdf', 'other/source.pdf', 'assets\\source.pdf'):
            self.entry['source_pdf'] = path
            with self.subTest(path=path):
                self.reject('invalid_source_pdf_path')
        outside = self.root / 'outside.pdf'
        outside.write_bytes(self.pdf_bytes)
        (self.root / 'assets/link.pdf').symlink_to(outside)
        self.entry['source_pdf'] = 'assets/link.pdf'
        self.reject('outside_assets')

    def test_manifest_must_reside_within_review_batches(self):
        other = self.root / 'outside.json'
        other.write_bytes(self.manifest.read_bytes())
        with self.assertRaisesRegex(CatalogReviewError, 'review_batches_path'):
            review_catalog_topics(self.root, other, apply=True)

    def test_missing_or_duplicate_course_ids_are_rejected(self):
        for ids, reason in (([], 'explicit_course_ids'), (['unknown'], 'unknown_course_id'), (['course-1', 'course-1'], 'duplicate_course_id')):
            self.entry['course_ids'] = ids
            with self.subTest(ids=ids):
                self.reject(reason)

    def test_every_explicit_course_needs_its_own_matching_original_topic_hash(self):
        self.catalog['courses'][1]['topics'][0]['text'] = 'different original context'
        self.save_catalog()
        self.batch['catalog_base_id'] = self.catalog['catalog_id']
        self.entry['course_ids'] = ['course-1', 'course-2']
        self.reject('stale_previous_topics_sha256')

    def test_duplicate_topic_ids_and_empty_or_oversize_text_are_rejected(self):
        original = copy.deepcopy(self.entry['topics'])
        self.entry['topics'][1]['id'] = 't1'
        self.reject('duplicate_reviewed_topic_id')
        for value in ('', '   ', 'x' * 4001):
            self.entry['topics'] = copy.deepcopy(original)
            self.entry['topics'][0]['text'] = value
            with self.subTest(length=len(value)):
                self.reject('invalid_reviewed_topic_text')
        self.entry['topics'] = []
        self.reject('invalid_reviewed_topic_count')
        self.entry['topics'] = [dict(original[0], id=f't{i}') for i in range(101)]
        self.reject('invalid_reviewed_topic_count')

    def test_topic_pages_and_review_pages_need_explicit_valid_pdf_evidence(self):
        for page in (0, 3, True, '1', None):
            self.entry['topics'][0]['page'] = page
            with self.subTest(page=page):
                self.reject('invalid_reviewed_topic_page')
        self.entry['topics'][0]['page'] = 1
        for pages, reason in (([], 'invalid_review_pages'), ([1, 3], 'invalid_review_pages'), ([1, True], 'invalid_review_pages'), ([1, 1], 'duplicate_review_page'), ([1], 'invalid_reviewed_topic_page')):
            self.entry['review_pages'] = pages
            with self.subTest(pages=pages):
                self.reject(reason)

    def test_invalid_second_entry_cannot_partially_apply_the_first(self):
        second = copy.deepcopy(self.entry)
        second['course_ids'] = ['course-2']
        second['topics'][0]['page'] = 99
        self.batch['entries'].append(second)
        self.reject('invalid_reviewed_topic_page')

    def test_duplicate_course_across_entries_and_wrong_review_kind_are_rejected(self):
        self.batch['entries'].append(copy.deepcopy(self.entry))
        self.reject('duplicate_course_id')
        self.batch['entries'] = [self.entry]
        self.entry['review_kind'] = 'automatic_guess'
        self.reject('invalid_review_kind')


if __name__ == '__main__':
    unittest.main()
