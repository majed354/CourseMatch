"""Independent CPU-only v7 evidence review; never reads a sealed benchmark.

Mock calls test protocol/validation only. They do not supply academic gold
scores, call Ollama, or turn literal facet counts into content percentages.
"""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from scripts.equivalency.atomic_scope import analytical_units, atomic_pairs
from scripts.equivalency.engine import calculate, compare, _propose_pairs
from scripts.equivalency.grounding import decode_reviews, validate_evidence, validate_reviews
from scripts.equivalency.ranges import invalid_scope_topics


def topic(key, text):
    return {'id': key, 'text': text}


def verse(key, first, last, chapter='البقرة'):
    return topic(key, f'تفسير سورة {chapter} الآيات{first}-{last} تفسيرا تحليليا.')


def answer(value):
    return {'done_reason': 'stop', 'message': {'content': json.dumps(value, ensure_ascii=False)}}


def pair(target, source, score):
    return {'target_id': target['id'], 'source_id': source['id'], 'score': score,
            'reason': 'التقدير يخص موضوع المفردة ونطاقها الصريح.',
            'target_quote': target['text'], 'source_quote': source['text']}


def row(target, source, verdict, support):
    return {'target_id': target['id'], 'source_id': source['id'],
            'verdict': verdict,
            'possible_tiers': {'strict': ['strict', 'lenient'], 'lenient': ['lenient'],
                               'weak': [], 'uncertain': ['strict', 'lenient']}[verdict],
            'reason': 'الدليل يخص مفردة المصدر المقابلة نفسها.',
            'facets': [{'target_start': 1, 'target_end': len(target['text'].split()),
                        'source_quote': source['text'] if support in ('aligned', 'related', 'limited') else '',
                        'support': support}]}


class IndependentV7AtomicEvidenceTests(unittest.TestCase):
    def test_impossible_scope_stops_even_exact_identity_before_any_model_call(self):
        for chapter, last in [('الفاتحة', 8), ('الناس', 1000)]:
            targets = [verse('t', 1, last, chapter)]
            sources = [verse('s', 1, last, chapter)]
            with self.subTest(chapter=chapter), patch('scripts.equivalency.engine.local_model', side_effect=AssertionError('No model')):
                evidence = compare({'topics': targets}, {'topics': sources})
                self.assertEqual(evidence['provider'], 'unresolved_scope')
                self.assertEqual(evidence['pairs'], [])
                result = calculate(targets, sources, evidence['pairs'])
                self.assertEqual(result['decision'], 'insufficient')
                self.assertIsNone(result['strict_coverage'])
                self.assertTrue(result['invalid_verse_scopes']['target'])

    def test_impossible_source_alone_cannot_be_accepted_or_called_absent(self):
        targets = [verse('t', 1, 7, 'الفاتحة')]
        sources = [topic('s', 'دراسة تفسيرية لسورة الفاتحة الآيات1-8')]
        with patch('scripts.equivalency.engine.local_model', side_effect=AssertionError('No model')):
            evidence = compare({'topics': targets}, {'topics': sources})
        result = calculate(targets, sources, evidence['pairs'])
        self.assertEqual(result['decision'], 'insufficient')
        self.assertEqual(result['invalid_verse_scopes']['target'], [])
        self.assertTrue(result['invalid_verse_scopes']['source'])

    def test_chapter_limits_preserve_pages_and_unambiguous_carry_without_guessing_compound_chapters(self):
        topics = [topic('a', 'التعريف بسورة الفاتحة'), topic('b', 'تفسير الآيات1-8')]
        self.assertEqual([p['topic_id'] for p in invalid_scope_topics(topics)], ['b'])
        self.assertEqual(invalid_scope_topics([topic('a', 'تفسير ص:99 الفاتحة (1-7) تفسيرا تحليليا.')]), [])
        self.assertEqual(invalid_scope_topics([topic('a', 'تفسير سورتي الفاتحة والبقرة الآيات1-100')]), [])

    def test_surah_specific_endpoints_cannot_receive_deterministic_proof(self):
        # Objective impossible endpoints, not estimates of academic similarity.
        for chapter, last in [('الفاتحة', 8), ('الإخلاص', 5), ('الناس', 7), ('البقرة', 287)]:
            with self.subTest(chapter=chapter):
                self.assertIsNone(analytical_units([verse('t', 1, last, chapter)]))

    def test_complete_union_of_separate_atomic_units_requires_segmentation_review(self):
        targets = [verse('t', 1, 10)]
        sources = [verse('s1', 1, 5), verse('s2', 6, 10)]
        with patch('scripts.equivalency.engine.local_model', side_effect=AssertionError('No inference')):
            evidence = compare({'topics': targets}, {'topics': sources})
        result = calculate(targets, sources, evidence['pairs'])
        # A merged unit would change the decision; do not combine source grades
        # into acceptance and do not issue a final rejection before segmentation.
        self.assertEqual(result['decision'], 'insufficient')
        self.assertIn('segmentation_review', result)
        self.assertTrue(all(p['score'] < 60 for p in evidence['pairs']))

    def test_split_scope_cannot_hold_an_irrelevant_course_rejection(self):
        targets = [verse('t0', 1, 10)] + [verse('t' + str(i), 30 + 10 * i, 39 + 10 * i) for i in range(1, 5)]
        sources = [verse('s1', 1, 5), verse('s2', 6, 10)]
        with patch('scripts.equivalency.engine.local_model', side_effect=AssertionError('No inference')):
            evidence = compare({'topics': targets}, {'topics': sources})
        result = calculate(targets, sources, evidence['pairs'])
        self.assertEqual(result['decision'], 'not_equivalent')
        self.assertNotIn('segmentation_review', result)

    def test_split_scope_union_uses_sixty_boundary_and_never_adds_overlap_twice(self):
        targets = [verse('t', 1, 100)]
        for parts, expected in [([(1, 30), (31, 60)], 'insufficient'),
                                ([(1, 30), (31, 59)], 'not_equivalent'),
                                ([(1, 40), (20, 50)], 'not_equivalent')]:
            sources = [verse('s' + str(i), *bounds) for i, bounds in enumerate(parts)]
            with self.subTest(parts=parts), patch('scripts.equivalency.engine.local_model', side_effect=AssertionError('No inference')):
                evidence = compare({'topics': targets}, {'topics': sources})
                result = calculate(targets, sources, evidence['pairs'])
                self.assertEqual(result['decision'], expected)
                self.assertFalse(any(p['score'] >= 60 for p in evidence['pairs']))

    def test_one_wide_source_cannot_count_for_two_atomic_target_units(self):
        targets = [verse('t1', 1, 5), verse('t2', 6, 10)]
        sources = [verse('s', 1, 10)]
        with patch('scripts.equivalency.engine.local_model', side_effect=AssertionError('No inference')):
            evidence = compare({'topics': targets}, {'topics': sources})
        result = calculate(targets, sources, evidence['pairs'])
        self.assertEqual(result['decision'], 'insufficient')
        self.assertEqual(result['segmentation_review']['distinct_source_matching'], 1)

    def test_independent_small_integer_overlap_oracle(self):
        # Test inclusive endpoints, containment, disjointness and floor semantics
        # without reproducing the parser or touching any held-out case.
        for first, last in [(20, 24), (20, 29), (20, 30), (20, 119)]:
            target = verse('t', first, last)
            target_set = set(range(first, last + 1))
            for source_first, source_last in [(1, 10), (19, 22), (20, 24), (23, 26), (20, 119), (100, 125)]:
                with self.subTest(target=(first, last), source=(source_first, source_last)):
                    source = verse('s', source_first, source_last)
                    proof = atomic_pairs([target], [source])[0]
                    overlap = len(target_set & set(range(source_first, source_last + 1)))
                    self.assertEqual(proof['score'], overlap * 100 // len(target_set))
                    self.assertEqual(proof['scope_proof']['intersection_verses'], overlap)
                    self.assertEqual(proof['scope_proof']['target_verses'], len(target_set))

    def test_any_extra_material_or_explicit_negation_prevents_numeric_shortcut(self):
        target = verse('t', 1, 10)
        for text in [
            'تفسير سورة البقرة الآيات1-10 تفسيرا تحليليا دون دراسة معانيها.',
            'تفسير سورة البقرة الآيات1-10 تفسيرا تحليليا وأحكام تلاوتها.',
            'تفسير سورة البقرة الآيات1-10 تفسيرا تحليليا مع مقارنة قانونية.',
            'حفظ سورة البقرة الآيات1-10 فقط دون التفسير.',
            'تفسير سورة البقرة الآيات1-10 تفسيرا موضوعيا.',
        ]:
            with self.subTest(text=text):
                self.assertIsNone(atomic_pairs([target], [topic('s', text)]))

    def test_effective_corrected_source_controls_method_proof(self):
        source = verse('s', 1, 10)
        source['corrected_text'] = 'حفظ سورة البقرة الآيات1-10 فقط دون التفسير.'
        self.assertIsNone(atomic_pairs([verse('t', 1, 10)], [source]))

    def test_each_atomic_proof_field_is_recomputed_from_current_effective_text(self):
        targets, sources = [verse('t', 20, 29)], [verse('s', 20, 27)]
        with patch('scripts.equivalency.engine.local_model', side_effect=AssertionError('No inference')):
            evidence = compare({'topics': targets}, {'topics': sources})
        mutations = [
            lambda p: p['scope_proof'].__setitem__('intersection_verses', 10),
            lambda p: p['scope_proof'].__setitem__('target_verses', 9),
            lambda p: p['scope_proof']['source'].__setitem__('end', 29),
            lambda p: p['scope_proof']['target'].__setitem__('chapter', 3),
            lambda p: p['scope_proof'].__setitem__('version', 'invented'),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(evidence)
            mutate(changed['pairs'][0])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validate_evidence(changed, targets, sources)
        changed_sources = copy.deepcopy(sources)
        changed_sources[0]['corrected_text'] = 'حفظ سورة البقرة الآيات20-27 فقط.'
        with self.assertRaises(ValueError):
            validate_evidence(evidence, targets, changed_sources)

    def test_mixed_curriculum_resolved_pair_does_not_exclude_other_pairs_for_same_target(self):
        intro_t = topic('ti', 'نشأة علم التفسير')
        intro_s = topic('si', 'تاريخ نشأة التفسير وتطوره')
        atomic_t, atomic_s = verse('ta', 20, 29), verse('sa', 20, 27)
        targets, sources = [intro_t, atomic_t], [intro_s, atomic_s]
        semantic = [pair(intro_t, intro_s, 85), pair(atomic_t, intro_s, 5)]
        reviews = [row(intro_t, intro_s, 'strict', 'aligned'), row(atomic_t, intro_s, 'weak', 'absent')]
        with tempfile.TemporaryDirectory() as cache, patch('scripts.equivalency.engine.local_model', return_value='local'), patch(
            'scripts.equivalency.engine.post_ollama', side_effect=[answer({'pairs': semantic}), answer({'reviews': reviews})]
        ) as calls:
            evidence = compare({'topics': targets}, {'topics': sources}, cache_dir=cache)
            payload = json.loads(calls.call_args_list[1].args[1]['messages'][1]['content'])
        self.assertEqual(payload['resolved_pairs'], [{'target_id': 'ta', 'source_id': 'sa'}])
        self.assertEqual({(p['target_id'], p['source_id']) for p in payload['requested_pairs']}, {('ta', 'si'), ('ti', 'si')})
        fixed = next(p for p in evidence['pairs'] if p['target_id'] == 'ta' and p['source_id'] == 'sa')
        self.assertEqual(fixed['score'], 80)
        self.assertEqual(fixed['adjudication']['kind'], 'deterministic_atomic_scope')
        self.assertNotIn('review', fixed['adjudication'])
        validate_evidence(evidence, targets, sources)

    def test_conflicting_duplicate_semantic_proposals_require_validation_repair(self):
        target = topic('t', 'تعريف العقد وأركانه')
        source = topic('s', 'مفهوم العقد وأركانه')
        duplicate = [pair(target, source, 10), pair(target, source, 85)]
        repaired = [pair(target, source, 85)]
        with tempfile.TemporaryDirectory() as cache, patch('scripts.equivalency.engine.local_model', return_value='local'), patch(
            'scripts.equivalency.engine.post_ollama', side_effect=[answer({'pairs': duplicate}), answer({'pairs': repaired})]
        ) as calls:
            evidence = _propose_pairs({'topics': [target]}, {'topics': [source]}, cache_dir=cache)
        # A dictionary last-write must not silently resolve conflicting scores.
        # The second model response resolves its own invalid duplication.
        self.assertEqual(evidence['validation_attempts'], 2)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(evidence['pairs'], repaired)

    def test_known_atomic_unit_cannot_swallow_uncertainty_of_another_unit(self):
        target = topic('tu', 'تعريف العقد وأركانه وآثاره')
        source = topic('su', 'التوصيف التفصيلي غير متاح')
        targets, sources = [verse('ta', 20, 29), target], [verse('sa', 20, 29), source]
        proposed = pair(target, source, 0)
        uncertain = row(target, source, 'uncertain', 'unknown')
        responses = [answer({'pairs': [proposed]}), answer({'reviews': [uncertain]})] * 2
        with tempfile.TemporaryDirectory() as cache, patch('scripts.equivalency.engine.local_model', return_value='local'), patch(
            'scripts.equivalency.engine.post_ollama', side_effect=responses
        ):
            evidence = compare({'topics': targets}, {'topics': sources}, cache_dir=cache)
        result = calculate(targets, sources, evidence['pairs'])
        self.assertEqual(result['decision'], 'insufficient')
        self.assertEqual(result['adjudication_review']['confirmed_strict_count'], 1)
        self.assertEqual(result['adjudication_review']['possible_strict_count'], 2)
        self.assertEqual([p['score'] for p in evidence['pairs'] if p['target_id'] == 'tu'], [0])


class IndependentV7WordEvidenceTests(unittest.TestCase):
    def test_repeated_phrase_does_not_cover_unselected_token_positions(self):
        target, source = topic('t', 'أحكام البيع أحكام البيع'), topic('s', 'أحكام البيع')
        review = row(target, source, 'strict', 'aligned')
        review['facets'][0]['target_end'] = 2
        decoded = decode_reviews([review], [target])
        # Only positions1/2 were selected. A string find-all must not invent a
        # second quoted span3/4. This is an audit invariant, not a claim that
        # repeated academic content should receive any different score.
        with self.assertRaises(ValueError):
            validate_reviews(decoded, [target], [source], {('t', 's')})

    def test_literal_source_evidence_cannot_be_borrowed_from_another_source_unit(self):
        target = topic('t', 'تعريف العقد وأركانه')
        sources = [topic('s1', 'تعريف العقد'), topic('s2', 'أركان العقد')]
        review = row(target, sources[0], 'strict', 'aligned')
        review['facets'][0]['source_quote'] = sources[1]['text']
        with self.assertRaises(ValueError):
            validate_reviews(decode_reviews([review], [target]), [target], sources, {('t', 's1')})

    def test_original_whitespace_and_punctuation_are_reconstructed_from_positions(self):
        target = topic('t', 'تعريفُ العقد،\nوأركانه\tوآثاره.')
        source = topic('s', 'مفهوم العقد وأركانه وآثاره')
        review = row(target, source, 'strict', 'aligned')
        decoded = decode_reviews([review], [target])
        self.assertEqual(decoded[0]['facets'][0]['target_quote'], target['text'])
        validate_reviews(decoded, [target], [source], {('t', 's')})


if __name__ == '__main__':
    unittest.main()
