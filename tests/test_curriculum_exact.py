import copy
import unittest

from scripts.equivalency.curriculum_exact import proven_alignment, validate_evidence, calculate, scope_upper_bound


def verse(key, bounds, chapter='البقرة', method='تفسير تحليلي'):
    return {'id':key, 'text':f'{method} سورة {chapter} الآيات{bounds}'}


class CurriculumExactTests(unittest.TestCase):
    def test_explicit_source_range_supports_multiple_distinct_target_ranges(self):
        targets=[verse('t1','1-10'),verse('t2','11-20')]
        sources=[verse('s1','1-20')]
        evidence=proven_alignment(targets,sources,'test')
        result=calculate(targets,sources,evidence,'test')
        self.assertEqual(result['decision'],'equivalent')
        self.assertEqual(result['strict_count'],2)
        self.assertEqual(result['total_topics'],2)

    def test_two_source_units_can_prove_one_target_without_averaging_scores(self):
        targets=[verse('t','1-20')];sources=[verse('s1','1-10'),verse('s2','11-20')]
        evidence=proven_alignment(targets,sources,'test')
        self.assertEqual(evidence['pairs'][0]['score'],100)
        self.assertEqual(evidence['pairs'][0]['source_ids'],['s1','s2'])
        self.assertEqual(calculate(targets,sources,evidence,'test')['strict_count'],1)

    def test_overlapping_sources_are_union_not_added(self):
        targets=[verse('t','1-10')];sources=[verse('s1','1-4'),verse('s2','3-6')]
        evidence=proven_alignment(targets,sources,'test')
        self.assertEqual(evidence['pairs'][0]['score'],60)
        self.assertEqual(calculate(targets,sources,evidence,'test')['decision'],'lenient')

    def test_incomplete_union_below_sixty_and_different_chapters_reject(self):
        for sources in [[verse('s1','1-3'),verse('s2','2-4')],[verse('s1','1-10','الفاتحة')]]:
            # The second case deliberately uses an invalid chapter range.
            if 'الفاتحة' in sources[0]['text']:
                sources=[verse('s1','1-7','الفاتحة')]
            evidence=proven_alignment([verse('t','1-10')],sources,'test')
            self.assertEqual(calculate([verse('t','1-10')],sources,evidence,'test')['decision'],'not_equivalent')

    def test_extra_method_or_unnamed_scope_never_gets_numeric_shortcut(self):
        for source in [{'id':'s','text':'تفسير سورة البقرة الآيات1-10'},
                       {'id':'s','text':'تفسير تحليلي سورة البقرة الآيات1-10 وحفظها'}]:
            self.assertIsNone(proven_alignment([verse('t','1-10')],[source],'test'))

    def test_raw_topic_rows_and_proof_are_not_mutated(self):
        targets=[verse('t','1-10')];sources=[verse('s','1-8')]
        before=copy.deepcopy((targets,sources));evidence=proven_alignment(targets,sources,'test')
        frozen=copy.deepcopy(evidence);validate_evidence(evidence,targets,sources,'test')
        self.assertEqual((targets,sources),before);self.assertEqual(evidence,frozen)
        altered=copy.deepcopy(evidence);altered['pairs'][0]['score']=100
        with self.assertRaises(ValueError):validate_evidence(altered,targets,sources,'test')

    def test_literal_original_targets_count_once_even_with_multiple_matching_sources(self):
        targets=[{'id':'t','text':'تعريف علم أصول الفقه ونشأته'}]
        sources=[{'id':'s1','text':targets[0]['text']},{'id':'s2','text':targets[0]['text']}]
        evidence=proven_alignment(targets,sources,'test');result=calculate(targets,sources,evidence,'test')
        self.assertEqual(result['strict_count'],1);self.assertEqual(result['strict_matches']['t'],['s1','s2'])

    def test_subset_cannot_be_used_as_full_curriculum_exclusion(self):
        targets=[verse('t','1-10')];sources=[verse('s1','11-20'),verse('s2','1-10')]
        self.assertEqual(scope_upper_bound(targets,sources,'t',['s1']),0)
        self.assertEqual(scope_upper_bound(targets,sources,'t',['s1','s2']),100)
        sources.append({'id':'s3','text':'موضوع غير محدد النطاق'})
        self.assertIsNone(scope_upper_bound(targets,sources,'t',['s1','s2','s3']))

    def test_bound_preserves_carried_chapter_context_and_deduplicates_overlap(self):
        targets=[verse('t','1-10')];sources=[verse('s1','1-4'),{'id':'s2','text':'تفسير تحليلي الآيات3-6'}]
        self.assertEqual(scope_upper_bound(targets,sources,'t',['s1','s2']),60)


if __name__=='__main__':unittest.main()
