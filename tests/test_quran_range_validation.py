"""Explicit Quran scope bounds cannot be promoted by a model or stale cache."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.equivalency.ranges import topic_scopes, range_issues, required_scope_pairs
from scripts.equivalency.engine import validate_pairs, compare, SYSTEM, PAIR_SCHEMA, RUBRIC_VERSION


def topic(tid,text):return {'id':tid,'text':text}
def case(target_text,source_text,score):
    target=[topic('t',target_text)];source=[topic('s',source_text)]
    pair={'target_id':'t','source_id':'s','score':score,'target_quote':target_text,'source_quote':source_text,'reason':'تقدير نطاق الموضوع'}
    return target,source,[pair]
def response(pairs):return {'message':{'content':json.dumps({'pairs':pairs},ensure_ascii=False)},'done_reason':'stop'}


class QuranRangeTests(unittest.TestCase):
    def test_real_two_of_eleven_cannot_score_eighty(self):
        target,source,pairs=case('تفسير سورة البقرة الآيات38-48','سورة البقرة المقطع الثاني الآيات21-39',80)
        issues=range_issues(pairs,target,source)
        self.assertEqual((issues[0]['intersection_verses'],issues[0]['target_verses']),(2,11))
        self.assertAlmostEqual(issues[0]['target_range_coverage'],18.1818)
        with self.assertRaisesRegex(ValueError,'verse_scope.*target_id.*t.*source_id.*s'):
            validate_pairs(pairs,target,source)
        self.assertEqual(pairs[0]['score'],80)

    def test_real_four_of_eight_cannot_score_seventy_five(self):
        target,source,pairs=case('سورة البقرة الآيات17-24','سورة البقرة الآيات1-20',75)
        self.assertEqual(range_issues(pairs,target,source)[0]['target_range_coverage'],50)
        with self.assertRaises(ValueError):validate_pairs(pairs,target,source)

    def test_partial_thresholds_are_integer_exact(self):
        for end,score,valid in [(8,80,True),(7,80,False),(6,60,True),(6,79,True),(5,60,False),(5,59,True)]:
            with self.subTest(end=end,score=score):
                target,source,pairs=case('سورة البقرة الآيات1-10',f'سورة البقرة الآيات1-{end}',score)
                self.assertEqual(not range_issues(pairs,target,source),valid)

    def test_reversed_arabic_digits_and_segment_cue(self):
        target,source,pairs=case('سورة البقرة الآيات١-٢٠','سورة البقرة المقطع الأول (٢٠–١)',80)
        self.assertEqual(range_issues(pairs,target,source),[])
        self.assertEqual(topic_scopes(source)['s'].end,20)

    def test_from_verse_to_verse_and_single_verse(self):
        target,source,pairs=case('سورة البقرة من الآية38 إلى الآية48','سورة البقرة الآيات38-48',90)
        self.assertFalse(range_issues(pairs,target,source))
        self.assertEqual(topic_scopes([topic('one','سورة الفاتحة الآية٣')])['one'].start,3)

    def test_chapter_changes_are_carried_from_the_confirmed_row(self):
        rows=[topic('a','التعريف بسورة الفاتحة'),topic('b','الآيات1-7'),
              topic('c','التعريف بسورة البقرة ومقاصدها ومواقف الناس'),topic('d','المقطع الثاني21-39')]
        scopes=topic_scopes(rows)
        self.assertEqual((scopes['b'].chapter,scopes['d'].chapter),(1,2))
        self.assertEqual(scopes['d'].chapter_topic_id,'c')

    def test_explicit_raw_target_page_number_is_not_a_verse(self):
        rows=[topic('a','مقدمة عن سورة البقرة – تفسير ص:2 البقرة الآيات1-5'),
              topic('b','تفسير ص:7 البقرة الآيات38-48')]
        scopes=topic_scopes(rows)
        self.assertEqual((scopes['a'].start,scopes['a'].end),(1,5))
        self.assertEqual((scopes['b'].chapter,scopes['b'].start),(2,38))

    def test_common_word_people_is_not_a_second_chapter(self):
        scopes=topic_scopes([topic('a','سورة البقرة الآيات1-20 (مواقف الناس من القرآن)')])
        self.assertEqual(scopes['a'].chapter,2)

    def test_multiple_chapters_clear_context_without_guessing(self):
        for text in ['سورة البقرة وسورة آل عمران الآيات1-20','سورة البقرة وآل عمران الآيات1-20','سورتي البقرة وآل عمران الآيات1-20']:
            with self.subTest(text=text):
                scopes=topic_scopes([topic('a','سورة البقرة'),topic('b',text),topic('c','الآيات1-20')])
                self.assertIsNone(scopes['b']);self.assertIsNone(scopes['c'])

    def test_unknown_chapter_clears_context(self):
        scopes=topic_scopes([topic('a','سورة البقرة'),topic('b','سورة غير محددة الآيات1-20'),topic('c','الآيات1-20')])
        self.assertIsNone(scopes['b']);self.assertIsNone(scopes['c'])

    def test_standalone_chapter_label_bound_to_verse_cue_is_confirmed(self):
        target,source,pairs=case('البقرة الآيات38-48','البقرة المقطع الثاني21-39',80)
        self.assertEqual(range_issues(pairs,target,source)[0]['intersection_verses'],2)

    def test_no_chapter_no_guard(self):
        target,source,pairs=case('الآيات38-48','الآيات21-39',90)
        self.assertFalse(range_issues(pairs,target,source))

    def test_two_ranges_in_one_topic_are_ambiguous(self):
        scopes=topic_scopes([topic('t','سورة البقرة الآيات1-20 والآيات30-40')])
        self.assertIsNone(scopes['t'])

    def test_unlabelled_dates_codes_pages_and_durations_are_ignored(self):
        examples=['سورة البقرة 1-20','سورة البقرة ص:2-5','سورة البقرة العام1446-1447',
                  'سورة البقرة المقرر200123-2','سورة البقرة المقطع1-20أسبوعا',
                  'سورة البقرة المقطع الخاص بالأحاديث1-20','سورة البقرة الحديث1-20',
                  'سورة البقرة المقطع رمز المقرر200-2','سورة البقرة المقطع تاريخ1-20',
                  'سورة البقرة المقطع1-20من الأحاديث']
        for text in examples:
            with self.subTest(text=text):self.assertIsNone(topic_scopes([topic('t',text)])['t'])

    def test_explicit_verse_range_survives_an_unrelated_teaching_duration(self):
        scope=topic_scopes([topic('t','سورة البقرة الآيات1-20 خلال3أسابيع')])['t']
        self.assertEqual((scope.start,scope.end),(1,20))

    def test_different_confirmed_chapters_reject_promotion_but_keep_weak_scores(self):
        target,source,pairs=case('سورة البقرة الآيات1-10','سورة آل عمران الآيات1-10',60)
        self.assertEqual(range_issues(pairs,target,source)[0]['field'],'surah')
        pairs[0]['score']=59
        self.assertEqual(validate_pairs(pairs,target,source)[0]['score'],59)

    def test_corrected_text_is_authoritative_for_reviewed_scope(self):
        target,source,pairs=case('سورة البقرة الآيات1-10','نص خام غير مقروء',80)
        source[0]['corrected_text']='سورة آل عمران الآيات1-10';pairs[0]['source_quote']=source[0]['corrected_text']
        self.assertEqual(range_issues(pairs,target,source)[0]['field'],'surah')

    def test_all_scope_failures_and_quote_errors_are_collected(self):
        target=[topic('t1','سورة البقرة الآيات38-48'),topic('t2','سورة البقرة الآيات17-24')]
        source=[topic('s1','سورة البقرة الآيات21-39'),topic('s2','سورة البقرة الآيات1-20')]
        pairs=[{'target_id':t['id'],'source_id':s['id'],'score':80,'reason':'مقارنة','target_quote':t['text'],'source_quote':s['text']} for t,s in zip(target,source)]
        pairs[0]['target_quote']='اقتباس غير موجود'
        with self.assertRaises(ValueError) as caught:validate_pairs(pairs,target,source)
        self.assertIn('target_quote',str(caught.exception));self.assertIn('t1',str(caught.exception));self.assertIn('t2',str(caught.exception))
        self.assertIn('18.1818',str(caught.exception));self.assertIn('50.0',str(caught.exception))

    def test_identical_words_cannot_be_pinned_to_100_across_chapter_contexts(self):
        target=[topic('t0','سورة البقرة'),topic('t1','الآيات1-20')]
        source=[topic('s0','سورة آل عمران'),topic('s1','الآيات1-20')]
        pairs=[{'target_id':t['id'],'source_id':s['id'],'score':40,'reason':'تختلف السورة','target_quote':t['text'],'source_quote':s['text']} for t,s in zip(target,source)]
        with tempfile.TemporaryDirectory() as directory,patch('scripts.equivalency.engine.local_model',return_value='digest'),patch('scripts.equivalency.engine.post_ollama',return_value=response(pairs)):
            result=compare({'topics':target},{'topics':source},cache_dir=directory)
        self.assertEqual(next(p['score'] for p in result['pairs'] if p['target_id']=='t1'),40)


class CachedRangeTests(unittest.TestCase):
    def setup_cache(self,directory,target,source,pairs):
        payload={'target':[{'id':t['id'],'text':t['text']} for t in target], 'source':[{'id':s['id'],'text':s['text']} for s in source], 'required_scope_pairs':required_scope_pairs(target,source)}
        key=hashlib.sha256(json.dumps([RUBRIC_VERSION,SYSTEM,PAIR_SCHEMA,'digest',payload],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        cache=Path(directory)/(key+'.json');cache.write_text(json.dumps({'pairs':pairs,'provider':'old-cache'}));return cache

    def test_bad_cached_range_is_preserved_and_fresh_valid_inference_runs(self):
        target,source,bad=case('سورة البقرة الآيات38-48','سورة البقرة الآيات21-39',80)
        good=[dict(bad[0],score=20)]
        with tempfile.TemporaryDirectory() as directory:
            cache=self.setup_cache(directory,target,source,bad)
            with patch('scripts.equivalency.engine.local_model',return_value='digest'),patch('scripts.equivalency.engine.post_ollama',return_value=response(good)) as call:
                result=compare({'topics':target},{'topics':source},cache_dir=directory)
            self.assertEqual(call.call_count,1);self.assertEqual(result['pairs'][0]['score'],20)
            self.assertTrue(result['rejected_cached_evidence'])
            trace=json.loads((Path(directory)/'traces'/cache.name).read_text())
            self.assertEqual(trace['rejected_cache']['response']['pairs'][0]['score'],80)
            self.assertEqual(trace['attempts'][0]['response']['message']['content'],response(good)['message']['content'])

    def test_cache_and_two_invalid_fresh_responses_never_produce_a_judgment(self):
        target,source,bad=case('سورة البقرة الآيات38-48','سورة البقرة الآيات21-39',80)
        with tempfile.TemporaryDirectory() as directory:
            cache=self.setup_cache(directory,target,source,bad);original=cache.read_bytes()
            with patch('scripts.equivalency.engine.local_model',return_value='digest'),patch('scripts.equivalency.engine.post_ollama',return_value=response(bad)) as call:
                with self.assertRaisesRegex(ValueError,'لم يصدر حكم'):compare({'topics':target},{'topics':source},cache_dir=directory)
            self.assertEqual(call.call_count,2);self.assertEqual(cache.read_bytes(),original)
            trace=json.loads((Path(directory)/'traces'/cache.name).read_text());self.assertEqual(len(trace['attempts']),2)
            self.assertEqual(trace['rejected_cache']['response']['pairs'][0]['score'],80)
            self.assertIn('target_range_coverage',call.call_args_list[1].args[1]['messages'][-1]['content'])


    def test_cache_cannot_hide_a_required_better_source_with_weak_valid_quotes(self):
        target=[topic('t9','سورة البقرة الآيات38-48')]
        source=[topic('t3','سورة البقرة الآيات21-39'),topic('t4','سورة البقرة الآيات40-66')]
        weak={'target_id':'t9','source_id':'t3','score':10,'reason':'تقاطع ضعيف','target_quote':target[0]['text'],'source_quote':source[0]['text']}
        best=dict(weak,source_id='t4',score=80,source_quote=source[1]['text'])
        with tempfile.TemporaryDirectory() as directory:
            cache=self.setup_cache(directory,target,source,[weak])
            with patch('scripts.equivalency.engine.local_model',return_value='digest'),patch('scripts.equivalency.engine.post_ollama',return_value=response([best])) as call:
                result=compare({'topics':target},{'topics':source},cache_dir=directory)
            self.assertEqual(call.call_count,1)
            self.assertEqual(result['pairs'][0]['source_id'],'t4')
            trace=json.loads((Path(directory)/'traces'/cache.name).read_text())
            self.assertIn('required_scope_pair',trace['rejected_cache']['validation_error'])
            self.assertEqual(trace['rejected_cache']['response']['pairs'][0]['source_id'],'t3')


class RequiredScopePairTests(unittest.TestCase):
    def test_real_better_source_t9_t4_is_required(self):
        target=[topic('t9','سورة البقرة الآيات38-48')]
        source=[topic('t3','سورة البقرة الآيات21-39'),topic('t4','سورة البقرة الآيات40-66')]
        required=required_scope_pairs(target,source)
        self.assertEqual([(p['target_id'],p['source_id']) for p in required],[('t9','t4')])
        self.assertEqual(required[0]['intersection_verses'],9)
        weak={'target_id':'t9','source_id':'t3','score':10,'reason':'تقاطع ضعيف','target_quote':target[0]['text'],'source_quote':source[0]['text']}
        with self.assertRaisesRegex(ValueError,'required_scope_pair.*target_id|target_id.*t9.*source_id.*t4'):
            validate_pairs([weak],target,source)

    def test_lenient_alternatives_are_required_without_forcing_their_grades(self):
        target=[topic('t','سورة البقرة الآيات1-10')]
        source=[topic('s1','سورة البقرة الآيات1-6'),topic('s2','سورة البقرة الآيات5-10')]
        required=required_scope_pairs(target,source)
        self.assertEqual(len(required),2)
        self.assertTrue(all(p['target_range_coverage']==60 for p in required))
        pairs=[{'target_id':'t','source_id':s['id'],'score':40,'reason':'اختلاف محتوى أو عمق','target_quote':target[0]['text'],'source_quote':s['text']} for s in source]
        self.assertEqual([p['score'] for p in validate_pairs(pairs,target,source)],[40,40])

    def test_general_text_and_uncertain_chapters_do_not_create_matrix_obligations(self):
        target=[topic(f't{i}',f'موضوع فقهي مستقل{i}') for i in range(50)]
        source=[topic(f's{i}',f'موضوع تفسيري مستقل{i}') for i in range(50)]
        self.assertEqual(required_scope_pairs(target,source),[])
        self.assertEqual(required_scope_pairs([topic('t','الآيات1-20')],[topic('s','الآيات1-20')]),[])
        self.assertEqual(required_scope_pairs([topic('t','سورة البقرة الآيات1-20')],[topic('s','سورة آل عمران الآيات1-20')]),[])

    def test_payload_requires_missing_best_and_bounded_retry_repairs_it(self):
        target=[topic('t9','سورة البقرة الآيات38-48')]
        source=[topic('t3','سورة البقرة الآيات21-39'),topic('t4','سورة البقرة الآيات40-66')]
        weak={'target_id':'t9','source_id':'t3','score':10,'reason':'تقاطع ضعيف','target_quote':target[0]['text'],'source_quote':source[0]['text']}
        best=dict(weak,source_id='t4',score=80,reason='تقاطع تسع آيات من إحدى عشرة',source_quote=source[1]['text'])
        with tempfile.TemporaryDirectory() as directory,patch('scripts.equivalency.engine.local_model',return_value='digest'),patch('scripts.equivalency.engine.post_ollama',side_effect=[response([weak]),response([weak,best])]) as call:
            result=compare({'topics':target},{'topics':source},cache_dir=directory)
            payload=json.loads(call.call_args_list[0].args[1]['messages'][1]['content'])
            self.assertEqual(payload['required_scope_pairs'][0]['source_id'],'t4')
            self.assertIn('required_scope_pair',call.call_args_list[1].args[1]['messages'][-1]['content'])
            self.assertEqual(result['required_scope_pair_count'],1)
            self.assertEqual(result['validation_attempts'],2)
            self.assertEqual(result['pairs'][1]['score'],80)

    def test_exact_match_shortcut_cannot_skip_another_required_range(self):
        target=[topic('t','سورة البقرة الآيات1-10')]
        source=[topic('s1',target[0]['text']),topic('s2','سورة البقرة الآيات1-20 وتفسيرها')]
        pairs=[{'target_id':'t','source_id':s['id'],'score':40,'reason':'اختلاف العمق','target_quote':target[0]['text'],'source_quote':s['text']} for s in source]
        with tempfile.TemporaryDirectory() as directory,patch('scripts.equivalency.engine.local_model',return_value='digest'),patch('scripts.equivalency.engine.post_ollama',return_value=response(pairs)) as call:
            result=compare({'topics':target},{'topics':source},cache_dir=directory)
            self.assertEqual(call.call_count,1)
            self.assertEqual(result['required_scope_pair_count'],2)
            self.assertEqual(next(p['score'] for p in result['pairs'] if p['source_id']=='s2'),40)

    def test_more_than_thirty_required_edges_has_a_larger_output_budget(self):
        target=[topic('t','سورة البقرة الآيات1-10 تفسير تحليلي')]
        source=[topic(f's{i}',f'سورة البقرة الآيات1-10 شرح المصدر{i}') for i in range(31)]
        pairs=[{'target_id':'t','source_id':s['id'],'score':40,'reason':'اختلاف العمق','target_quote':target[0]['text'],'source_quote':s['text']} for s in source]
        with tempfile.TemporaryDirectory() as directory,patch('scripts.equivalency.engine.local_model',return_value='digest'),patch('scripts.equivalency.engine.post_ollama',return_value=response(pairs)) as call:
            result=compare({'topics':target},{'topics':source},cache_dir=directory)
            self.assertEqual(result['required_scope_pair_count'],31)
            self.assertGreater(call.call_args.args[1]['options']['num_predict'],7000)
            self.assertGreater(call.call_args.args[1]['options']['num_ctx'],16384)
