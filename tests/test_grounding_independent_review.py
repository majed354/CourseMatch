"""Independent CPU-only guards for evidence bounds and graph adjudication.

These are contract/mechanism tests, not academic gold labels. They neither read
the sealed evaluation set nor call a local model. Numeric proposals are retained
verbatim; no synthetic facet weight is used.
"""
import itertools
import json
import random
import tempfile
import unittest
from unittest.mock import patch

from scripts.equivalency.engine import calculate, compare
from scripts.equivalency.grounding import adjudicate, validate_reviews


def topic(key, text):
    return {'id': key, 'text': text}


def proposal(target, source, score):
    return {'target_id': target['id'], 'source_id': source['id'], 'score': score,
            'target_quote': target['text'], 'source_quote': source['text'],
            'reason': 'يقتصر التقدير على النطاق المصرح به في هذه المفردة.'}


def review(target, source, verdict, support, possible=None):
    if possible is None:
        possible = {'strict': ['strict', 'lenient'], 'lenient': ['lenient'],
                    'weak': [], 'uncertain': ['strict', 'lenient']}[verdict]
    return {'target_id': target['id'], 'source_id': source['id'],
            'verdict': verdict, 'possible_tiers': possible,
            'reason': 'تعكس المراجعة ما يثبت من نص المفردة المقابلة نفسها.',
            'facets': [{'target_quote': target['text'],
                        'source_quote': source['text'] if support in ('aligned', 'related', 'limited') else '',
                        'support': support}]}


def answer(value):
    return {'done_reason': 'stop', 'message': {'content': json.dumps(value, ensure_ascii=False)}}


def exhaustive_matching(edges):
    """Tiny independent oracle: enumerate assignments, without augmenting paths."""
    targets = sorted({t for t, _ in edges})
    choices = [[None] + sorted(s for t, s in edges if t == target) for target in targets]
    best = 0
    for assignment in itertools.product(*choices):
        selected = [s for s in assignment if s is not None]
        if len(selected) == len(set(selected)):
            best = max(best, len(selected))
    return best


class IndependentGroundingReviewTests(unittest.TestCase):
    def test_unknown_content_cannot_be_swallowed_as_definitive_rejection(self):
        targets = [topic('t', 'تعريف العقد وأركانه وآثاره')]
        sources = [topic('s', 'التوصيف التفصيلي غير متاح للمراجعة')]
        row = review(targets[0], sources[0], 'uncertain', 'unknown', [])
        # Invalid empty bounds may be rejected for repair. If accepted, absence
        # of evidence must still not become a definitive course rejection.
        try:
            validate_reviews([row], targets, sources, {('t', 's')})
        except ValueError:
            return
        pairs = adjudicate([proposal(targets[0], sources[0], 10)], [row], targets, sources)
        self.assertEqual(calculate(targets, sources, pairs)['decision'], 'insufficient')

    def test_initial_lexical_waw_is_part_of_a_word_not_a_disposable_connector(self):
        target = topic('t', 'وسائل البحث')
        source = topic('s', 'سائل البحث')
        row = review(target, source, 'strict', 'aligned')
        row['facets'][0]['target_quote'] = 'سائل البحث'
        with self.assertRaises(ValueError):
            validate_reviews([row], [target], [source], {('t', 's')})

    def test_literal_attached_conjunction_remains_valid_when_quoted(self):
        target = topic('t', 'تعريف العقد وآثاره')
        source = topic('s', 'مفهوم العقد والآثار المترتبة عليه')
        row = review(target, source, 'strict', 'aligned')
        row['facets'] = [
            {'target_quote': 'تعريف العقد', 'source_quote': 'مفهوم العقد', 'support': 'aligned'},
            {'target_quote': 'وآثاره', 'source_quote': 'والآثار المترتبة عليه', 'support': 'aligned'},
        ]
        validate_reviews([row], [target], [source], {('t', 's')})

    def test_whole_target_related_cannot_be_described_as_weak(self):
        target = topic('t', 'تفسير سورة البقرة الآيات6-16')
        source = topic('s', 'تفسير سورة البقرة الآيات6-12')
        row = review(target, source, 'weak', 'related')
        with self.assertRaises(ValueError):
            validate_reviews([row], [target], [source], {('t', 's')})

    def test_all_explicit_requirements_aligned_cannot_be_described_as_weak(self):
        target = topic('t', 'تعريف العقد وآثاره')
        source = topic('s', 'مفهوم العقد والآثار المترتبة عليه')
        row = review(target, source, 'weak', 'absent')
        row['facets'] = [
            {'target_quote': 'تعريف العقد', 'source_quote': 'مفهوم العقد', 'support': 'aligned'},
            {'target_quote': 'وآثاره', 'source_quote': 'والآثار المترتبة عليه', 'support': 'aligned'},
        ]
        # Every explicit requirement already asserts >=80 content agreement.
        # Reject the contradictory evidence; do not manufacture a higher score.
        with self.assertRaises(ValueError):
            validate_reviews([row], [target], [source], {('t', 's')})

    def test_limited_relation_has_a_distinct_valid_subsixty_state(self):
        target = topic('t', 'تفسير سورة البقرة الآيات6-16')
        source = topic('s', 'تفسير سورة البقرة الآيات6-10')
        row = review(target, source, 'weak', 'limited')
        validate_reviews([row], [target], [source], {('t', 's')})
        pairs = adjudicate([proposal(target, source, 45)], [row], [target], [source])
        self.assertEqual(calculate([target], [source], pairs)['decision'], 'not_equivalent')
        self.assertEqual(pairs[0]['score'], 45)

    def test_partial_numeric_scope_above_sixty_can_confirm_lenient(self):
        target = topic('t', 'تفسير سورة البقرة الآيات6-16')
        source = topic('s', 'تفسير سورة البقرة الآيات6-12')
        row = review(target, source, 'lenient', 'related')
        validate_reviews([row], [target], [source], {('t', 's')})
        pairs = adjudicate([proposal(target, source, 63)], [row], [target], [source])
        result = calculate([target], [source], pairs)
        self.assertEqual(result['decision'], 'lenient')
        self.assertEqual(result['strict_count'], 0)
        self.assertEqual(result['lenient_count'], 1)
        self.assertEqual(pairs[0]['score'], 63)

    def test_hard_numeric_exclusion_need_not_invent_positive_possibilities(self):
        target = topic('t', 'تفسير سورة البقرة الآيات6-16')
        source = topic('s', 'تفسير سورة آل عمران الآيات6-16')
        row = review(target, source, 'uncertain', 'unknown', [])
        validate_reviews([row], [target], [source], {('t', 's')})
        pairs = adjudicate([proposal(target, source, 10)], [row], [target], [source])
        self.assertEqual(calculate([target], [source], pairs)['decision'], 'not_equivalent')

    @staticmethod
    def split_source_evidence(include_last_requirement=False):
        target = topic('t', 'أحكام الاستعاذة وأحكام البسملة' + (' وأحكام الفاتحة' if include_last_requirement else ''))
        sources = [topic('s1', 'أحكام الاستعاذة'), topic('s2', 'أحكام البسملة')]
        raw, reviews = [], []
        for index, source in enumerate(sources):
            raw.append(proposal(target, source, 40))
            row = review(target, source, 'weak', 'absent')
            row['facets'] = [
                {'target_quote': 'أحكام الاستعاذة',
                 'source_quote': sources[0]['text'] if index == 0 else '',
                 'support': 'aligned' if index == 0 else 'absent'},
                {'target_quote': 'وأحكام البسملة',
                 'source_quote': sources[1]['text'] if index == 1 else '',
                 'support': 'aligned' if index == 1 else 'absent'},
            ]
            if include_last_requirement:
                row['facets'].append({'target_quote': 'وأحكام الفاتحة', 'source_quote': '', 'support': 'absent'})
            reviews.append(row)
        return [target], sources, raw, reviews

    def test_reverse_segmentation_is_held_without_combining_source_scores(self):
        targets, sources, raw, reviews = self.split_source_evidence()
        pairs = adjudicate(raw, reviews, targets, sources)
        result = calculate(targets, sources, pairs)
        self.assertEqual(result['decision'], 'insufficient')
        self.assertIn('segmentation_review', result)
        self.assertEqual([pair['score'] for pair in pairs], [40, 40])
        self.assertTrue(all(pair['adjudication']['state'] == 'confirmed' for pair in pairs))

    def test_reverse_segmentation_must_not_invent_a_missing_requirement(self):
        targets, sources, raw, reviews = self.split_source_evidence(include_last_requirement=True)
        result = calculate(targets, sources, adjudicate(raw, reviews, targets, sources))
        self.assertEqual(result['decision'], 'not_equivalent')
        self.assertNotIn('segmentation_review', result)

    def test_reverse_segmentation_below_decision_boundary_remains_irrelevant(self):
        targets, sources, raw, reviews = self.split_source_evidence()
        # Even resolving this one compound topic leaves four proven gaps.
        # Uncertainty must only block a decision when it could change it.
        for index in range(4):
            target = topic('missing' + str(index), 'موضوع غائب رقم ' + str(index))
            targets.append(target)
            raw.append(proposal(target, sources[0], 10))
            reviews.append(review(target, sources[0], 'weak', 'absent'))
        result = calculate(targets, sources, adjudicate(raw, reviews, targets, sources))
        self.assertEqual(result['decision'], 'not_equivalent')
        self.assertNotIn('segmentation_review', result)

    def test_distinct_confirmed_and_possible_bounds_match_exhaustive_assignments(self):
        # Seeded mechanism graphs exercise augmenting paths, ambiguous edges,
        # definitive rejections and segmentation without treating counts as
        # semantic facet weights. All targets have at least one reviewed pair.
        rng = random.Random(20261103)
        targets = [topic('t' + str(i), 'الموضوع المستقل رقم ' + str(i)) for i in range(3)]
        sources = [topic('s' + str(i), 'المصدر الموصوف رقم ' + str(i)) for i in range(3)]
        states = [(85, 'strict', 'aligned'), (65, 'lenient', 'related'),
                  (20, 'weak', 'absent'), (20, 'uncertain', 'unknown')]
        for case in range(100):
            raw, reviews = [], []
            strict, lenient, possible_strict, possible_lenient = set(), set(), set(), set()
            for target in targets:
                selected = rng.sample(sources, rng.randrange(1, 4))
                for source in selected:
                    score, verdict, support = rng.choice(states)
                    edge = (target['id'], source['id'])
                    raw.append(proposal(target, source, score))
                    reviews.append(review(target, source, verdict, support))
                    if verdict == 'strict':
                        strict.add(edge)
                        possible_strict.add(edge)
                    if verdict in ('strict', 'lenient'):
                        lenient.add(edge)
                        possible_lenient.add(edge)
                    if verdict == 'uncertain':
                        possible_strict.add(edge)
                        possible_lenient.add(edge)
            counts = [exhaustive_matching(edges) for edges in
                      (strict, lenient, possible_strict, possible_lenient)]
            cs, cl, ps, pl = counts
            individual = len({t for t, _ in possible_lenient})
            if pl * 100 < 240 <= individual * 100:
                expected = 'insufficient'
            elif cs * 100 >= 240:
                expected = 'equivalent'
            elif ps * 100 < 240 <= cl * 100:
                expected = 'lenient'
            elif pl * 100 < 240:
                expected = 'not_equivalent'
            else:
                expected = 'insufficient'
            with self.subTest(graph=case, bounds=counts):
                result = calculate(targets, sources, adjudicate(raw, reviews, targets, sources))
                self.assertEqual(result['decision'], expected)
                if expected != 'insufficient':
                    self.assertEqual((result['strict_count'], result['lenient_count']), (cs, cl))

    def test_reviewer_alternative_is_required_and_rechecked_after_bounded_repair(self):
        target = topic('t', 'تفسير البسملة')
        wrong = topic('s1', 'أحكام مد البدل')
        better = topic('s2', 'شرح معاني البسملة وتفسيرها')
        weak = proposal(target, wrong, 10)
        positive = proposal(target, better, 90)
        rows = [review(target, wrong, 'weak', 'absent'), review(target, better, 'strict', 'aligned')]
        # First repair omits the reviewer alternative. It must be rejected,
        # rather than silently dropping the source that can change the result.
        responses = [answer({'pairs': [weak]}), answer({'reviews': rows}),
                     answer({'pairs': [weak]}), answer({'pairs': [weak, positive]}),
                     answer({'reviews': rows})]
        with tempfile.TemporaryDirectory() as folder, \
                patch('scripts.equivalency.engine.local_model', return_value='mock-digest'), \
                patch('scripts.equivalency.engine.post_ollama', side_effect=responses) as call:
            result = compare({'topics': [target]}, {'topics': [wrong, better]}, cache_dir=folder)
            self.assertEqual(calculate([target], [wrong, better], result['pairs'])['decision'], 'equivalent')
            pairs = {(p['target_id'], p['source_id']): p for p in result['pairs']}
            self.assertEqual(pairs[('t', 's2')]['score'], 90)
            self.assertEqual(pairs[('t', 's2')]['adjudication']['state'], 'confirmed')
            retry_instruction = call.call_args_list[3].args[1]['messages'][-1]['content']
            self.assertIn('بدائل مراجعة', retry_instruction)
            final_review = json.loads(call.call_args_list[-1].args[1]['messages'][1]['content'])
            self.assertEqual({(p['target_id'], p['source_id']) for p in final_review['requested_pairs']},
                             {('t', 's1'), ('t', 's2')})
            self.assertNotIn('score', json.dumps(final_review))

    def test_repair_cannot_forget_an_earlier_confirmed_positive_pair(self):
        targets = [topic('t1', 'تفسير البسملة'), topic('t2', 'تفسير الاستعاذة')]
        sources = [topic('s1', 'شرح معاني البسملة وتفسيرها'),
                   topic('s2', 'دراسة الاستعاذة مختصرة'), topic('s3', 'أحكام مد البدل')]
        initial = [proposal(targets[0], sources[0], 85), proposal(targets[1], sources[1], 85)]
        initial_reviews = [review(targets[0], sources[0], 'strict', 'aligned'),
                           review(targets[1], sources[1], 'lenient', 'related')]
        # Only t2 initially disagrees. An unrelated weak replacement for t1
        # must not erase its previous confirmed positive edge from the graph.
        dropped_positive = [proposal(targets[0], sources[2], 10), proposal(targets[1], sources[1], 65)]
        repaired = [proposal(targets[0], sources[0], 80), proposal(targets[1], sources[1], 65)]
        responses = [answer({'pairs': initial}), answer({'reviews': initial_reviews}),
                     answer({'pairs': dropped_positive}), answer({'pairs': repaired}),
                     # The targeted repair retains the unrelated weak edge
                     # instead of silently deleting an already valid proposal.
                     answer({'reviews': initial_reviews+[review(targets[0],sources[2],'weak','absent')]})]
        with tempfile.TemporaryDirectory() as folder, \
                patch('scripts.equivalency.engine.local_model', return_value='mock-digest'), \
                patch('scripts.equivalency.engine.post_ollama', side_effect=responses) as call:
            result = compare({'topics': targets}, {'topics': sources}, cache_dir=folder)
            final_pairs = {(p['target_id'], p['source_id']): p for p in result['pairs']}
            self.assertIn(('t1', 's1'), final_pairs)
            # Re-evaluation is permitted. Keeping an ID does not pin 85.
            self.assertEqual(final_pairs[('t1', 's1')]['score'], 80)
            self.assertEqual(calculate(targets, sources, result['pairs'])['decision'], 'lenient')
            feedback = json.loads(call.call_args_list[2].args[1]['messages'][1]['content'])
            self.assertIn(('t1', 's1'), {(r['target_id'], r['source_id'])
                                        for r in feedback['facet_review_feedback']})
            self.assertEqual(result['validation_attempts'], 2)


if __name__ == '__main__':
    unittest.main()
