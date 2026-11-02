import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.equivalency.engine import _propose_pairs as compare,calculate,search

class ResilienceTests(unittest.TestCase):
    def comparison_data(self):
        t={'topics':[{'id':'t1','text':'نشأة علم القراءات وتطوره'}]}
        s={'topics':[{'id':'s1','text':'تاريخ القراءات القرآنية ومراحل نشأتها'}]}
        p={'target_id':'t1','source_id':'s1','score':85,'reason':'يتناول الطرفان نشأة العلم وتاريخه',
           'target_quote':'نشأة علم القراءات','source_quote':'تاريخ القراءات القرآنية'}
        return t,s,p
    def response(self,pair):
        return {'message':{'content':json.dumps({'pairs':[pair]},ensure_ascii=False)},'done_reason':'stop'}
    def test_invalid_quote_is_traced_and_retried_once_without_weakening_validation(self):
        t,s,p=self.comparison_data();bad={**p,'source_quote':'اقتباس لم يرد في المقرر'}
        with tempfile.TemporaryDirectory() as folder,patch('scripts.equivalency.engine.local_model',return_value='local'),patch('scripts.equivalency.engine.post_ollama',side_effect=[self.response(bad),self.response(p)]) as model:
            result=compare(t,s,cache_dir=folder)
            self.assertEqual(result['validation_attempts'],2)
            self.assertEqual(result['pairs'][0]['score'],85)
            self.assertEqual(model.call_count,2)
            trace=json.loads(next((Path(folder)/'traces').glob('*.json')).read_text())
            self.assertIn('source_quote',trace['attempts'][0]['validation_error'])
            self.assertIn('t1',model.call_args.args[1]['messages'][-1]['content'])
    def test_repeated_invalid_quotes_never_create_a_cached_report_or_judgment(self):
        t,s,p=self.comparison_data();p['target_quote']='نص غير موجود في المفردة'
        with tempfile.TemporaryDirectory() as folder,patch('scripts.equivalency.engine.local_model',return_value='local'),patch('scripts.equivalency.engine.post_ollama',return_value=self.response(p)) as model:
            with self.assertRaisesRegex(ValueError,'لم يصدر حكم'):
                compare(t,s,cache_dir=folder)
            self.assertEqual(model.call_count,2)
            self.assertEqual(list(Path(folder).glob('*.json')),[])
            self.assertEqual(len(json.loads(next((Path(folder)/'traces').glob('*.json')).read_text())['attempts']),2)
    def test_exact_course_title_remains_discoverable_with_corrupted_topic_text(self):
        courses=[{'id':'correct','name':'مصادر الالتزام','context_id':'c','context':{},'topics':[{'id':'t1','text':'مصادط بلا تزب'}]},
                 {'id':'irrelevant','name':'العقود التجارية','context_id':'c','context':{},'topics':[{'id':'t1','text':'العقد والالتزام وآثار الالتزام'}]}]
        catalog={'contexts':[{'id':'c'}],'courses':courses}
        source={'name':'مصادر الالتزام','topics':[{'id':'s1','text':'العقد والالتزام وآثار الالتزام'}]}
        self.assertEqual(search(catalog,source,all_contexts=True,semantic=False)['candidates'][0]['id'],'correct')
        self.assertEqual(source['topics'][0]['text'],'العقد والالتزام وآثار الالتزام')
    def test_grouping_conflict_is_pending_rather_than_false_rejection(self):
        t=[{'id':'t1','text':'أحكام الباب الأول'},{'id':'t2','text':'أحكام الباب الثاني'}]
        s=[{'id':'s1','text':'أحكام الباب الأول وأحكام الباب الثاني'}]
        pairs=[{'target_id':x['id'],'source_id':'s1','score':90,'reason':'يغطي الباب المحدد','target_quote':x['text'],'source_quote':s[0]['text']} for x in t]
        result=calculate(t,s,pairs)
        self.assertEqual(result['decision'],'insufficient')
        self.assertIsNone(result['strict_coverage'])
        self.assertEqual(result['segmentation_review']['individually_covered'],2)
