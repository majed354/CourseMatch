"""Pure readiness gates for an uploaded target; no models, files or services."""
import copy
import hashlib
import json
import unittest

from scripts.equivalency.reference_quality import (
    extraction_integrity, reference_problem, topics_digest, uploaded_target_reference,
)


class UploadedTargetReadinessTests(unittest.TestCase):
    def course(self, *, sha='a' * 64, texts=('النص الأول', 'النص الثاني'), state='ready_visual'):
        course = {'id': 'canonical', 'source_sha256': sha,
                  'topics': [{'id': f'c{i}', 'text': text, 'page': 5} for i, text in enumerate(texts)]}
        course['reference_quality'] = {'source_sha256': sha, 'topics_sha256': topics_digest(course['topics']), 'state': state}
        return course

    def uploaded(self, course):
        return {'id': 'uploaded-artifact', 'source_sha256': course['source_sha256'], 'upload_pdf': True,
                'topics': [{'id': f'u{i}', 'text': t.get('corrected_text') or t['text']} for i, t in enumerate(course['topics'])]}

    def test_known_blocked_pdf_cannot_drop_a_canonical_hold(self):
        course = self.course(state='needs_review'); target = self.uploaded(course)
        self.assertIsNone(reference_problem(target))  # The original bypass route.
        anchor, problem = uploaded_target_reference(target, [course])
        self.assertIsNone(anchor); self.assertTrue(problem)
        course['reference_quality']['state'] = 'ready_visual'
        course['reference_review'] = {'comparison_readiness': 'blocked', 'readiness_reason': 'النطاق غير مكتمل'}
        self.assertTrue(uploaded_target_reference(target, [course])[1])

    def test_failed_visual_target_is_held_even_when_its_bytes_match_a_ready_anchor(self):
        course=self.course();target=self.uploaded(course)
        for status in ('unavailable','uncertain'):
            target['visual_verification_status']=status
            self.assertTrue(uploaded_target_reference(target,[course])[1])
        target['visual_verification_status']='completed'
        self.assertIs(uploaded_target_reference(target,[course])[0],course)

    def test_ready_anchor_matches_effective_ordered_text_ignoring_ids_and_audit_only(self):
        course = self.course(); course['topics'][0]['corrected_text'] = 'النص المصحح'
        course['reference_quality']['topics_sha256'] = topics_digest(course['topics'])
        target = self.uploaded(course); target['topics'][0].update(text='  النص المصحح  ', page=999, bbox=[0, 0, 1, 1])
        before = copy.deepcopy([target, course])
        anchor, problem = uploaded_target_reference(target, [course])
        self.assertIs(anchor, course); self.assertIsNone(problem); self.assertEqual([target, course], before)
        for texts in [('نص مختلف', 'النص الثاني'), ('النص الثاني', 'النص المصحح'), ('النص المصحح',), ('النص المصحح', 'النص الثاني', 'النص الثاني')]:
            changed = copy.deepcopy(target); changed['topics'] = [{'id': str(i), 'text': t} for i, t in enumerate(texts)]
            self.assertTrue(uploaded_target_reference(changed, [course])[1])

    def test_duplicate_contexts_searches_all_appearances_and_preserves_duplicate_rows(self):
        wrong = self.course(texts=('أ', 'ب')); blocked = self.course(texts=('أ', 'أ'), state='needs_review')
        ready = self.course(texts=('أ', 'أ')); ready['id'] = 'matching-second-context'
        target = self.uploaded(ready)
        self.assertIs(uploaded_target_reference(target, {'wrong':wrong, 'blocked':blocked, 'ready':ready})[0], ready)
        self.assertTrue(uploaded_target_reference(self.uploaded(wrong), [ready])[1])

    def test_unknown_pdf_and_empty_sha_entries_do_not_create_a_reference_hold(self):
        target = self.uploaded(self.course(sha='b' * 64))
        self.assertEqual(uploaded_target_reference(target, [self.course()]), (None, None))
        target['source_sha256'] = ''; blank = self.course(sha='', state='needs_review')
        self.assertEqual(uploaded_target_reference(target, [blank]), (None, None))
        target.pop('source_sha256'); blank.pop('source_sha256')
        self.assertEqual(uploaded_target_reference(target, [blank]), (None, None))

    def test_hours_warnings_general_status_and_missing_total_are_not_topic_defects(self):
        target = self.uploaded(self.course(sha='b' * 64))
        target.update(warnings=['نهاية جدول الموضوعات غير مثبتة', 'بعض الساعات غير محسومة', 'صفوف ناقصة'], status='needs_review',
                      table_evidence=[{'total': False}], hours=34)
        target['topics'][0].update(hours_status='missing_in_source', hours_raw='', row_number_status='unreadable_label_preserved')
        summary = extraction_integrity(target); self.assertEqual(summary['issues'], [])
        target['extraction_integrity'] = summary
        self.assertEqual(uploaded_target_reference(target, []), (None, None))

    def test_explicit_partial_table_defects_hold_known_and_unknown_targets(self):
        variations = [({'table_evidence':[{'empty_rows':[{'label':'7'}]}]}, 'empty_topic_rows'),
                      ({'additional_topic_sections':[{'page':10}]}, 'ambiguous_topic_sections'),
                      ({'fixed_topic_curriculum':False}, 'no_fixed_topics'),
                      ({'topic_table_incomplete':True}, 'incomplete_topic_table'),
                      ({'table_evidence':[{'missing_continuation':True}]}, 'incomplete_topic_table')]
        for extra, code in variations:
            for known in (False, True):
                course = self.course(); target = self.uploaded(course); target.update(extra)
                before = copy.deepcopy(target); summary = extraction_integrity(target)
                self.assertEqual(target, before); self.assertIn(code, summary['issues'])
                target['extraction_integrity'] = summary
                self.assertIsNone(uploaded_target_reference(target, [course] if known else [])[0])
                self.assertTrue(uploaded_target_reference(target, [course] if known else [])[1])
        target = self.uploaded(self.course()); target['topics'][0]['segmentation_status'] = 'merged_source_rows'
        self.assertEqual(extraction_integrity(target)['issues'], ['merged_source_rows'])
        target.update(additional_topic_sections=[{'page':10}], topic_sections_resolved=True)
        self.assertNotIn('ambiguous_topic_sections', extraction_integrity(target)['issues'])

    def test_integrity_summary_is_bound_to_sha_and_exact_effective_signature(self):
        target = self.uploaded(self.course(sha='b' * 64)); summary = extraction_integrity(target)
        expected = hashlib.sha256(json.dumps(['النص الأول', 'النص الثاني'], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(summary['effective_topics_sha256'], expected)
        target['extraction_integrity'] = summary
        for field, value in [('source_sha256','c'*64), ('topics',[{'id':'u0','text':'نص مختلف'}])]:
            changed = copy.deepcopy(target); changed[field] = value
            self.assertTrue(uploaded_target_reference(changed, [])[1])
        target['topics'][0].update(id='new-id', page=900, raw_audit='ignored')
        self.assertEqual(uploaded_target_reference(target, []), (None, None))

    def test_malformed_or_stale_summary_never_silently_disappears(self):
        original = self.uploaded(self.course()); summary = extraction_integrity(original)
        malformed = [None, [], {}, {**summary, 'version':'old'}, {**summary, 'issues':'empty_topic_rows'},
                     {**summary, 'issues':['unknown_code']}, {**summary, 'issues':[{}]},
                     {**summary, 'issues':['no_fixed_topics','no_fixed_topics']}, {**summary,'extra':'not-schema'},
                     {**summary, 'effective_topics_sha256':123}, {**summary, 'source_sha256':''}]
        for value in malformed:
            target = copy.deepcopy(original); target['extraction_integrity'] = value
            with self.subTest(value=value):
                self.assertTrue(uploaded_target_reference(target, [self.course()])[1])

    def test_canonical_hash_readiness_must_be_valid_even_when_effective_text_matches(self):
        course = self.course(); target = self.uploaded(course)
        course['reference_quality']['topics_sha256'] = 'stale'
        self.assertTrue(uploaded_target_reference(target, [course])[1])
        self.assertEqual(uploaded_target_reference({'source_sha256':'x','topics':'malformed'}, [] )[0], None)
        self.assertTrue(uploaded_target_reference({'source_sha256':'x','topics':'malformed'}, [])[1])


if __name__ == '__main__':
    unittest.main()
