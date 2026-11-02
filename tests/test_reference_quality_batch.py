import copy
import unittest
from scripts.apply_reference_quality import prepared_catalog
from scripts.equivalency.reference_quality import topics_digest

class ReferenceQualityBatchTests(unittest.TestCase):
    def setUp(self):
        self.course={'id':'c','source_sha256':'sha','source_pdf':'assets/c.pdf','topics':[{'id':'t','text':'نص الموضوع'}]}
        self.catalog={'courses':[self.course],'library_sources':[]}
        self.record={'source_sha256':'sha','review_status':'verified_native','risk_reasons':[],'proposed_topics':copy.deepcopy(self.course['topics'])}
        self.native={'proofs':[{'source_sha256':'sha','course_proofs':[{'course_id':'c','canonical_topics_sha256':topics_digest(self.course['topics'])}]}]}
    def prepare(self,batches=None,overrides=None):
        return prepared_catalog(self.catalog,{'sha':self.record},self.native,batches or {},'test-quality',overrides)[0]['courses'][0]['reference_quality']
    def test_exact_bound_native_proof_is_distinguished_from_visual_approval(self):
        q=self.prepare();self.assertEqual(q['state'],'ready_native');self.assertFalse(q['human_visual_review'])
    def test_count_delta_or_source_or_original_text_change_blocks_native(self):
        for mutation in ('count','source','text','risk'):
            with self.subTest(mutation=mutation):
                old=copy.deepcopy(self.record);original=copy.deepcopy(self.course)
                if mutation=='count':self.record['proposed_topics'].append({'id':'t2','text':'موضوع آخر'})
                if mutation=='source':self.course['source_sha256']='another'
                if mutation=='text':self.course['topics'][0]['text']='نص جديد'
                if mutation=='risk':self.record['risk_reasons']=['denominator_changes']
                if mutation=='source':
                    with self.assertRaisesRegex(ValueError,'missing'):self.prepare()
                else:self.assertEqual(self.prepare()['state'],'needs_review')
                self.record=old;self.course.clear();self.course.update(original)
    def test_curated_approval_requires_current_topics_in_the_bound_batch(self):
        self.course['reference_review']={'batch_id':'visual','source_sha256':'sha','kind':'visual_source_transcription'}
        batch={'proof.json':{'batch_id':'visual','entries':[{'course_ids':['c'],'source_sha256':'sha','topics':copy.deepcopy(self.course['topics'])}]}}
        self.assertEqual(self.prepare(batch)['state'],'ready_visual')
        self.course['topics'][0]['text']='later unchecked edit'
        self.assertEqual(self.prepare(batch)['state'],'needs_review')
    def test_scope_hold_and_academic_override_take_precedence(self):
        self.course['reference_review']={'comparison_readiness':'scope_incomplete','readiness_reason':'نطاق ناقص'}
        self.assertEqual(self.prepare()['state'],'needs_review')
        self.assertEqual(self.prepare()['reason'],'نطاق ناقص')
        self.assertEqual(self.prepare(overrides={'sha':'تعارض المصدر'})['reason'],'تعارض المصدر')
    def test_verified_no_fixed_topics_override_is_explicit_not_native_approval(self):
        q=self.prepare(overrides={'sha':{'state':'no_fixed_topics','reason':'لا قائمة موضوعات ثابتة'}})
        self.assertEqual(q['state'],'no_fixed_topics');self.assertEqual(q['reason'],'لا قائمة موضوعات ثابتة')

    def test_unknown_source_cannot_be_silently_skipped(self):
        with self.assertRaises(ValueError):prepared_catalog(self.catalog,{},self.native,{},'batch')
    def test_input_catalog_is_not_mutated_by_preparation(self):
        before=copy.deepcopy(self.catalog);self.prepare();self.assertEqual(self.catalog,before)
