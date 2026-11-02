import copy
import unittest
from scripts.equivalency.reference_quality import reference_problem,reference_pending,topics_digest,evidence_calculation
from scripts.equivalency.engine import RUBRIC_VERSION

class ReferenceQualityTests(unittest.TestCase):
    def setUp(self):
        self.t={'source_sha256':'abc','topics':[{'id':'t','text':'موضوع موثق','page':1}]}
        self.quality={'state':'ready_native','source_sha256':'abc','topics_sha256':topics_digest(self.t['topics'])}
        self.t['reference_quality']=self.quality
    def test_intact_audited_reference_is_usable(self):self.assertIsNone(reference_problem(self.t))
    def test_glyph_or_count_changes_invalidate_reference_readiness(self):
        for mutation in ('text','count','sha'):
            t=copy.deepcopy(self.t)
            if mutation=='text':t['topics'][0]['text']='نص آخر'
            if mutation=='count':t['topics'].append({'id':'t2','text':'موضوع جديد'})
            if mutation=='sha':t['source_sha256']='different'
            self.assertTrue(reference_problem(t))
    def test_curated_incomplete_scope_cannot_be_cleared_by_native_ready_flag(self):
        self.t['reference_review']={'comparison_readiness':'scope_incomplete','readiness_reason':'تطبيقات غير مسماة'}
        self.assertEqual(reference_problem(self.t),'تطبيقات غير مسماة')
    def test_flagged_reference_returns_pending_without_fabricated_coverage(self):
        self.quality.update(state='needs_review',reason='لم تثبت جميع صفوف المرجع')
        e=reference_pending(self.t,{},RUBRIC_VERSION)
        self.assertEqual(e['pairs'],[])
        c=evidence_calculation(self.t['topics'],[{'id':'s','text':'موضوع موثق'}],e)
        self.assertEqual(c['decision'],'insufficient');self.assertIsNone(c['strict_coverage']);self.assertEqual(c['reference_review']['target'],'لم تثبت جميع صفوف المرجع')
    def test_uploads_and_manual_inputs_retain_existing_student_review_gate(self):
        self.assertIsNone(reference_problem({'topics':[{'id':'s','text':'مدخل طالب'}]}))
    def test_empty_or_forged_reference_state_does_not_authorize_judgment(self):
        for state in ('',None,'verified','ready'):
            self.quality['state']=state;self.assertTrue(reference_problem(self.t))
