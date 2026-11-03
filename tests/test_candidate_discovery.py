import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.equivalency import candidates as c


def response(value, **extra):
    return {'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps(value, ensure_ascii=False)}, **extra}


def answer(payload, status='possible'):
    ts = {t['id']: t['text'] for t in payload['target']}
    ss = {s['id']: s['text'] for s in payload['source']}
    scan = {}
    for pair in payload['requested_pairs']:
        tid, sid = pair['target_id'], pair['source_id']
        row = {'source_id': sid, 'status': status}
        if status != 'unrelated':
            row.update(reason='مرشح للفحص دون حكم توافق.', target_quote=ts[tid], source_quote=ss[sid])
        scan.setdefault(tid, []).append(row)
    return {'scan': [{'target_id': tid, 'sources': rows} for tid, rows in scan.items()], 'groups': []}


def group(tid, ids, targets, sources):
    ts = {r['id']: r.get('corrected_text') or r['text'] for r in targets}
    ss = {r['id']: r.get('corrected_text') or r['text'] for r in sources}
    return {'target_id': tid, 'source_ids': ids, 'reason': 'أدلة صفوف مستقلة تستحق فحص التكامل.',
            'evidence': [{'source_id': sid, 'target_quote': ts[tid], 'source_quote': ss[sid]} for sid in ids]}


class Caller:
    def __init__(self, fn=None):
        self.calls = []
        self.fn = fn or (lambda payload, n: answer(payload))

    def __call__(self, endpoint, payload):
        assert endpoint == 'chat'
        parsed = json.loads(payload['messages'][1]['content'])
        self.calls.append(copy.deepcopy(payload))
        value = self.fn(parsed, len(self.calls))
        return value if 'message' in value else response(value)


class CandidateDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.targets = [{'id': 't1', 'text': 'تعريف العقد وأركانه وآثاره'},
                        {'id': 't2', 'text': 'تعريف العقد وتطبيقاته'}]
        self.sources = [{'id': 's1', 'text': 'تعريف العقد'}, {'id': 's2', 'text': 'أركان العقد'},
                        {'id': 's3', 'text': 'آثار العقد'}, {'id': 's4', 'text': 'تطبيقات العقد'}]

    def discover(self, caller, targets=None, sources=None, **extra):
        return c.discover_candidates(targets or self.targets, sources or self.sources,
            model='test-local:1', model_digest=extra.pop('model_digest', 'digest-A'), caller=caller,
            cache_dir=self.tmp.name, **extra)

    def raw(self, status='possible'):
        return answer({'target': self.targets, 'source': self.sources,
                       'requested_pairs': [{'target_id': t['id'], 'source_id': s['id']}
                                           for t in self.targets for s in self.sources]}, status)

    def test_complete_CPU_plan_keeps_zero_overlap_and_preserves_reusable_source(self):
        targets = self.targets + [{'id': 't3', 'text': 'التفسير التحليلي لسورة البقرة الآيات1-5'}]
        pairs = c.plan_candidate_pairs(targets, self.sources)
        self.assertEqual(len(pairs), 12)
        self.assertEqual({p['target_id'] for p in pairs if p['source_id'] == 's1'}, {'t1', 't2', 't3'})
        self.assertTrue(any(p['retrieval_channel'] == 'complete_enumeration' for p in pairs))
        self.assertFalse(any('score' in p or 'verdict' in p for p in pairs))

    def test_full_scan_and_three_nonadjacent_sources_group_no_grade(self):
        def fake(payload, _):
            value = answer(payload)
            value['groups'] = [group('t1', ['s4', 's1', 's3'], self.targets, self.sources)]
            return value
        found = self.discover(Caller(fake))
        self.assertEqual(found['coverage'], {'targets': 2, 'sources': 4, 'pairs': 8, 'complete': True})
        self.assertEqual(found['groups'][0]['source_ids'], ['s1', 's3', 's4'])
        self.assertNotIn('score', found['groups'][0])
        self.assertEqual(len(found['ordered_pairs']), 8)

    def test_groups_two_to_four_and_source_reuse_across_targets(self):
        value = self.raw()
        value['groups'] = [group('t1', ['s1', 's2'], self.targets, self.sources),
                           group('t2', ['s1', 's2', 's3', 's4'], self.targets, self.sources)]
        self.assertEqual(len(c.validate_discovery(value, self.targets, self.sources)['groups']), 2)

    def test_unrelated_is_compact_and_cannot_be_an_exclusion_grade(self):
        found = self.discover(Caller(lambda payload, _: answer(payload, 'unrelated')))
        self.assertEqual(found['scan'][0]['sources'][0], {'source_id': 's1', 'status': 'unrelated'})
        self.assertIn('ليس إثبات غياب', found['warning'])
        self.assertNotIn('decision', found)

    def test_real_umbrella_alternative_not_lost_when_no_literal_words_overlap(self):
        targets = [{'id': 'T', 'text': 'الصحة والفساد والبطلان، العزيمة والرخصة، الأداء والإعادة والقضاء'}]
        sources = [{'id': 'S', 'text': 'أقسام الحكم الشرعي الوضعي'}]
        self.assertEqual(c.plan_candidate_pairs(targets, sources)[0]['source_id'], 'S')
        found = self.discover(Caller(), targets, sources)
        self.assertEqual(found['scan'][0]['sources'][0]['status'], 'possible')
        self.assertNotIn('score', found['scan'][0]['sources'][0])

    def test_complete_small_real_size_matrix_13_by15(self):
        ts = [{'id': f't{i}', 'text': f'موضوع الهدف {i}'} for i in range(13)]
        ss = [{'id': f's{i}', 'text': f'موضوع المصدر {i}'} for i in range(15)]
        caller = Caller(lambda payload, _: answer(payload, 'unrelated'))
        found = self.discover(caller, ts, ss)
        self.assertEqual(found['coverage']['pairs'], 195)
        self.assertEqual(len(caller.calls), 5)
        self.assertTrue(all(len(json.loads(x['messages'][1]['content'])['target']) <= 3 for x in caller.calls))

    def test_repair_only_missing_preserves_original_valid_pair(self):
        original = self.raw()['scan'][0]['sources'][0]
        original['reason'] = 'دليل أول سليم لا يعاد توليده.'
        def fake(payload, n):
            if n == 1:
                return {'scan': [{'target_id': 't1', 'sources': [original]}], 'groups': []}
            self.assertTrue(payload['repair_only'])
            self.assertNotIn({'target_id': 't1', 'source_id': 's1'}, payload['requested_pairs'])
            return answer(payload)
        caller = Caller(fake)
        result = self.discover(caller)
        self.assertEqual(len(caller.calls), 2)
        self.assertEqual(result['scan'][0]['sources'][0], original)

    def test_duplicate_pairs_require_repair_and_do_not_choose_favorable_one(self):
        def fake(payload, n):
            value = answer(payload)
            if n == 1:
                value['scan'][0]['sources'].append(copy.deepcopy(value['scan'][0]['sources'][0]))
            return value
        caller = Caller(fake)
        self.assertEqual(self.discover(caller)['coverage']['pairs'], 8)
        self.assertEqual(len(caller.calls), 2)

    def test_foreign_ID_missing_ID_and_duplicate_target_are_rejected(self):
        for mutate in [lambda v: v['scan'][0]['sources'][0].update(source_id='foreign'),
                       lambda v: v['scan'][0]['sources'].pop(),
                       lambda v: v['scan'].append(copy.deepcopy(v['scan'][0]))]:
            with self.subTest(mutate=mutate):
                value = self.raw(); mutate(value)
                with self.assertRaises(ValueError): c.validate_discovery(value, self.targets, self.sources)

    def test_quotes_must_belong_to_exact_source_and_target(self):
        for field, value in [('source_quote', 'اقتباس من مصدر آخر'), ('target_quote', 'موضوع لم يرد')]:
            with self.subTest(field=field):
                raw = self.raw(); raw['scan'][0]['sources'][0][field] = value
                with self.assertRaises(ValueError): c.validate_discovery(raw, self.targets, self.sources)

    def test_group_rejects_foreign_duplicate_unrelated_or_unquoted_source(self):
        for kind in ['foreign', 'duplicate', 'unrelated', 'missing_anchor', 'wrong_quote']:
            with self.subTest(kind=kind):
                value = self.raw(); g = group('t1', ['s1', 's2'], self.targets, self.sources)
                if kind == 'foreign': g['source_ids'][1] = 'foreign'
                elif kind == 'duplicate': g['source_ids'][1] = 's1'
                elif kind == 'unrelated': value['scan'][0]['sources'][1] = {'source_id': 's2', 'status': 'unrelated'}
                elif kind == 'missing_anchor': g['evidence'].pop()
                else: g['evidence'][0]['source_quote'] = 'العقد غير مذكور هنا'
                value['groups'] = [g]
                with self.assertRaises(ValueError): c.validate_discovery(value, self.targets, self.sources)

    def test_group_repeated_in_different_order_rejected(self):
        value = self.raw();g = group('t1', ['s1', 's2'], self.targets, self.sources)
        value['groups'] = [g, group('t1', ['s2', 's1'], self.targets, self.sources)]
        with self.assertRaises(ValueError): c.validate_discovery(value, self.targets, self.sources)

    def test_no_score_verdict_or_extra_fields_accepted(self):
        for field, val in [('score', 99), ('verdict', 'strict')]:
            value = self.raw();value['scan'][0]['sources'][0][field] = val
            with self.assertRaises(ValueError): c.validate_discovery(value, self.targets, self.sources)

    def test_corrected_text_effective_and_generator_grades_never_visible(self):
        targets = copy.deepcopy(self.targets);targets[0].update(text='نص قديم', corrected_text='النص الفعال الصحيح', score=100)
        caller = Caller();found = self.discover(caller, targets)
        payload = json.loads(caller.calls[0]['messages'][1]['content'])
        self.assertEqual(payload['target'][0], {'id': 't1', 'text': 'النص الفعال الصحيح'})
        self.assertNotIn('score', payload['target'][0])
        targets[0]['score'] = 1
        again = self.discover(Caller(lambda *_: self.fail('score-only mutation must use cache')), targets)
        self.assertEqual(found['cache_key'], again['cache_key'])

    def test_valid_cache_reused_without_caller_or_hidden_network(self):
        caller = Caller();first = self.discover(caller)
        with patch('urllib.request.urlopen', side_effect=AssertionError('No network outside caller')):
            second = self.discover(Caller(lambda *_: self.fail('cache miss')))
        self.assertTrue(second['cache_hit']);self.assertEqual(second['attempts'], 0)
        self.assertEqual(first['scan'], second['scan'])
        cache = Path(self.tmp.name) / 'candidate-discovery' / (first['cache_key'] + '.json')
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)

    def test_model_digest_and_effective_text_and_ID_change_cache_identity(self):
        base = self.discover(Caller())
        variants = [self.discover(Caller(), model_digest='digest-B')]
        ss = copy.deepcopy(self.sources);ss[0]['text'] = 'محتوى جديد'
        variants.append(self.discover(Caller(), sources=ss))
        ss[0]['id'] = 'new-id';variants.append(self.discover(Caller(), sources=ss))
        self.assertEqual(len({base['cache_key']} | {v['cache_key'] for v in variants}), 4)

    def test_tampered_or_incomplete_cache_is_revalidated(self):
        first = self.discover(Caller());cache = Path(self.tmp.name)/'candidate-discovery'/(first['cache_key']+'.json')
        value = json.loads(cache.read_text());value['scan'][0]['sources'].pop();cache.write_text(json.dumps(value))
        caller = Caller();fixed = self.discover(caller)
        self.assertFalse(fixed['cache_hit']);self.assertEqual(len(caller.calls), 1)
        self.assertEqual(fixed['coverage']['pairs'], 8)

    def test_truncated_raw_saved_before_validation_and_only_two_attempts(self):
        caller = Caller(lambda payload, _: response(answer(payload), done_reason='length'))
        with self.assertRaises(c.CandidateDiscoveryError) as caught: self.discover(caller)
        self.assertEqual(len(caller.calls), 2)
        trace = json.loads(Path(caught.exception.trace_path).read_text())
        self.assertEqual(trace['attempts'][0]['response']['done_reason'], 'length')
        self.assertTrue(trace['attempts'][0]['validation_error'])
        self.assertEqual(list((Path(self.tmp.name)/'candidate-discovery').glob('*.json')), [])

    def test_transport_error_propagates_without_retry_or_unrelated_result(self):
        calls = []
        def fail(*args):calls.append(args);raise TimeoutError('test transport')
        with self.assertRaises(TimeoutError):self.discover(fail)
        self.assertEqual(len(calls), 1)
        traces = list((Path(self.tmp.name)/'candidate-discovery'/'traces').glob('*.json'))
        self.assertEqual(len(traces), 1)
        self.assertNotIn('response', json.loads(traces[0].read_text())['attempts'][0])

    def test_invalid_inputs_fail_before_any_caller_and_never_silently_trim(self):
        bad = [[], [{'id': 'x', 'text': ''}], [{'id': 'x', 'text': 'صحيح'}, {'id': 'x', 'text': 'مكرر'}],
               [{'id': str(i), 'text': 'موضوع'} for i in range(101)]]
        for rows in bad:
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                c.discover_candidates(rows, self.sources, model='test', model_digest='digest', caller=lambda *_: self.fail(), cache_dir=self.tmp.name)

    def test_order_change_preserves_complete_candidate_ID_set(self):
        a=c.plan_candidate_pairs(self.targets,self.sources);b=c.plan_candidate_pairs(list(reversed(self.targets)),list(reversed(self.sources)))
        self.assertEqual({(p['target_id'],p['source_id']) for p in a},{(p['target_id'],p['source_id']) for p in b})
        self.assertEqual({(p['target_id'],p['source_id']):p['rank'] for p in a},{(p['target_id'],p['source_id']):p['rank'] for p in b})

    def test_100_topics_accepted_without_trimming_in_either_side(self):
        rows = [{'id': str(i), 'text': f'موضوع مستقل {i}'} for i in range(100)]
        self.assertEqual(len(c.plan_candidate_pairs(rows, self.sources)), 400)
        self.assertEqual(len(c.plan_candidate_pairs(self.targets, rows)), 200)
        for targets, sources in [(rows + [{'id': '100', 'text': 'موضوع101'}], self.sources),
                                 (self.targets, rows + [{'id': '100', 'text': 'موضوع101'}])]:
            with self.assertRaises(ValueError):
                c.plan_candidate_pairs(targets, sources)
            with self.assertRaises(ValueError):
                c.discover_candidates(targets, sources, model='test', model_digest='digest',
                    caller=lambda *_: self.fail('101 must fail before inference'), cache_dir=self.tmp.name)

    def test_100_source_curriculum_stays_whole_and_group_can_cross_old96_boundary(self):
        sources = [{'id': f's{i}', 'text': f'موضوع مستقل من المنهج {i}'} for i in range(100)]
        def fake(payload, _):
            self.assertEqual(len(payload['source']), 100)
            self.assertLessEqual(len(payload['requested_pairs']), 100)
            value = answer(payload)
            tid = payload['target'][0]['id']
            value['groups'] = [group(tid, ['s0', 's99'], self.targets, sources)]
            return value
        caller = Caller(fake)
        found = self.discover(caller, sources=sources)
        self.assertEqual(len(caller.calls), 2)
        self.assertEqual(found['coverage']['pairs'], 200)
        self.assertEqual(len(found['groups']), 2)
        for row in found['scan']:
            self.assertEqual(len(row['sources']), 100)
            self.assertEqual({s['source_id'] for s in row['sources']}, {s['id'] for s in sources})

    def test_group_repair_preserves_other_valid_group_and_all_scan_rows(self):
        stable=group('t1',['s1','s2'],self.targets,self.sources)
        fixed=group('t2',['s3','s4'],self.targets,self.sources)
        def fake(payload,n):
            if n==1:
                value=answer(payload);bad=copy.deepcopy(fixed);bad['evidence'][0]['source_quote']='نص غير مطبوع'
                value['groups']=[stable,bad];return value
            self.assertEqual(payload['requested_pairs'],[])
            self.assertEqual(payload['required_groups'],[{'target_id':'t2','source_ids':['s3','s4']}])
            return {'scan':[],'groups':[fixed]}
        found=self.discover(Caller(fake))
        self.assertEqual(len(found['groups']),2)
        self.assertEqual(found['groups'][0],stable)
        self.assertEqual(found['coverage']['pairs'],8)

    def test_multi_batch_rows_and_groups_retained_without_rewriting(self):
        ts=[{'id':f't{i}','text':f'تعريف موضوع {i} وآثاره'} for i in range(7)]
        def fake(payload,n):
            value=answer(payload)
            tid=payload['target'][0]['id']
            value['groups']=[group(tid,['s1','s2'],ts,self.sources)]
            return value
        caller=Caller(fake);found=self.discover(caller,ts)
        self.assertEqual(len(caller.calls),3)
        self.assertEqual(found['coverage']['pairs'],28)
        self.assertEqual({g['target_id'] for g in found['groups']},{'t0','t3','t6'})

    def test_out_of_batch_target_group_not_accepted(self):
        ts=[{'id':f't{i}','text':f'تعريف موضوع {i} وآثاره'} for i in range(4)]
        def fake(payload,n):
            value=answer(payload)
            if n>=2:value['groups']=[group('t0',['s1','s2'],ts,self.sources)]
            return value
        with self.assertRaises(c.CandidateDiscoveryError):self.discover(Caller(fake),ts)

    def test_blank_optional_unrelated_quotes_are_normalized_but_not_required(self):
        value=self.raw('unrelated');value['scan'][0]['sources'][0].update(reason='',source_quote='',target_quote='')
        found=c.validate_discovery(value,self.targets,self.sources)
        self.assertEqual(found['scan'][0]['sources'][0],{'source_id':'s1','status':'unrelated'})

    def test_cache_rank_and_coverage_metadata_are_recomputed_not_trusted(self):
        found=self.discover(Caller());cache=Path(self.tmp.name)/'candidate-discovery'/(found['cache_key']+'.json')
        bad=json.loads(cache.read_text());bad['ordered_pairs']=[];bad['coverage']={'pairs':0};cache.write_text(json.dumps(bad))
        restored=self.discover(Caller(lambda *_:self.fail('valid matrix should be reused')))
        self.assertEqual(len(restored['ordered_pairs']),8)
        self.assertEqual(restored['coverage']['pairs'],8)

    def test_malformed_ID_types_raise_validation_error(self):
        value=self.raw();value['scan'][0]['sources'][0]['source_id']=['s1']
        with self.assertRaises(ValueError):c.validate_discovery(value,self.targets,self.sources)
        value=self.raw();value['groups']=[{'target_id':[]}]
        with self.assertRaises(ValueError):c.validate_discovery(value,self.targets,self.sources)

    def test_persistent_missing_pair_fails_bounded_and_retained_raw_is_auditable(self):
        good=self.raw()['scan'][0]['sources'][0]
        def fake(payload,n):
            return {'scan':[{'target_id':'t1','sources':[good]}],'groups':[]} if n==1 else {'scan':[],'groups':[]}
        caller=Caller(fake)
        with self.assertRaises(c.CandidateDiscoveryError) as caught:self.discover(caller)
        self.assertEqual(len(caller.calls),2)
        trace=json.loads(Path(caught.exception.trace_path).read_text())
        self.assertEqual(trace['attempts'][1]['request_payload']['retained_pairs'],[{'target_id':'t1','source_id':'s1'}])
        self.assertEqual(len(caught.exception.missing_pairs),7)


if __name__ == '__main__':
    unittest.main()
