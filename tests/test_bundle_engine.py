import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from scripts.equivalency.bundle_engine import compare_curriculum, validate_curriculum_evidence
from scripts.equivalency.bundle_evidence import partition_parts, calculate
from scripts.equivalency.bundle_wire import source_view
from scripts.equivalency.engine import EvidenceValidationError


def answer(field, rows, sources):
    rows=copy.deepcopy(rows)
    views={v['source_id']:v for v in source_view(sources)}
    texts={v['id']:v.get('corrected_text') or v['text'] for v in sources}
    for row in rows:
        for facet in row['facets']:
            refs=[]
            for proof in facet['source_quotes']:
                sid,quote=proof['source_id'],proof['quote']
                if quote==texts.get(sid):refs.append(views[sid]['whole_ref'])
                else:
                    anchor=next((a['ref'] for a in views.get(sid,{}).get('anchors',[]) if a['quote']==quote),None)
                    refs.append(anchor or 'INVALID_TEST_WIRE_REFERENCE')
            facet['source_quotes']=refs
    return {'done_reason':'stop','message':{'content':json.dumps({field:rows},ensure_ascii=False)}}


def evaluation(target, source, *, score=85, support='aligned', review=False, verdict=None):
    row={'target_id':target['id'], 'facets':[{'part_id':p['part_id'],'support':support,
          'source_quotes':([] if support in ('absent','unknown') else [{'source_id':source['id'],'quote':source['text']}]),
          'reason':'دليل موضوعي مستقل من نص المصدر.'} for p in partition_parts(target['text'])],
         'reason':'يتحدد النطاق الكلي من الموضوعات المذكورة في المقرر.'}
    if review:
        verdict=verdict or ('strict' if score>=80 else 'lenient' if score>=60 else 'weak')
        row.update(verdict=verdict,possible_tiers={'strict':['strict','lenient'],'lenient':['lenient'],'weak':[],
                    'uncertain':['strict','lenient']}[verdict],uncertainty='decisive' if verdict=='uncertain' else 'none')
    else:
        band=verdict or ('strict' if score>=80 else 'lenient' if score>=60 else 'weak')
        row.update(band=band,possible_tiers={'strict':['strict','lenient'],'lenient':['lenient'],'weak':[],
                   'uncertain':['strict','lenient']}[band],uncertainty='decisive' if band=='uncertain' else 'none')
    return row


class BundleEngineTests(unittest.TestCase):
    def setUp(self):
        self.t={'id':'t','text':'تفسير البسملة.'}
        self.s={'id':'s','text':'شرح معاني البسملة وتفسيرها.'}
        self.targets={'topics':[self.t]};self.sources={'topics':[self.s]}

    def run_case(self, caller, directory, **kwargs):
        return compare_curriculum(self.targets,self.sources,caller=caller,model_digest='test-digest',
                                  cache_dir=directory,discover=False,**kwargs)

    def test_fresh_review_is_score_blinded_full_curriculum_and_cache_is_revalidated(self):
        calls=[]
        def caller(endpoint,payload):
            calls.append(copy.deepcopy(payload))
            review='reviews' in payload['format']['properties']
            return answer('reviews' if review else 'evaluations',[evaluation(self.t,self.s,review=review)],self.sources['topics'])
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_case(caller,directory)
            self.assertEqual(calculate(self.targets['topics'],self.sources['topics'],result['pairs'])['decision'],'equivalent')
            self.assertEqual(len(calls),2)
            blind=json.loads(calls[1]['messages'][1]['content'])
            self.assertEqual(blind['source'],source_view(self.sources['topics']));self.assertNotIn('preferred_sources',blind)
            self.assertNotIn('score',json.dumps(blind));self.assertNotIn('evaluations',blind)
            self.assertNotIn('facets',blind)
            with patch('scripts.equivalency.engine.post_ollama',side_effect=AssertionError('no hidden inference')):
                again=self.run_case(lambda *_:self.fail('cached comparison must not call a model'),directory)
            self.assertEqual(result['pairs'],again['pairs'])
            self.assertTrue(all(c['cache_hit'] for c in again['calls']))
            changed=copy.deepcopy(again);changed['scope_review']['source_ids']=[]
            with self.assertRaises(ValueError):validate_curriculum_evidence(changed,self.targets['topics'],self.sources['topics'])
            changed=copy.deepcopy(again);changed['inputs_sha256']='0'*64
            with self.assertRaises(ValueError):validate_curriculum_evidence(changed,self.targets['topics'],self.sources['topics'])

    def test_distributed_parts_keep_multiple_original_source_rows_and_one_target(self):
        target={'id':'t','text':'تعريف العقد؛ أركانه.'}
        sources=[{'id':'s1','text':'حد العقد وتعريفه'},{'id':'s2','text':'الأركان المطلوبة لانعقاد العقد'}]
        def caller(_,payload):
            review='reviews' in payload['format']['properties']
            row=evaluation(target,sources[0],review=review)
            self.assertEqual(len(row['facets']),2)
            row['facets'][1]['source_quotes']=[{'source_id':'s2','quote':sources[1]['text']}]
            return answer('reviews' if review else 'evaluations',[row],sources)
        with tempfile.TemporaryDirectory() as directory:
            result=compare_curriculum({'topics':[target]},{'topics':sources},caller=caller,model_digest='d',cache_dir=directory,discover=False)
        calculation=calculate([target],sources,result['pairs'])
        self.assertEqual(calculation['strict_count'],1)
        self.assertEqual(calculation['strict_matches']['t'],['s1','s2'])
        self.assertEqual(calculation['total_topics'],1)

    def test_valid_rows_survive_targeted_repair_without_regenerating_their_scores(self):
        t2={'id':'t2','text':'تفسير الاستعاذة.'};s2={'id':'s2','text':'شرح معاني الاستعاذة وتفسيرها.'}
        self.targets={'topics':[self.t,t2]};self.sources={'topics':[self.s,s2]};calls=[]
        good=evaluation(self.t,self.s)
        def caller(_,payload):
            calls.append(copy.deepcopy(payload));review='reviews' in payload['format']['properties']
            request=json.loads(payload['messages'][1]['content'])
            if review:return answer('reviews',[evaluation(self.t,self.s,review=True),evaluation(t2,s2,review=True)],self.sources['topics'])
            if len(calls)==1:
                bad=evaluation(t2,s2);bad['facets'][0]['source_quotes'][0]['quote']='NOT_IN_THE_SOURCE'
                return answer('evaluations',[good,bad],self.sources['topics'])
            self.assertEqual(request['requested_target_ids'],['t2']);self.assertEqual(request['retained_target_ids'],['t'])
            return answer('evaluations',[evaluation(t2,s2)],self.sources['topics'])
        with tempfile.TemporaryDirectory() as directory:result=self.run_case(caller,directory)
        self.assertEqual(len(calls),3)
        self.assertEqual({k:v for k,v in result['pairs'][0].items() if k!='adjudication'},good)

    def test_missing_alternative_is_completed_without_manually_injecting_a_grade(self):
        irrelevant={'id':'s0','text':'أحكام المد والتجويد'};self.sources={'topics':[irrelevant,self.s]}
        calls=[]
        def caller(_,payload):
            request=json.loads(payload['messages'][1]['content']);calls.append(request)
            review='reviews' in payload['format']['properties'];guided='retrieved_evidence' in request
            row=evaluation(self.t,self.s if guided else irrelevant,score=85 if guided else 10,
                           support='aligned' if guided else 'absent',review=review)
            return answer('reviews' if review else 'evaluations',[row],self.sources['topics'])
        discovered={'scan':[{'target_id':'t','sources':[{'source_id':'s0','status':'unrelated'},
                    {'source_id':'s','status':'candidate','source_quote':self.s['text'],'target_quote':self.t['text']}]}],
                    'groups':[],'cache_key':'discovery-test'}
        with tempfile.TemporaryDirectory() as directory,patch('scripts.equivalency.candidates.discover_candidates',return_value=discovered) as scan:
            result=compare_curriculum(self.targets,self.sources,caller=caller,model_digest='d',cache_dir=directory)
            self.assertEqual(scan.call_count,1)
            self.assertEqual(calculate(self.targets['topics'],self.sources['topics'],result['pairs'])['decision'],'equivalent')
            self.assertEqual(len(calls),4)
            traces=list((Path(directory)/'curriculum-v2'/'proposal'/'traces').glob('*.json'))
            self.assertEqual(len(traces),2)
            original=[json.loads(t.read_text()) for t in traces if 'retrieved_evidence' not in json.loads(t.read_text())['attempts'][0]['request_payload']][0]
            raw=json.loads(original['attempts'][0]['response']['message']['content'])['evaluations'][0]
            self.assertEqual(raw['band'],'weak')
            self.assertEqual(result['pairs'][0]['band'],'strict')

    def test_tolerance_also_discovers_missing_support_without_prescribing_a_revised_grade(self):
        target2={'id':'t2','text':'تفسير الاستعاذة.'}
        source2={'id':'s2','text':'شرح معاني الاستعاذة وتفسيرها.'}
        targets=[self.t,target2];sources=[self.s,source2]
        discovery={'scan':[{'target_id':'t','sources':[{'source_id':'s','status':'candidate',
                   'source_quote':self.s['text'],'target_quote':self.t['text']}]}],
                   'groups':[],'cache_key':'tolerance-alternative'}
        for initial_review in ('lenient','strict'):
            for completed_band in ('lenient','strict'):
                with self.subTest(initial_review=initial_review,completed_band=completed_band):
                    calls=[]
                    def caller(_,payload):
                        request=json.loads(payload['messages'][1]['content']);calls.append(request)
                        review='reviews' in payload['format']['properties']
                        guided='retrieved_evidence' in request
                        band=completed_band if guided else initial_review if review else 'lenient'
                        row=evaluation(self.t,self.s,verdict=band,review=review,
                                       support='aligned' if band=='strict' else 'related')
                        self.assertEqual(request['source'],source_view(sources))
                        if guided:
                            self.assertEqual(request['requested_target_ids'],['t'])
                            self.assertNotIn('band',json.dumps(request['retrieved_evidence']))
                            self.assertNotIn('verdict',json.dumps(request['retrieved_evidence']))
                            rows=[row]
                        else:rows=[row,evaluation(target2,source2,review=review)]
                        return answer('reviews' if review else 'evaluations',rows,sources)
                    with tempfile.TemporaryDirectory() as directory,patch('scripts.equivalency.candidates.discover_candidates',return_value=discovery) as scan:
                        result=compare_curriculum({'topics':targets},{'topics':sources},caller=caller,
                                                  model_digest='d',cache_dir=directory)
                        self.assertEqual(scan.call_count,1)
                        self.assertEqual(scan.call_args.args,([self.t],sources))
                        self.assertEqual(len(calls),4)
                        final={r['target_id']:r for r in result['pairs']}
                        self.assertEqual(final['t']['band'],completed_band)
                        self.assertEqual(final['t']['adjudication']['review']['verdict'],completed_band)
                        self.assertEqual(final['t2']['band'],'strict')
                        self.assertEqual(calculate(targets,sources,result['pairs'])['decision'],
                                         'equivalent' if completed_band=='strict' else 'lenient')

    def test_persistent_evidence_failure_is_typed_but_transport_failure_is_not(self):
        bad=evaluation(self.t,self.s);bad['facets'][0]['source_quotes'][0]['quote']='invented'
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(EvidenceValidationError) as error:
                self.run_case(lambda *_:answer('evaluations',[bad],self.sources['topics']),directory)
            self.assertEqual(error.exception.stage,'proposal')
            self.assertEqual(error.exception.target_ids,['t'])
        with tempfile.TemporaryDirectory() as directory:
            def failed(*_):raise urllib.error.URLError('test transport interruption')
            with self.assertRaises(urllib.error.URLError):self.run_case(failed,directory)

    def test_scope_or_unreadable_inputs_do_not_start_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            for rows in [[{'id':'t','text':'أخرى'}],[{'id':'t','text':'تفسير سورة الفاتحة الآيات1-9'}]]:
                self.targets={'topics':rows}
                result=self.run_case(lambda *_:self.fail('unresolved input must not start inference'),directory)
                self.assertEqual(result['pairs'],[])


if __name__=='__main__':unittest.main()
