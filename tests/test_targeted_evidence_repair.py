"""CPU contract for bounded pair repair, not an evaluation of semantic accuracy.

The second call repairs invalid/missing IDs only. Already validated evidence is
retained verbatim, including positive alternatives, and duplicate replacements
are visible only in raw traces. No fixture calls a model or chooses a course
acceptance outcome; all aggregates must still pass the production validators.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.equivalency import engine, grounding


def response(field, rows):
    return {'done': True, 'done_reason': 'stop',
            'message': {'content': json.dumps({field: rows}, ensure_ascii=False)}}


def topic(key, text):
    return {'id': key, 'text': text}


def pair(target, source, score=87):
    return {'target_id': target['id'], 'source_id': source['id'], 'score': score,
            'target_quote': target['text'], 'source_quote': source['text'],
            'reason': 'PRIVATE_GENERATOR_REASON يجب ألا يصل إلى المراجع.'}


def review(target, source, verdict='strict', support=None, reason='دليل الأجزاء كما ورد في النص.'):
    support = support or {'strict': 'aligned', 'lenient': 'related', 'weak': 'absent', 'uncertain': 'unknown'}[verdict]
    possible = {'strict': ['strict', 'lenient'], 'lenient': ['lenient'], 'weak': [], 'uncertain': ['strict', 'lenient']}[verdict]
    return {'target_id': target['id'], 'source_id': source['id'], 'verdict': verdict,
            'possible_tiers': possible, 'reason': reason,
            'facets': [{'target_start': 1, 'target_end': len(target['text'].split()),
                        'source_quote': source['text'] if support in ('aligned', 'related', 'limited') else '',
                        'support': support}]}


def ids(rows):
    return {(row['target_id'], row['source_id']) for row in rows}


def payload_with(call, key):
    for message in reversed(call['messages']):
        if message.get('role') != 'user':
            continue
        try:
            value = json.loads(message['content'])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and key in value:
            return value
    raise AssertionError('No structured user payload containing ' + key)


class TargetedEvidenceRepairTests(unittest.TestCase):
    def setUp(self):
        self.targets = [topic('t1', 'تعريف العقد وأركانه'), topic('t2', 'شروط البيع وآثاره'),
                        topic('t3', 'أحكام الوكالة ونطاقها')]
        self.sources = [topic('s1', 'بيان تعريف العقد وأركانه'), topic('s2', 'توضيح شروط البيع وآثاره'),
                        topic('s3', 'شرح أحكام الوكالة ونطاقها'), topic('s4', 'تفصيل تعريف العقد وأركانه ومقاصده')]

    def run_review(self, folder, batches, requested=None):
        calls = []
        def caller(endpoint, payload):
            self.assertEqual(endpoint, 'chat')
            calls.append(copy.deepcopy(payload))
            if len(calls) > len(batches):
                self.fail('Repair exceeded the bounded CPU fixture call count')
            return response('reviews', copy.deepcopy(batches[len(calls) - 1]))
        self.calls = calls
        result = grounding.review_pairs(self.targets, self.sources,
            requested if requested is not None else [pair(self.targets[i], self.sources[i]) for i in range(2)],
            model='cpu-fixture', model_digest='cpu-only-model-digest', cache_dir=folder, caller=caller)
        grounding.validate_reviews(result['reviews'], self.targets, self.sources,
                                   ids(requested if requested is not None else [pair(self.targets[i], self.sources[i]) for i in range(2)]))
        return result

    def assert_blinded(self, calls):
        self.assertNotIn('PRIVATE_GENERATOR_REASON', json.dumps(calls, ensure_ascii=False))
        for call in calls:
            body = payload_with(call, 'requested_pairs')
            self.assertNotIn('"score"', json.dumps(body))

    def test_unknown_weak_repairs_only_bad_pair_and_retains_valid_row_and_raw_responses(self):
        valid = review(self.targets[0], self.sources[0], reason='السبب الأصلي المحفوظ حرفيًا.')
        bad = review(self.targets[1], self.sources[1], 'weak', 'unknown')
        repaired = review(self.targets[1], self.sources[1], 'uncertain')
        expected = grounding.decode_reviews([valid], self.targets)[0]
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_review(folder, [[valid, bad], [repaired]])
            self.assertEqual(len(self.calls), 2)
            body = payload_with(self.calls[1], 'requested_pairs')
            self.assertEqual(ids(body['requested_pairs']), {('t2', 's2')})
            self.assertEqual(ids(body['repair_issues']), {('t2', 's2')})
            self.assertTrue(all(isinstance(issue['error'], str) and issue['error'] for issue in body['repair_issues']))
            rows = {(r['target_id'], r['source_id']): r for r in result['reviews']}
            self.assertEqual(rows[('t1', 's1')], expected)
            self.assertEqual(rows[('t2', 's2')]['verdict'], 'uncertain')
            self.assertEqual(result['validation_attempts'], 2)
            raw = '\n'.join(p.read_text() for p in (Path(folder) / 'facet-reviews' / 'traces').glob('*.json'))
            for batch in [[valid, bad], [repaired]]:
                self.assertIn(json.dumps(response('reviews', batch)['message']['content'], ensure_ascii=False), raw)
        self.assert_blinded(self.calls)

    def test_all_bad_ids_and_missing_pairs_are_collected_in_one_targeted_repair(self):
        first = [review(self.targets[0], self.sources[0], 'weak', 'unknown'),
                 review(self.targets[1], self.sources[1], 'strict', 'absent')]
        repaired = [review(self.targets[i], self.sources[i], 'uncertain' if i == 0 else 'strict') for i in range(3)]
        requested = [pair(t, s) for t, s in zip(self.targets, self.sources)]
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_review(folder, [first, repaired], requested)
        body = payload_with(self.calls[1], 'requested_pairs')
        self.assertEqual(ids(body['requested_pairs']), {('t1', 's1'), ('t2', 's2'), ('t3', 's3')})
        self.assertEqual(ids(body['repair_issues']), ids(body['requested_pairs']))
        self.assertEqual(len(result['reviews']), 3); self.assertEqual(len(self.calls), 2)
        self.assert_blinded(self.calls)

    def test_second_response_cannot_replace_a_previously_valid_review(self):
        valid = review(self.targets[0], self.sources[0], reason='FIRST_VALID_REASON')
        duplicate = review(self.targets[0], self.sources[0], 'uncertain', reason='SECOND_DUPLICATE_MUST_NOT_REPLACE')
        bad = review(self.targets[1], self.sources[1], 'weak', 'unknown')
        repaired = review(self.targets[1], self.sources[1], 'uncertain')
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_review(folder, [[valid, bad], [duplicate, repaired]])
            original = next(r for r in result['reviews'] if r['target_id'] == 't1')
            self.assertEqual(original, grounding.decode_reviews([valid], self.targets)[0])
            raw = '\n'.join(p.read_text() for p in (Path(folder) / 'facet-reviews' / 'traces').glob('*.json'))
            self.assertIn('SECOND_DUPLICATE_MUST_NOT_REPLACE', raw)
        self.assert_blinded(self.calls)

    def test_discovered_positive_alternative_survives_an_unrelated_targeted_repair(self):
        base = review(self.targets[0], self.sources[0], 'weak')
        alternative = review(self.targets[0], self.sources[3], 'strict')
        bad = review(self.targets[1], self.sources[1], 'weak', 'unknown')
        repaired = review(self.targets[1], self.sources[1], 'uncertain')
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_review(folder, [[base, alternative, bad], [repaired]])
        self.assertEqual(ids(payload_with(self.calls[1], 'requested_pairs')['requested_pairs']), {('t2', 's2')})
        self.assertIn(('t1', 's4'), ids(result['reviews']))
        self.assertEqual(next(r for r in result['reviews'] if r['source_id'] == 's4'),
                         grounding.decode_reviews([alternative], self.targets)[0])

    def test_aggregate_cache_is_revalidated_and_tampered_cache_cannot_skip_review(self):
        valid = [review(self.targets[i], self.sources[i]) for i in range(2)]
        with tempfile.TemporaryDirectory() as folder:
            first = self.run_review(folder, [valid])
            cached = grounding.review_pairs(self.targets, self.sources, [pair(self.targets[i], self.sources[i]) for i in range(2)],
                model='cpu-fixture', model_digest='cpu-only-model-digest', cache_dir=folder,
                caller=lambda *args: self.fail('Validated cache should need no new model response'))
            self.assertEqual(cached['reviews'], first['reviews'])
            cache = Path(folder) / 'facet-reviews' / (first['cache_key'] + '.json')
            value = json.loads(cache.read_text()); value['reviews'][0]['facets'][0]['source_quote'] = 'اقتباس غير موجود'
            cache.write_text(json.dumps(value, ensure_ascii=False))
            result = self.run_review(folder, [valid])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(result['reviews'], first['reviews'])

    def test_unrepaired_contradictions_still_fail_after_exactly_two_calls(self):
        valid = review(self.targets[0], self.sources[0]); bad = review(self.targets[1], self.sources[1], 'weak', 'unknown')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                self.run_review(folder, [[valid, bad], [bad]])
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(ids(payload_with(self.calls[1], 'requested_pairs')['requested_pairs']), {('t2', 's2')})
            self.assertFalse(list((Path(folder) / 'facet-reviews').glob('*.json')))
        self.assert_blinded(self.calls)

    def test_generator_repairs_only_missing_feedback_edge_and_never_rescores_valid_rows(self):
        targets, sources = self.targets[:2], self.sources[:3]
        kept = [pair(targets[0], sources[0], 87), pair(targets[1], sources[1], 44)]
        missing = pair(targets[1], sources[2], 65)
        changed_duplicate = {**kept[0], 'score': 12, 'reason': 'SECOND_PROPOSAL_MUST_NOT_REPLACE'}
        feedback = [{'target_id': targets[1]['id'], 'source_id': sources[2]['id']}]
        with tempfile.TemporaryDirectory() as folder, patch.object(engine, 'local_model', return_value='cpu-only-model-digest'), \
                patch.object(engine, 'post_ollama', side_effect=[response('pairs', kept), response('pairs', [changed_duplicate, missing])]) as caller:
            result = engine._propose_pairs({'topics': targets}, {'topics': sources}, cache_dir=folder, review_feedback=feedback)
            self.assertEqual(caller.call_count, 2)
            second = payload_with(caller.call_args_list[1].args[1], 'required_review_pairs')
            self.assertEqual(ids(second['required_review_pairs']), {('t2', 's3')})
            rows = {(r['target_id'], r['source_id']): r for r in result['pairs']}
            for original in kept:
                self.assertEqual(rows[(original['target_id'], original['source_id'])], original)
            self.assertEqual(rows[('t2', 's3')], missing)
            engine.validate_pairs(result['pairs'], targets, sources)
            raw = '\n'.join(p.read_text() for p in (Path(folder) / 'traces').glob('*.json'))
            self.assertIn('SECOND_PROPOSAL_MUST_NOT_REPLACE', raw)
            self.assertIn('PRIVATE_GENERATOR_REASON', raw)

    def test_generator_cannot_drop_a_required_positive_alternative_after_two_calls(self):
        targets, sources = self.targets[:2], self.sources[:3]
        kept = [pair(targets[0], sources[0], 87), pair(targets[1], sources[1], 44)]
        feedback = [{'target_id': 't2', 'source_id': 's3', 'verdict': 'strict', 'possible_tiers': ['strict', 'lenient']}]
        with tempfile.TemporaryDirectory() as folder, patch.object(engine, 'local_model', return_value='cpu-only-model-digest'), \
                patch.object(engine, 'post_ollama', side_effect=[response('pairs', kept), response('pairs', [])]) as caller:
            with self.assertRaises(ValueError):
                engine._propose_pairs({'topics': targets}, {'topics': sources}, cache_dir=folder, review_feedback=feedback)
            self.assertEqual(caller.call_count, 2)
            second = payload_with(caller.call_args_list[1].args[1], 'required_review_pairs')
            self.assertEqual(ids(second['required_review_pairs']), {('t2', 's3')})
            self.assertFalse(list(Path(folder).glob('*.json')))


if __name__ == '__main__':
    unittest.main()
