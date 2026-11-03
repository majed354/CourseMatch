"""Outbound transport, lease recovery and private task isolation; no Ollama calls."""
import copy
import importlib.util
import io
import json
import os
import plistlib
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch, call

import pymupdf
from scripts import cloud_worker as worker
from scripts.equivalency.reference_quality import topics_digest


TOKEN = 'test_token_' + 'x' * 40
CONFIG = worker.Config('https://coursematch.example', TOKEN, 'test-mac')
LOCAL_CONFIG = worker.Config(CONFIG.cloud_url, TOKEN, 'test-mac', local_url='http://127.0.0.1:8800')
SESSION = '1' * 64
LEASE = 'a' * 48


def task(kind='source', payload=None, key='a' * 32, session=SESSION, lease=LEASE):
    return {'id': key, 'kind': kind, 'payload': {} if payload is None else payload, 'session_id': session, 'lease_token': lease, 'lease_expires_at': '2099-01-01T00:00:00+00:00'}


def pdf_bytes(text='test fixture'):
    with pymupdf.open() as document:
        document.new_page().insert_text((30, 40), text)
        return document.tobytes()


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'equivalency-pilot').mkdir()
        (self.root / 'assets').mkdir()
        (self.root / 'data.json').write_text('{}')
        (self.root / 'site-edits.json').write_text('{}')
        self.pdf = self.root / 'assets/course.pdf'
        self.pdf.write_bytes(pdf_bytes())
        self.course = {'id': 'c' * 20, 'name': 'مقرر الهدف', 'code': 'TEST-2', 'hours': 2, 'source_pdf': 'assets/course.pdf', 'source_sha256': worker.digest(self.pdf),
                       'context_id': 'b' * 16, 'context': {'program': 'الشريعة', 'degree': 'بكالوريوس', 'plan_type': 'جديدة', 'version': '47'},
                       'topics': [{'id': 't1', 'text': 'تعريف العلم ونشأته'}], 'warnings': [], 'reviewed': False}
        self.catalog = {'catalog_id': 'test-catalog', 'source_hashes': {name: worker.digest(self.root / name) for name in ('data.json', 'site-edits.json')}, 'courses': [self.course],
                        'contexts': [{'id': self.course['context_id'], **self.course['context']}], 'library_sources': []}
        (self.root / 'equivalency-pilot/catalog.json').write_text(json.dumps(self.catalog, ensure_ascii=False))
        self.processor = worker.Processor(self.root)
        self.source = {'id': 'f' * 32, 'name': 'المقرر المصدر', 'topics': [{'id': 's1', 'text': 'تعريف العلم ونشأته'}], 'warnings': []}

    def compare_task(self, **updates):
        payload = {'catalog_version': self.catalog['catalog_id'], 'source_id': self.source['id'], 'source': copy.deepcopy(self.source), 'target_id': self.course['id'], 'reviewed': True,
                   'beneficiary': {'name': 'مستفيد للاختبار', 'number': '٠٠٠123'}}
        payload.update(updates)
        return task('compare', payload)

    def exact_evidence(self):
        return {'provider': 'exact_text', 'rubric': worker.RUBRIC_VERSION, 'pairs': [{'target_id': 't1', 'source_id': 's1', 'score': 100, 'reason': 'تتطابق المفردتان في الموضوع والنطاق.',
                                                   'target_quote': 'تعريف العلم ونشأته', 'source_quote': 'تعريف العلم ونشأته'}]}


class TransportTests(unittest.TestCase):
    def test_only_https_origin_is_allowed_and_config_repr_hides_token(self):
        for origin in ('http://example.com', 'file:///tmp/test', 'https://user:pass@example.com', 'https://example.com/path', 'https://example.com?token=bad'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                worker.Config(origin, TOKEN, 'mac')
        self.assertNotIn(TOKEN, repr(CONFIG))

    def test_local_destination_is_an_exact_optional_loopback_exception(self):
        self.assertEqual(CONFIG.local_url, '')
        self.assertEqual(LOCAL_CONFIG.local_url, 'http://127.0.0.1:8800')
        for origin in (None, False, 'http://localhost:8800', 'http://127.0.0.2:8800', 'http://[::1]:8800',
                       'http://127.0.0.1', 'http://127.0.0.1:8801', 'https://127.0.0.1:8800',
                       'http://127.0.0.1:8800/', 'http://127.0.0.1:8800/private',
                       'http://user@127.0.0.1:8800', 'http://user:pass@127.0.0.1:8800',
                       'http://127.0.0.1:8800?', 'http://127.0.0.1:8800#',
                       'http://127.0.0.1:8800?token=secret', 'http://127.0.0.1:8800#fragment',
                       ' http://127.0.0.1:8800', 'http://127.0.0.1:8800\\@foreign.example'):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, 'fixed_loopback'):
                worker.Config(CONFIG.cloud_url, TOKEN, 'mac', local_url=origin)
        with self.assertRaisesRegex(ValueError, 'requires_https'):
            worker.Config('http://public.example', TOKEN, 'mac', local_url=LOCAL_CONFIG.local_url)

    def test_local_destination_is_read_literally_from_private_file_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env.worker'
            path.write_text('COURSEMATCH_CLOUD_URL=' + CONFIG.cloud_url + '\nCOURSEMATCH_WORKER_TOKEN=' + TOKEN + '\nCOURSEMATCH_WORKER_ID=test-mac\nCOURSEMATCH_LOCAL_URL=http://127.0.0.1:8800\n')
            path.chmod(0o600)
            self.assertEqual(worker.Config.from_file(path, environ={}), LOCAL_CONFIG)
            self.assertEqual(worker.Config.from_file(path, environ={'COURSEMATCH_LOCAL_URL': ''}), CONFIG)
            with self.assertRaisesRegex(ValueError, 'fixed_loopback'):
                worker.Config.from_file(path, environ={'COURSEMATCH_LOCAL_URL': 'http://localhost:8800'})

    def test_secret_file_is_literal_private_and_never_shell_expanded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env.worker'
            path.write_text("# local settings\nCOURSEMATCH_CLOUD_URL=https://coursematch.example\nCOURSEMATCH_WORKER_TOKEN='" + TOKEN + "'\nCOURSEMATCH_WORKER_ID=test-mac\n")
            path.chmod(0o600)
            self.assertEqual(worker.Config.from_file(path, environ={}), CONFIG)
            path.chmod(0o640)
            with self.assertRaisesRegex(ValueError, 'permissions'):
                worker.load_env_file(path)
            path.chmod(0o600)
            path.write_text('COURSEMATCH_WORKER_ID=$(touch /tmp/never-executed)\n')
            self.assertEqual(worker.load_env_file(path)['COURSEMATCH_WORKER_ID'], '$(touch /tmp/never-executed)')
            with self.assertRaises(ValueError):
                worker.Config.from_file(path, environ={'COURSEMATCH_CLOUD_URL': CONFIG.cloud_url, 'COURSEMATCH_WORKER_TOKEN': TOKEN})
            link = Path(directory) / 'linked.env'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                worker.load_env_file(link)

    def test_claim_lane_is_explicit_only_for_specialized_workers_and_invalid_lanes_fail_closed(self):
        for lane in ('all', 'interactive', 'compare'):
            client = worker.CloudClient(CONFIG, lane=lane)
            client.request = Mock(return_value={'task': None})
            self.assertIsNone(client.claim())
            body = client.request.call_args.args[2]
            self.assertEqual(body, {'worker_id': CONFIG.worker_id, **({'lane': lane} if lane != 'all' else {})})
        for lane in ('source', 'COMPARE', '', None):
            with self.subTest(lane=lane), self.assertRaises(ValueError):
                worker.CloudClient(CONFIG, lane=lane)

    def test_pdf_fetch_has_fixed_https_destination_and_current_lease_header(self):
        raw = b'%PDF-test'
        response = io.BytesIO(raw)
        opener = SimpleNamespace(open=Mock(return_value=response))
        client = worker.CloudClient(CONFIG, opener)
        self.assertEqual(client.input_pdf(task()), raw)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, CONFIG.cloud_url + '/api/worker/tasks/' + 'a' * 32 + '/input-pdf')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertEqual(request.get_header('X-lease-token'), LEASE)
        self.assertEqual(request.method, 'GET')
        with self.assertRaises(ValueError):
            client.request('GET', 'https://foreign.example/private')
        self.assertIsNone(worker.NoRedirect().redirect_request(request, None, 302, 'redirect', {}, 'https://foreign.example'))

    def test_local_transport_keeps_bearer_lease_and_refuses_every_redirect(self):
        opener = SimpleNamespace(open=Mock(return_value=io.BytesIO(b'%PDF-local')))
        client = worker.CloudClient(LOCAL_CONFIG, opener)
        self.assertEqual(client.input_pdf(task()), b'%PDF-local')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, LOCAL_CONFIG.local_url + '/api/worker/tasks/' + 'a' * 32 + '/input-pdf')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertEqual(request.get_header('X-lease-token'), LEASE)
        self.assertIsNone(request.get_header('Origin'))
        self.assertTrue(any(isinstance(handler, worker.NoRedirect) for handler in worker.CloudClient(LOCAL_CONFIG).opener.handlers))
        for destination in (CONFIG.cloud_url, LOCAL_CONFIG.local_url + '/api/worker/other', 'http://foreign.example'):
            with self.subTest(destination=destination):
                self.assertIsNone(worker.NoRedirect().redirect_request(request, None, 302, 'redirect', {}, destination))
                reply = io.BytesIO(b'{}')
                reply.geturl = lambda: destination
                with self.assertRaises(worker.CloudError) as error:
                    worker.CloudClient(LOCAL_CONFIG, SimpleNamespace(open=Mock(return_value=reply))).claim()
                self.assertEqual(error.exception.status, 302)

    def test_redirects_and_response_bodies_never_leak_credentials_or_pii(self):
        response = io.BytesIO(b'{}')
        response.geturl = lambda: 'https://foreign.example/private'
        client = worker.CloudClient(CONFIG, SimpleNamespace(open=Mock(return_value=response)))
        with self.assertRaises(worker.CloudError):
            client.claim()
        private_error = urllib.error.HTTPError('https://private/' + TOKEN, 401, 'PII secret beneficiary', {}, io.BytesIO(b'private response'))
        client = worker.CloudClient(CONFIG, SimpleNamespace(open=Mock(side_effect=private_error)))
        with self.assertRaises(worker.CloudError) as error:
            client.claim()
        self.assertEqual(error.exception.status, 401)
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertNotIn('beneficiary', str(error.exception))
        stale = urllib.error.HTTPError(CONFIG.cloud_url, 409, 'expired', {}, None)
        with self.assertRaises(worker.LeaseLost):
            worker.CloudClient(CONFIG, SimpleNamespace(open=Mock(side_effect=stale))).claim()


class ProcessorTests(WorkerFixture):
    def test_image_source_uses_vision_and_retains_original_container_ownership(self):
        raw=pdf_bytes('container fixture');cloud=Mock(input_pdf=Mock(return_value=raw))
        result={'source_sha256':__import__('hashlib').sha256(raw).hexdigest(),'topics':[{'id':'t1','text':'التفسير [غير واضح]'}],
                'reviewed':False,'warnings':[],'transcription_models':[{'model':'local-vision','digest':'digest'}],
                'image_proof':{'page':1,'image_sha256':'test'},
                'visual_spatial_proof':{'source_sha256':'test','selection':{'origin':'pixel_table_region'}}}
        with patch('scripts.equivalency.image_input.extract_image_pdf',return_value=result) as image, \
             patch('scripts.equivalency.image_input.verify_pdf_topics') as native:
            value=self.processor.process(task(payload={'has_pdf':True,'input_kind':'image','original_filename':'المقرر.png'}),cloud)
        image.assert_called_once();native.assert_not_called()
        self.assertEqual(value['source_media_kind'],'image');self.assertEqual(value['original_filename'],'المقرر.png')
        self.assertFalse(value['reviewed']);self.assertEqual(value['transcription_models'],result['transcription_models'])
        self.assertEqual(value['visual_spatial_proof'],result['visual_spatial_proof'])

    def test_visually_approved_identical_pdf_reuses_evidence_without_model_calls(self):
        self.catalog['courses'][0]['reference_quality']={'state':'ready_visual','source_sha256':self.course['source_sha256'],'topics_sha256':topics_digest(self.course['topics'])}
        (self.root/'equivalency-pilot/catalog.json').write_text(json.dumps(self.catalog,ensure_ascii=False))
        self.processor=worker.Processor(self.root)
        with patch('scripts.cloud_worker.extract_cached') as extraction, \
             patch('scripts.equivalency.image_input.verify_pdf_topics') as verify:
            value=self.processor.process(task(payload={'has_pdf':True}),Mock(input_pdf=Mock(return_value=self.pdf.read_bytes())))
        extraction.assert_not_called();verify.assert_not_called()
        self.assertEqual(value['visual_verification']['method'],'verified_reference_sha256')
        self.assertEqual(value['topics'],self.course['topics']);self.assertFalse(value['reviewed'])

    def test_automatic_report_has_no_claim_of_user_confirmation(self):
        with patch('scripts.cloud_worker.compare',return_value=self.exact_evidence()):
            report=self.processor.process(self.compare_task(review_mode='automatic'),Mock())
        self.assertEqual(report['reviewed_by'],'automated_pipeline');self.assertFalse(report['human_reviewed'])

    def test_invalid_ids_ownership_and_input_paths_are_rejected_before_file_access(self):
        for key, replacement in (('id', '../outside'), ('session_id', '../outside'), ('lease_token', 'invalid')):
            malformed = self.compare_task()
            malformed[key] = replacement
            with self.subTest(key=key), self.assertRaises(worker.TaskInputError):
                self.processor.process(malformed, Mock())
        wrong_owner = self.compare_task()
        wrong_owner['payload']['source']['session_id'] = '2' * 64
        with self.assertRaisesRegex(worker.TaskInputError, 'جلسة'):
            self.processor.process(wrong_owner, Mock())
        for source_id, source_path in (('../outside', None), (self.source['id'], '/etc/passwd')):
            payload = self.compare_task()
            payload['payload']['source']['id'] = source_id
            if source_path:
                payload['payload']['source']['source_pdf'] = source_path
            cloud = Mock()
            with self.assertRaises(worker.TaskInputError):
                self.processor.process(payload, cloud)
            cloud.input_pdf.assert_not_called()

    def test_session_symlink_cannot_create_a_directory_outside_private_runtime(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.processor.runtime / 'sessions').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(worker.TaskInputError):
            self.processor.session_directory(SESSION)
        self.assertEqual(list(outside.iterdir()), [])

    def test_two_pdf_sessions_isolate_sources_extraction_and_private_contents(self):
        raws = [pdf_bytes('session one'), pdf_bytes('session two')]
        cloud = Mock(input_pdf=Mock(side_effect=raws))
        def extracted(path, cache, vision):
            marker = 'بيانات المستفيد الأول' if path.read_bytes() == raws[0] else 'بيانات المستفيد الثاني'
            return {'source_sha256': worker.digest(path), 'topics': [{'id': 't1', 'text': marker}], 'warnings': [], 'local_path': '/Users/private/never-expose'}
        tasks = [task(payload={'has_pdf': True, 'name': 'مقرر أ'}), task(payload={'has_pdf': True, 'name': 'مقرر ب'}, key='b' * 32, session='2' * 64)]
        with patch('scripts.cloud_worker.extract_cached', side_effect=extracted) as extraction, \
             patch('scripts.equivalency.image_input.verify_pdf_topics', side_effect=lambda path,value,cache:value):
            first, second = [self.processor.process(item, cloud) for item in tasks]
        self.assertNotIn('المستفيد الأول', json.dumps(second, ensure_ascii=False))
        self.assertNotIn('المستفيد الثاني', json.dumps(first, ensure_ascii=False))
        self.assertNotIn('local_path', first)
        self.assertEqual(first['id'], tasks[0]['id'])
        self.assertTrue(first['upload_pdf'])
        self.assertNotEqual(extraction.call_args_list[0].args[1], extraction.call_args_list[1].args[1])
        for index, item in enumerate(tasks):
            stored = self.processor.runtime / 'sessions' / item['session_id'] / 'sources' / (item['id'] + '.pdf')
            self.assertEqual(stored.read_bytes(), raws[index])
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('source_pdf', first)

    def test_uploaded_source_can_be_rehydrated_for_compare_and_its_digest_is_verified(self):
        raw = pdf_bytes('remote source')
        uploaded = copy.deepcopy(self.source)
        uploaded.update(upload_pdf=True, source_sha256=__import__('hashlib').sha256(raw).hexdigest())
        cloud = Mock(input_pdf=Mock(return_value=raw))
        with patch('scripts.cloud_worker.compare', return_value=self.exact_evidence()):
            report = self.processor.process(self.compare_task(source=uploaded), cloud)
        self.assertEqual(report['calculation']['decision'], 'equivalent')
        self.assertEqual(report['beneficiary']['number'], '٠٠٠123')
        self.assertNotIn('source_pdf', report['source'])
        cloud.input_pdf.assert_called_once()
        wrong = copy.deepcopy(uploaded)
        wrong['source_sha256'] = '0' * 64
        with self.assertRaisesRegex(worker.TaskInputError, 'بصمة'):
            self.processor.process(self.compare_task(source=wrong), cloud)

    def test_missing_review_or_wrong_catalog_version_never_reaches_the_model(self):
        with patch('scripts.cloud_worker.compare') as comparison:
            for update in ({'reviewed': False}, {'catalog_version': 'different-version'}, {'target_id': '../../unknown'}):
                with self.subTest(update=update), self.assertRaises(worker.TaskInputError):
                    self.processor.process(self.compare_task(**update), Mock())
            comparison.assert_not_called()

    def test_catalog_and_pdf_are_verified_before_and_after_comparison(self):
        with patch('scripts.cloud_worker.compare', return_value=self.exact_evidence()) as comparison:
            report = self.processor.process(self.compare_task(), Mock())
        self.assertEqual(report['id'], 'a' * 32)
        self.assertEqual(report['target']['context'], self.course['context'])
        self.assertEqual(report['calculation']['strict_coverage'], 100)
        self.assertTrue(comparison.call_args.kwargs['cache_dir'].is_relative_to(self.processor.runtime / 'sessions' / SESSION))
        def mutate(*args, **kwargs):
            self.pdf.write_bytes(b'changed source')
            return self.exact_evidence()
        with patch('scripts.cloud_worker.compare', side_effect=mutate):
            with self.assertRaisesRegex(worker.TaskInputError, 'تغير'):
                self.processor.process(self.compare_task(), Mock())
        (self.root / 'data.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(worker.TaskInputError, 'مكتبة'):
            self.processor.process(task(payload={'topics': []}), Mock())


    def test_only_relative_catalog_assets_and_owner_safe_metadata_can_leave_the_worker(self):
        public = worker.public_source({'id': 'f' * 32, 'name': 'مقرر', 'source_note': '/Users/private/source.pdf', 'warnings': ['failed at /tmp/private.pdf'], 'input_pdf_key': 'private-key'}, catalog_pdf='assets/course.pdf')
        self.assertEqual(public['source_pdf'], 'assets/course.pdf')
        self.assertNotIn('/Users/', json.dumps(public))
        self.assertNotIn('/tmp/', json.dumps(public))
        self.assertNotIn('private-key', json.dumps(public))
        course = dict(self.course, source_pdf=str(self.pdf))
        with self.assertRaises(worker.TaskInputError):
            self.processor.course_path(course)

    def test_search_uses_reviewed_topics_and_scope_without_comparing_or_exposing_private_paths(self):
        source = copy.deepcopy(self.source)
        source['input_pdf_key'] = 'private-blob-key'
        payload = {'source_id': source['id'], 'source': source, 'topics': [{'id': 's1', 'text': 'الموضوع بعد المراجعة'}], 'all_contexts': True, 'limit': 8}
        with patch('scripts.cloud_worker.search', return_value={'candidates': [self.course]}) as retrieval, patch('scripts.cloud_worker.compare') as comparison:
            result = self.processor.process(task('search', payload), Mock())
        self.assertEqual(result['candidates'][0]['id'], self.course['id'])
        self.assertEqual(retrieval.call_args.kwargs['all_contexts'], True)
        self.assertEqual(retrieval.call_args.kwargs['limit'], 8)
        self.assertEqual(retrieval.call_args.args[1]['topics'][0]['corrected_text'], 'الموضوع بعد المراجعة')
        self.assertNotIn('input_pdf_key', retrieval.call_args.args[1])
        comparison.assert_not_called()


class RecoveryTests(WorkerFixture):
    def fake_processor(self, result=None):
        return SimpleNamespace(catalog=self.catalog, process=Mock(return_value=result or {'id': 'a' * 32, 'value': 'completed'}))

    def test_worker_lanes_have_disjoint_ids_runtime_locks_and_checkpoint_ownership(self):
        processor = self.fake_processor()
        interactive = worker.Worker(CONFIG, self.root, processor=processor, lane='interactive')
        compare = worker.Worker(CONFIG, self.root, processor=processor, lane='compare')
        legacy = worker.Worker(CONFIG, self.root, processor=processor)
        self.assertEqual(interactive.config.worker_id, 'test-mac-interactive')
        self.assertEqual(compare.config.worker_id, 'test-mac-compare')
        self.assertEqual(legacy.config.worker_id, 'test-mac')
        self.assertEqual(interactive.runtime, self.root.resolve() / 'runtime/cloud-worker-interactive')
        self.assertEqual(compare.runtime, legacy.runtime)
        self.assertNotEqual(interactive.runtime, compare.runtime)
        interactive.checkpoints.save(task(), self.catalog['catalog_id'], {}, None)
        self.assertEqual(list(compare.checkpoints.pending()), [])
        long_config = worker.Config(CONFIG.cloud_url, TOKEN, 'x' * 64)
        first = worker.Worker(long_config, self.root, lane='interactive', processor=processor)
        second = worker.Worker(worker.Config(CONFIG.cloud_url, TOKEN, 'x' * 63 + 'y'), self.root, lane='interactive', processor=processor)
        self.assertLessEqual(len(first.config.worker_id), 64)
        self.assertNotEqual(first.config.worker_id, second.config.worker_id)

    def test_interactive_worker_rejects_comparison_before_processor_or_heartbeat(self):
        cloud = Mock()
        processor = self.fake_processor()
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=processor, lane='interactive')
        with self.assertRaises(worker.TaskInputError):
            instance.process_task(self.compare_task())
        processor.process.assert_not_called()
        cloud.heartbeat.assert_not_called()
        cloud.complete.assert_not_called()
        self.assertEqual(list(instance.checkpoints.directory.glob('*.json')), [])
        self.assertTrue(instance.process_task(task('search')))
        processor.process.assert_called_once()

    def saved_comparison(self, key='a' * 32):
        claimed = self.compare_task()
        claimed['id'] = key
        with patch('scripts.cloud_worker.compare', return_value=self.exact_evidence()):
            report = self.processor.process(claimed, Mock())
        instance = worker.Worker(CONFIG, self.root, cloud=Mock(), processor=self.processor)
        saved = instance.checkpoints.save(claimed, self.catalog['catalog_id'], report, None)
        return instance, claimed, saved

    def test_current_comparison_checkpoint_retries_ack_without_regrading_or_inference(self):
        instance, claimed, saved = self.saved_comparison()
        original = copy.deepcopy(saved['result'])
        with patch.object(self.processor, 'process', side_effect=AssertionError('no inference on recovery')):
            self.assertTrue(instance.process_task(claimed))
        self.assertEqual(instance.cloud.complete.call_args.kwargs, {'result': original, 'error': None})
        self.assertEqual(saved['result'], original)

    def test_real_processor_rejects_obsolete_contradictory_or_tampered_checkpoint_before_publication(self):
        for index, variation in enumerate(('old_rubric', 'current_contradiction', 'changed_calculation', 'wrong_identity', 'missing_evidence', 'invalid_quote'), 1):
            with self.subTest(variation=variation):
                instance, claimed, saved = self.saved_comparison(key=f'{index:032x}')
                if variation == 'old_rubric':
                    saved['result']['evidence']['rubric'] = 'topic-alignment-80-60-v4-compound-and-printed-scopes'
                elif variation == 'current_contradiction':
                    saved['result']['evidence']['pairs'][0].update(score=90, reason='التغطية جزئية في الموضوع.')
                elif variation == 'changed_calculation':
                    saved['result']['calculation']['strict_coverage'] = 0
                elif variation == 'wrong_identity':
                    saved['result']['source']['id'] = '0' * 32
                elif variation == 'invalid_quote':
                    saved['result']['evidence']['pairs'][0]['source_quote'] = 'اقتباس لم يرد في المصدر'
                else:
                    del saved['result']['evidence']
                original_result = copy.deepcopy(saved['result'])
                with self.assertRaisesRegex(worker.TaskInputError, 'أعد المقارنة'):
                    self.processor.verify_checkpoint(saved, instance.cloud)
                self.assertEqual(saved['result'], original_result)
                worker.atomic_json(instance.checkpoints.path(claimed['id']), saved)
                with patch.object(self.processor, 'process', side_effect=AssertionError('no automatic regrading')):
                    self.assertTrue(instance.process_task(claimed))
                completed = instance.cloud.complete.call_args.kwargs
                self.assertIsNone(completed['result'])
                self.assertIn('أعد المقارنة', completed['error'])
                self.assertNotIn('v4', completed['error'])
                self.assertNotIn('جزئية', completed['error'])
                self.assertEqual(json.loads(instance.checkpoints.path(claimed['id']).read_text())['state'], 'acknowledged')

    def test_unresolved_comparison_checkpoint_keeps_insufficient_instead_of_becoming_a_rejection(self):
        instance, claimed, saved = self.saved_comparison()
        saved['result']['source']['topics'] = [{'id': 's1', 'text': 'أخرى'}]
        saved['result']['evidence'] = {'rubric': worker.RUBRIC_VERSION, 'provider': 'unresolved_source', 'pairs': []}
        saved['result']['calculation'] = worker.calculate(saved['result']['target']['topics'], saved['result']['source']['topics'], [])
        self.assertEqual(saved['result']['calculation']['decision'], 'insufficient')
        original = copy.deepcopy(saved)
        self.processor.verify_checkpoint(saved, instance.cloud)
        self.assertEqual(saved, original)
        # An empty response for known topics cannot masquerade as insufficient.
        saved['result']['source']['topics'] = copy.deepcopy(self.source['topics'])
        with self.assertRaisesRegex(worker.TaskInputError, 'أعد المقارنة'):
            self.processor.verify_checkpoint(saved, instance.cloud)

    def test_empty_source_or_target_checkpoint_can_legitimately_be_insufficient(self):
        for empty_side in ('source', 'target'):
            with self.subTest(empty_side=empty_side):
                instance, claimed, saved = self.saved_comparison()
                saved['result'][empty_side]['topics'] = []
                saved['result']['evidence'] = {'rubric': worker.RUBRIC_VERSION, 'provider': 'none', 'pairs': []}
                saved['result']['calculation'] = worker.calculate(saved['result']['target']['topics'], saved['result']['source']['topics'], [])
                self.assertEqual(saved['result']['calculation']['decision'], 'insufficient')
                worker.atomic_json(instance.checkpoints.path(claimed['id']), saved)
                with patch.object(self.processor, 'process', side_effect=AssertionError('no inference on valid insufficient recovery')):
                    self.assertTrue(instance.process_task(claimed))
                self.assertIsNone(instance.cloud.complete.call_args.kwargs['error'])
                self.assertEqual(instance.cloud.complete.call_args.kwargs['result'], saved['result'])

    def test_real_source_and_search_recovery_do_not_require_comparison_metadata(self):
        for kind in ('source', 'search'):
            with self.subTest(kind=kind):
                payload = {'topics': copy.deepcopy(self.source['topics'])} if kind == 'source' else {'source_id': self.source['id'], 'source': copy.deepcopy(self.source), 'all_contexts': True}
                claimed = task(kind, payload)
                with patch('scripts.cloud_worker.search', return_value={'candidates': [self.course]}):
                    result = self.processor.process(claimed, Mock())
                self.assertNotIn('evidence', result)
                instance = worker.Worker(CONFIG, self.root, cloud=Mock(), processor=self.processor)
                instance.checkpoints.save(claimed, self.catalog['catalog_id'], result, None)
                with patch.object(self.processor, 'process', side_effect=AssertionError('no inference on source/search recovery')):
                    instance.recover_pending()
                self.assertEqual(instance.cloud.complete.call_args.kwargs, {'result': result, 'error': None})

    def test_acknowledged_comparison_history_is_not_revalidated_or_overwritten_on_recovery(self):
        instance, claimed, saved = self.saved_comparison()
        saved['result']['evidence']['rubric'] = 'old-historical-rubric'
        instance.checkpoints.mark(saved, 'acknowledged')
        path = instance.checkpoints.path(claimed['id'])
        previous = path.read_bytes()
        with patch.object(self.processor, 'verify_checkpoint', side_effect=AssertionError('history is immutable')):
            instance.recover_pending()
        instance.cloud.complete.assert_not_called()
        self.assertEqual(path.read_bytes(), previous)

    def test_checkpoint_retries_lost_ack_after_restart_without_repeating_inference(self):
        cloud = Mock()
        cloud.complete.side_effect = [worker.CloudError(), {}]
        processor = self.fake_processor()
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=processor)
        with self.assertRaises(worker.CloudError):
            instance.process_task(task())
        saved = json.loads(instance.checkpoints.path('a' * 32).read_text())
        self.assertEqual(saved['state'], 'ready')
        self.assertEqual(instance.checkpoints.path('a' * 32).stat().st_mode & 0o777, 0o600)
        fresh_processor = self.fake_processor()
        restarted = worker.Worker(CONFIG, self.root, cloud=cloud, processor=fresh_processor)
        restarted.recover_pending()
        fresh_processor.process.assert_not_called()
        self.assertEqual(cloud.complete.call_count, 2)
        self.assertEqual(cloud.complete.call_args_list[0].kwargs, cloud.complete.call_args_list[1].kwargs)
        self.assertEqual(json.loads(instance.checkpoints.path('a' * 32).read_text())['state'], 'acknowledged')

    def test_a_reclaimed_lease_reuses_the_result_but_never_reuses_the_stale_token(self):
        cloud = Mock()
        cloud.complete.side_effect = [worker.LeaseLost(409), {}]
        processor = self.fake_processor()
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=processor)
        self.assertFalse(instance.process_task(task()))
        renewed = task(lease='b' * 48)
        self.assertTrue(instance.process_task(renewed))
        self.assertEqual(processor.process.call_count, 1)
        self.assertEqual(cloud.complete.call_args.args[0]['lease_token'], 'b' * 48)

    def test_a_rejected_result_becomes_a_safe_task_error_instead_of_blocking_the_queue(self):
        cloud = Mock()
        cloud.complete.side_effect = [worker.CloudError(400), {}]
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=self.fake_processor())
        self.assertTrue(instance.process_task(task()))
        self.assertEqual(cloud.complete.call_count, 2)
        self.assertIsNone(cloud.complete.call_args.kwargs['result'])
        self.assertIn('تعذر حفظ', cloud.complete.call_args.kwargs['error'])
        self.assertEqual(json.loads(instance.checkpoints.path('a' * 32).read_text())['state'], 'acknowledged')

    def test_changed_task_input_cannot_reuse_an_unrelated_checkpoint(self):
        cloud = Mock()
        cloud.complete.side_effect = [worker.LeaseLost(409), {}]
        processor = self.fake_processor()
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=processor)
        instance.process_task(task(payload={'name': 'original'}))
        instance.process_task(task(payload={'name': 'new input'}, lease='b' * 48))
        self.assertEqual(processor.process.call_count, 2)

    def test_lease_is_heartbeated_while_a_task_runs(self):
        cloud = Mock()
        processor = self.fake_processor()
        processor.process.side_effect = lambda *_: (time.sleep(0.045) or {'id': 'a' * 32})
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=processor, heartbeat_interval=0.01)
        self.assertTrue(instance.process_task(task()))
        self.assertGreaterEqual(cloud.heartbeat.call_count, 2)
        self.assertEqual(cloud.complete.call_count, 1)

    def test_lease_loss_does_not_publish_results_and_errors_do_not_leak_private_details(self):
        cloud = Mock()
        cloud.heartbeat.side_effect = worker.LeaseLost(409)
        processor = self.fake_processor()
        processor.process.side_effect = lambda *_: (time.sleep(0.04) or {'id': 'a' * 32})
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=processor, heartbeat_interval=0.01)
        self.assertFalse(instance.process_task(task()))
        cloud.complete.assert_not_called()
        self.assertEqual(json.loads(instance.checkpoints.path('a' * 32).read_text())['state'], 'waiting_for_lease')
        cloud = Mock()
        processor = self.fake_processor()
        processor.process.side_effect = ValueError('/Users/private/person ' + TOKEN)
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=processor)
        with self.assertLogs(worker.LOGGER, level='INFO') as captured:
            instance.process_task(task(key='b' * 32))
        self.assertNotIn(TOKEN, cloud.complete.call_args.kwargs['error'])
        self.assertNotIn('/Users/', cloud.complete.call_args.kwargs['error'])
        self.assertNotIn(TOKEN, '\n'.join(captured.output))


    def test_polling_backs_off_on_transport_failure_without_busy_looping(self):
        class Stop:
            def __init__(self):
                self.delays = []
            def is_set(self):
                return len(self.delays) >= 4
            def wait(self, delay):
                self.delays.append(delay)
        cloud = Mock()
        cloud.claim.side_effect = worker.CloudError()
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=self.fake_processor())
        stop = Stop()
        with self.assertLogs(worker.LOGGER, level='WARNING'), patch('scripts.cloud_worker.time.monotonic', return_value=5):
            instance.run(stop=stop, poll=4)
        self.assertEqual(stop.delays, [8, 16, 32, 60])
        self.assertEqual(cloud.claim.call_count, 4)
        cloud.idle_heartbeat.assert_called_once()

    def test_first_heartbeat_and_expiry_cleanup_do_not_depend_on_machine_uptime(self):
        cloud = Mock();cloud.claim.return_value = None
        instance = worker.Worker(CONFIG, self.root, cloud=cloud, processor=self.fake_processor())
        expired = instance.checkpoints.directory / ('8' * 32 + '.json')
        expired.write_text('{}')
        old = time.time() - 86_401
        os.utime(expired,(old,old))
        with patch('scripts.cloud_worker.time.monotonic', return_value=0):
            self.assertFalse(instance.run_once())
            self.assertFalse(instance.run_once())
        self.assertFalse(expired.exists())
        cloud.idle_heartbeat.assert_called_once()

    def test_expired_local_private_files_are_pruned_without_touching_recent_or_unrelated_files(self):
        instance = worker.Worker(CONFIG, self.root, cloud=Mock(), processor=self.fake_processor())
        expired = instance.runtime / 'sessions' / ('3' * 64)
        recent = instance.runtime / 'sessions' / ('4' * 64)
        unrelated = instance.runtime / 'sessions' / 'user-supplied-folder'
        for directory in (expired, recent, unrelated):
            directory.mkdir(parents=True)
            (directory / 'source.pdf').write_bytes(b'private file')
        checkpoint = instance.checkpoints.path('c' * 32)
        worker.atomic_json(checkpoint, {'private': 'expired test data'})
        old = time.time() - 90_000
        for path in (expired / 'source.pdf', expired, unrelated / 'source.pdf', unrelated, checkpoint):
            os.utime(path, (old, old))
        instance.last_prune = -10_000
        instance.prune_expired()
        self.assertFalse(expired.exists())
        self.assertFalse(checkpoint.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())

    def test_active_session_prunes_old_files_and_empty_directories_without_following_symlinks(self):
        instance = worker.Worker(CONFIG, self.root, cloud=Mock(), processor=self.fake_processor())
        session = instance.runtime / 'sessions' / ('5' * 64)
        sources, cache = session / 'sources', session / 'extraction' / 'old-cache'
        sources.mkdir(parents=True)
        cache.mkdir(parents=True)
        old_pdf, old_cache, recent = sources / 'old.pdf', cache / 'topics.json', sources / 'recent.pdf'
        old_pdf.write_bytes(b'old private PDF')
        old_cache.write_text('old private extraction')
        recent.write_bytes(b'recent private PDF')
        outside = self.root / 'outside-retention'
        outside.mkdir()
        outside_file = outside / 'must-stay.pdf'
        outside_file.write_bytes(b'outside private PDF')
        file_link, directory_link = sources / 'linked.pdf', session / 'linked-directory'
        file_link.symlink_to(outside_file)
        directory_link.symlink_to(outside, target_is_directory=True)
        session_link = instance.runtime / 'sessions' / ('6' * 64)
        session_link.symlink_to(outside, target_is_directory=True)
        special = session / 'pipe'
        os.mkfifo(special)
        old = time.time() - 90_000
        for path in (old_pdf, old_cache, cache, outside_file, outside, special):
            os.utime(path, (old, old))
        os.utime(file_link, (old, old), follow_symlinks=False)
        os.utime(directory_link, (old, old), follow_symlinks=False)
        instance.last_prune = -10_000
        instance.prune_expired()
        self.assertFalse(old_pdf.exists())
        self.assertFalse(old_cache.exists())
        self.assertFalse((session / 'extraction').exists())
        self.assertEqual(recent.read_bytes(), b'recent private PDF')
        self.assertEqual(outside_file.read_bytes(), b'outside private PDF')
        self.assertTrue(file_link.is_symlink())
        self.assertTrue(directory_link.is_symlink())
        self.assertTrue(session_link.is_symlink())
        self.assertTrue(special.exists())
        self.assertTrue(session.exists())

    def test_a_second_worker_in_the_same_runtime_is_rejected(self):
        path = self.processor.runtime / 'worker.lock'
        with worker.singleton_lock(path):
            with self.assertRaisesRegex(RuntimeError, 'another_worker'):
                with worker.singleton_lock(path):
                    self.fail('second worker acquired the lock')


class InstallerTests(unittest.TestCase):
    def installer(self):
        spec = importlib.util.spec_from_file_location('coursematch_installer', Path(__file__).resolve().parents[1] / 'scripts/install-worker-macos.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_launchagent_keeps_unicode_paths_as_arguments_without_secrets_or_shell(self):
        module = self.installer()
        with tempfile.TemporaryDirectory(prefix='worker with spaces ') as directory:
            root = Path(directory) / 'منصة المعادلات'
            python = root / '.venv/bin/python'
            agent = module.launch_agent(root, python, root / '.env.worker')
            self.assertEqual(agent['ProgramArguments'][:2], ['/usr/bin/caffeinate', '-i'])
            self.assertIn(str(root.resolve() / 'scripts/cloud_worker.py'), agent['ProgramArguments'])
            self.assertNotIn(TOKEN, plistlib.dumps(agent).decode())
            self.assertNotIn('COURSEMATCH_WORKER_TOKEN', agent['EnvironmentVariables'])
            self.assertTrue(agent['KeepAlive'])
            self.assertTrue(agent['RunAtLoad'])
            runner = Mock()
            destination = Path(directory) / 'LaunchAgents/org.coursematch.worker.plist'
            module.install(agent, destination, runner=runner)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(plistlib.loads(destination.read_bytes()), agent)
            self.assertEqual(runner.call_args.args[0], ['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(destination)])
            self.assertNotIn('shell', runner.call_args.kwargs)

class ProtocolTests(WorkerFixture):
    @unittest.skipUnless(__import__('shutil').which('node'), 'Node is required for the cross-language API protocol test')
    def test_python_worker_completes_the_real_node_api_queue_protocol(self):
        self.run_node_api_queue_protocol(CONFIG)

    @unittest.skipUnless(__import__('shutil').which('node'), 'Node is required for the cross-language API protocol test')
    def test_python_worker_completes_the_host_gated_local_node_api_queue_protocol(self):
        self.run_node_api_queue_protocol(LOCAL_CONFIG)

    def run_node_api_queue_protocol(self, config):
        import base64
        import subprocess
        code = r'''
import {createApi} from './netlify/lib/api.mjs';
import {MemoryRepository,MemoryPdfs} from './tests-cloud/helpers.mjs';
import {createLocalServer} from './scripts/local-service.mjs';
import {createInterface} from 'node:readline';
import http from 'node:http';
let api,server;
for await(const line of createInterface({input:process.stdin,crlfDelay:Infinity})) {
  const data=JSON.parse(line);
  if(data.init){
    api=createApi({repository:new MemoryRepository(),pdfs:new MemoryPdfs(),catalog:data.catalog,env:{CONTEXT:'production',COURSEMATCH_SESSION_SECRET:'test-session-'+'s'.repeat(40),COURSEMATCH_WORKER_TOKEN:data.token},logger:{error(){}}});
    if(data.local){server=createLocalServer({api,origin:data.origin,port:8800});await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));}
    process.stdout.write(JSON.stringify({ready:true})+'\n');continue;
  }
  let value;
  if(server){
    const url=new URL(data.url);
    value=await new Promise((resolve,reject)=>{
      // A temporary socket avoids occupying 8800; preserve the real worker Host.
      const request=http.request({hostname:'127.0.0.1',port:server.address().port,path:url.pathname,method:data.method,headers:{Host:url.host,...data.headers}},response=>{
        const chunks=[];response.on('data',chunk=>chunks.push(chunk));response.on('end',()=>resolve({status:response.statusCode,headers:response.headers,base64:Buffer.concat(chunks).toString('base64')}));
      });request.on('error',reject);request.end(data.body);
    });
  }else{
    const response=await api(new Request(data.url,{method:data.method,headers:data.headers,...(data.body!==null?{body:data.body}:{})}));
    value={status:response.status,headers:Object.fromEntries(response.headers),base64:Buffer.from(await response.arrayBuffer()).toString('base64')};
  }
  process.stdout.write(JSON.stringify(value)+'\n');
}
if(server)await new Promise(resolve=>server.close(resolve));
'''
        project = Path(__file__).resolve().parents[1]
        process = subprocess.Popen(['node', '--input-type=module', '-e', code], cwd=project, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def close():
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(close)
        def exchange(value):
            process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
            process.stdin.flush()
            line = process.stdout.readline()
            self.assertTrue(line, 'Node API bridge stopped before replying')
            return json.loads(line)
        self.assertTrue(exchange({'init': True, 'catalog': self.catalog, 'token': TOKEN, 'local': bool(config.local_url), 'origin': config.cloud_url})['ready'])
        if config.local_url:
            blocked = {'url': config.cloud_url + '/api/worker/claim', 'method': 'POST', 'headers': {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'}, 'body': '{"worker_id":"test-mac"}'}
            self.assertEqual(exchange(blocked)['status'], 403)
            blocked['url'] = config.local_url + '/api/worker/claim'
            blocked['headers']['Origin'] = config.cloud_url
            self.assertEqual(exchange(blocked)['status'], 403)
            del blocked['headers']['Origin']
            del blocked['headers']['Authorization']
            self.assertEqual(exchange(blocked)['status'], 403)
        class Reply(io.BytesIO):
            def __init__(self, raw, url):
                super().__init__(raw)
                self.url = url
            def geturl(self):
                return self.url
        class Opener:
            def open(inner, request, timeout):
                value = exchange({'url': request.full_url, 'method': request.method, 'headers': dict(request.header_items()), 'body': request.data.decode() if request.data is not None else None})
                raw = base64.b64decode(value['base64'])
                if value['status'] >= 400:
                    raise urllib.error.HTTPError(request.full_url, value['status'], 'protocol rejection', value['headers'], io.BytesIO(raw))
                return Reply(raw, request.full_url)
        instance = worker.Worker(config, self.root, cloud=worker.CloudClient(config, Opener()), processor=self.processor)
        def browser(route, body=None, cookie=None, token=None):
            headers = {'Origin': config.cloud_url, 'Content-Type': 'application/json'} if body is not None else {}
            if cookie:
                headers['Cookie'] = cookie
            if token:
                headers['X-Equivalency-Token'] = token
            value = exchange({'url': config.cloud_url + route, 'method': 'POST' if body is not None else 'GET', 'headers': headers, 'body': json.dumps(body, ensure_ascii=False) if body is not None else None})
            self.assertLess(value['status'], 400, 'the API rejected a worker-compatible request')
            return json.loads(base64.b64decode(value['base64'])), value['headers']
        bootstrap, headers = browser('/api/catalog')
        cookie_header = headers['set-cookie']
        cookie, csrf = (cookie_header[0] if isinstance(cookie_header, list) else cookie_header).split(';')[0], bootstrap['token']
        created, _ = browser('/api/sources', {'name': 'مقرر بروتوكول', 'topics': self.source['topics']}, cookie, csrf)
        self.assertTrue(instance.run_once())
        source_result, _ = browser('/api/operations/' + created['operation_id'], cookie=cookie)
        source_id = source_result['result']['id']
        self.assertEqual(source_result['status'], 'done')
        created, _ = browser('/api/search', {'source_id': source_id, 'all_contexts': True}, cookie, csrf)
        with patch('scripts.cloud_worker.search', return_value={'candidates': [self.course], 'scope_count': 1, 'method': 'test'}):
            self.assertTrue(instance.run_once())
        search_result, _ = browser('/api/operations/' + created['operation_id'], cookie=cookie)
        self.assertEqual(search_result['result']['candidates'][0]['id'], self.course['id'])
        created, _ = browser('/api/jobs', {'source_id': source_id, 'target_id': self.course['id'], 'reviewed': True, 'beneficiary': {'name': 'مستفيد بروتوكول', 'number': '000123'}}, cookie, csrf)
        with patch('scripts.cloud_worker.compare', return_value=self.exact_evidence()):
            self.assertTrue(instance.run_once())
        report, _ = browser('/api/jobs/' + created['id'], cookie=cookie)
        self.assertEqual(report['status'], 'done')
        self.assertEqual(report['result']['source']['id'], source_id)
        self.assertEqual(report['result']['target']['id'], self.course['id'])
        self.assertEqual(report['result']['beneficiary']['number'], '000123')
        self.assertEqual(report['result']['calculation']['strict_coverage'], 100)


if __name__ == '__main__':
    unittest.main()

class ReferenceReadinessWorkerTests(WorkerFixture):
    def test_canonical_reference_hold_never_calls_model_and_remains_pending(self):
        original=self.processor.courses[self.course['id']]
        original['reference_review']={'comparison_readiness':'scope_incomplete','readiness_reason':'لم يحدد موضوع التطبيقات'}
        with patch('scripts.cloud_worker.compare',side_effect=AssertionError('no grading for incomplete reference')):
            result=self.processor.process(self.compare_task(),Mock())
        self.assertEqual(result['calculation']['decision'],'insufficient')
        self.assertIsNone(result['calculation']['strict_coverage'])
        self.assertEqual(result['evidence']['provider'],'reference_pending')

    def test_readiness_uses_complete_canonical_rows_before_student_cleaning(self):
        from scripts.equivalency.reference_quality import topics_digest
        original=self.processor.courses[self.course['id']]
        original['topics'][0].update(page=1,bbox=[1,2,3,4],raw_text='raw')
        original['reference_quality']={'state':'ready_visual','source_sha256':original['source_sha256'],'topics_sha256':topics_digest(original['topics'])}
        with patch('scripts.cloud_worker.compare',return_value=self.exact_evidence()) as comparison:
            result=self.processor.process(self.compare_task(),Mock())
        comparison.assert_called_once();self.assertEqual(result['calculation']['decision'],'equivalent')

    def test_catalog_source_cannot_strip_its_canonical_hold(self):
        original=copy.deepcopy(self.course);original['id']='d'*20
        original['reference_review']={'comparison_readiness':'scope_incomplete','readiness_reason':'تطبيقات غير مسماة'}
        self.processor.sources[original['id']]=original
        supplied=copy.deepcopy(self.source);supplied.update(catalog_course_id=original['id'],source_sha256=original['source_sha256'])
        supplied['reference_review']={'comparison_readiness':'ready'}
        with patch('scripts.cloud_worker.compare',side_effect=AssertionError('source hold was bypassed')):
            result=self.processor.process(self.compare_task(source=supplied),Mock())
        self.assertEqual(result['evidence']['reference_review'],{'source':'تطبيقات غير مسماة'})
        self.assertEqual(result['calculation']['decision'],'insufficient')

    def test_ollama_proposal_without_second_pass_cannot_be_published(self):
        evidence=self.exact_evidence();evidence['provider']='ollama_local'
        with patch('scripts.cloud_worker.compare',return_value=evidence):
            with self.assertRaises(worker.TaskInputError):self.processor.process(self.compare_task(),Mock())

class UploadedTargetFixture(WorkerFixture):
    def uploaded_task(self, target_topics=None):
        raw=pdf_bytes('uploaded target fixture')
        target={'id':'e'*32,'name':'المقرر الهدف المرفوع','topics':copy.deepcopy(target_topics or self.course['topics']),
                'upload_pdf':True,'source_sha256':__import__('hashlib').sha256(raw).hexdigest(),'warnings':[]}
        value=self.compare_task(target_mode='uploaded',target_source_id=target['id'],target=target,
                                target_topics=[{'id':t['id'],'text':t.get('corrected_text') or t['text']} for t in target['topics']],review_mode='automatic')
        value['payload'].pop('target_id')
        return value,raw


class UploadedTargetTests(UploadedTargetFixture):
    def test_uploaded_target_is_rehydrated_by_role_and_report_keeps_private_identity(self):
        value,raw=self.uploaded_task();cloud=Mock(input_pdf=Mock(return_value=raw))
        with patch('scripts.cloud_worker.compare',return_value=self.exact_evidence()) as compare:
            report=self.processor.process(value,cloud)
        cloud.input_pdf.assert_called_once_with(value,role='target')
        self.assertEqual(report['target']['id'],'e'*32);self.assertTrue(report['target']['upload_pdf'])
        self.assertNotIn('source_pdf',report['target']);self.assertNotIn('context',report['target'])
        self.assertEqual(report['target_mode'],'uploaded');self.assertFalse(report['human_reviewed'])
        self.assertEqual(report['calculation']['decision'],'equivalent');compare.assert_called_once()

    def test_target_denominator_is_uploaded_target_count_not_source_count(self):
        first='تفسير البقرة (1-5) تفسيرا تحليليا.'
        topics=[{'id':'t1','text':first},{'id':'t2','text':'تفسير البقرة (6-10) تفسيرا تحليليا.'}]
        value,raw=self.uploaded_task(topics);value['payload']['source']['topics']=[{'id':'s1','text':first}]
        with patch('scripts.equivalency.engine.post_ollama',side_effect=AssertionError('atomic fixture must not infer')):
            report=self.processor.process(value,Mock(input_pdf=Mock(return_value=raw)))
        self.assertEqual(report['evidence']['provider'],'curriculum_exact')
        self.assertEqual(report['calculation']['total_topics'],2)
        self.assertEqual(report['calculation']['strict_count'],1)
        self.assertEqual(report['calculation']['strict_coverage'],50)
        self.assertEqual(report['calculation']['decision'],'not_equivalent')

    def test_unresolved_uploaded_target_stops_judgment_without_inference(self):
        value,raw=self.uploaded_task([{'id':'t1','text':'تعريف العلم [غير واضح]'}])
        with patch('scripts.equivalency.engine.post_ollama',side_effect=AssertionError('no inference for unresolved target')):
            report=self.processor.process(value,Mock(input_pdf=Mock(return_value=raw)))
        self.assertEqual(report['calculation']['decision'],'insufficient')

    def test_uploaded_target_rejects_forged_topics_digest_session_and_catalog_path(self):
        value,raw=self.uploaded_task()
        variations=[]
        bad=copy.deepcopy(value);bad['payload']['target_topics'][0]['text']='نص هدف بديل';variations.append(bad)
        bad=copy.deepcopy(value);bad['payload']['target']['source_sha256']='0'*64;variations.append(bad)
        bad=copy.deepcopy(value);bad['payload']['target']['session_id']='9'*64;variations.append(bad)
        bad=copy.deepcopy(value);bad['payload']['target']['source_pdf']='/tmp/foreign.pdf';variations.append(bad)
        bad=copy.deepcopy(value);bad['payload']['target_id']=self.course['id'];variations.append(bad)
        for bad in variations:
            with self.subTest(payload=bad['payload'].get('target_topics')),patch('scripts.cloud_worker.compare') as compare:
                with self.assertRaises(worker.TaskInputError):self.processor.process(bad,Mock(input_pdf=Mock(return_value=raw)))
                compare.assert_not_called()

    def test_uploads_on_both_sides_fetch_the_correct_role_and_verify_both_hashes(self):
        value,target_raw=self.uploaded_task();source_raw=pdf_bytes('original uploaded source')
        value['payload']['source'].update(upload_pdf=True,source_sha256=__import__('hashlib').sha256(source_raw).hexdigest())
        cloud=Mock(input_pdf=Mock(side_effect=lambda task,**kwargs:target_raw if kwargs.get('role')=='target' else source_raw))
        with patch('scripts.cloud_worker.compare',return_value=self.exact_evidence()):report=self.processor.process(value,cloud)
        self.assertEqual(cloud.input_pdf.call_args_list,[call(value),call(value,role='target')])
        self.assertEqual(report['source']['source_sha256'],__import__('hashlib').sha256(source_raw).hexdigest())
        self.assertEqual(report['target']['source_sha256'],__import__('hashlib').sha256(target_raw).hexdigest())

    def test_uploaded_target_file_change_during_comparison_is_rejected(self):
        value,raw=self.uploaded_task();cloud=Mock(input_pdf=Mock(return_value=raw))
        def changed(*args,**kwargs):
            (self.processor.session_directory(SESSION)/'sources'/('e'*32+'.pdf')).write_bytes(b'%PDF-changed')
            return self.exact_evidence()
        with patch('scripts.cloud_worker.compare',side_effect=changed):
            with self.assertRaisesRegex(worker.TaskInputError,'الهدف'):self.processor.process(value,cloud)

    def test_uploaded_target_checkpoint_validates_immutable_target_without_regrading(self):
        value,raw=self.uploaded_task();cloud=Mock(input_pdf=Mock(return_value=raw))
        with patch('scripts.cloud_worker.compare',return_value=self.exact_evidence()):report=self.processor.process(value,cloud)
        checkpoint={**value,'result':report,'catalog_id':self.catalog['catalog_id']}
        with patch('scripts.cloud_worker.compare',side_effect=AssertionError('checkpoint cannot regrade')):
            self.processor.verify_checkpoint(checkpoint,cloud)
            for mutate in ('topics','sha','mode'):
                bad=copy.deepcopy(checkpoint)
                if mutate=='topics':bad['result']['target']['topics'][0]['text']='تغيير المقام'
                elif mutate=='sha':bad['result']['target']['source_sha256']='0'*64
                else:bad['result']['target_mode']='catalog'
                with self.subTest(mutate=mutate),self.assertRaises(worker.TaskInputError):self.processor.verify_checkpoint(bad,cloud)

class UploadedTargetReadinessTests(UploadedTargetFixture):
    def register_target_reference(self,raw,*,held=False):
        self.pdf.write_bytes(raw);self.course['source_sha256']=worker.digest(self.pdf)
        self.course['reference_quality']={'state':'ready_visual','source_sha256':self.course['source_sha256'],'topics_sha256':topics_digest(self.course['topics'])}
        if held:self.course['reference_review']={'comparison_readiness':'scope_incomplete','readiness_reason':'مفردات المرجع غير مكتملة في الدليل'}
        (self.root/'equivalency-pilot/catalog.json').write_text(json.dumps(self.catalog,ensure_ascii=False));self.processor=worker.Processor(self.root)

    def test_uploading_a_known_held_reference_cannot_bypass_its_hold(self):
        value,raw=self.uploaded_task();self.register_target_reference(raw,held=True)
        with patch('scripts.cloud_worker.compare',side_effect=AssertionError('held reference cannot infer')):
            report=self.processor.process(value,Mock(input_pdf=Mock(return_value=raw)))
        self.assertEqual(report['calculation']['decision'],'insufficient');self.assertIn('target',report['calculation']['reference_review'])
        self.assertNotIn('context',report['target'])

    def test_known_ready_target_with_different_row_count_is_held(self):
        value,raw=self.uploaded_task(self.course['topics']+[{'id':'t2','text':'صف إضافي مختلف'}]);self.register_target_reference(raw)
        with patch('scripts.cloud_worker.compare',side_effect=AssertionError('mismatched denominator cannot infer')):
            report=self.processor.process(value,Mock(input_pdf=Mock(return_value=raw)))
        self.assertEqual(report['calculation']['decision'],'insufficient')

    def test_unknown_target_with_explicit_extraction_holes_is_held(self):
        value,raw=self.uploaded_task();metadata={**value['payload']['target'],'table_evidence':[{'empty_rows':[2]}]}
        value['payload']['target']['extraction_integrity']=worker.extraction_integrity(metadata)
        with patch('scripts.cloud_worker.compare',side_effect=AssertionError('partial target cannot infer')):
            report=self.processor.process(value,Mock(input_pdf=Mock(return_value=raw)))
        self.assertEqual(report['calculation']['decision'],'insufficient')


class TargetPdfTransportTests(unittest.TestCase):
    def test_target_pdf_uses_leased_target_endpoint_and_rejects_invalid_roles(self):
        data=pdf_bytes('target transport fixture');opener=SimpleNamespace(open=Mock(return_value=io.BytesIO(data)));client=worker.CloudClient(CONFIG,opener);value=task('compare')
        self.assertEqual(client.input_pdf(value,role='target'),data)
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith('/input-target-pdf'))
        opener.open.reset_mock()
        with self.assertRaises(ValueError):client.input_pdf(value,role='../source')
        opener.open.assert_not_called()

class AuditIncompleteWorkerTests(UploadedTargetFixture):
    def typed_failure(self,stage='facet_review'):
        return worker.EvidenceValidationError('/private/raw-response.json secret model output',stage=stage,target_ids=['t1'])

    def test_only_typed_evidence_failure_issues_bound_private_artifact_study_without_grades(self):
        for stage in ['proposal','facet_review']:
            value,target_pdf=self.uploaded_task();source_pdf=pdf_bytes('uploaded original fixture')
            value['payload']['source'].update(upload_pdf=True,source_sha256=__import__('hashlib').sha256(source_pdf).hexdigest())
            cloud=Mock(input_pdf=Mock(side_effect=lambda task,role='source':target_pdf if role=='target' else source_pdf))
            before=copy.deepcopy(value)
            with patch('scripts.cloud_worker.compare',side_effect=self.typed_failure(stage)):
                report=self.processor.process(value,cloud)
            self.assertEqual(report['calculation']['decision'],'insufficient')
            self.assertIsNone(report['calculation']['strict_coverage']);self.assertIsNone(report['calculation']['lenient_coverage'])
            self.assertEqual(report['evidence'],{'provider':'audit_incomplete','rubric':worker.RUBRIC_VERSION,'pairs':[],
                                                'evidence_review':{'stage':stage,'target_ids':['t1']}})
            for key in ['source','target']:
                self.assertEqual(report[key]['source_sha256'],value['payload'][key]['source_sha256'])
            self.assertEqual(value,before);self.assertFalse(report['human_reviewed'])
            self.assertNotIn('/private/',json.dumps(report));self.assertNotIn('secret',json.dumps(report))

    def test_unrelated_runtime_file_and_network_failures_never_become_academic_reports(self):
        for index,error in enumerate([RuntimeError('private bug'),OSError('/private/path'),urllib.error.URLError('private network failure')],1):
            with self.subTest(error=type(error).__name__),patch('scripts.cloud_worker.compare',side_effect=error):
                claimed=self.compare_task();claimed['id']=f'{index:032x}';cloud=Mock();instance=worker.Worker(CONFIG,self.root,cloud=cloud,processor=self.processor)
                self.assertTrue(instance.process_task(claimed))
                reply=cloud.complete.call_args.kwargs;self.assertIsNone(reply['result']);self.assertTrue(reply['error'])
                self.assertNotIn('private',reply['error'])

    def saved_hold(self):
        claimed=self.compare_task(review_mode='automatic')
        with patch('scripts.cloud_worker.compare',side_effect=self.typed_failure()):
            result=self.processor.process(claimed,Mock())
        instance=worker.Worker(CONFIG,self.root,cloud=Mock(),processor=self.processor)
        return instance,claimed,instance.checkpoints.save(claimed,self.catalog['catalog_id'],result,None)

    def test_formal_incomplete_checkpoint_retries_ack_without_model_or_mutation(self):
        instance,claimed,saved=self.saved_hold();before=copy.deepcopy(saved)
        self.processor.verify_checkpoint(saved,instance.cloud)
        with patch.object(self.processor,'process',side_effect=AssertionError('no model on valid checkpoint')):
            self.assertTrue(instance.process_task(claimed))
        self.assertEqual(instance.cloud.complete.call_args.kwargs,{'result':before['result'],'error':None})
        self.assertEqual(saved,before)

    def test_formal_checkpoint_rejects_forged_grade_metadata_input_or_new_reference_hold(self):
        instance,claimed,saved=self.saved_hold()
        for mutation in ['grade','pairs','stage','foreign_id','extra_private_key','source_text','target_text','source_hash','target_hash','reference_hold']:
            bad=copy.deepcopy(saved);report=bad['result'];e=report['evidence']
            if mutation=='grade':report['calculation'].update(decision='equivalent',strict_coverage=100)
            elif mutation=='pairs':e['pairs']=[{'target_id':'t1','source_id':'s1','score':100}]
            elif mutation=='stage':e['evidence_review']['stage']='network'
            elif mutation=='foreign_id':e['evidence_review']['target_ids']=['not-current']
            elif mutation=='extra_private_key':e['raw_path']='/private/raw.json'
            elif mutation=='source_text':report['source']['topics'][0]['text']='Different private source'
            elif mutation=='target_text':report['target']['topics'][0]['text']='Different target'
            elif mutation=='source_hash':report['source']['source_sha256']='0'*64
            elif mutation=='target_hash':report['target']['source_sha256']='0'*64
            else:self.processor.courses[self.course['id']]['reference_review']={'comparison_readiness':'scope_incomplete'}
            with self.subTest(mutation=mutation),self.assertRaises(worker.TaskInputError):
                self.processor.verify_checkpoint(bad,instance.cloud)
            self.processor.courses[self.course['id']].pop('reference_review',None)

    def test_reference_or_visual_hold_has_precedence_over_model_audit_failure(self):
        value=self.compare_task();value['payload']['source'].update(upload_pdf=True,visual_verification_status='unavailable',source_sha256=worker.digest(self.pdf))
        with patch('scripts.cloud_worker.compare',side_effect=AssertionError('unready source must not compare')) as model:
            report=self.processor.process(value,Mock(input_pdf=Mock(return_value=self.pdf.read_bytes())))
        model.assert_not_called();self.assertEqual(report['evidence']['provider'],'reference_pending')
        self.assertEqual(report['calculation']['decision'],'insufficient')
