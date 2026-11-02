import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from scripts.equivalency.atomic_scope import atomic_pairs,confirmed_atomic
from scripts.equivalency.engine import compare,calculate,RUBRIC_VERSION
from scripts.equivalency.grounding import decode_reviews,span_quote,validate_reviews,validate_evidence,VERSION


def topic(key,text):return {'id':key,'text':text}
def analytical(key,first,last):return topic(key,f'تفسير سورة البقرة الآيات{first}-{last} تفسيرا تحليليا.')
def response(value):return {'done_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}


class AtomicScopeTests(unittest.TestCase):
    def test_thresholds_are_inclusive_exact_integer_intersections_not_completeness(self):
        t=analytical('t',1,100)
        for end,score,decision in [(80,80,'equivalent'),(79,79,'lenient'),(60,60,'lenient'),(59,59,'not_equivalent')]:
            with self.subTest(end=end),patch('scripts.equivalency.engine.local_model',side_effect=AssertionError('numeric proof needs no model')):
                s=analytical('s',1,end);e=compare({'topics':[t]},{'topics':[s]})
                self.assertEqual(e['provider'],'exact_scope');self.assertEqual(e['pairs'][0]['score'],score)
                self.assertEqual(calculate([t],[s],e['pairs'])['decision'],decision)

    def test_printed_page_descending_bounds_and_paraphrased_atomic_method(self):
        t=topic('t','تفسير ص:3 البقرة ( 16 -6 ) تفسيرا تحليليا.')
        s=topic('s','دراسة تفسيرية تحليلية لسورة البقرة، الآيات6-12.')
        pairs=atomic_pairs([t],[s]);self.assertEqual(pairs[0]['score'],63)
        self.assertEqual(pairs[0]['scope_proof']['intersection_verses'],7)
        self.assertEqual(calculate([t],[s],pairs)['decision'],'lenient')

    def test_different_chapter_has_no_atomic_material_overlap(self):
        t=analytical('t',1,5);s=topic('s','تفسير سورة النساء الآيات1-5 تفسيرا تحليليا.')
        self.assertEqual(atomic_pairs([t],[s])[0]['score'],0)

    def test_extra_material_mixed_method_or_ambiguous_scope_stays_semantic(self):
        t=analytical('t',1,5)
        for text in ['حفظ سورة البقرة الآيات1-5','تفسير سورة البقرة الآيات1-5',
                     'تفسير سورة البقرة الآيات1-5 تفسيرا تحليليا وأسباب نزولها',
                     'تفسير سورة البقرة الآيات1-5 تفسيرا تحليليا وفق المنهج المقارن',
                     'دراسة تفسيرية تحليلية لسورة البقرة، الآيات1-5 والآيات10-15.',
                     'دراسة تفسيرية تحليلية لسورة غير محددة، الآيات1-5.']:
            with self.subTest(text=text):self.assertIsNone(atomic_pairs([t],[topic('s',text)]))

    def test_atomic_proof_is_recomputed_at_evidence_validation(self):
        ts=[analytical('t',1,5)];ss=[analytical('s',1,4)]
        e=compare({'topics':ts},{'topics':ss});validate_evidence(e,ts,ss)
        e['pairs'][0]['score']=100
        with self.assertRaises(ValueError):validate_evidence(e,ts,ss)

    def test_numeric_edges_in_mixed_curricula_are_calculated_outside_semantic_grading(self):
        ts=[topic('t0','نشأة علم التفسير'),analytical('t1',25,29)]
        ss=[topic('s0','تاريخ نشأة التفسير وتطوره'),analytical('s1',25,28)]
        intro={'target_id':'t0','source_id':'s0','score':85,'reason':'توافق موضوع النشأة والتاريخ','target_quote':ts[0]['text'],'source_quote':ss[0]['text']}
        wrong={'target_id':'t1','source_id':'s1','score':60,'reason':'تغطية بعض الآيات','target_quote':ts[1]['text'],'source_quote':ss[1]['text']}
        review={'target_id':'t0','source_id':'s0','verdict':'strict','possible_tiers':['strict','lenient'],'reason':'يتناول الطرفان نشأة العلم',
                'facets':[{'target_start':1,'target_end':3,'source_quote':ss[0]['text'],'support':'aligned'}]}
        with tempfile.TemporaryDirectory() as folder,patch('scripts.equivalency.engine.local_model',return_value='local'),patch('scripts.equivalency.engine.post_ollama',side_effect=[response({'pairs':[intro,wrong]}),response({'reviews':[review]})]) as call:
            e=compare({'topics':ts},{'topics':ss},cache_dir=folder)
            self.assertEqual(calculate(ts,ss,e['pairs'])['decision'],'equivalent')
            atomic=next(p for p in e['pairs'] if p['target_id']=='t1')
            self.assertEqual(atomic['score'],80);self.assertEqual(atomic['adjudication']['kind'],'deterministic_atomic_scope')
            payload=json.loads(call.call_args_list[1].args[1]['messages'][1]['content'])
            self.assertEqual(payload['resolved_pairs'],[{'target_id':'t1','source_id':'s1'}])
            self.assertEqual(payload['requested_pairs'],[{'target_id':'t0','source_id':'s0'}])
            validate_evidence(e,ts,ss)
            atomic['score']=60
            with self.assertRaisesRegex(ValueError,'ذري'):validate_evidence(e,ts,ss)


class WordBoundaryEvidenceTests(unittest.TestCase):
    def test_arabic_conjunction_is_copied_from_original_token_without_retyping(self):
        t=topic('t','تعريف العقد وأركانه وشروط صحته.')
        r={'target_id':'t','source_id':'s','facets':[{'target_start':3,'target_end':3,'source_quote':'أركان العقد','support':'aligned'}]}
        decoded=decode_reviews([r],[t]);self.assertEqual(decoded[0]['facets'][0]['target_quote'],'وأركانه')
        self.assertEqual(decoded[0]['facets'][0]['target_token_span'],[3,3]);self.assertEqual(r['facets'][0]['target_start'],3)

    def test_original_waw_in_wasail_cannot_be_silently_removed(self):
        self.assertEqual(span_quote('وسائل البحث',1,1),'وسائل')
        self.assertNotEqual(span_quote('وسائل البحث',1,1),'سائل')

    def test_out_of_bounds_reversed_and_boolean_indices_are_rejected(self):
        for first,last in [(0,1),(1,3),(2,1),(True,2),('1',2)]:
            with self.subTest(first=first,last=last),self.assertRaises(ValueError):span_quote('كلمتان فقط',first,last)

    def test_tampered_word_boundaries_do_not_survive_cache_validation(self):
        t=topic('t','تعريف العقد وأركانه');s=topic('s','مفهوم العقد وأركانه')
        r={'target_id':'t','source_id':'s','verdict':'strict','possible_tiers':['strict','lenient'],'reason':'المحتوى صريح',
           'facets':[{'target_start':1,'target_end':3,'source_quote':s['text'],'support':'aligned'}]}
        rows=decode_reviews([r],[t]);validate_reviews(rows,[t],[s],{('t','s')})
        rows[0]['facets'][0]['target_token_span']=[1,2]
        with self.assertRaisesRegex(ValueError,'حدود كلمات'):validate_reviews(rows,[t],[s],{('t','s')})

    def test_skipping_a_whole_material_word_is_still_rejected(self):
        t=topic('t','تعريف العقد وأركانه');s=topic('s','مفهوم العقد وأركانه')
        r={'target_id':'t','source_id':'s','verdict':'strict','possible_tiers':['strict','lenient'],'reason':'المحتوى صريح',
           'facets':[{'target_start':1,'target_end':2,'source_quote':s['text'],'support':'aligned'}]}
        with self.assertRaisesRegex(ValueError,'سقطت'):validate_reviews(decode_reviews([r],[t]),[t],[s],{('t','s')})
