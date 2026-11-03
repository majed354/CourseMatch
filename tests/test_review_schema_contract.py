"""Standard JSON Schema contracts, without Ollama or post-hoc coercion.

This is a structural grammar test, not proof of semantic alignment. Multi-facet
uncertainty remains expressible; existing semantic validators still determine
whether a particular decomposition, literal evidence and verdict are coherent.
The jsonschema dependency is required: missing installs must not silently skip.
"""
import copy
import itertools
import unittest

from jsonschema import Draft202012Validator, ValidationError

from scripts.equivalency.review_schema import model_review_schema


SUPPORTS = ['aligned', 'related', 'limited', 'absent', 'unknown']
ORDER = ['target_id', 'source_id', 'facets', 'reason', 'verdict', 'possible_tiers']
BASE = {'type': 'object', 'properties': {'reviews': {'type': 'array', 'items': {
    'type': 'object', 'properties': {
        'target_id': {'type': 'string'}, 'source_id': {'type': 'string'},
        'verdict': {'type': 'string', 'enum': ['strict', 'lenient', 'weak', 'uncertain']},
        'possible_tiers': {'type': 'array', 'items': {'type': 'string', 'enum': ['strict', 'lenient']}},
        'reason': {'type': 'string'},
        'facets': {'type': 'array', 'minItems': 1, 'items': {
            'type': 'object', 'properties': {
                'target_start': {'type': 'integer', 'minimum': 1},
                'target_end': {'type': 'integer', 'minimum': 1},
                'source_quote': {'type': 'string'},
                'support': {'type': 'string', 'enum': SUPPORTS}},
            'required': ['target_start', 'target_end', 'source_quote', 'support'], 'additionalProperties': False}}},
    'required': ['target_id', 'source_id', 'verdict', 'possible_tiers', 'reason', 'facets'],
    'additionalProperties': False}}}, 'required': ['reviews'], 'additionalProperties': False}


def row(verdict, supports, possible=None):
    default = {'strict': ['strict', 'lenient'], 'lenient': ['lenient'], 'weak': [], 'uncertain': ['strict', 'lenient']}
    return {'target_id': 't1', 'source_id': 's1',
            'facets': [{'target_start': i + 1, 'target_end': i + 1,
                        'source_quote': '' if support in ('absent', 'unknown') else 'دليل حرفي', 'support': support}
                       for i, support in enumerate(supports)],
            'reason': 'دليل المادة والنطاق كما وردا في النص.', 'verdict': verdict,
            'possible_tiers': default[verdict] if possible is None else possible}


class ReviewSchemaContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = model_review_schema(BASE)
        Draft202012Validator.check_schema(self.schema)
        self.validator = Draft202012Validator(self.schema)

    def accepts(self, value):
        self.validator.validate({'reviews': [value]})

    def rejects(self, value):
        with self.assertRaises(ValidationError):
            self.accepts(value)

    def test_four_row_branches_put_facets_and_reason_before_verdict(self):
        branches = self.schema['properties']['reviews']['items']['anyOf']
        self.assertEqual(len(branches), 4)
        self.assertEqual([branch['properties']['verdict']['enum'] for branch in branches],
                         [['strict'], ['lenient'], ['weak'], ['uncertain']])
        for branch in branches:
            self.assertEqual(list(branch['properties']), ORDER)
            self.assertEqual(branch['required'], ORDER)
            self.assertFalse(branch['additionalProperties'])

    def test_coherent_strict_lenient_weak_and_uncertain_are_expressible(self):
        for value in [row('strict', ['aligned']), row('strict', ['aligned', 'aligned']),
                      row('lenient', ['related']), row('lenient', ['aligned', 'related']),
                      row('weak', ['limited']), row('weak', ['absent']), row('weak', ['limited', 'absent']),
                      row('uncertain', ['unknown']), row('uncertain', ['aligned', 'unknown']),
                      row('uncertain', ['limited', 'related']), row('uncertain', ['aligned', 'aligned'])]:
            with self.subTest(value=value):
                self.accepts(value)

    def test_all_five_real_lenient_unknown_patterns_fail_before_semantic_validation(self):
        # Same contradiction observed with several content/identity combinations.
        for index, supports in enumerate([['related', 'unknown'], ['aligned', 'unknown'],
                                          ['unknown', 'related'], ['related', 'related', 'unknown'],
                                          ['unknown', 'aligned', 'related']], 1):
            value = row('lenient', supports); value['target_id'] = 'real-pattern-' + str(index)
            with self.subTest(pattern=index):
                self.rejects(value)
                uncertain = copy.deepcopy(value); uncertain['verdict'] = 'uncertain'
                # Grammar permits an honest uncertainty result; the builder
                # does not transform the original invalid response into it.
                self.accepts(uncertain)
                self.assertEqual(value['verdict'], 'lenient')

    def test_every_inconsistent_singleton_is_rejected(self):
        allowed = {('strict', 'aligned'), ('lenient', 'related'), ('weak', 'limited'),
                   ('weak', 'absent'), ('uncertain', 'unknown')}
        for verdict, support in itertools.product(['strict', 'lenient', 'weak', 'uncertain'], SUPPORTS):
            with self.subTest(verdict=verdict, support=support):
                value = row(verdict, [support])
                if (verdict, support) in allowed:
                    self.accepts(value)
                else:
                    self.rejects(value)

    def test_decisive_verdicts_never_admit_unknown_and_strict_only_allows_aligned(self):
        for verdict in ('strict', 'lenient', 'weak'):
            self.rejects(row(verdict, ['aligned', 'unknown']))
        for support in ('related', 'limited', 'absent', 'unknown'):
            self.rejects(row('strict', ['aligned', support]))

    def test_fixed_possible_tiers_and_uncertainty_enumeration_reject_duplicates(self):
        for verdict, supports, choices in [
                ('strict', ['aligned'], [['strict', 'lenient']]),
                ('lenient', ['related'], [['lenient']]), ('weak', ['limited'], [[]]),
                ('uncertain', ['unknown'], [['lenient'], ['strict', 'lenient'], ['lenient', 'strict']])]:
            for possible in choices:
                self.accepts(row(verdict, supports, possible))
            for possible in [['strict'], ['strict', 'strict'], ['lenient', 'lenient'], ['weak'], ['strict', 'lenient', 'strict']]:
                with self.subTest(verdict=verdict, possible=possible):
                    self.rejects(row(verdict, supports, possible))
        self.rejects(row('strict', ['aligned'], []))
        self.rejects(row('lenient', ['related'], []))
        self.rejects(row('weak', ['limited'], ['lenient']))
        self.rejects(row('uncertain', ['unknown'], []))

    def test_empty_uncertain_needs_an_explicit_boolean_exception(self):
        schema = model_review_schema(BASE, allow_empty_uncertain=True)
        validator = Draft202012Validator(schema)
        value = row('uncertain', ['unknown'], [])
        validator.validate({'reviews': [value]})
        self.rejects(value)
        self.assertEqual(model_review_schema(BASE), self.schema)
        for flag in ('true', 1, None, []):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                model_review_schema(BASE, allow_empty_uncertain=flag)
        with self.assertRaises(ValidationError):
            validator.validate({'reviews': [row('lenient', ['unknown'], [])]})

    def test_original_word_span_types_required_keys_and_extra_field_rejection_survive(self):
        original = row('strict', ['aligned'])
        for mutate in ('start_zero', 'start_string', 'end_missing', 'extra_quote', 'extra_grade', 'empty_facets'):
            value = copy.deepcopy(original)
            if mutate == 'start_zero': value['facets'][0]['target_start'] = 0
            if mutate == 'start_string': value['facets'][0]['target_start'] = '1'
            if mutate == 'end_missing': value['facets'][0].pop('target_end')
            if mutate == 'extra_quote': value['facets'][0]['target_quote'] = 'نص'
            if mutate == 'extra_grade': value['score'] = 80
            if mutate == 'empty_facets': value['facets'] = []
            with self.subTest(mutate=mutate): self.rejects(value)

    def test_builder_is_pure_and_branches_do_not_share_mutable_facet_rules(self):
        before = copy.deepcopy(BASE); schema = model_review_schema(BASE)
        self.assertEqual(BASE, before)
        branches = schema['properties']['reviews']['items']['anyOf']
        branches[0]['properties']['facets']['items']['properties']['support']['enum'].append('unknown')
        self.assertEqual(BASE, before)
        self.assertEqual(branches[1]['properties']['facets']['anyOf'][0]['items']['properties']['support']['enum'], ['related'])
        self.assertNotEqual(model_review_schema(BASE), schema)

    def test_only_portable_condition_operators_are_introduced(self):
        forbidden = {'contains', 'if', 'then', 'else', 'const', 'oneOf', 'allOf', 'not', 'uniqueItems', '$ref'}
        def walk(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden & set(value))
                for child in value.values(): walk(child)
            elif isinstance(value, list):
                for child in value: walk(child)
        walk(self.schema)

    def test_literal_quote_schema_or_malformed_base_is_not_silently_reinterpreted(self):
        for malformed in [None, {}, {'properties': {}}, {'properties': {'reviews': {'items': {}}}}]:
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                model_review_schema(malformed)
        literal = copy.deepcopy(BASE)
        props = literal['properties']['reviews']['items']['properties']['facets']['items']['properties']
        props['target_quote'] = {'type': 'string'}
        with self.assertRaises(ValueError): model_review_schema(literal)


if __name__ == '__main__':
    unittest.main()
