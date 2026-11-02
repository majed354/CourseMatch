"""Protect the preregistered benchmark, without importing or exercising models."""
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/compound-calibration-20261102.json'
FROZEN_DEVELOPMENT_SHA256 = '922b490f1c5d58bb7fb204c53e9900b493c4666863bb413fea11e1ff5f3e2494'
FROZEN_HELDOUT_SHA256 = '118e86889113c58ea60e98f04dd64e8fcab948d1f63dc74a0b1e4a513aeb6b92'


class CompoundCalibrationFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = FIXTURE.read_bytes()
        cls.data = json.loads(cls.raw)
        cls.cases = cls.data['development_cases']
        cls.by_id = {case['id']: case for case in cls.cases}

    def test_frozen_development_inputs_and_labels_are_unchanged(self):
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), FROZEN_DEVELOPMENT_SHA256)
        self.assertEqual(self.data['schema_version'], 1)
        self.assertEqual(self.data['policy']['course_coverage'], 80)
        self.assertEqual(self.data['policy']['strict_topic'], 80)
        self.assertEqual(self.data['policy']['lenient_topic'], 60)
        self.assertEqual(self.data['amendments_before_evaluation'][0]['case_id'], 'D03')
        self.assertTrue(self.data['amendments_before_evaluation'][0]['before_any_v6_model_outputs'])

    def test_heldout_inputs_and_labels_are_not_in_the_tracked_fixture(self):
        # Never open the private heldout file here, even when it exists locally.
        partitions = self.data['partitions']
        self.assertEqual(partitions['heldout_count'], 8)
        self.assertEqual(partitions['heldout_sha256'], FROZEN_HELDOUT_SHA256)
        self.assertNotIn('heldout_cases', self.data)
        commitments = partitions['heldout_case_commitments']
        self.assertEqual([c['id'] for c in commitments], [f'H{i:02d}' for i in range(1, 9)])
        for entry in commitments:
            self.assertEqual(set(entry), {'id', 'sha256'})
            self.assertRegex(entry['sha256'], r'^[a-f0-9]{64}$')
        self.assertEqual([c['id'] for c in self.cases], [f'D{i:02d}' for i in range(1, 21)])
        self.assertTrue(all(c['partition'] == 'development' for c in self.cases))

    def test_facet_spans_cover_every_character_of_each_full_target(self):
        for case in self.cases:
            targets = {row['id']: row for row in case['target_units']}
            self.assertEqual(set(case['target_facets']), set(targets), case['id'])
            for tid, facets in case['target_facets'].items():
                content = targets[tid]['text']
                position = 0
                for facet in facets:
                    start, end = facet['target_span']
                    self.assertEqual(start, position, (case['id'], tid))
                    self.assertGreater(end, start)
                    self.assertEqual(content[start:end], facet['target_quote'])
                    self.assertIsNone(facet['weight'], 'No inferred equal component weights')
                    self.assertIn(facet['expected_grounding'], {'supported', 'partial', 'absent', 'unknown'})
                    position = end
                self.assertEqual(position, len(content), (case['id'], tid))
                self.assertEqual(''.join(f['target_quote'] for f in facets), content)

    def test_support_quotes_are_literal_and_belong_to_the_same_source_unit(self):
        for case in self.cases:
            sources = {row['id']: row for row in case['source_units']}
            self.assertEqual(len(sources), len(case['source_units']))
            self.assertFalse(case['allow_source_union'])
            for facets in case['target_facets'].values():
                for facet in facets:
                    self.assertIn(facet['source_id'], sources)
                    quotes = facet['supporting_source_quotes']
                    if facet['expected_grounding'] in {'supported', 'partial'}:
                        self.assertTrue(quotes, (case['id'], facet['id']))
                    for quote in quotes:
                        self.assertTrue(quote)
                        self.assertIn(quote, sources[facet['source_id']]['text'])
            for row in case['fixed_pairs']:
                self.assertIn(row['source_id'], sources)
                self.assertIn(row['target_id'], {t['id'] for t in case['target_units']})

    def test_non_gold_review_constraints_cannot_enter_accuracy_denominators(self):
        gold = []
        hypotheses = []
        for case in self.cases:
            expected = case['expectation']
            if expected['label_authority'] == 'objective_control':
                gold.append(case)
            else:
                hypotheses.append(case)
                self.assertEqual(expected['label_authority'], 'reviewer_constraint_not_gold')
                self.assertIsNone(expected['expected_tier'])
                self.assertFalse(expected['tier_scored_for_accuracy'])
                self.assertTrue(expected['unweighted_facets_must_not_be_converted_to_percentage'])
        self.assertEqual(len(gold), 13)
        self.assertEqual(len(hypotheses), 7)
        self.assertEqual(sum(c['expectation']['expected_tier'] == 'strict' for c in gold), 7)
        self.assertEqual(sum(c['expectation']['expected_tier'] == 'weak' for c in gold), 4)

    def test_numeric_gold_bands_follow_explicit_scope_not_component_counts(self):
        for case in self.cases:
            if 'objective_scope' not in case:
                continue
            scope = case['objective_scope']
            self.assertEqual(scope['unit'], 'explicit_verse_count')
            self.assertGreater(scope['target_units'], 0)
            ratio = scope['covered_units'] / scope['target_units'] if scope['same_chapter'] else 0
            self.assertAlmostEqual(scope['coverage_ratio'], ratio)
            expected = 'strict' if ratio >= .8 else 'lenient' if ratio >= .6 else 'weak'
            self.assertEqual(case['expectation']['expected_tier'], expected, case['id'])

    def test_segmentation_and_alternate_source_controls_preserve_their_distinctions(self):
        segmentation = self.by_id['D19']
        self.assertEqual(len(segmentation['target_units']), 2)
        self.assertEqual(len(segmentation['source_units']), 1)
        self.assertEqual(segmentation['matching_constraints']['maximum_distinct_source_matching'], 1)
        self.assertTrue(segmentation['matching_constraints']['segmentation_review_required'])
        self.assertIsNone(segmentation['expectation']['expected_tier'])
        alternative = self.by_id['D20']
        self.assertEqual(alternative['fixed_pairs'][0]['source_id'], 's1')
        self.assertEqual(alternative['alternative_pair_required']['source_id'], 's2')
        self.assertEqual(alternative['target_facets']['t1'][0]['source_id'], 's2')
        # General heading is not ground truth for the definition listed in f1.
        first = next(iter(self.by_id['D03']['target_facets'].values()))[0]
        self.assertEqual(first['expected_grounding'], 'unknown')
        self.assertFalse(first['supporting_source_quotes'])

    def test_public_fixture_has_no_student_identity_or_session_data(self):
        forbidden = {'beneficiary', 'student_name', 'student_number', 'session_id',
                     'cookie', 'token', 'lease_token', 'worker_token'}
        def visit(value):
            if isinstance(value, dict):
                self.assertFalse(set(value) & forbidden)
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(self.data)
        # Authentic fragments must retain page/fingerprint provenance in CI,
        # while private source PDFs themselves are deliberately not required.
        origins = [unit['provenance'] for case in self.cases
                   for side in ('target_units', 'source_units') for unit in case[side]
                   if unit['provenance']['kind'] == 'authentic_pdf_topic_fragment']
        self.assertTrue(origins)
        for origin in origins:
            self.assertRegex(origin['source_sha256'], r'^[a-f0-9]{64}$')
            self.assertIsInstance(origin['page'], int)
            self.assertGreater(origin['page'], 0)
            self.assertTrue(origin['source_pdf'])
            self.assertNotIn('/Users/', origin['source_pdf'])


if __name__ == '__main__':
    unittest.main()
