"""Academic uncertainty, grounded facets and production second-pass regressions."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from scripts.equivalency.engine import compare, calculate, RUBRIC_VERSION
from scripts.equivalency.grounding import validate_reviews, adjudicate, validate_evidence, VERSION


def topic(key,text): return {'id':key,'text':text}
def proposal(t,s,score=85):
    return {'target_id':t['id'],'source_id':s['id'],'score':score,'target_quote':t['text'],'source_quote':s['text'],'reason':'يتناول الموضوع والنطاق المثبتين في النص.'}
def review(t,s,verdict='strict',supports=None):
    possible={'strict':['strict','lenient'],'lenient':['lenient'],'weak':[],'uncertain':['strict','lenient']}[verdict]
    return {'target_id':t['id'],'source_id':s['id'],'verdict':verdict,'possible_tiers':possible,'reason':'يغطي النص المتطلبات المبينة في أدلة الأجزاء.',
            'facets':supports or [{'target_quote':t['text'],'source_quote':s['text'],'support':{'strict':'aligned','lenient':'related','weak':'absent','uncertain':'unknown'}[verdict]}]}
def answer(value): return {'done_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}


class FacetGroundingTests(unittest.TestCase):
    def setUp(self):
        self.t=topic('t','تعريف العقد وأركانه وآثاره')
        self.s=topic('s','مفهوم العقد وأركانه والآثار المترتبة عليه')

    def check(self,r):return validate_reviews([r],[self.t],[self.s],{('t','s')})

    def test_every_material_character_of_target_must_be_reviewed(self):
        r=review(self.t,self.s);r['facets'][0]['target_quote']='تعريف العقد'
        with self.assertRaisesRegex(ValueError,'سقطت أجزاء'):self.check(r)

    def test_repeated_target_words_do_not_need_fabricated_weights(self):
        self.check(review(self.t,self.s))

    def test_missing_compound_requirement_cannot_be_hidden_by_high_verdict(self):
        r=review(self.t,self.s,supports=[{'target_quote':'تعريف العقد وأركانه','source_quote':'مفهوم العقد وأركانه','support':'aligned'},
                                    {'target_quote':'وآثاره','source_quote':'','support':'absent'}])
        with self.assertRaisesRegex(ValueError,'جزء غير مثبت'):self.check(r)

    def test_other_unit_quote_cannot_be_borrowed(self):
        r=review(self.t,self.s);r['facets'][0]['source_quote']='شروط صحة العقد'
        with self.assertRaisesRegex(ValueError,'خارج مفردة'):self.check(r)

    def test_unknown_is_not_definitive_absence(self):
        r=review(self.t,self.s,'weak');r['facets'][0]['support']='unknown'
        with self.assertRaisesRegex(ValueError,'غير محسوم'):self.check(r)
        self.check(review(self.t,self.s,'uncertain'))

    def test_positive_relation_needs_literal_source_evidence(self):
        r=review(self.t,self.s);r['facets'][0]['source_quote']=''
        with self.assertRaisesRegex(ValueError,'دون دليل'):self.check(r)

    def test_review_must_include_every_requested_pair(self):
        with self.assertRaisesRegex(ValueError,'لم تكتمل'):validate_reviews([],[self.t],[self.s],{('t','s')})

    def test_strict_numeric_boundary_is_guarded_even_in_possible_edges(self):
        t=topic('t','تفسير سورة البقرة الآيات1-100');s=topic('s','تفسير سورة البقرة الآيات1-79')
        with self.assertRaisesRegex(ValueError,'نطاق الآيات'):validate_reviews([review(t,s)],[t],[s],{('t','s')})
        r=review(t,s,'lenient');validate_reviews([r],[t],[s],{('t','s')})

    def test_disagreement_preserves_original_grade_and_blocks_premature_acceptance(self):
        p=proposal(self.t,self.s,85);r=review(self.t,self.s,'lenient')
        result=adjudicate([p],[r],[self.t],[self.s]);self.assertEqual(result[0]['score'],85)
        self.assertEqual(result[0]['adjudication']['state'],'uncertain')
        calc=calculate([self.t],[self.s],result);self.assertEqual(calc['decision'],'insufficient')
        self.assertEqual(calc['adjudication_review']['possible_strict_count'],1)
        self.assertIsNone(calc['strict_coverage'])

    def test_agreement_confirms_weak_strict_and_lenient_without_changing_grades(self):
        for score,tier,decision in [(87,'strict','equivalent'),(63,'lenient','lenient'),(35,'weak','not_equivalent')]:
            pairs=adjudicate([proposal(self.t,self.s,score)],[review(self.t,self.s,tier)],[self.t],[self.s])
            self.assertEqual(calculate([self.t],[self.s],pairs)['decision'],decision)
            self.assertEqual(pairs[0]['score'],score)

    def test_unscored_alternative_prevents_false_rejection(self):
        other=topic('s2','تعريف العقد وأركانه وآثاره وأحكامه')
        pairs=adjudicate([proposal(self.t,self.s,30)],[review(self.t,self.s,'weak'),review(self.t,other)], [self.t],[self.s,other])
        self.assertIsNone(pairs[1]['score']);self.assertTrue(pairs[1]['candidate_only'])
        self.assertEqual(calculate([self.t],[self.s,other],pairs)['decision'],'insufficient')

    def test_segmentation_hold_remains_independent_of_optimistic_matching_bound(self):
        targets=[topic('t1','أحكام الاستعاذة'),topic('t2','أحكام البسملة')];source=[topic('s','أحكام الاستعاذة وأحكام البسملة')]
        pairs=adjudicate([proposal(t,source[0]) for t in targets],[review(t,source[0]) for t in targets],targets,source)
        calc=calculate(targets,source,pairs);self.assertEqual(calc['decision'],'insufficient');self.assertIn('segmentation_review',calc)

    def test_unresolved_strict_cannot_be_published_as_definitive_lenient(self):
        ts=[topic('t'+str(i),'موضوع '+str(i)) for i in range(5)];ss=[topic('s'+str(i),'دراسة موضوع '+str(i)) for i in range(5)]
        raw=[proposal(t,s,65 if i==4 else 85) for i,(t,s) in enumerate(zip(ts,ss))]
        reviews=[review(t,s,'strict' if i<3 else 'lenient') for i,(t,s) in enumerate(zip(ts,ss))]
        pairs=adjudicate(raw,reviews,ts,ss);c=calculate(ts,ss,pairs)
        self.assertEqual(c['decision'],'insufficient');self.assertEqual(c['adjudication_review']['confirmed_lenient_count'],4)
        self.assertEqual(c['adjudication_review']['possible_strict_count'],4)

    def test_four_fifths_confirmed_remain_accepted_despite_irrelevant_uncertainty(self):
        ts=[topic('t'+str(i),'موضوع '+str(i)) for i in range(5)];ss=[topic('s'+str(i),'دراسة موضوع '+str(i)) for i in range(5)]
        pairs=adjudicate([proposal(t,s) for t,s in zip(ts,ss)],[review(t,s,'strict' if i<4 else 'uncertain') for i,(t,s) in enumerate(zip(ts,ss))],ts,ss)
        self.assertEqual(calculate(ts,ss,pairs)['decision'],'equivalent')

    def test_tampered_cached_bounds_or_states_are_rejected(self):
        pairs=adjudicate([proposal(self.t,self.s)],[review(self.t,self.s,'lenient')],[self.t],[self.s])
        e={'provider':'ollama_local','rubric':RUBRIC_VERSION,'pairs':pairs}
        validate_evidence(e,[self.t],[self.s]);pairs[0]['adjudication']['state']='confirmed'
        with self.assertRaisesRegex(ValueError,'تغيرت'):validate_evidence(e,[self.t],[self.s])

    def test_raw_proposal_is_never_accepted_as_complete_production_evidence(self):
        with self.assertRaisesRegex(ValueError,'مفقودة'):validate_evidence({'provider':'ollama_local','rubric':RUBRIC_VERSION,'pairs':[proposal(self.t,self.s)]},[self.t],[self.s])

    def test_fake_exact_provider_cannot_skip_grounding(self):
        with self.assertRaisesRegex(ValueError,'حرفي'):validate_evidence({'provider':'exact_text','rubric':RUBRIC_VERSION,'pairs':[proposal(self.t,self.s,100)]},[self.t],[self.s])

    def test_production_review_is_score_blinded_and_confirmed_cache_revalidated(self):
        with tempfile.TemporaryDirectory() as folder,patch('scripts.equivalency.engine.local_model',return_value='digest'),patch('scripts.equivalency.engine.post_ollama',side_effect=[answer({'pairs':[proposal(self.t,self.s)]}),answer({'reviews':[review(self.t,self.s)]})]) as call:
            e=compare({'topics':[self.t]},{'topics':[self.s]},cache_dir=folder)
            self.assertEqual(calculate([self.t],[self.s],e['pairs'])['decision'],'equivalent')
            payload=json.loads(call.call_args_list[1].args[1]['messages'][1]['content'])
            self.assertEqual(set(payload),{'target','source','requested_pairs'})
            self.assertNotIn('score',json.dumps(payload));self.assertNotIn('reason',json.dumps(payload))
            self.assertEqual(compare({'topics':[self.t]},{'topics':[self.s]},cache_dir=folder)['pairs'],e['pairs'])
            self.assertEqual(call.call_count,2)

    def test_bounded_disagreement_never_caps_a_score_to_fit_review(self):
        with tempfile.TemporaryDirectory() as folder,patch('scripts.equivalency.engine.local_model',return_value='digest'),patch('scripts.equivalency.engine.post_ollama',side_effect=[answer({'pairs':[proposal(self.t,self.s)]}),answer({'reviews':[review(self.t,self.s,'lenient')]}),answer({'pairs':[proposal(self.t,self.s)]})]) as call:
            e=compare({'topics':[self.t]},{'topics':[self.s]},cache_dir=folder)
            self.assertEqual(call.call_count,3) # identical blinded review reuses its validated cache
            self.assertEqual(e['pairs'][0]['score'],85);self.assertEqual(e['pairs'][0]['adjudication']['state'],'uncertain')
            self.assertEqual(calculate([self.t],[self.s],e['pairs'])['decision'],'insufficient')

if __name__=='__main__':unittest.main()
