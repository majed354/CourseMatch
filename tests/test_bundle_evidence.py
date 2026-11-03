"""CPU contracts, not measurements of generative academic accuracy."""
import copy
import unittest
from unittest.mock import patch
from jsonschema import Draft202012Validator
from scripts.equivalency import bundle_evidence as bundle


def topic(key,text):return {'id':key,'text':text}
def facet(part_id,support='aligned',sid='s1',quote='تعريف العقد',reason='سبب أكاديمي محدد لنطاق الجزء'):
    return {'part_id':part_id,'support':support,'source_quotes':([] if sid is None else [{'source_id':sid,'quote':quote}]),'reason':reason}
def proposal(tid='t1',band='strict',facets=None,uncertainty='none',possible=None):
    # Synthetic category fixture: no conversion from a numeric model score.
    return {'target_id':tid,'facets':facets or [facet('p1')],'reason':'تقييم كامل نطاق مفردة الهدف من منهج المصدر',
            'band':band,'possible_tiers':possible if possible is not None else {'strict':['strict','lenient'],'lenient':['lenient'],'weak':[],'uncertain':['strict','lenient']}[band],
            'uncertainty':uncertainty}
def review(tid='t1',verdict='strict',facets=None,uncertainty='none',possible=None):
    return {'target_id':tid,'facets':facets or [facet('p1')],'reason':'مراجعة مستقلة لكامل منهج المصدر ونطاق الهدف',
            'verdict':verdict,'possible_tiers':possible if possible is not None else {'strict':['strict','lenient'],'lenient':['lenient'],'weak':[],'uncertain':['strict','lenient']}[verdict],
            'uncertainty':uncertainty}


class BundleEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.targets=[topic('t1','تعريف العقد')];self.sources=[topic('s1','تعريف العقد')]

    def test_literal_partition_covers_every_character_without_changing_text_or_ranges(self):
        texts=['تعريف العقد، أركانه؛ شروطه. - تطبيقاته',' 1. تعريف العقد.. ثم أحكامه! ',
               'تفسير سورة البقرة (1 - 5)، والآيات (6-10).','النسبة1.5، مثال1،3 والنطاق1-3.','مصطلح-متصل بلا تقسيم','تعريف بلا ترقيم']
        for text in texts:
            with self.subTest(text=text):
                parts=bundle.partition_parts(text)
                self.assertEqual(''.join(p['quote'] for p in parts),text)
                self.assertEqual(parts[0]['start'],0);self.assertEqual(parts[-1]['end'],len(text))
                self.assertEqual([p['part_id'] for p in parts],[f'p{i+1}'for i in range(len(parts))])
                for p in parts:self.assertEqual(p['quote'],text[p['start']:p['end']])
                for a,b in zip(parts,parts[1:]):self.assertEqual(a['end'],b['start'])
        self.assertEqual(len(bundle.partition_parts('تفسير البقرة (1 - 5) تفسيرا تحليليا.')),1)

    def test_proposal_and_blinded_review_schemas_reject_wrong_types_and_score_leak(self):
        for schema in [bundle.PROPOSAL_SCHEMA,bundle.REVIEW_SCHEMA]:Draft202012Validator.check_schema(schema)
        self.assertTrue(Draft202012Validator(bundle.PROPOSAL_SCHEMA).is_valid({'evaluations':[proposal()]}))
        self.assertTrue(Draft202012Validator(bundle.REVIEW_SCHEMA).is_valid({'reviews':[review()]}))
        bad=review();bad['score']=85
        self.assertFalse(Draft202012Validator(bundle.REVIEW_SCHEMA).is_valid({'reviews':[bad]}))
        bad=proposal();bad['band']=False
        self.assertFalse(Draft202012Validator(bundle.PROPOSAL_SCHEMA).is_valid({'evaluations':[bad]}))
        with self.assertRaises(ValueError):bundle.validate_reviews([{**review(),'score':85}],self.targets,self.sources)

    def test_every_original_target_and_part_are_required_once(self):
        targets=[topic('t1','تعريف العقد، أركانه.'),topic('t2','شروط العقد')]
        sources=[topic('s1','تعريف العقد وأركانه وشروطه')]
        one=proposal(facets=[facet('p1',quote='تعريف العقد'),facet('p2',quote='أركانه')])
        two=proposal('t2',facets=[facet('p1',quote='شروطه')])
        self.assertEqual(bundle.validate_proposals([one,two],targets,sources),[one,two])
        for rows in [[one],[one,one],[{**one,'target_id':'foreign'},two],
                     [{**one,'facets':[one['facets'][0],one['facets'][0]]},two],
                     [{**one,'facets':[one['facets'][0]]},two]]:
            with self.subTest(rows=rows),self.assertRaises(ValueError):bundle.validate_proposals(rows,targets,sources)

    def test_partial_validation_keeps_complete_tables_for_context_and_bounds(self):
        targets=[topic('heading','سورة البقرة'),topic('t1','تفسير الآيات (1-5) تفسيرا تحليليا')]
        sources=[topic('s1','تفسير سورة البقرة (1-5) تفسيرا تحليليا')]
        row=proposal(facets=[facet('p1',quote=sources[0]['text'])])
        with patch.object(bundle,'_bound',return_value=100) as bound:
            bundle.validate_proposals([row],targets,sources,target_ids=['t1'])
        self.assertEqual(bound.call_args.args[0],targets);self.assertEqual(bound.call_args.args[1],sources)
        with self.assertRaises(ValueError):bundle.validate_proposals([row],targets,sources)
        with self.assertRaises(ValueError):bundle.validate_proposals([row],targets,sources,target_ids=['foreign'])

    def test_quotes_bind_to_own_source_literal_and_do_not_accept_invented_or_duplicate_evidence(self):
        sources=[topic('s1','تعريف العقد'),topic('s2','أركان العقد')]
        for proof in [[{'source_id':'s1','quote':'أركان العقد'}],[{'source_id':'foreign','quote':'تعريف العقد'}],
                      [{'source_id':'s1','quote':'تعريف العقد.'}],
                      [{'source_id':'s1','quote':'تعريف العقد'}]*2,[]]:
            row=proposal();row['facets'][0]['source_quotes']=proof
            with self.subTest(proof=proof),self.assertRaises(ValueError):bundle.validate_proposals([row],self.targets,sources)

    def test_several_sources_can_cover_one_whole_target_without_averaging_part_scores(self):
        targets=[topic('t1','تعريف العقد، أركان العقد.')]
        sources=[topic('s1','تعريف العقد'),topic('s2','أركان العقد')]
        facets=[facet('p1'),facet('p2',sid='s2',quote='أركان العقد')]
        rows=bundle.adjudicate([proposal(band='strict',facets=facets)],[review(facets=facets)],targets,sources)
        calculation=bundle.calculate(targets,sources,rows)
        self.assertEqual(rows[0]['band'],'strict');self.assertEqual(calculation['total_topics'],1)
        self.assertEqual(calculation['strict_count'],1);self.assertEqual(calculation['strict_matches'],{'t1':['s1','s2']})

    def test_one_explicit_source_can_support_multiple_original_targets_without_capacity_packing(self):
        targets=[topic('t1','تعريف العقد'),topic('t2','أركان العقد')]
        sources=[topic('s1','تعريف العقد، أركان العقد')]
        proposals=[proposal(),proposal('t2',facets=[facet('p1',quote='أركان العقد')])]
        reviews=[review(),review('t2',facets=[facet('p1',quote='أركان العقد')])]
        rows=bundle.adjudicate(proposals,reviews,targets,sources);before=copy.deepcopy(rows)
        result=bundle.calculate(targets,sources,rows)
        self.assertEqual(result['strict_count'],2);self.assertEqual(result['strict_coverage'],100)
        self.assertEqual(result['strict_matches'],{'t1':['s1'],'t2':['s1']});self.assertEqual(rows,before)

    def test_minor_unknown_can_be_nondecisive_for_a_reasoned_whole_lenient_verdict(self):
        targets=[topic('t1','تعريف العقد، تفصيل توضيحي.')]
        facets=[facet('p1'),facet('p2','unknown',None,reason='الصياغة الثانوية مختصرة؛ نطاق تعريف العقد مثبت ولا يتغير حد60 بسببها.')]
        p=proposal(band='lenient',facets=facets,uncertainty='non_decisive');r=review(verdict='lenient',facets=facets,uncertainty='non_decisive')
        rows=bundle.adjudicate([p],[r],targets,self.sources);result=bundle.calculate(targets,self.sources,rows)
        self.assertEqual(rows[0]['band'],'lenient');self.assertEqual(rows[0]['adjudication']['state'],'confirmed')
        self.assertEqual(result['decision'],'lenient');self.assertEqual(result['lenient_count'],1)
        bad=copy.deepcopy(r);bad['uncertainty']='none'
        with self.assertRaises(ValueError):bundle.validate_reviews([bad],targets,self.sources)

    def test_decisive_unknown_stays_uncertain_instead_of_becoming_low_score_or_rejection(self):
        targets=[topic('t1','تعريف العقد، شرط أساسي غير محسوم.')]
        facets=[facet('p1'),facet('p2','unknown',None,reason='لا يثبت الشرط الأساسي من المصدر؛ يؤثر في الحد.')]
        rows=bundle.adjudicate([proposal(band='lenient',facets=facets,uncertainty='non_decisive')],[review(verdict='uncertain',facets=facets,uncertainty='decisive')],targets,self.sources)
        result=bundle.calculate(targets,self.sources,rows)
        self.assertEqual(result['decision'],'insufficient');self.assertIsNone(result['strict_coverage']);self.assertIsNone(result['lenient_coverage'])
        bad=review(verdict='lenient',facets=facets,uncertainty='decisive')
        with self.assertRaises(ValueError):bundle.validate_reviews([bad],targets,self.sources)

    def test_all_unknown_is_not_proven_absence_and_all_aligned_is_not_lenient_or_weak(self):
        unknown=[facet('p1','unknown',None)]
        for r in [review(verdict='weak',facets=unknown,uncertainty='non_decisive'),
                  review(verdict='uncertain',facets=unknown,uncertainty='decisive',possible=[]),
                  review(verdict='lenient'),review(verdict='weak')]:
            with self.subTest(review=r),self.assertRaises(ValueError):bundle.validate_reviews([r],self.targets,self.sources)
        with self.assertRaises(ValueError):bundle.validate_proposals([proposal(band='lenient')],self.targets,self.sources)

    def test_weak_minor_unknown_needs_explicit_negative_core_evidence(self):
        targets=[topic('t1','أحكام الرهن، مثال توضيحي.')];sources=[topic('s1','أحكام الصلاة')]
        f=[facet('p1','absent','s1','أحكام الصلاة',reason='منهج المصدر الصلاة؛ لا يثبت موضوع الرهن الأساسي.'),facet('p2','unknown',None,reason='المثال غير محسوم لكنه لا يغير اختلاف الموضوع الأساسي دون60.')]
        r=review(verdict='weak',facets=f,uncertainty='non_decisive')
        bundle.validate_reviews([r],targets,sources)
        bad=copy.deepcopy(r);bad['facets'][0]['source_quotes']=[]
        with self.assertRaises(ValueError):bundle.validate_reviews([bad],targets,sources)

    def test_disagreement_keeps_original_band_and_unions_both_independent_possible_bounds(self):
        p=proposal(band='strict');r=review(verdict='lenient',facets=[facet('p1','related')])
        originals=copy.deepcopy([p,r]);rows=bundle.adjudicate([p],[r],self.targets,self.sources)
        self.assertEqual(rows[0]['band'],'strict');self.assertEqual(rows[0]['adjudication']['state'],'partial')
        self.assertEqual(rows[0]['adjudication']['confirmed_tiers'],['lenient'])
        self.assertEqual(set(rows[0]['adjudication']['possible_tiers']),{'strict','lenient'})
        self.assertEqual(bundle.calculate(self.targets,self.sources,rows)['decision'],'lenient');self.assertEqual([p,r],originals)

    def test_metadata_grade_or_review_tampering_cannot_pass_revalidation(self):
        rows=bundle.adjudicate([proposal()],[review()],self.targets,self.sources)
        bundle.validate_evidence({'provider':'curriculum_evidence','pairs':rows},self.targets,self.sources)
        for mutation in ['state','possible','confirmed_tiers','version','review_score','score','band']:
            bad=copy.deepcopy(rows)
            if mutation=='state':bad[0]['adjudication']['state']='uncertain'
            elif mutation=='possible':bad[0]['adjudication']['possible_tiers']=[]
            elif mutation=='confirmed_tiers':bad[0]['adjudication']['confirmed_tiers']=[]
            elif mutation=='version':bad[0]['adjudication']['version']='old'
            elif mutation=='review_score':bad[0]['adjudication']['review']['score']=100
            elif mutation=='score':bad[0]['score']=40
            else:bad[0]['band']='weak'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):bundle.validate_evaluations(bad,self.targets,self.sources)

    def test_proven_scope_limits_positive_witness_but_negative_bound_uses_every_source(self):
        target=[topic('t1','تفسير سورة البقرة الآيات (1-10) تفسيرا تحليليا.')]
        source=[topic('s1','تفسير سورة البقرة الآيات (1-4) تفسيرا تحليليا.'),topic('s2','تفسير سورة البقرة الآيات (5-10) تفسيرا تحليليا.')]
        f=[facet('p1','aligned','s1',source[0]['text'])]
        with self.assertRaises(ValueError):bundle.validate_proposals([proposal(band='strict',facets=f)],target,source)
        r=review(verdict='uncertain',facets=[facet('p1','unknown','s1',source[0]['text'])],uncertainty='decisive',possible=[])
        with self.assertRaises(ValueError):bundle.validate_reviews([r],target,source)
        r['possible_tiers']=['strict','lenient'];bundle.validate_reviews([r],target,source)
        with self.assertRaises(ValueError):bundle.validate_reviews([review(facets=f)],target,source)

    def test_uncertain_without_positive_tier_requires_proven_full_curriculum_exclusion(self):
        target=[topic('t1','تفسير سورة البقرة الآيات (1-10) تفسيرا تحليليا.')]
        source=[topic('s1','تفسير سورة آل عمران الآيات (1-10) تفسيرا تحليليا.')]
        f=[facet('p1','unknown','s1',source[0]['text'])]
        r=review(verdict='uncertain',facets=f,uncertainty='decisive',possible=[])
        bundle.validate_reviews([r],target,source)
        source.append(topic('s2','موضوع عام غير محدد النطاق'))
        with self.assertRaises(ValueError):bundle.validate_reviews([r],target,source)

    def test_integer_threshold_original_target_count_and_full_possible_upper_bound(self):
        targets=[topic(f't{i}','تعريف العقد' if i<9 else 'أحكام الرهن')for i in range(13)]
        sources=[topic('s1','تعريف العقد')];ps=[];rs=[]
        for i,t in enumerate(targets):
            if i<9:ps.append(proposal(t['id'],band='strict'));rs.append(review(t['id']))
            elif i==9:
                f=[facet('p1','unknown',None)];ps.append(proposal(t['id'],band='uncertain',facets=f,uncertainty='decisive',possible=['lenient']));rs.append(review(t['id'],'uncertain',f,'decisive',['lenient']))
            else:
                f=[facet('p1','absent',None)];ps.append(proposal(t['id'],band='weak',facets=f));rs.append(review(t['id'],'weak',f))
        result=bundle.calculate(targets,sources,bundle.adjudicate(ps,rs,targets,sources))
        self.assertEqual(result['decision'],'not_equivalent');self.assertEqual(result['total_topics'],13)
        self.assertEqual(result['possible_lenient_count'],10);self.assertEqual(result['strict_count'],9)
        self.assertAlmostEqual(result['strict_coverage'],900/13)

    def test_adding_parts_does_not_create_target_credits_or_component_weights(self):
        targets=[topic('t1','تعريف العقد، أركانه؛ شروطه.')];sources=[topic('s1','تعريف العقد، أركانه؛ شروطه.')]
        parts=bundle.partition_parts(targets[0]['text']);f=[facet(p['part_id'],quote=p['quote'])for p in parts]
        result=bundle.calculate(targets,sources,bundle.adjudicate([proposal(facets=f)],[review(facets=f)],targets,sources))
        self.assertEqual(len(parts),3);self.assertEqual(result['total_topics'],1);self.assertEqual(result['strict_count'],1)
        self.assertEqual(result['weighting'],'topic_count')

    def test_empty_inputs_or_unreviewed_payload_stay_insufficient(self):
        for targets,sources,reviewed in [([],self.sources,True),(self.targets,[],True),(self.targets,self.sources,False)]:
            result=bundle.calculate(targets,sources,[],reviewed=reviewed)
            self.assertEqual(result['decision'],'insufficient');self.assertIsNone(result['strict_coverage'])

    def test_ten_of_thirteen_does_not_round_up_but_eleven_qualifies_without_source_capacity(self):
        for qualified,decision in [(10,'not_equivalent'),(11,'equivalent')]:
            targets=[topic(f't{i}','تعريف العقد' if i<qualified else 'أحكام الرهن')for i in range(13)]
            ps=[];rs=[]
            for i,target in enumerate(targets):
                if i<qualified:ps.append(proposal(target['id']));rs.append(review(target['id']))
                else:
                    absent=[facet('p1','absent',None)];ps.append(proposal(target['id'],'weak',absent));rs.append(review(target['id'],'weak',absent))
            result=bundle.calculate(targets,self.sources,bundle.adjudicate(ps,rs,targets,self.sources))
            self.assertEqual(result['decision'],decision);self.assertEqual(result['strict_count'],qualified)
            self.assertEqual(result['accounting'],'original_target_topics');self.assertEqual(result['total_topics'],13)

    def test_all_aligned_non_strict_band_is_rejected_without_reclassification(self):
        for band in ['lenient','weak','uncertain']:
            row=proposal(band=band,uncertainty='decisive' if band=='uncertain' else 'none');before=copy.deepcopy(row)
            with self.subTest(band=band),self.assertRaises(ValueError):bundle.validate_proposals([row],self.targets,self.sources)
            self.assertEqual(row,before)

    def test_numeric_scores_are_rejected_instead_of_becoming_bands(self):
        for row in [dict(proposal(),score=85),dict(proposal(),band=85)]:
            before=copy.deepcopy(row)
            with self.assertRaises(ValueError):bundle.validate_proposals([row],self.targets,self.sources)
            self.assertEqual(row,before)

    def test_proposal_and_review_follow_the_same_evidence_and_uncertainty_rules(self):
        targets=[topic('t1','تعريف العقد، مثال.')]
        facets=[facet('p1'),facet('p2','unknown',None,reason='المثال ثانوي لا يغير الصلة المثبتة.')]
        p=proposal(band='lenient',facets=facets,uncertainty='non_decisive')
        r=review(verdict='lenient',facets=facets,uncertainty='non_decisive')
        bundle.validate_proposals([p],targets,self.sources);bundle.validate_reviews([r],targets,self.sources)
        for row,validate in [(p,bundle.validate_proposals),(r,bundle.validate_reviews)]:
            bad=copy.deepcopy(row);bad['uncertainty']='none'
            with self.assertRaises(ValueError):validate([bad],targets,self.sources)
            bad=copy.deepcopy(row);bad['uncertainty']='decisive'
            with self.assertRaises(ValueError):validate([bad],targets,self.sources)

    def test_both_uncertain_readers_never_confirm_and_union_possible_tiers_symmetrically(self):
        f=[facet('p1','unknown',None)]
        p=proposal(band='uncertain',facets=f,uncertainty='decisive',possible=['lenient'])
        r=review(verdict='uncertain',facets=f,uncertainty='decisive',possible=['strict','lenient'])
        rows=bundle.adjudicate([p],[r],self.targets,self.sources)
        self.assertEqual(rows[0]['band'],'uncertain');self.assertEqual(rows[0]['adjudication']['state'],'uncertain')
        self.assertEqual(set(rows[0]['adjudication']['possible_tiers']),{'strict','lenient'})
        self.assertEqual(bundle.calculate(self.targets,self.sources,rows)['decision'],'insufficient')
        # A weak review cannot erase the independent proposal's possible bound.
        r=review(verdict='weak',facets=[facet('p1','absent',None)])
        rows=bundle.adjudicate([p],[r],self.targets,self.sources)
        self.assertEqual(rows[0]['adjudication']['possible_tiers'],['lenient'])
        self.assertEqual(rows[0]['adjudication']['state'],'uncertain')


    def test_strict_whole_target_can_retain_a_reasoned_minor_gap_without_part_averaging(self):
        targets=[topic('t1','تعريف العقد، مثال توضيحي؛ تطبيق ثانوي.')]
        sources=[topic('s1','تعريف العقد وأركانه وشروط صحته')]
        # One core anchor and two minor details are not a one-third score.
        facets=[facet('p1'),
            facet('p2','limited','s1','تعريف العقد',reason='المثال تفصيل توضيحي لا يغير نطاق تعريف العقد.'),
            facet('p3','unknown',None,reason='التطبيق الثانوي غير مصرح به ولا يغير المضمون الأساسي المثبت.')]
        reason='المصدر يثبت تعريف العقد وأركانه وشروط صحته؛ المثال والتطبيق تفصيلان ثانويان، ولا يغير اختصارهما ثبوت نطاق المفردة الكلي عند حد80.'
        p=proposal(band='strict',facets=facets,uncertainty='non_decisive');p['reason']=reason
        r=review(verdict='strict',facets=facets,uncertainty='non_decisive');r['reason']=reason
        before=copy.deepcopy([p,r]);rows=bundle.adjudicate([p],[r],targets,sources)
        self.assertEqual(rows[0]['band'],'strict');self.assertEqual(rows[0]['reason'],reason)
        self.assertEqual(rows[0]['adjudication']['state'],'confirmed')
        result=bundle.calculate(targets,sources,rows)
        self.assertEqual(result['strict_count'],1);self.assertEqual(result['total_topics'],1)
        self.assertEqual(result['decision'],'equivalent');self.assertEqual([p,r],before)
        for row,validate in [(p,bundle.validate_proposals),(r,bundle.validate_reviews)]:
            bad=copy.deepcopy(row);bad['uncertainty']='none'
            with self.assertRaises(ValueError):validate([bad],targets,sources)
            bad=copy.deepcopy(row);bad['reason']=''
            with self.assertRaises(ValueError):validate([bad],targets,sources)

    def test_strict_without_quoted_core_alignment_is_not_substantiated_for_either_reader(self):
        targets=[topic('t1','تعريف العقد، تفاصيله')]
        for support in ['related','limited','absent','unknown']:
            facets=[facet('p1',support,None if support in ('unknown','absent') else 's1'),
                    facet('p2',support,None if support in ('unknown','absent') else 's1')]
            for row,validate in [(proposal(band='strict',facets=facets,uncertainty='non_decisive'),bundle.validate_proposals),
                                 (review(verdict='strict',facets=facets,uncertainty='non_decisive'),bundle.validate_reviews)]:
                before=copy.deepcopy(row)
                with self.subTest(support=support),self.assertRaises(ValueError):validate([row],targets,self.sources)
                self.assertEqual(row,before)

    def test_material_scope_gap_review_does_not_confirm_primary_strict_even_with_core_evidence(self):
        targets=[topic('t1','تعريف العقد، أحكامه الأساسية')]
        facets=[facet('p1'),facet('p2','unknown',None,reason='لم يثبت نطاق الأحكام الأساسية من النص.')]
        p=proposal(band='strict',facets=facets,uncertainty='non_decisive')
        p['reason']='تعريف العقد ثابت؛ يقدّر القارئ الأول أن ذكر الأحكام تفصيل تابع لا يغير حد80.'
        for verdict,uncertainty in [('lenient','non_decisive'),('uncertain','decisive')]:
            r=review(verdict=verdict,facets=facets,uncertainty=uncertainty)
            r['reason']='الأحكام الأساسية أوسع من التعريف؛ غياب نطاقها مؤثر في إثبات حد80 للمفردة كاملة.'
            rows=bundle.adjudicate([p],[r],targets,self.sources)
            self.assertEqual(rows[0]['band'],'strict');self.assertEqual(rows[0]['adjudication']['state'],'partial' if verdict=='lenient' else 'uncertain')
            self.assertEqual(rows[0]['adjudication']['confirmed_tiers'],['lenient'] if verdict=='lenient' else [])
            self.assertIn('strict',rows[0]['adjudication']['possible_tiers'])
            self.assertEqual(bundle.calculate(targets,self.sources,rows)['decision'],'lenient' if verdict=='lenient' else 'insufficient')


    def test_lenient_proves_sixty_with_strict_still_possible_without_changing_raw_band(self):
        p=proposal(band='lenient',facets=[facet('p1','related')],uncertainty='non_decisive',possible=['strict','lenient'])
        r=review(verdict='lenient',facets=[facet('p1','related')],uncertainty='non_decisive',possible=['strict','lenient'])
        originals=copy.deepcopy([p,r]);rows=bundle.adjudicate([p],[r],self.targets,self.sources)
        self.assertEqual(rows[0]['band'],'lenient');self.assertEqual(rows[0]['adjudication']['state'],'confirmed')
        self.assertEqual(rows[0]['adjudication']['confirmed_tiers'],['lenient'])
        result=bundle.calculate(self.targets,self.sources,rows)
        self.assertEqual(result['decision'],'lenient');self.assertEqual(result['lenient_count'],1)
        self.assertEqual(result['strict_count'],0);self.assertEqual(result['possible_strict_count'],1)
        self.assertEqual([p,r],originals)
        for row,validate in [(p,bundle.validate_proposals),(r,bundle.validate_reviews)]:
            bad=copy.deepcopy(row);bad['uncertainty']='none'
            with self.assertRaisesRegex(ValueError,'unresolved_strict_not_disclosed'):validate([bad],self.targets,self.sources)

    def test_established_overlap_is_symmetric_and_strict_uncertain_does_not_prove_sixty(self):
        cases=[('strict','lenient','partial',['lenient']),('lenient','strict','partial',['lenient']),
               ('strict','uncertain','uncertain',[]),('uncertain','strict','uncertain',[]),('weak','weak','confirmed',[])]
        for first,second,state,confirmed in cases:
            def facets(band):return [facet('p1','aligned' if band=='strict' else 'related' if band=='lenient' else 'unknown' if band=='uncertain' else 'absent',None if band in ('uncertain','weak') else 's1')]
            p=proposal(band=first,facets=facets(first),uncertainty='decisive' if first=='uncertain' else 'none')
            r=review(verdict=second,facets=facets(second),uncertainty='decisive' if second=='uncertain' else 'none')
            originals=copy.deepcopy([p,r]);rows=bundle.adjudicate([p],[r],self.targets,self.sources)
            with self.subTest(first=first,second=second):
                self.assertEqual(rows[0]['adjudication']['state'],state)
                self.assertEqual(rows[0]['adjudication']['confirmed_tiers'],confirmed)
                self.assertEqual(rows[0]['band'],first);self.assertEqual(rows[0]['adjudication']['review']['verdict'],second)
                self.assertEqual([p,r],originals)
                self.assertEqual(bundle.calculate(self.targets,self.sources,rows)['decision'],'lenient' if confirmed else 'not_equivalent' if first==second=='weak' else 'insufficient')

    def test_original_target_threshold_four_of_five_and_ten_of_thirteen_keep_established_lenience(self):
        for total,established,decision in [(5,4,'lenient'),(5,3,'insufficient'),(13,10,'insufficient'),(13,11,'lenient')]:
            targets=[topic(f't{i}','تعريف العقد')for i in range(total)];ps=[];rs=[]
            for i,t in enumerate(targets):
                if i<established:
                    ps.append(proposal(t['id'],band='strict'))
                    rs.append(review(t['id'],verdict='lenient',facets=[facet('p1','related')],uncertainty='non_decisive',possible=['strict','lenient']))
                else:
                    f=[facet('p1','unknown',None)]
                    ps.append(proposal(t['id'],band='uncertain',facets=f,uncertainty='decisive'))
                    rs.append(review(t['id'],verdict='uncertain',facets=f,uncertainty='decisive'))
            result=bundle.calculate(targets,self.sources,bundle.adjudicate(ps,rs,targets,self.sources))
            with self.subTest(total=total,established=established):
                self.assertEqual(result['decision'],decision)
                self.assertEqual(result['possible_strict_count'],total)
                if decision=='lenient':
                    self.assertEqual(result['lenient_count'],established);self.assertEqual(result['strict_count'],0)
                    self.assertAlmostEqual(result['lenient_coverage'],100*established/total)
                else:
                    self.assertIsNone(result['lenient_coverage']);self.assertEqual(result['adjudication_review']['confirmed_lenient_count'],established)
