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
from unittest.mock import Mock, patch

import pymupdf
from scripts import cloud_worker as worker


TOKEN = 'test_token_' + 'x' * 40
CONFIG = worker.Config('https://coursematch.example', TOKEN, 'test-mac')
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
        return {'provider': 'exact_text', 'pairs': [{'target_id': 't1', 'source_id': 's1', 'score': 100, 'reason': 'تتطابق المفردتان في الموضوع والنطاق.',
                                                   'target_quote': 'تعريف العلم ونشأته', 'source_quote': 'تعريف العلم ونشأته'}]}


class TransportTests(unittest.TestCase):
    def test_only_https_origin_is_allowed_and_config_repr_hides_token(self):
        for origin in ('http://example.com', 'file:///tmp/test', 'https://user:pass@example.com', 'https://example.com/path', 'https://example.com?token=bad'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                worker.Config(origin, TOKEN, 'mac')
        self.assertNotIn(TOKEN, repr(CONFIG))

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
        with patch('scripts.cloud_worker.extract_cached', side_effect=extracted) as extraction:
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
        with self.assertLogs(worker.LOGGER, level='WARNING'):
            instance.run(stop=stop, poll=4)
        self.assertEqual(stop.delays, [8, 16, 32, 60])
        self.assertEqual(cloud.claim.call_count, 4)
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
        import base64
        import subprocess
        import sys
        code = r'''
import {createApi} from './netlify/lib/api.mjs';
import {MemoryRepository,MemoryPdfs} from './tests-cloud/helpers.mjs';
import {createInterface} from 'node:readline';
let api;
for await(const line of createInterface({input:process.stdin,crlfDelay:Infinity})) {
  const data=JSON.parse(line);
  if(data.init){api=createApi({repository:new MemoryRepository(),pdfs:new MemoryPdfs(),catalog:data.catalog,env:{CONTEXT:'production',COURSEMATCH_SESSION_SECRET:'test-session-'+'s'.repeat(40),COURSEMATCH_WORKER_TOKEN:data.token},logger:{error(){}}});process.stdout.write(JSON.stringify({ready:true})+'\n');continue;}
  const response=await api(new Request(data.url,{method:data.method,headers:data.headers,...(data.body!==null?{body:data.body}:{})}));
  process.stdout.write(JSON.stringify({status:response.status,headers:Object.fromEntries(response.headers),base64:Buffer.from(await response.arrayBuffer()).toString('base64')})+'\n');
}
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
        self.assertTrue(exchange({'init': True, 'catalog': self.catalog, 'token': TOKEN})['ready'])
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
        instance = worker.Worker(CONFIG, self.root, cloud=worker.CloudClient(CONFIG, Opener()), processor=self.processor)
        def browser(route, body=None, cookie=None, token=None):
            headers = {'Origin': CONFIG.cloud_url, 'Content-Type': 'application/json'} if body is not None else {}
            if cookie:
                headers['Cookie'] = cookie
            if token:
                headers['X-Equivalency-Token'] = token
            value = exchange({'url': CONFIG.cloud_url + route, 'method': 'POST' if body is not None else 'GET', 'headers': headers, 'body': json.dumps(body, ensure_ascii=False) if body is not None else None})
            self.assertLess(value['status'], 400, 'the API rejected a worker-compatible request')
            return json.loads(base64.b64decode(value['base64'])), value['headers']
        bootstrap, headers = browser('/api/catalog')
        cookie, csrf = headers['set-cookie'].split(';')[0], bootstrap['token']
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
