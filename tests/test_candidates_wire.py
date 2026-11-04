"""CPU-only transport checks; synthetic retrieval labels are not grade golds."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from scripts.equivalency import candidates as domain
from scripts.equivalency import candidates_wire as wire


class CandidatesWireTests(unittest.TestCase):
    def setUp(self):
        self.targets = [{'id': 'same', 'text': 'المُكرِه لا يُقبل؛ الآيات١–٣. الحكمُ الثالثُ.'},
                        {'id': 't2', 'text': 'أحكام الأيمان، وأحكام الإيمان.'}]
        self.sources = [{'id': 'same', 'text': 'النصُّ الأولُ؛ لا يسقطُ القيدُ. ثم المعنى الثالثُ.'},
                        {'id': 's2', 'text': 'أحكام الأيمان؛ غيرُ أحكام الإيمان.'}]

    def pair(self, sid='same', tid='same', **changes):
        return {'source_id': sid, 'status': 'possible', 'reason': 'العنوان يستحق الفحص ولا يثبت تفصيلًا زائدًا.',
                'source_ref': f'source:{sid}:whole', 'target_ref': f'target:{tid}:whole', **changes}

    def test_ordered_anchors_reconstruct_every_original_once_and_sides_are_disjoint(self):
        original=copy.deepcopy((self.targets,self.sources));view=wire.views(self.targets,self.sources)
        for side,topics in [('target',self.targets),('source',self.sources)]:
            self.assertEqual([r['id']for r in view[side]],[r['id']for r in topics])
            for row,topic in zip(view[side],topics):
                self.assertEqual(set(row),{'id','whole_ref','anchors'})
                cursor=0
                for anchor in row['anchors']:
                    self.assertEqual(set(anchor),{'ref','quote'})
                    self.assertEqual(topic['text'][cursor:cursor+len(anchor['quote'])],anchor['quote'])
                    cursor+=len(anchor['quote'])
                self.assertEqual(cursor,len(topic['text']))
                self.assertEqual(''.join(a['quote']for a in row['anchors']),topic['text'])
        self.assertNotEqual(view['target'][0]['whole_ref'],view['source'][0]['whole_ref'])
        self.assertEqual((self.targets,self.sources),original)

    def test_decoding_preserves_literal_numbers_marks_negation_status_and_reason(self):
        raw=self.pair();before=copy.deepcopy(raw);row=wire.decode_pair(raw,'same',self.targets,self.sources)
        self.assertEqual(row,{'source_id':'same','source_quote':self.sources[0]['text'],
                              'target_quote':self.targets[0]['text'],'reason':raw['reason'],'status':'possible'})
        self.assertIn('المُكرِه لا',row['target_quote']);self.assertIn('١–٣',row['target_quote'])
        self.assertEqual(raw,before)

    def test_references_cannot_cross_target_source_or_side_even_when_text_is_identical(self):
        ts=[{'id':'same','text':'نص واحد موثق'},{'id':'t2','text':'نص واحد موثق'}]
        ss=[{'id':'same','text':'نص واحد موثق'},{'id':'s2','text':'نص واحد موثق'}]
        for changes in [{'source_ref':'source:s2:whole'},{'target_ref':'target:t2:whole'},
                        {'source_ref':'target:same:whole'},{'target_ref':'source:same:whole'},
                        {'source_ref':'source:foreign:p1'},{'target_ref':'target:same:p999'}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                wire.decode_pair(self.pair(**changes),'same',ts,ss)

    def test_wire_never_accepts_legacy_quotes_nonstring_refs_or_grade_fields(self):
        for value in [None,{},['source:same:p1'],17]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                wire.decode_pair(self.pair(source_ref=value),'same',self.targets,self.sources)
        for changes in [{'score':95},{'band':'strict'},{'verdict':'lenient'},
                        {'source_quote':self.sources[0]['text']}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                wire.decode_pair(self.pair(**changes),'same',self.targets,self.sources)
        with self.assertRaises(ValueError):
            wire.decode_pair({'source_id':'same','status':'unrelated','source_ref':'source:same:whole'},'same',self.targets,self.sources)

    def test_schema_is_finite_copy_and_domain_still_requires_its_literal_contract(self):
        before=copy.deepcopy(domain.SCHEMA);schema=wire.build_schema(domain.SCHEMA,self.targets,self.sources)
        validator=Draft202012Validator(schema)
        value={'scan':[{'target_id':'same','sources':[self.pair()]}],'groups':[]}
        self.assertEqual(list(validator.iter_errors(value)),[])
        bad=copy.deepcopy(value);bad['scan'][0]['sources'][0]['source_ref']='source:foreign:whole'
        self.assertTrue(list(validator.iter_errors(bad)))
        legacy=wire.decode_discovery(value,self.targets,self.sources)
        self.assertTrue(list(validator.iter_errors(legacy)))
        self.assertEqual(domain.SCHEMA,before)
        with self.assertRaises(ValueError):domain.validate_discovery(value,[self.targets[0]],[self.sources[0]])
        self.assertEqual(domain.validate_discovery(legacy,[self.targets[0]],[self.sources[0]]),legacy)

    def test_disconnected_group_spans_stay_separate_and_do_not_enclose_middle(self):
        view=wire.views(self.targets,self.sources);target=view['target'][0]
        self.assertGreaterEqual(len(target['anchors']),3)
        raw={'target_id':'same','source_ids':['same','s2'],'reason':'شاهدان مستقلان، لا حكم تغطية.',
             'evidence':[{'source_id':'same','source_ref':'source:same:p1','target_ref':target['anchors'][0]['ref']},
                         {'source_id':'s2','source_ref':'source:s2:whole','target_ref':target['anchors'][-1]['ref']}]}
        decoded=wire.decode_group(raw,self.targets,self.sources)
        self.assertEqual(decoded['reason'],raw['reason']);self.assertEqual(decoded['source_ids'],raw['source_ids'])
        self.assertEqual(decoded['evidence'][0]['target_quote'],target['anchors'][0]['quote'])
        self.assertEqual(decoded['evidence'][1]['target_quote'],target['anchors'][-1]['quote'])
        self.assertNotIn(target['anchors'][1]['quote'],decoded['evidence'][0]['target_quote'])
        for mutate in ('owner','extra'):
            bad=copy.deepcopy(raw)
            if mutate=='owner':bad['evidence'][1]['source_ref']='source:same:whole'
            else:bad['evidence'][0]['support']='aligned'
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):wire.decode_group(bad,self.targets,self.sources)

    def test_effective_text_only_and_view_tampering_cannot_change_decoded_literal(self):
        source=copy.deepcopy(self.sources);source[0].update(text='نص قديم لا يرسل',corrected_text='النص الصحيحُ؛ لا يُبدَّل.',score=99)
        view=wire.views(self.targets,source);view['source'][0]['anchors'][0]['quote']='مزور'
        decoded=wire.decode_pair(self.pair(),'same',self.targets,source)
        self.assertEqual(decoded['source_quote'],source[0]['corrected_text'])
        actual=json.dumps(wire.views(self.targets,source),ensure_ascii=False)
        self.assertNotIn('نص قديم',actual);self.assertNotIn('score',actual);self.assertNotIn('مزور',actual)

    def test_complete36_pair_scan_keeps_full_curriculum_and_group_and_raw_wire_trace(self):
        targets=[{'id':f't{i}','text':f'هدف{i}؛ لا يحذف قيده.'}for i in range(3)]
        sources=[{'id':f's{i}','text':f'مصدر{i}؛ أثرهُ وتطبيقهُ١–٣.'}for i in range(12)]
        calls=[]
        def caller(endpoint,request):
            self.assertEqual(endpoint,'chat');calls.append(copy.deepcopy(request))
            payload=json.loads(request['messages'][1]['content'])
            self.assertEqual(len(payload['source']),12)
            for original,view in zip(sources,payload['source']):
                self.assertEqual(''.join(a['quote']for a in view['anchors']),original['text'])
            rows=[{'target_id':t['id'],'sources':[self.pair(s['id'],t['id'])for s in sources]}for t in targets]
            group={'target_id':'t0','source_ids':['s0','s11'],'reason':'أدلة متكاملة تستحق الفحص.',
                   'evidence':[{'source_id':sid,'source_ref':f'source:{sid}:whole','target_ref':'target:t0:whole'}for sid in ('s0','s11')]}
            result={'scan':rows,'groups':[group]}
            Draft202012Validator(request['format']).validate(result)
            return {'done':True,'done_reason':'stop','message':{'content':json.dumps(result,ensure_ascii=False)}}
        with tempfile.TemporaryDirectory()as cache,patch('urllib.request.urlopen',side_effect=AssertionError('no hidden network')):
            result=domain.discover_candidates(targets,sources,model='fixture:local',model_digest='fixture-digest',caller=caller,cache_dir=cache)
            self.assertEqual(result['coverage'],{'targets':3,'sources':12,'pairs':36,'complete':True})
            self.assertEqual(len(calls),1);self.assertEqual(len(result['scan']),3)
            self.assertEqual(result['groups'][0]['source_ids'],['s0','s11'])
            self.assertEqual(result['scan'][2]['sources'][11]['source_quote'],sources[11]['text'])
            trace=json.loads(next((Path(cache)/'candidate-discovery/traces').glob('*.json')).read_text())
            raw=json.loads(trace['attempts'][0]['response']['message']['content'])
            self.assertIn('source_ref',raw['scan'][0]['sources'][0]);self.assertNotIn('source_quote',raw['scan'][0]['sources'][0])
            before={p:p.read_bytes()for p in Path(cache).rglob('*.json')}
            again=domain.discover_candidates(targets,sources,model='fixture:local',model_digest='fixture-digest',caller=lambda *_:self.fail('warm cache must not call'),cache_dir=cache)
            self.assertTrue(again['cache_hit']);self.assertEqual(again['scan'],result['scan'])
            self.assertTrue(all(p.read_bytes()==content for p,content in before.items()))

    def test_wire_protocol_identity_separates_caches_and_preserves_old_trace_bytes(self):
        def caller(_,request):
            value={'scan':[{'target_id':'same','sources':[self.pair()]}],'groups':[]}
            return {'done':True,'done_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}
        targets,sources=self.targets[:1],self.sources[:1]
        with tempfile.TemporaryDirectory()as cache:
            old=domain.discover_candidates(targets,sources,model='fixture:local',model_digest='fixed',caller=caller,cache_dir=cache)
            saved={p:p.read_bytes()for p in Path(cache).rglob('*.json')}
            with patch.object(wire,'VERSION','synthetic-new-wire-identity'):
                new=domain.discover_candidates(targets,sources,model='fixture:local',model_digest='fixed',caller=caller,cache_dir=cache)
            self.assertNotEqual(old['cache_key'],new['cache_key'])
            self.assertEqual(old['scan'],new['scan']);self.assertEqual(old['groups'],new['groups'])
            self.assertTrue(all(p.read_bytes()==raw for p,raw in saved.items()))

    def test_partial_retrieval_scope_is_general_and_assigns_no_label(self):
        self.assertIn('بأي مطلب جزئي',domain.SYSTEM)
        self.assertIn('لا يشترط ترجيح التوافق لكامل الهدف',domain.SYSTEM)
        # The clarification is an instruction to inspect, never a postprocessor.
        self.assertEqual(wire.decode_pair({'source_id':'same','status':'unrelated'},'same',self.targets,self.sources),
                         {'source_id':'same','status':'unrelated'})
        for case_word in ('عيوب الزوجين','فسخ النكاح','t7','C02'):
            self.assertNotIn(case_word,domain.SYSTEM)


if __name__=='__main__':unittest.main()
