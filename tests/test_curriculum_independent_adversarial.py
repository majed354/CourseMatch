"""Independent CPU contracts; stub classifications are not academic calibration.

Numeric expectations below come only from explicit verse-set arithmetic. Model
stubs exercise cache/blinding, not the correctness of a semantic assessment.
Semantic proposals preserve an explicit category; no numerical grade is inferred.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.equivalency import bundle_evidence as bundle
from scripts.equivalency import bundle_engine, candidates, curriculum_exact as exact
from scripts.equivalency.engine import RUBRIC_VERSION


def verse(key, start, end, chapter='البقرة'):
    return {'id':key,'text':f'تفسير تحليلي سورة {chapter} الآيات{start}-{end}'}


def fake_response(field, rows):
    return {'done':True,'done_reason':'stop','message':{'content':json.dumps({field:rows},ensure_ascii=False)}}


def cache_only_caller(calls):
    def call(endpoint, request):
        calls.append(copy.deepcopy(request))
        payload=json.loads(request['messages'][1]['content'])
        source=payload['source'][0]
        reviewing='reviews' in request['format']['properties']
        rows=[]
        for target in payload['target']:
            row={'target_id':target['id'],'facets':[{
                'part_id':part['part_id'],'support':'aligned',
                'source_quotes':[source['whole_ref']],
                'reason':'CPU fixture for provenance and payload only.'} for part in target['parts']],
                'reason':'CPU fixture, not an academic accuracy assertion.'}
            if reviewing:row.update(verdict='strict',possible_tiers=['strict','lenient'],uncertainty='none')
            else:row.update(band='strict',possible_tiers=['strict','lenient'],uncertainty='none')
            rows.append(row)
        return fake_response('reviews' if reviewing else 'evaluations',rows)
    return call


class IndependentCurriculumAdversarialTests(unittest.TestCase):
    def test_explicit_diacritics_cannot_reverse_actor_or_topic_in_literal_proof(self):
        for target,source in [('حكم المُكرِه','حكم المُكرَه'),('أحكام المُحرِم','أحكام المُحرَم'),
                              ('حكم المُكرِه','حكم المكره')]:
            with self.subTest(target=target,source=source):
                self.assertIsNone(exact.proven_alignment([{'id':'t','text':target}],
                                                          [{'id':'s','text':source}],RUBRIC_VERSION))

    def test_literal_cannot_discard_negation_or_change_written_numeric_subject(self):
        for target,source in [('لا يصح بيع الغرر','يصح بيع الغرر'),
                              ('أحكام الطلاق الثلاث','أحكام الطلاق الواحد'),
                              ('تفسير سورة البقرة الآيات1-10','تفسير سورة البقرة الآيات11-20')]:
            with self.subTest(target=target):
                self.assertIsNone(exact.proven_alignment([{'id':'t','text':target}],
                                                          [{'id':'s','text':source}],RUBRIC_VERSION))

    def test_invalid_identity_must_not_become_academic_rejection_or_acceptance(self):
        cases=[([{'id':'t','text':'البيع'},{'id':'t','text':'البيع'}],[{'id':'s','text':'البيع'}]),
               ([{'id':'t','text':'البيع'}],[{'id':'','text':'البيع'}]),
               ([{'id':'t','text':'البيع'}],[{'id':'s','text':'البيع'},{'id':'s','text':'البيع'}])]
        for target,source in cases:
            with self.subTest(target=target,source=source),self.assertRaises(ValueError):
                exact.proven_alignment(target,source,RUBRIC_VERSION)

    def test_range_union_is_a_set_and_foreign_chapter_cannot_fill_missing_verses(self):
        target=[verse('t',1,10)]
        source=[verse('s1',1,4),verse('s2',3,6),verse('s3',1,7,'الفاتحة')]
        frozen=copy.deepcopy((target,source))
        evidence=exact.proven_alignment(target,source,RUBRIC_VERSION)
        self.assertEqual(evidence['pairs'][0]['scope_proof']['covered_verses'],list(range(1,7)))
        self.assertEqual(evidence['pairs'][0]['score'],60)
        self.assertEqual(exact.calculate(target,source,evidence,RUBRIC_VERSION)['decision'],'lenient')
        self.assertEqual((target,source),frozen)

    def test_repeated_same_passage_cannot_turn_forty_percent_into_eighty(self):
        target=[verse('t',1,5)];source=[verse('s1',1,2),verse('s2',1,2)]
        evidence=exact.proven_alignment(target,source,RUBRIC_VERSION)
        self.assertEqual(evidence['pairs'][0]['score'],40)
        self.assertEqual(exact.calculate(target,source,evidence,RUBRIC_VERSION)['decision'],'not_equivalent')

    def test_original_denominator_exact_eighty_and_below_without_source_capacity(self):
        target=[verse(f't{i}',i*5+1,i*5+5)for i in range(10)]
        for end,strict_count,lenient_count,decision in [(40,8,8,'equivalent'),(39,8,8,'equivalent'),
                                                          (38,7,8,'lenient'),(37,7,7,'not_equivalent')]:
            # The eighth original target has80%,60%,40% respectively at39,38,37.
            with self.subTest(end=end):
                source=[verse('single-explicit-source',1,end)]
                evidence=exact.proven_alignment(target,source,RUBRIC_VERSION)
                result=exact.calculate(target,source,evidence,RUBRIC_VERSION)
                self.assertEqual(result['total_topics'],10)
                self.assertEqual(result['strict_count'],strict_count)
                self.assertEqual(result['lenient_count'],lenient_count)
                self.assertEqual(result['decision'],decision)
                self.assertEqual(len(result['strict_matches']),strict_count)

    def test_thirteen_target_threshold_uses_eleven_not_rounded_ten(self):
        target=[verse(f't{i}',i*5+1,i*5+5)for i in range(13)]
        for end,decision in [(50,'not_equivalent'),(55,'equivalent')]:
            result=exact.calculate(target,[verse('s',1,end)],
                exact.proven_alignment(target,[verse('s',1,end)],RUBRIC_VERSION),RUBRIC_VERSION)
            self.assertEqual(result['decision'],decision)
            self.assertEqual(result['total_topics'],13)
            self.assertEqual(result['strict_count'],end//5)

    def test_negative_target_and_unspecified_extra_method_disable_verse_shortcut(self):
        for target,source in [
            ({'id':'t','text':'لا يدرس تفسير تحليلي سورة البقرة الآيات1-10'},verse('s',1,10)),
            (verse('t',1,10),{'id':'s','text':'تفسير تحليلي سورة البقرة الآيات1-10 وحفظها'}),
        ]:
            self.assertIsNone(exact.proven_alignment([target],[source],RUBRIC_VERSION))

    def test_multiple_nonadjacent_explicit_ranges_support_one_original_target_in_bundle(self):
        target=[verse('t',1,10)]
        source=[verse('s1',1,4),{'id':'unrelated','text':'أحكام الطلاق'},verse('s3',5,10)]
        facets=[{'part_id':'p1','support':'aligned','source_quotes':[
                    {'source_id':source[i]['id'],'quote':source[i]['text']}for i in (0,2)],
                 'reason':'نطاق1–10 مكتمل من اتحاد1–4 و5–10 بالطريقة والسورة نفسيهما.'}]
        p={'target_id':'t','facets':facets,'reason':'اتحاد نطاق صريح مثبت، لا متوسط أجزاء.',
           'band':'strict','possible_tiers':['strict','lenient'],'uncertainty':'none'}
        r={'target_id':'t','facets':facets,'reason':'نطاق الآيات والطريقة صريحان في المصدرين.',
           'verdict':'strict','possible_tiers':['strict','lenient'],'uncertainty':'none'}
        result=bundle.calculate(target,source,bundle.adjudicate([p],[r],target,source))
        self.assertEqual(result['strict_matches'],{'t':['s1','s3']})
        self.assertEqual(result['total_topics'],1);self.assertEqual(result['strict_count'],1)

    def test_nondecisive_unknown_is_allowed_only_with_declared_whole_scope_reason(self):
        target=[{'id':'t','text':'دلالة العقد؛ مثال توضيحي.'}];source=[{'id':'s','text':'دلالة العقد'}]
        facets=[{'part_id':'p1','support':'related','source_quotes':[{'source_id':'s','quote':'دلالة العقد'}],'reason':'موضوع محدد ذو صلة.'},
                {'part_id':'p2','support':'unknown','source_quotes':[],'reason':'المثال غير معين، ولا يغير ثبوت صلة أصل الموضوع.'}]
        proposal={'target_id':'t','facets':facets,'reason':'Structural nondecisive-unknown fixture.',
                  'band':'lenient','possible_tiers':['lenient'],'uncertainty':'non_decisive'}
        review={'target_id':'t','facets':facets,'reason':'الجزء غير المعين مثال توضيحي؛ أصل الموضوع ذو صلة معتبرة.',
                'verdict':'lenient','possible_tiers':['lenient'],'uncertainty':'non_decisive'}
        rows=bundle.adjudicate([proposal],[review],target,source)
        self.assertEqual(bundle.calculate(target,source,rows)['decision'],'lenient')
        review['uncertainty']='decisive'
        with self.assertRaises(ValueError):bundle.validate_reviews([review],target,source)
        review.update(verdict='uncertain',possible_tiers=['strict','lenient'])
        rows=bundle.adjudicate([proposal],[review],target,source)
        result=bundle.calculate(target,source,rows)
        self.assertEqual(result['decision'],'insufficient');self.assertIsNone(result['strict_coverage'])

    def test_categories_are_preserved_without_inventing_a_point_score_or_coercing_disagreement(self):
        target=[{'id':'t','text':'دلالة العقد'}];source=[{'id':'s','text':'شرح دلالة العقد'}]
        facet={'part_id':'p1','support':'related','source_quotes':[{'source_id':'s','quote':'دلالة العقد'}],
               'reason':'Structural category-preservation fixture only.'}
        proposal={'target_id':'t','facets':[facet],'reason':'First independent category.',
                  'band':'lenient','possible_tiers':['lenient'],'uncertainty':'none'}
        review={'target_id':'t','facets':[{**facet,'support':'aligned'}],'reason':'Second independent category.',
                'verdict':'strict','possible_tiers':['strict','lenient'],'uncertainty':'none'}
        frozen=copy.deepcopy((proposal,review))
        result=bundle.adjudicate([proposal],[review],target,source)
        self.assertEqual(result[0]['band'],'lenient');self.assertNotIn('score',result[0])
        self.assertEqual(result[0]['adjudication']['state'],'partial')
        self.assertEqual(result[0]['adjudication']['confirmed_tiers'],['lenient'])
        self.assertEqual(set(result[0]['adjudication']['possible_tiers']),{'strict','lenient'})
        self.assertEqual(bundle.calculate(target,source,result)['decision'],'lenient')
        self.assertEqual((proposal,review),frozen)
        legacy=copy.deepcopy(proposal);legacy['score']=8
        saved_legacy=copy.deepcopy(legacy)
        with self.assertRaises(ValueError):bundle.validate_proposals([legacy],target,source)
        self.assertEqual(legacy,saved_legacy,'Reject old numeric evidence without regrading or mutating it')
        del legacy['band']
        with self.assertRaises(ValueError):bundle.validate_proposals([legacy],target,source)

    def test_primary_uncertainty_is_not_relabelled_weak_when_both_readers_remain_uncertain(self):
        target=[{'id':'t','text':'شرط عقد غير محسوم'}];source=[{'id':'s','text':'عنوان العقد العام'}]
        facets=[{'part_id':'p1','support':'unknown','source_quotes':[],
                 'reason':'لا يتحدد من العنوان ارتباط الشرط المطلوب.'}]
        proposal={'target_id':'t','facets':facets,'reason':'لا تكفي أدلة الفئة.',
                  'band':'uncertain','possible_tiers':['strict','lenient'],'uncertainty':'decisive'}
        review={'target_id':'t','facets':facets,'reason':'الحد الإيجابي ما زال ممكنًا.',
                'verdict':'uncertain','possible_tiers':['lenient'],'uncertainty':'decisive'}
        rows=bundle.adjudicate([proposal],[review],target,source)
        self.assertEqual(rows[0]['band'],'uncertain');self.assertNotIn('score',rows[0])
        self.assertEqual(rows[0]['adjudication']['state'],'uncertain')
        self.assertEqual(set(rows[0]['adjudication']['possible_tiers']),{'strict','lenient'})
        result=bundle.calculate(target,source,rows)
        self.assertEqual(result['decision'],'insufficient');self.assertIsNone(result['lenient_coverage'])

    def test_common_sixty_counts_four_of_five_even_when_eighty_remains_possible(self):
        # Declared-reader fixtures test consensus arithmetic, not academic accuracy.
        labels=['تعريف الدلالة','أقسام الدلالة','صيغ العموم','شروط البيع','أحكام الرهن']
        targets=[{'id':f't{i+1}','text':label}for i,label in enumerate(labels)]
        sources=[{'id':'s','text':'؛ '.join(labels[:4])}]
        proposals=[];reviews=[]
        for i,target in enumerate(targets):
            support='aligned' if i<4 else 'absent'
            facets=[{'part_id':'p1','support':support,
                     'source_quotes':[{'source_id':'s','quote':target['text']}]if i<4 else [],
                     'reason':'Declared independent-reader fixture only.'}]
            p={'target_id':target['id'],'facets':facets,'reason':'First reader preserves its own category.',
               'band':'strict'if i<4 else 'weak',
               'possible_tiers':['strict','lenient']if i<4 else [],'uncertainty':'none'}
            r={k:copy.deepcopy(v)for k,v in p.items()if k!='band'};r['verdict']=p['band']
            if i==3:
                r['verdict']='lenient';r['possible_tiers']=['lenient']
                r['facets'][0]['support']='related'
                r['reason']='Second reader establishes sixty without establishing eighty.'
            proposals.append(p);reviews.append(r)
        frozen=copy.deepcopy((proposals,reviews))
        rows=bundle.adjudicate(proposals,reviews,targets,sources)
        disputed=rows[3]
        self.assertEqual(disputed['band'],'strict')
        self.assertEqual(disputed['adjudication']['review']['verdict'],'lenient')
        self.assertEqual(disputed['adjudication']['state'],'partial')
        self.assertEqual(disputed['adjudication']['confirmed_tiers'],['lenient'])
        result=bundle.calculate(targets,sources,rows)
        self.assertEqual(result['total_topics'],5)
        self.assertEqual(result['strict_count'],3)
        self.assertEqual(result['lenient_count'],4)
        self.assertEqual(result['possible_strict_count'],4)
        self.assertEqual(result['lenient_coverage'],80)
        self.assertEqual(result['decision'],'lenient')
        self.assertEqual((proposals,reviews),frozen)
        self.assertTrue(all('score'not in row for row in rows))

    def test_both_lenient_with_possible_strict_still_establish_the_lenient_lower_bound(self):
        targets=[{'id':'t','text':'دلالة العقد'}];sources=[{'id':'s','text':'شرح دلالة العقد'}]
        facets=[{'part_id':'p1','support':'related','source_quotes':[{'source_id':'s','quote':'دلالة العقد'}],
                 'reason':'أصل الصلة ثابت، وبلوغ الحد الأعلى غير محسوم في هذه البنية الاختبارية.'}]
        p={'target_id':'t','facets':facets,'reason':'ثبت الحد الأدنى دون ادعاء نفي الأعلى.',
           'band':'lenient','possible_tiers':['strict','lenient'],'uncertainty':'non_decisive'}
        r={k:copy.deepcopy(v)for k,v in p.items()if k!='band'};r['verdict']='lenient'
        original=copy.deepcopy((p,r))
        rows=bundle.adjudicate([p],[r],targets,sources)
        self.assertEqual(rows[0]['adjudication']['state'],'confirmed')
        self.assertEqual(rows[0]['adjudication']['confirmed_tiers'],['lenient'])
        self.assertEqual(set(rows[0]['adjudication']['possible_tiers']),{'strict','lenient'})
        result=bundle.calculate(targets,sources,rows)
        self.assertEqual(result['strict_count'],0)
        self.assertEqual(result['lenient_count'],1)
        self.assertEqual(result['possible_strict_count'],1)
        self.assertEqual(result['decision'],'lenient')
        self.assertEqual((p,r),original)

    def test_strict_and_uncertain_never_confirm_sixty_from_only_one_reader(self):
        targets=[{'id':'t','text':'دلالة العقد'}];sources=[{'id':'s','text':'شرح دلالة العقد'}]
        p={'target_id':'t','facets':[{'part_id':'p1','support':'aligned',
                'source_quotes':[{'source_id':'s','quote':'دلالة العقد'}],'reason':'First reader fixture.'}],
           'reason':'First reader asserts the strict band.','band':'strict',
           'possible_tiers':['strict','lenient'],'uncertainty':'none'}
        r={'target_id':'t','facets':[{'part_id':'p1','support':'unknown','source_quotes':[],
                'reason':'لا يحسم هذا القارئ حتى الحد الأدنى في البنية الاختبارية.'}],
           'reason':'Second reader has no confirmed positive lower bound.','verdict':'uncertain',
           'possible_tiers':['strict','lenient'],'uncertainty':'decisive'}
        frozen=copy.deepcopy((p,r))
        rows=bundle.adjudicate([p],[r],targets,sources)
        self.assertEqual(rows[0]['band'],'strict')
        self.assertEqual(rows[0]['adjudication']['review']['verdict'],'uncertain')
        self.assertEqual(rows[0]['adjudication']['state'],'uncertain')
        self.assertEqual(rows[0]['adjudication']['confirmed_tiers'],[])
        result=bundle.calculate(targets,sources,rows)
        self.assertEqual(result['decision'],'insufficient')
        self.assertIsNone(result['strict_coverage']);self.assertIsNone(result['lenient_coverage'])
        self.assertEqual((p,r),frozen)

    def _compare(self, directory, calls, **kwargs):
        return bundle_engine.compare_curriculum({'topics':[{'id':'t','text':'دلالة البسملة.'}]},
            {'topics':[{'id':'s','text':'شرح دلالة البسملة.'}]},model_digest='cpu-fixture-digest',
            caller=cache_only_caller(calls),cache_dir=directory,discover=False,**kwargs)

    def test_review_payload_has_whole_source_but_no_proposal_scores_reasons_or_facets(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[];self._compare(directory,calls)
            self.assertEqual(len(calls),2)
            request=json.loads(calls[1]['messages'][1]['content'])
            self.assertEqual([r['source_id'] for r in request['source']],['s'])
            self.assertEqual(''.join(a['quote'] for a in request['source'][0]['anchors']),'شرح دلالة البسملة.')
            def keys(value):
                if isinstance(value,dict):
                    return set(value).union(*(keys(v)for v in value.values()))
                if isinstance(value,list):
                    return set().union(*(keys(v)for v in value))
                return set()
            self.assertFalse({'score','band','verdict','possible_tiers','uncertainty','reason',
                              'facets','evaluations','preferred_sources','adjudication'}&keys(request))
            self.assertNotIn('CPU fixture',json.dumps(request))
            self._compare(directory,calls);self.assertEqual(len(calls),2)

    def test_policy_and_validator_versions_cannot_silently_relabel_old_cache(self):
        for module,attribute in [('scripts.equivalency.engine','RUBRIC_VERSION'),
                                 ('scripts.equivalency.bundle_evidence','VERSION')]:
            with self.subTest(module=module),tempfile.TemporaryDirectory() as directory:
                calls=[];self._compare(directory,calls);self.assertEqual(len(calls),2)
                with patch(module+'.'+attribute,'independent-new-contract-version'):
                    again=self._compare(directory,calls)
                self.assertEqual(len(calls),4,'Different policy/validator contract must require fresh stage evidence')
                self.assertTrue(all(not row['cache_hit'] for row in again['calls']))

    def test_complete_discovery_can_group_nonadjacent_rows_without_grade_payload(self):
        target=[{'id':'t','text':'أركان العقد وشروطه'}]
        source=[{'id':'s1','text':'أركان العقد'},{'id':'s2','text':'أحكام الطهارة'},
                {'id':'s3','text':'فضائل الصلاة'},{'id':'s4','text':'شروط العقد'}]
        calls=[]
        def caller(_,request):
            payload=json.loads(request['messages'][1]['content']);calls.append(payload)
            self.assertEqual({p['source_id'] for p in payload['requested_pairs']},{s['id'] for s in source})
            self.assertNotIn('score',json.dumps(payload))
            items=[{'source_id':s['id'],'status':'unrelated'}for s in source]
            for i in (0,3):items[i].update(status='candidate',reason='جزء صريح مكمل.',source_ref='source:'+source[i]['id']+':whole',target_ref='target:t:whole')
            return {'done':True,'done_reason':'stop','message':{'content':json.dumps({'scan':[{'target_id':'t','sources':items}],
                'groups':[{'target_id':'t','source_ids':['s1','s4'],'reason':'الأركان والشروط أدلة متكاملة غير متجاورة.',
                           'evidence':[{'source_id':source[i]['id'],'source_ref':'source:'+source[i]['id']+':whole','target_ref':'target:t:whole'}for i in (0,3)]}]},ensure_ascii=False)}}
        with tempfile.TemporaryDirectory() as directory:
            result=candidates.discover_candidates(target,source,model='cpu-only',model_digest='cpu-only',caller=caller,cache_dir=directory)
        self.assertEqual(len(calls),1);self.assertEqual(result['coverage']['pairs'],4)
        self.assertEqual(result['groups'][0]['source_ids'],['s1','s4'])
        self.assertEqual(result['groups'][0]['evidence'],[
            {'source_id':source[i]['id'],'source_quote':source[i]['text'],'target_quote':target[0]['text']}
            for i in (0,3)])
        self.assertEqual(result['coverage']['complete'],True)


if __name__=='__main__':unittest.main()
