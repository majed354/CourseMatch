"""CPU-only score-blinded review reuse and strict v4 cache promotion.

All replies are fixtures. Reuse is evidence preservation for unchanged inputs,
not approval of semantic conclusions or a replacement for strict validators.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from jsonschema import Draft202012Validator, ValidationError

from scripts.equivalency import grounding as g


def ids(rows):
    return {(row['target_id'], row['source_id']) for row in rows}


def proposal(target, source, score=87):
    return {'target_id': target['id'], 'source_id': source['id'], 'score': score,
            'reason': 'PRIVATE_GENERATOR_REASON_NEVER_REVIEW',
            'target_quote': target['text'], 'source_quote': source['text']}


def review(target, source, verdict='strict', reason='الصياغة الأصلية لدليل المراجعة.'):
    return {'target_id': target['id'], 'source_id': source['id'],
            'facets': [{'target_start': 1, 'target_end': len(target['text'].split()),
                        'source_quote': source['text'] if verdict == 'strict' else '',
                        'support': 'aligned' if verdict == 'strict' else 'absent'}],
            'reason': reason, 'verdict': verdict,
            'possible_tiers': ['strict', 'lenient'] if verdict == 'strict' else []}


def answer(rows):
    return {'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps({'reviews': rows}, ensure_ascii=False)}}


class BlindedReviewReuseTests(unittest.TestCase):
    def setUp(self):
        self.targets = [{'id': 'ta', 'text': 'تعريف العقد وأركانه'}, {'id': 'tb', 'text': 'شروط الوكالة وآثارها'}]
        self.sources = [{'id': 'sa', 'text': 'شرح تعريف العقد وأركانه'},
                        {'id': 'sb', 'text': 'بيان شروط الوكالة وآثارها'},
                        {'id': 'sx', 'text': 'تفصيل تعريف العقد وأركانه وتطبيقاته'}]
        self.prior_pairs = [proposal(self.targets[0], self.sources[0], 11)]
        self.current_pairs = [proposal(self.targets[0], self.sources[0], 99), proposal(self.targets[1], self.sources[1], 67)]

    def run_review(self, folder, batches, *, targets=None, sources=None, pairs=None,
                   previous=None, digest='fixture-model-digest', excluded=()):
        calls = []
        def caller(endpoint, payload):
            self.assertEqual(endpoint, 'chat'); calls.append(copy.deepcopy(payload))
            if len(calls) > len(batches): self.fail('Unexpected extra model invocation')
            return answer(copy.deepcopy(batches[len(calls) - 1]))
        self.calls = calls
        return g.review_pairs(targets or self.targets, sources or self.sources,
                              self.current_pairs if pairs is None else pairs,
                              model='fixture-local', model_digest=digest, cache_dir=folder,
                              caller=caller, previous_pairs=previous, excluded_pairs=excluded)

    def seed(self, folder):
        rows = [review(self.targets[0], self.sources[0]), review(self.targets[0], self.sources[2])]
        result = self.run_review(folder, [rows], pairs=self.prior_pairs)
        self.assertEqual(len(self.calls), 1)
        return result

    def assert_blinded(self, calls):
        text = json.dumps(calls, ensure_ascii=False)
        self.assertNotIn('PRIVATE_GENERATOR_REASON_NEVER_REVIEW', text)
        for call in calls:
            body = json.loads(call['messages'][1]['content'])
            self.assertNotIn('"score"', json.dumps(body))

    def payload(self, targets=None, sources=None, wanted=None):
        targets, sources = targets or self.targets, sources or self.sources
        wanted = ids(self.prior_pairs) if wanted is None else wanted
        value = {'target': [{'id': t['id'], 'text': t['text'],
                             'numbered_words': [str(i) + ': ' + w.group() for i, w in enumerate(g.word_spans(t['text']), 1)]}
                            for t in targets],
                 'source': [{'id': s['id'], 'text': s['text']} for s in sources],
                 'requested_pairs': [{'target_id': t, 'source_id': s} for t, s in sorted(wanted)]}
        facts = g.scope_facts(targets, sources, wanted)
        if any(f['target'] is not None or f['source'] is not None for f in facts): value['scope_facts'] = facts
        return value

    def write_legacy(self, folder, *, version=None, stored_version=None):
        version = version or g.LEGACY_VERSION
        payload = self.payload()
        key = hashlib.sha256(json.dumps([version, g.LEGACY_SYSTEM, g.LEGACY_MODEL_SCHEMA,
                                        g.SCHEMA, 'fixture-model-digest', payload],
                                       ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        rows = g.decode_reviews([review(self.targets[0], self.sources[0]),
                                 review(self.targets[0], self.sources[2])], self.targets)
        value = {'version': stored_version or version, 'cache_key': key, 'reviews': rows, 'validation_attempts': 1}
        path = Path(folder) / 'facet-reviews' / (key + '.json'); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path, value

    def write_prior_v5(self, folder, *, invalid_empty=False):
        key = hashlib.sha256(json.dumps([g.VERSION, g.PRIOR_SYSTEM, g.PRIOR_MODEL_SCHEMA,
                                        g.SCHEMA, 'fixture-model-digest', self.payload()],
                                       ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        rows = g.decode_reviews([review(self.targets[0], self.sources[0])], self.targets)
        if invalid_empty:
            rows[0].update(verdict='uncertain', possible_tiers=[])
            rows[0]['facets'][0].update(support='unknown', source_quote='')
        value = {'version':g.VERSION, 'cache_key':key, 'reviews':rows, 'validation_attempts':1}
        path = Path(folder) / 'facet-reviews' / (key + '.json'); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path, value

    def test_same_inputs_reuse_verbatim_reviews_and_only_request_the_new_pair(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = self.seed(folder)
            result = self.run_review(folder, [[review(self.targets[1], self.sources[1])]], previous=self.prior_pairs)
            self.assertEqual(len(self.calls), 1)
            body = json.loads(self.calls[0]['messages'][1]['content'])
            self.assertTrue(body['completion_only']); self.assertEqual(ids(body['requested_pairs']), {('tb', 'sb')})
            self.assertEqual(ids(body['retained_pairs']), {('ta', 'sa'), ('ta', 'sx')})
            for old in previous['reviews']:
                self.assertEqual(next(r for r in result['reviews'] if (r['target_id'], r['source_id']) == (old['target_id'], old['source_id'])), old)
            self.assertEqual(result['reused_review_count'], 2)
            self.assertEqual(result['reused_review_cache_keys'], [previous['cache_key']])
            self.assertIn(('ta', 'sx'), ids(result['reviews']))  # reviewer-discovered positive alternative
            g.validate_reviews(result['reviews'], self.targets, self.sources, ids(self.current_pairs))
            self.assert_blinded(self.calls)

    def test_duplicate_in_completion_cannot_reassess_any_retained_review_and_stays_raw(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = self.seed(folder)
            duplicate = review(self.targets[0], self.sources[0], 'weak', reason='RAW_DUPLICATE_REASSESSMENT')
            result = self.run_review(folder, [[duplicate, review(self.targets[1], self.sources[1])]], previous=self.prior_pairs)
            self.assertEqual(next(r for r in result['reviews'] if r['source_id'] == 'sa'), previous['reviews'][0])
            trace = json.loads((Path(folder) / 'facet-reviews/traces' / (result['cache_key'] + '.json')).read_text())
            self.assertIn('RAW_DUPLICATE_REASSESSMENT', json.dumps(trace, ensure_ascii=False))
            self.assertEqual(trace['reused_review_cache_keys'], [previous['cache_key']])
            self.assert_blinded(self.calls)

    def test_any_full_input_text_or_model_digest_change_prevents_reuse(self):
        for changed in ('target', 'source', 'digest'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder:
                self.seed(folder); targets, sources = copy.deepcopy(self.targets), copy.deepcopy(self.sources)
                digest = 'fixture-model-digest'
                # Changing even an unused source changes the full bound input.
                if changed == 'target': targets[1]['text'] += ' وتطبيقاتها'
                if changed == 'source': sources[2]['text'] += ' وأمثلتها'
                if changed == 'digest': digest = 'different-model-digest'
                result = self.run_review(folder, [[review(targets[i], sources[i]) for i in range(2)]],
                                         targets=targets, sources=sources, digest=digest, previous=self.prior_pairs)
                body = json.loads(self.calls[0]['messages'][1]['content'])
                self.assertNotIn('completion_only', body); self.assertEqual(ids(body['requested_pairs']), ids(self.current_pairs))
                self.assertEqual(result['reused_review_count'], 0); self.assertEqual(result['reused_review_cache_keys'], [])
                self.assert_blinded(self.calls)

    def test_changed_resolved_scope_cannot_reuse_a_prior_positive_alternative(self):
        with tempfile.TemporaryDirectory() as folder:
            self.seed(folder)
            result = self.run_review(folder, [[review(self.targets[i], self.sources[i]) for i in range(2)]],
                                     previous=self.prior_pairs, excluded=[{'target_id': 'ta', 'source_id': 'sx'}])
            body = json.loads(self.calls[0]['messages'][1]['content'])
            self.assertEqual(body['resolved_pairs'], [{'target_id': 'ta', 'source_id': 'sx'}])
            self.assertNotIn('completion_only', body); self.assertEqual(result['reused_review_count'], 0)
            self.assertNotIn(('ta', 'sx'), ids(result['reviews']))

    def test_tampered_previous_cache_is_strictly_revalidated_before_any_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = self.seed(folder); cache = Path(folder) / 'facet-reviews' / (previous['cache_key'] + '.json')
            corrupt = json.loads(cache.read_text()); corrupt['reviews'][0]['facets'][0]['source_quote'] = 'اقتباس مزور'
            cache.write_text(json.dumps(corrupt, ensure_ascii=False))
            result = self.run_review(folder, [[review(self.targets[i], self.sources[i]) for i in range(2)]], previous=self.prior_pairs)
            self.assertEqual(result['reused_review_count'], 0); self.assertEqual(ids(result['reviews']), ids(self.current_pairs))
            trace = json.loads((Path(folder) / 'facet-reviews/traces' / (result['cache_key'] + '.json')).read_text())
            self.assertTrue(any(attempt.get('invalid_cache') for attempt in trace['attempts']))
            self.assertIn('اقتباس مزور', json.dumps(trace, ensure_ascii=False))

    def test_missing_new_pair_after_two_calls_fails_without_erasing_prior_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = self.seed(folder); cache = Path(folder) / 'facet-reviews' / (previous['cache_key'] + '.json')
            before = cache.read_bytes()
            with self.assertRaises(ValueError): self.run_review(folder, [[], []], previous=self.prior_pairs)
            self.assertEqual(len(self.calls), 2); self.assertEqual(cache.read_bytes(), before)
            for call in self.calls:
                self.assertEqual(ids(json.loads(call['messages'][1]['content'])['requested_pairs']), {('tb', 'sb')})
            self.assertEqual({p.name for p in (Path(folder) / 'facet-reviews').glob('*.json')}, {cache.name})
            self.assert_blinded(self.calls)

    def test_valid_v4_cache_promotes_without_model_call_and_records_exact_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy, saved = self.write_legacy(folder); before = legacy.read_bytes()
            result = self.run_review(folder, [], pairs=self.prior_pairs)
            self.assertEqual(self.calls, []); self.assertEqual(result['version'], g.VERSION)
            self.assertEqual(result['reviews'], saved['reviews']); self.assertEqual(result['validation_attempts'], 0)
            self.assertEqual(result['reused_review_cache_keys'], [saved['cache_key']]); self.assertEqual(result['reused_review_count'], 2)
            self.assertNotEqual(result['cache_key'], saved['cache_key']); self.assertEqual(legacy.read_bytes(), before)
            trace = json.loads((Path(folder) / 'facet-reviews/traces' / (result['cache_key'] + '.json')).read_text())
            self.assertEqual(trace['attempts'], []); self.assertEqual(trace['reused_review_cache_keys'], [saved['cache_key']])
            g.validate_reviews(result['reviews'], self.targets, self.sources, ids(self.prior_pairs))

    def test_v4_promotion_requires_exact_input_key_and_never_accepts_v3(self):
        for mismatch in ('target', 'source', 'digest', 'v3-key', 'v3-marker', 'invalid-v4-proof'):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as folder:
                version = 'blinded-facet-review-v3-explicit-range-facts'
                legacy, saved = self.write_legacy(folder, version=version if mismatch == 'v3-key' else None,
                                                   stored_version=version if mismatch == 'v3-marker' else None)
                if mismatch == 'invalid-v4-proof':
                    saved['reviews'][0]['facets'][0]['source_quote'] = 'دليل غير حرفي'
                    legacy.write_text(json.dumps(saved, ensure_ascii=False))
                targets, sources, digest = copy.deepcopy(self.targets), copy.deepcopy(self.sources), 'fixture-model-digest'
                if mismatch == 'target': targets[0]['text'] += ' وآثاره'
                if mismatch == 'source': sources[0]['text'] += ' وآثاره'
                if mismatch == 'digest': digest = 'different-model-digest'
                result = self.run_review(folder, [[review(targets[0], sources[0])]], targets=targets, sources=sources,
                                         digest=digest, pairs=self.prior_pairs)
                self.assertEqual(len(self.calls), 1); self.assertEqual(result['reused_review_count'], 0)
                self.assertEqual(result['reused_review_cache_keys'], []); self.assertNotEqual(result['cache_key'], saved['cache_key'])
                self.assert_blinded(self.calls)


    def test_valid_prior_v5_schema_cache_promotes_without_rewriting_its_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            prior, saved = self.write_prior_v5(folder); before = prior.read_bytes()
            result = self.run_review(folder, [], pairs=self.prior_pairs)
            self.assertEqual(self.calls, []); self.assertEqual(result['reviews'], saved['reviews'])
            self.assertEqual(result['reused_review_cache_keys'], [saved['cache_key']])
            self.assertNotEqual(result['cache_key'], saved['cache_key'])
            self.assertEqual(result['validation_attempts'], 0); self.assertEqual(prior.read_bytes(), before)
            trace = json.loads((Path(folder) / 'facet-reviews/traces' / (result['cache_key'] + '.json')).read_text())
            self.assertEqual(trace['reused_review_cache_keys'], [saved['cache_key']])

    def test_invalid_prior_v5_empty_uncertainty_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as folder:
            prior, saved = self.write_prior_v5(folder, invalid_empty=True); before = prior.read_bytes()
            result = self.run_review(folder, [[review(self.targets[0], self.sources[0])]], pairs=self.prior_pairs)
            self.assertEqual(len(self.calls), 1); self.assertEqual(result['reused_review_count'], 0)
            self.assertEqual(result['reused_review_cache_keys'], []); self.assertEqual(prior.read_bytes(), before)
            self.assertEqual(result['reviews'][0]['verdict'], 'strict')  # fixture's own new review, no coercion
            trace = json.loads((Path(folder) / 'facet-reviews/traces' / (result['cache_key'] + '.json')).read_text())
            self.assertTrue(any(r.get('invalid_cache') for r in trace['attempts']))

    def test_general_batch_schema_rejects_empty_and_model_repairs_its_own_bounds(self):
        invalid = review(self.targets[0], self.sources[0], 'weak')
        invalid.update(verdict='uncertain', possible_tiers=[])
        invalid['facets'][0].update(support='unknown', source_quote='')
        repaired = copy.deepcopy(invalid); repaired['possible_tiers'] = ['lenient']
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_review(folder, [[invalid], [repaired]], pairs=self.prior_pairs)
        self.assertEqual(len(self.calls), 2)
        for call in self.calls:
            with self.assertRaises(ValidationError):
                Draft202012Validator(call['format']).validate({'reviews':[invalid]})
        self.assertEqual(result['reviews'][0]['possible_tiers'], ['lenient'])
        self.assertEqual(invalid['possible_tiers'], [])  # no in-place or automatic change
        self.assert_blinded(self.calls)

    def test_hard_verse_exclusion_uses_explicit_empty_exception_without_loosening_global_schema(self):
        targets = [{'id':'tv','text':'تفسير سورة البقرة الآيات1-10'}]
        sources = [{'id':'sv','text':'تفسير سورة البقرة الآيات1-2'}]
        uncertain = review(targets[0], sources[0], 'weak')
        uncertain.update(verdict='uncertain', possible_tiers=[])
        uncertain['facets'][0].update(support='unknown', source_quote='')
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_review(folder, [[uncertain]], targets=targets, sources=sources,
                                     pairs=[proposal(targets[0], sources[0])])
        Draft202012Validator(self.calls[0]['format']).validate({'reviews':[uncertain]})
        with self.assertRaises(ValidationError):
            Draft202012Validator(g.MODEL_SCHEMA).validate({'reviews':[uncertain]})
        self.assertEqual(result['reviews'][0]['possible_tiers'], [])
        self.assertEqual(result['reviews'][0]['verdict'], 'uncertain')
        self.assert_blinded(self.calls)

    def test_mixed_batch_exception_does_not_allow_empty_for_an_unexcluded_pair(self):
        from scripts.equivalency.engine import EvidenceValidationError
        targets = [self.targets[0], {'id':'tv','text':'تفسير سورة البقرة الآيات1-10'}]
        sources = [self.sources[0], {'id':'sv','text':'تفسير سورة البقرة الآيات1-2'}]
        rows = []
        for target, source in zip(targets, sources):
            value = review(target, source, 'weak'); value.update(verdict='uncertain', possible_tiers=[])
            value['facets'][0].update(support='unknown', source_quote=''); rows.append(value)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(EvidenceValidationError) as caught:
                self.run_review(folder, [rows, [rows[0]]], targets=targets, sources=sources,
                                pairs=[proposal(t,s) for t,s in zip(targets,sources)])
            self.assertEqual(caught.exception.stage, 'facet_review')
            self.assertEqual(set(caught.exception.target_ids), {'ta'})
        self.assertEqual(len(self.calls), 2)
        Draft202012Validator(self.calls[0]['format']).validate({'reviews':rows})
        with self.assertRaises(ValidationError):
            Draft202012Validator(self.calls[1]['format']).validate({'reviews':[rows[0]]})
        self.assertEqual(ids(json.loads(self.calls[1]['messages'][1]['content'])['requested_pairs']), {('ta','sa')})

    def test_exhaustion_and_truncation_are_typed_but_transport_errors_are_not_converted(self):
        from scripts.equivalency.engine import EvidenceValidationError
        for kind in ('invalid','truncated'):
            calls = []
            def caller(endpoint,payload):
                calls.append(copy.deepcopy(payload))
                if kind == 'truncated': return {'done':True,'done_reason':'length','message':{'content':'{incomplete'}}
                value = review(self.targets[0],self.sources[0],'weak')
                value['facets'][0]['support'] = 'unknown'
                return answer([value])
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(EvidenceValidationError) as caught:
                    g.review_pairs(self.targets,self.sources,self.prior_pairs,model='fixture-local',
                                   model_digest='digest',cache_dir=folder,caller=caller)
                self.assertIsInstance(caught.exception,ValueError)
                self.assertEqual(caught.exception.stage,'facet_review')
                self.assertEqual(set(caught.exception.target_ids),{'ta'})
                self.assertEqual(len(calls),1 if kind=='truncated' else 2)
        for error in (TimeoutError('queue timeout'), HTTPError('http://127.0.0.1/api/chat',503,'busy',{},None)):
            calls = []
            def technical(endpoint,payload): calls.append(payload); raise error
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(type(error)) as caught:
                    g.review_pairs(self.targets,self.sources,self.prior_pairs,model='fixture-local',
                                   model_digest='digest',cache_dir=folder,caller=technical)
                self.assertIs(caught.exception,error); self.assertNotIsInstance(caught.exception,EvidenceValidationError)
                self.assertEqual(len(calls),1)


if __name__ == '__main__':

    unittest.main()
