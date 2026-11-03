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
    def test_failed_or_uncertain_visual_reading_cannot_turn_into_definitive_rejection(self):
        for status in ('unavailable','uncertain'):
            source={'upload_pdf':True,'visual_verification_status':status,'topics':[{'id':'s','text':'التةريف بة م ثصول الفقو'}]}
            evidence=reference_pending(self.t,source,RUBRIC_VERSION)
            self.assertTrue(evidence['reference_review']['source'])
            calculation=evidence_calculation(self.t['topics'],source['topics'],evidence)
            self.assertEqual(calculation['decision'],'insufficient')
            self.assertIsNone(calculation['strict_coverage']);self.assertIsNone(calculation['lenient_coverage'])
    def test_completed_upload_or_manual_entry_is_not_held_by_this_visual_gate(self):
        for source in ({'upload_pdf':True,'visual_verification_status':'completed'},
                       {'upload_pdf':False,'visual_verification_status':'unavailable'},{}):
            self.assertIsNone(reference_problem(source))
    def test_empty_or_forged_reference_state_does_not_authorize_judgment(self):
        for state in ('',None,'verified','ready'):
            self.quality['state']=state;self.assertTrue(reference_problem(self.t))

class IncompleteEvidenceCalculationTests(unittest.TestCase):
    def setUp(self):
        self.targets=[{'id':'t1','text':'موضوع هدف موثق'},{'id':'t2','text':'موضوع آخر'}]
        self.sources=[{'id':'s1','text':'موضوع مصدر موثق'}]
        self.evidence={'provider':'audit_incomplete','rubric':RUBRIC_VERSION,'pairs':[],
                       'evidence_review':{'stage':'facet_review','target_ids':['t2']}}

    def test_formal_incomplete_study_has_no_partial_or_zero_grade(self):
        original=copy.deepcopy(self.evidence)
        calculation=evidence_calculation(self.targets,self.sources,self.evidence)
        self.assertEqual(calculation['decision'],'insufficient')
        self.assertIsNone(calculation['strict_coverage']);self.assertIsNone(calculation['lenient_coverage'])
        self.assertEqual(calculation['strict_matches'],{});self.assertEqual(calculation['lenient_matches'],{})
        self.assertEqual(calculation['reason'],'لم تثبت أدلة توافق كافية لحسم المعادلة.')
        self.assertEqual(calculation['evidence_review'],self.evidence['evidence_review'])
        self.assertEqual(self.evidence,original)
        calculation['evidence_review']['target_ids'].append('t1')
        self.assertEqual(self.evidence,original)

    def test_incomplete_evidence_requires_exact_current_bound_contract(self):
        variants=[]
        for field,value in [('rubric','old-rubric'),('pairs',[{'score':100}]),('debug','/private/raw.json')]:
            changed=copy.deepcopy(self.evidence);changed[field]=value;variants.append(changed)
        for review in [{},{'stage':'network','target_ids':['t1']},{'stage':'proposal','target_ids':[]},
                       {'stage':'proposal','target_ids':['foreign']},{'stage':'proposal','target_ids':['t1','t1']},
                       {'stage':'proposal','target_ids':['t1'],'reason':'private model response'},
                       {'stage':'proposal','target_ids':[None]}]:
            changed=copy.deepcopy(self.evidence);changed['evidence_review']=review;variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                evidence_calculation(self.targets,self.sources,changed)
