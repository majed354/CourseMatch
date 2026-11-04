import copy
import hashlib
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
            self.assertEqual(request['source'],source_view(sources))
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


if __name__=='__main__':unittest.main()
