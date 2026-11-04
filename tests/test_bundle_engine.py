import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from scripts.equivalency.bundle_engine import compare_curriculum, validate_curriculum_evidence
from scripts.equivalency import bundle_engine as budget_engine
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
            self.assertEqual(blind['source'],budget_engine._model_source_view(self.sources['topics']));self.assertNotIn('preferred_sources',blind)
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
                        self.assertEqual(request['source'],budget_engine._model_source_view(sources))
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


    def test_ordered_grammar_has_separate_cache_and_preserves_previous_raw(self):
        from scripts.equivalency import bundle_wire
        original=bundle_wire.build_schema;calls=[]
        def old_order(*args):
            schema=original(*args)
            def visit(value):
                if isinstance(value,dict):
                    if value.get('type')=='object'and set(value.get('properties',{}))=={'part_id','support','source_quotes','reason'}:
                        order=['part_id','support','source_quotes','reason']
                        value['properties']={k:value['properties'][k]for k in order};value['required']=order
                    for v in value.values():visit(v)
                elif isinstance(value,list):
                    for v in value:visit(v)
            visit(schema);return schema
        def caller(_,payload):
            calls.append(copy.deepcopy(payload));review='reviews'in payload['format']['properties']
            return answer('reviews'if review else'evaluations',[evaluation(self.t,self.s,review=review)],self.sources['topics'])
        with tempfile.TemporaryDirectory()as directory:
            with patch.object(bundle_wire,'build_schema',side_effect=old_order):old=self.run_case(caller,directory)
            saved={p:hashlib.sha256(p.read_bytes()).hexdigest()for p in Path(directory).rglob('*.json')}
            new=self.run_case(caller,directory)
            self.assertEqual(len(calls),4);self.assertEqual(old['pairs'],new['pairs'])
            self.assertTrue(all(not c['cache_hit']for c in new['calls']))
            self.assertNotEqual([c['cache_key']for c in old['calls']],[c['cache_key']for c in new['calls']])
            self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest()==sha for p,sha in saved.items()))
            again=self.run_case(caller,directory);self.assertEqual(len(calls),4)
            self.assertTrue(all(c['cache_hit']for c in again['calls']))
            traces=[json.loads(p.read_text())for p in Path(directory).rglob('traces/*.json')]
            for trace in traces:
                for attempt in trace['attempts']:
                    self.assertIs(attempt['think'],False)
                    self.assertEqual(json.loads(attempt['user_content']),attempt['request_payload'])
                    self.assertIn('ordered_request_sha256',attempt)

    def test_exact_user_serialization_and_thinking_mode_change_identity_without_changing_data(self):
        from scripts.equivalency.bundle_engine import _generation_request,_request_identity
        first={'target':'same','source':'same'};second={'source':'same','target':'same'}
        self.assertEqual(first,second)
        request=_generation_request('test','system',{'type':'object'},{'seed':1},json.dumps(first))
        changed=copy.deepcopy(request);changed['messages'][1]['content']=json.dumps(second)
        self.assertNotEqual(_request_identity(request),_request_identity(changed))
        changed=copy.deepcopy(request);changed['think']=True
        self.assertNotEqual(_request_identity(request),_request_identity(changed))

    def test_completion_keeps_each_readers_own_literal_quotes_even_when_discovery_says_unrelated(self):
        source_a={'id':'sA','text':'تعريف العلم ومبادئه'};source_b={'id':'sB','text':'شرح مفهوم العلم'};unused={'id':'sC','text':'مسألة أخرى'}
        target={'id':'t','text':'ماهية العلم'};sources=[source_a,source_b,unused];calls=[]
        def caller(_,payload):
            request=json.loads(payload['messages'][1]['content']);calls.append(request)
            review='reviews'in payload['format']['properties'];own=source_b if review else source_a
            if 'retrieved_evidence'in request:
                facts=request['retrieved_evidence'][0]['evidence']
                self.assertEqual(facts,[{'source_id':own['id'],'source_quote':own['text'],'target_quote':target['text']}])
                for fact in facts:self.assertEqual(set(fact),{'source_id','source_quote','target_quote'})
                self.assertNotIn(source_a['text']if review else source_b['text'],json.dumps(facts,ensure_ascii=False))
            self.assertEqual(request['source'],budget_engine._model_source_view(sources))
            return answer('reviews'if review else'evaluations',[evaluation(target,own,review=review,verdict='lenient',support='related')],sources)
        discovery={'scan':[{'target_id':'t','sources':[{'source_id':s['id'],'status':'unrelated'}for s in sources]}],'groups':[],'cache_key':'synthetic'}
        with tempfile.TemporaryDirectory()as directory,patch('scripts.equivalency.candidates.discover_candidates',return_value=discovery):
            result=compare_curriculum({'topics':[target]},{'topics':sources},caller=caller,model_digest='d',cache_dir=directory)
        self.assertEqual(len(calls),4);self.assertEqual(result['pairs'][0]['band'],'lenient')
        self.assertEqual(result['pairs'][0]['adjudication']['review']['verdict'],'lenient')
        self.assertEqual(calculate([target],sources,result['pairs'])['decision'],'lenient')

    def test_completion_deduplicates_per_part_without_losing_overlapping_distinct_source_text(self):
        from scripts.equivalency.bundle_engine import _completion_guide
        target={'id':'t','text':'المطلب الأول؛ المطلب الثاني.'};source={'id':'s','text':'ABCDE'}
        prior=[{'target_id':'t','band':'lenient','reason':'SECRET_REASON','facets':[
            {'part_id':'p1','support':'related','source_quotes':[{'source_id':'s','quote':'ABC'}]},
            {'part_id':'p2','support':'related','source_quotes':[{'source_id':'s','quote':'CDE'}]},
            {'part_id':'p2','support':'related','source_quotes':[{'source_id':'s','quote':'ABC'}]}]}]
        discovered={'t':[{'source_id':'s','source_quote':'C','target_quote':'المطلب الأول'}]}
        before=copy.deepcopy([prior,discovered]);guide=_completion_guide([target],[source],prior,discovered)
        facts=guide[0]['evidence'];self.assertEqual({f['source_quote']for f in facts},{'ABC','CDE','C'})
        self.assertEqual(len(facts),4);self.assertEqual([prior,discovered],before)
        for f in facts:
            self.assertEqual(set(f),{'source_id','source_quote','target_quote'})
            self.assertIn(f['target_quote'],target['text']);self.assertIn(f['source_quote'],source['text'])
        self.assertNotIn('SECRET_REASON',json.dumps(guide))

    def test_interpretive_terms_are_shared_without_becoming_input_content_or_resource_lists(self):
        from scripts.equivalency.academic_terms import ACADEMIC_TERMS
        from scripts.equivalency.bundle_engine import PROPOSAL_SYSTEM,REVIEW_SYSTEM
        from scripts.equivalency.candidates import SYSTEM
        for prompt in [PROPOSAL_SYSTEM,REVIEW_SYSTEM,SYSTEM]:self.assertIn(ACADEMIC_TERMS,prompt)
        for term in ['استمداد العلم','نشأته','موضوعه','تعريفه','قائمة موارد إلزامية']:
            self.assertIn(term,ACADEMIC_TERMS)
        self.assertNotIn('القرآن والسنة',ACADEMIC_TERMS)
        self.assertNotIn('t1',ACADEMIC_TERMS);self.assertNotIn('C01',ACADEMIC_TERMS)


    def test_completion_unions_new_discovery_facts_with_own_previous_quotes_only(self):
        from scripts.equivalency.bundle_engine import _completion_guide
        target={'id':'t','text':'المطلب الأول؛ المطلب الثاني.'}
        sources=[{'id':'a','text':'شاهد أول مستقل'},{'id':'b','text':'شاهد القارئ الآخر'},{'id':'c','text':'شاهد اكتشاف جديد'}]
        first=evaluation(target,sources[0],verdict='lenient',support='related')
        other=evaluation(target,sources[1],review=True,verdict='lenient',support='related')
        discovered={'t':[{'source_id':'c','source_quote':sources[2]['text'],'target_quote':'المطلب الثاني'}]}
        a=_completion_guide([target],sources,[first],discovered)[0]['evidence']
        b=_completion_guide([target],sources,[other],discovered)[0]['evidence']
        self.assertEqual({f['source_id']for f in a},{'a','c'});self.assertEqual({f['source_id']for f in b},{'b','c'})
        self.assertEqual(len(a),3);self.assertEqual(len(b),3)
        for facts in [a,b]:
            for fact in facts:self.assertEqual(set(fact),{'source_id','source_quote','target_quote'})


    def test_contained_completion_quote_keeps_the_longest_context_including_negation(self):
        from scripts.equivalency.bundle_engine import _completion_guide
        target={'id':'t','text':'الاستدلال؛ شروطه.'}
        source={'id':'s','text':'الاستدلال بالقياس ولا يلزم الاجتهاد المطلق'}
        prior=evaluation(target,source,verdict='lenient',support='related')
        discovered={'t':[{'source_id':'s','source_quote':'القياس','target_quote':'الاستدلال؛'}]}
        facts=_completion_guide([target],[source],[prior],discovered)[0]['evidence']
        self.assertEqual(facts,[{'source_id':'s','source_quote':source['text'],'target_quote':part['quote']}for part in partition_parts(target['text'])])
        self.assertIn('ولا يلزم',facts[0]['source_quote'])

    def test_unproven_description_wording_is_shared_without_rewriting_any_result(self):
        from scripts.equivalency.academic_terms import ACADEMIC_TERMS
        from scripts.equivalency.bundle_engine import PROPOSAL_SYSTEM,REVIEW_SYSTEM
        from scripts.equivalency.candidates import SYSTEM
        for system in [PROPOSAL_SYSTEM,REVIEW_SYSTEM,SYSTEM]:
            self.assertIn('ما لم يثبت من الوصف',system)
            self.assertIn('في جميع الفئات',system)
        self.assertIn('لا تدّع غياب الموضوع أو عدم تدريسه',ACADEMIC_TERMS)


    def test_separated_target_witnesses_never_imply_the_intervening_demand(self):
        from scripts.equivalency.bundle_engine import _completion_guide
        target={'id':'t','text':'المطلب الأول؛ المطلب الثاني؛ المطلب الثالث.'};source={'id':'s','text':'شاهد حرفي'}
        parts=partition_parts(target['text']);prior=evaluation(target,source)
        prior['facets']=[f for f in prior['facets']if f['part_id']in ('p1','p3')]
        facts=_completion_guide([target],[source],[prior],{})[0]['evidence']
        self.assertEqual([f['target_quote']for f in facts],[parts[0]['quote'],parts[2]['quote']])
        self.assertTrue(all('المطلب الثاني'not in f['target_quote']for f in facts))

    def test_independent_review_model_uses_own_cache_digest_and_keeps_primary_cache(self):
        calls=[]
        def caller(_,payload):
            calls.append(copy.deepcopy(payload));review='reviews'in payload['format']['properties']
            return answer('reviews'if review else'evaluations',[evaluation(self.t,self.s,review=review)],self.sources['topics'])
        with tempfile.TemporaryDirectory()as directory:
            def run(digest):return compare_curriculum(self.targets,self.sources,model='primary:1',model_digest='P',review_model='gemma4:31b',review_model_digest=digest,caller=caller,cache_dir=directory,discover=False)
            first=run('G');self.assertEqual([c['model']for c in calls],['primary:1','gemma4:31b'])
            self.assertEqual(first['review_model'],'gemma4:31b');self.assertEqual(first['review_model_digest'],'G')
            for stage,model,digest in [('proposal','primary:1','P'),('review','gemma4:31b','G')]:
                cached=next((Path(directory)/'curriculum-v2'/stage).glob('*.json'))
                metadata=json.loads(cached.read_text());self.assertEqual(metadata['model'],model);self.assertEqual(metadata['model_digest'],digest)
            self.assertEqual([(c['stage'],c['model'],c['model_digest'])for c in first['calls']],[('proposal','primary:1','P'),('review','gemma4:31b','G')])
            saved={p:hashlib.sha256(p.read_bytes()).hexdigest()for p in Path(directory).rglob('*.json')}
            changed=run('G2');self.assertEqual(len(calls),3);self.assertEqual(calls[-1]['model'],'gemma4:31b')
            self.assertTrue(changed['calls'][0]['cache_hit']);self.assertFalse(changed['calls'][1]['cache_hit'])
            self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest()==sha for p,sha in saved.items()))
            warm=run('G2');self.assertEqual(len(calls),3);self.assertTrue(all(c['cache_hit']for c in warm['calls']))

    def test_default_single_model_has_no_second_digest_lookup_or_warm_caller(self):
        def caller(_,payload):
            review='reviews'in payload['format']['properties']
            return answer('reviews'if review else'evaluations',[evaluation(self.t,self.s,review=review)],self.sources['topics'])
        with tempfile.TemporaryDirectory()as directory,patch('scripts.equivalency.engine.local_model',side_effect=AssertionError('no extra metadata lookup')):
            result=self.run_case(caller,directory)
            self.assertEqual(result['model'],result['review_model']);self.assertEqual(result['model_digest'],result['review_model_digest'])
            warm=self.run_case(lambda *_:self.fail('warm cache must not call'),directory)
            self.assertTrue(all(c['cache_hit']for c in warm['calls']))

    def test_independent_review_model_resolves_only_its_own_missing_digest(self):
        def caller(_,payload):
            review='reviews'in payload['format']['properties']
            return answer('reviews'if review else'evaluations',[evaluation(self.t,self.s,review=review)],self.sources['topics'])
        with tempfile.TemporaryDirectory()as directory,patch('scripts.equivalency.engine.local_model',return_value='actual-review-digest')as lookup:
            result=compare_curriculum(self.targets,self.sources,model_digest='primary-digest',review_model='gemma4:31b',caller=caller,cache_dir=directory,discover=False)
        lookup.assert_called_once_with('gemma4:31b');self.assertEqual(result['review_model_digest'],'actual-review-digest')

    def test_adaptive_completion_uses_correct_reader_model_while_discovery_stays_primary(self):
        calls=[]
        def caller(_,payload):
            calls.append(payload['model']);review='reviews'in payload['format']['properties']
            return answer('reviews'if review else'evaluations',[evaluation(self.t,self.s,review=review,verdict='lenient',support='related')],self.sources['topics'])
        discovery={'scan':[{'target_id':'t','sources':[{'source_id':'s','status':'candidate','source_quote':self.s['text'],'target_quote':self.t['text']}]}],'groups':[],'cache_key':'synthetic'}
        with tempfile.TemporaryDirectory()as directory,patch('scripts.equivalency.candidates.discover_candidates',return_value=discovery)as scan:
            result=compare_curriculum(self.targets,self.sources,model='primary:1',model_digest='P',review_model='gemma4:31b',review_model_digest='G',caller=caller,cache_dir=directory)
        self.assertEqual(calls,['primary:1','gemma4:31b','primary:1','gemma4:31b'])
        self.assertEqual(scan.call_args.kwargs['model'],'primary:1');self.assertEqual(scan.call_args.kwargs['model_digest'],'P')
        self.assertEqual([c['model_digest']for c in result['calls']],['P','G','P','G'])
        self.assertEqual(result['pairs'][0]['band'],'lenient')

    def test_common_demands_whole_scope_rationale_without_adding_a_required_detail(self):
        from scripts.equivalency.bundle_engine import COMMON
        self.assertIn('إثبات محور مستقل لا يثبت محورًا آخر',COMMON)
        self.assertIn('إن لم تسند أثر النقص فلا تحسم strict',COMMON)

    def test_model_projection_reconstructs_exact_original_text_and_all_wire_refs(self):
        sources=[{'id':'unit-z','text':'obsolete','corrected_text':'  أول؛ أول.\nالنفي: لا يُشترط (١٢–١٦)، وغيرُه!  '},
                 {'id':'unit-a','text':'تفسير سورة البقرة الآيات 6–16؛ شروطه واستثناءاته.'}]
        targets=[{'id':'target-z','text':'التعريف؛ الأركان.\nالشرط، والاستثناء! '},
                 {'id':'target-a','text':'النص السابق','corrected_text':'الحد: (٢)؛ دون الزيادة.'}]
        before=copy.deepcopy((targets,sources));wire=source_view(sources)
        projected=budget_engine._model_source_view(sources)
        self.assertEqual([r['source_id']for r in projected],['unit-z','unit-a'])
        for original,full,compact in zip(sources,wire,projected):
            self.assertEqual(set(compact),{'source_id','whole_ref','anchors'})
            self.assertEqual(compact['whole_ref'],full['whole_ref'])
            self.assertEqual([a['ref']for a in compact['anchors']],[a['ref']for a in full['anchors']])
            self.assertEqual(''.join(a['quote']for a in compact['anchors']),original.get('corrected_text')or original['text'])
            self.assertEqual([a['quote']for a in compact['anchors']],[a['quote']for a in full['anchors']])
            self.assertTrue(all(set(a)=={'ref','quote'}for a in compact['anchors']))
        projected_targets=budget_engine._model_target_view(targets,['target-a','target-z'])
        self.assertEqual([r['id']for r in projected_targets],['target-z','target-a'])
        for original,compact in zip(targets,projected_targets):
            text=original.get('corrected_text')or original['text'];parts=partition_parts(text)
            self.assertEqual(compact['text'],text)
            self.assertEqual(''.join(p['quote']for p in compact['parts']),text)
            self.assertEqual(compact['parts'],[{'part_id':p['part_id'],'quote':p['quote']}for p in parts])
        self.assertLess(len(json.dumps(projected,ensure_ascii=False).encode()),len(json.dumps(wire,ensure_ascii=False).encode()))
        self.assertEqual((targets,sources),before)

    def test_model_projection_uses_validated_source_view_without_repairing_bad_inputs(self):
        for sources in [[{'id':'s','text':''}], [{'id':'s','text':'أول'},{'id':'s','text':'ثان'}]]:
            with self.assertRaises(ValueError):budget_engine._model_source_view(sources)
        with patch('scripts.equivalency.bundle_wire.source_view',side_effect=ValueError('invalid_anchor_coverage')):
            with self.assertRaisesRegex(ValueError,'invalid_anchor_coverage'):
                budget_engine._model_source_view([self.s])

    def test_lossless_projection_does_not_read_legacy_cache_or_change_original_wire_contract(self):
        from scripts.equivalency.bundle_wire import build_schema
        from scripts.equivalency.bundle_evidence import PROPOSAL_SCHEMA,REVIEW_SCHEMA
        targets=[self.t];sources=[self.s];calls=[]
        def caller(_,request):
            calls.append(copy.deepcopy(request));review='reviews'in request['format']['properties']
            return answer('reviews'if review else'evaluations',[evaluation(self.t,self.s,review=review)],sources)
        with tempfile.TemporaryDirectory()as directory:
            directory=Path(directory);legacy={}
            for stage,model,digest,system,base in [('proposal','primary:local','P',budget_engine.PROPOSAL_SYSTEM,PROPOSAL_SCHEMA),
                                                   ('review','independent:local','G',budget_engine.REVIEW_SYSTEM,REVIEW_SCHEMA)]:
                old={'target':[{'id':self.t['id'],'text':self.t['text'],'parts':partition_parts(self.t['text'])}],
                     'source':source_view(sources),'requested_target_ids':['t'],
                     'scope_context':budget_engine.scope_context(targets,sources)}
                if stage=='proposal':old['preferred_sources']=[{'target_id':'t','source_ids':['s']}]
                options=budget_engine._options(stage,system,old);schema=build_schema(stage,base,sources)
                request=budget_engine._generation_request(model,system,schema,options,json.dumps(old,ensure_ascii=False))
                identity=[budget_engine.VERSION,budget_engine.evaluation_contract(),stage,system,digest,options,
                          budget_engine.input_binding(targets,sources),budget_engine._request_identity(request)]
                key=hashlib.sha256(budget_engine._encoded(identity).encode()).hexdigest()
                path=directory/stage/(key+'.json');path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'cache_key':key,'version':budget_engine.VERSION,
                    'contract':budget_engine.evaluation_contract(),'model':model,'model_digest':digest,
                    'rows':[evaluation(self.t,self.s,review=stage=='review')]}))
                legacy[path]=hashlib.sha256(path.read_bytes()).hexdigest()
                rows,metadata=budget_engine._stage(stage,targets,sources,['t'],model=model,digest=digest,caller=caller,directory=directory)
                self.assertFalse(metadata['cache_hit']);self.assertNotEqual(metadata['cache_key'],key)
                self.assertEqual(rows,[evaluation(self.t,self.s,review=stage=='review')])
                self.assertEqual(calls[-1]['format'],schema)
                trace=json.loads((directory/stage/'traces'/(metadata['cache_key']+'.json')).read_text())
                self.assertEqual(trace['model_input_version'],budget_engine.MODEL_INPUT_VERSION)
                self.assertEqual(trace['model'],model);self.assertEqual(trace['model_digest'],digest)
                _,warm=budget_engine._stage(stage,targets,sources,['t'],model=model,digest=digest,
                    caller=lambda *_:self.fail('new projection cache must be reusable'),directory=directory)
                self.assertTrue(warm['cache_hit'])
            self.assertEqual(len(calls),2)
            self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in legacy.items()))




    def _budget_payload(self,stage,targets,sources,wanted):
        from scripts.equivalency.engine import topic_text
        from scripts.equivalency.candidates import plan_candidate_pairs
        payload={'target':budget_engine._model_target_view(targets,wanted),
                 'source':budget_engine._model_source_view(sources),'requested_target_ids':sorted(wanted),'scope_context':budget_engine.scope_context(targets,sources)}
        if stage=='proposal':
            ranked=plan_candidate_pairs(targets,sources)
            payload['preferred_sources']=[{'target_id':tid,'source_ids':[p['source_id']for p in ranked if p['target_id']==tid]}for tid in wanted]
        return payload

    def _budget_split_fixture(self):
        targets=[{'id':'alpha','text':'; '.join(['نطاق أكاديمي موثق']*20)},
                 {'id':'beta','text':'; '.join(['نطاق أكاديمي موثق']*20)}]
        # Find a byte-sized full curriculum where the pair fits the old2048
        # reserve but must split for39+ required facet records. All sources stay.
        for length in range(100,1800,10):
            sources=[{'id':f's{i}','text':'نطاق أكاديمي موثق '+('x'*length)}for i in range(12)]
            fits=[]
            for stage,system in [('proposal',budget_engine.PROPOSAL_SYSTEM),('review',budget_engine.REVIEW_SYSTEM)]:
                pair=self._budget_payload(stage,targets,sources,['alpha','beta'])
                available=32768-len(system.encode())-len(json.dumps(pair,ensure_ascii=False).encode())-1024
                try:
                    budget_engine._options(stage,system,pair);rejected=False
                except RuntimeError:rejected=True
                single=True
                for target in targets:
                    try:budget_engine._options(stage,system,self._budget_payload(stage,targets,sources,[target['id']]))
                    except RuntimeError:single=False
                fits.append(available>=2048 and rejected and single)
            if all(fits):return targets,sources
        self.fail('no independent budget split fixture found')

    def test_output_budget_recursive_whole_target_split_preserves_sources_ids_and_reader_identity(self):
        targets,sources=self._budget_split_fixture();before=copy.deepcopy((targets,sources));calls=[];rows_by_stage={};active=0;max_active=0
        with tempfile.TemporaryDirectory()as directory:
            for stage,model,digest in [('proposal','primary:local','P'),('review','independent:local','G')]:
                def caller(endpoint,payload):
                    nonlocal active,max_active
                    active+=1;max_active=max(max_active,active)
                    request=json.loads(payload['messages'][1]['content']);calls.append((stage,copy.deepcopy(payload),request))
                    self.assertEqual(request['source'],budget_engine._model_source_view(sources));self.assertEqual(request['scope_context'],budget_engine.scope_context(targets,sources))
                    self.assertEqual(len(request['target']),1);self.assertEqual(payload['model'],model);self.assertIs(payload['think'],False)
                    tid=request['requested_target_ids'][0];target=next(t for t in targets if t['id']==tid)
                    self.assertEqual(request['target'],budget_engine._model_target_view([target],[target['id']]))
                    self.assertGreaterEqual(payload['options']['num_predict'],128*20+256)
                    row=evaluation(target,sources[0],review=stage=='review');active-=1
                    return answer('reviews'if stage=='review'else'evaluations',[row],sources)
                rows,metadata=budget_engine._stage(stage,targets,sources,['alpha','beta'],model=model,digest=digest,caller=caller,directory=Path(directory))
                rows_by_stage[stage]=rows
                self.assertEqual([r['target_id']for r in rows],['alpha','beta']);self.assertEqual(len(metadata['split_batches']),2)
                traces=[json.loads(p.read_text())for p in (Path(directory)/stage/'traces').glob('*.json')]
                self.assertEqual(len(traces),2);self.assertTrue(all(t['model']==model and t['model_digest']==digest for t in traces))
            self.assertEqual(len(calls),4);self.assertEqual(max_active,1);self.assertEqual((targets,sources),before)
            for stage in ('proposal','review'):
                ids=[tid for s,_,request in calls if s==stage for tid in request['requested_target_ids']]
                self.assertEqual(ids,['alpha','beta'])
                if stage=='review':
                    for s,payload,request in calls:
                        if s=='review':
                            self.assertNotIn('preferred_sources',request);self.assertNotIn('facets',json.dumps(request));self.assertNotIn('band',json.dumps(request))
        self.preflight_evidence={'mock_calls':len(calls),'readers':['primary:local','independent:local'],'digests':['P','G'],'all_ids_once_per_reader':True,'full_source_retained':True,'max_concurrent_mock_calls':max_active,'original_target_denominator':len(targets)}

    def test_output_budget_single_unsplittable_target_fails_before_any_model(self):
        target={'id':'large','text':'; '.join(['مطلب أصلي محفوظ']*150)};source={'id':'source','text':'نطاق أكاديمي موثق'}
        before=copy.deepcopy((target,source));calls=[]
        with tempfile.TemporaryDirectory()as directory:
            with self.assertRaises(RuntimeError):budget_engine._stage('proposal',[target],[source],['large'],model='primary:local',digest='P',caller=lambda *args:calls.append(args),directory=Path(directory))
            self.assertEqual(list(Path(directory).rglob('*.json')),[])
        self.assertEqual(calls,[]);self.assertEqual((target,source),before)
        self.preflight_evidence={'facets':150,'minimum_output_required':19456,'output_cap':16000,'mock_calls':0,'safe_failure':True}

    def test_output_budget_still_rejects_length_even_with_parseable_complete_json(self):
        target={'id':'t','text':'نطاق أكاديمي موثق'};source={'id':'s','text':'نطاق أكاديمي موثق تفصيلي'}
        raw=answer('evaluations',[evaluation(target,source)],[source]);raw['done_reason']='length';before=copy.deepcopy(raw);calls=[]
        with tempfile.TemporaryDirectory()as directory:
            def caller(*args):calls.append(args);return raw
            with self.assertRaises(EvidenceValidationError):budget_engine._stage('proposal',[target],[source],['t'],model='primary:local',digest='P',caller=caller,directory=Path(directory))
            traces=list((Path(directory)/'proposal'/'traces').glob('*.json'));self.assertEqual(len(traces),1)
            saved=json.loads(traces[0].read_text());self.assertEqual(saved['attempts'][0]['response'],before)
            self.assertEqual(list((Path(directory)/'proposal').glob('*.json')),[])
        self.assertEqual(len(calls),1);self.assertEqual(raw,before)
        self.preflight_evidence={'parseable_json':True,'done_reason':'length','accepted':False,'raw_preserved':True,'mock_calls':1}

    def test_output_budget_repair_keeps_whole_pending_targets_and_all_sources(self):
        targets=[{'id':key,'text':'; '.join(['نطاق أكاديمي موثق']*12)}for key in ['left','right']]
        sources=[{'id':'s','text':'نطاق أكاديمي موثق مع تفاصيل أصلية'}];calls=[]
        good=evaluation(targets[0],sources[0]);bad=evaluation(targets[1],sources[0]);bad['facets'][0]['source_quotes']=[{'source_id':'s','quote':'not literal'}]
        def caller(_,payload):
            request=json.loads(payload['messages'][1]['content']);calls.append(copy.deepcopy(request))
            self.assertEqual(request['source'],budget_engine._model_source_view(sources))
            if len(calls)==1:return answer('evaluations',[good,bad],sources)
            self.assertEqual(request['requested_target_ids'],['right']);self.assertEqual(request['retained_target_ids'],['left'])
            self.assertEqual(request['target'],budget_engine._model_target_view([targets[1]],['right']))
            self.assertGreaterEqual(payload['options']['num_predict'],128*12+256)
            return answer('evaluations',[evaluation(targets[1],sources[0])],sources)
        with tempfile.TemporaryDirectory()as directory:
            rows,_=budget_engine._stage('proposal',targets,sources,['left','right'],model='primary:local',digest='P',caller=caller,directory=Path(directory))
        self.assertEqual([r['target_id']for r in rows],['left','right']);self.assertEqual(rows[0],good);self.assertEqual(len(calls),2)
        self.preflight_evidence={'mock_calls':2,'retained_ids':['left'],'repaired_ids':['right'],'original_targets_atomic':True,'full_source_retained':True}


if __name__=='__main__':unittest.main()
