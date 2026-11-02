"""Public allowlist, independent process ownership and Mac service configuration."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import service_supervisor as service


class HostingFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.dist = self.root / 'dist'
        self.dist.mkdir()
        (self.root / 'equivalency-pilot').mkdir()
        (self.root / 'assets/course-specifications').mkdir(parents=True)
        self.pdf = 'assets/course-specifications/source.pdf'
        (self.root / self.pdf).write_bytes(b'%PDF-sample-public-source')
        self.catalog = {'catalog_id': 'test-version', 'courses': [{'id': 'a' * 20, 'source_pdf': self.pdf}], 'library_sources': []}
        for name, text in [('index.html', '<html>CourseMatch</html>'), ('app.js', 'window.CourseMatch=true;'), ('style.css', 'body{color:#123}')]:
            (self.root / 'equivalency-pilot' / name).write_text(text)
            (self.dist / name).write_text(text)
        catalog_text = json.dumps(self.catalog)
        (self.root / 'equivalency-pilot/catalog.json').write_text(catalog_text)
        (self.dist / 'catalog.json').write_text(catalog_text)
        (self.dist / '404.html').write_text('<h1>404</h1>')
        (self.dist / 'build-info.json').write_text(json.dumps({'project': 'CourseMatch', 'catalog_id': self.catalog['catalog_id'], 'commit': 'test-commit'}))
        (self.dist / self.pdf).parent.mkdir(parents=True)
        (self.dist / self.pdf).write_bytes((self.root / self.pdf).read_bytes())
        (self.root / 'netlify.toml').write_text('[build]\npublish="dist"\n')
        self.config = {'public_origin': 'https://coursematch.example', 'site_id': '12345678-1234-1234-1234-123456789abc', 'node_executable': '/bin/echo', 'python_executable': '/bin/echo', 'cloudflared_executable': '/bin/echo', 'netlify_command': ['/bin/echo', 'netlify-cli'], 'port': 8800}

    def supervisor(self):
        return service.ServiceSupervisor(self.config, self.root, popen=Mock())


class PublishGuardTests(HostingFixture):
    def test_exact_public_package_and_routes_are_accepted(self):
        routes = service.render_redirects('https://example-words.trycloudflare.com')
        (self.dist / '_redirects').write_text(routes)
        result = service.validate_publish(self.root, routes)
        self.assertEqual(result['public_files'], 8)
        self.assertEqual(result['pdf_files'], 1)
        self.assertFalse(result['private_files_included'])
        self.assertFalse(result['functions_included'])
        self.assertEqual(routes.splitlines()[0], '/api/worker/* /404.html 404!')
        self.assertEqual(len(routes.splitlines()), 4)
        self.assertIn('/pdf/* https://example-words.trycloudflare.com/pdf/:splat 200', routes)

    def test_routes_reject_injected_or_foreign_origins_and_worker_exposure(self):
        for value in ('http://words.trycloudflare.com', 'https://words.trycloudflare.com.evil.test', 'https://evil.test', 'https://user@words.trycloudflare.com', 'https://words.trycloudflare.com/path', 'https://words.trycloudflare.com\n/api/* https://evil.test 200'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.render_redirects(value)
        routes = service.render_redirects('https://words.trycloudflare.com').replace('/api/worker/* /404.html 404!', '/api/worker/* https://words.trycloudflare.com/api/worker/:splat 200')
        (self.dist / '_redirects').write_text(routes)
        with self.assertRaisesRegex(ValueError, 'unsafe_proxy'):
            service.validate_publish(self.root)

    def test_extra_private_file_symlink_and_serverless_function_stop_publication(self):
        forbidden = self.dist / '.env.worker'
        forbidden.write_text('PRIVATE_VALUE')
        with self.assertRaisesRegex(ValueError, 'unexpected_public'):
            service.validate_publish(self.root)
        forbidden.unlink()
        link = self.dist / 'runtime'
        link.symlink_to(self.root / 'equivalency-pilot', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            service.validate_publish(self.root)
        link.unlink()
        functions = self.root / 'netlify/functions'
        functions.mkdir(parents=True)
        (functions / 'old-api.mjs').write_text('private code')
        with self.assertRaisesRegex(ValueError, 'serverless_functions'):
            service.validate_publish(self.root)

    def test_stale_ui_catalog_or_pdf_bytes_stop_publication(self):
        for path in ('app.js', 'catalog.json', self.pdf):
            original = (self.dist / path).read_bytes()
            (self.dist / path).write_bytes(b'changed source')
            with self.subTest(path=path), self.assertRaises(ValueError):
                service.validate_publish(self.root)
            (self.dist / path).write_bytes(original)

    def test_guard_failure_precedes_any_deploy_process(self):
        instance = self.supervisor()
        (self.dist / 'private.py').write_text('private source')
        with self.assertRaises(ValueError):
            instance.deploy('https://words.trycloudflare.com')
        instance.popen.assert_not_called()


class ConfigurationTests(HostingFixture):
    def test_private_config_allows_safe_command_array_but_no_protected_port_or_secret_flags(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config))
        path.chmod(0o600)
        self.assertEqual(service.load_config(path)['port'], 8800)
        for update in ({'port': 8794}, {'port': 8773}, {'public_origin': 'http://insecure.test'}, {'netlify_command': ['/bin/echo', '--auth', 'SECRET']}, {'worker_token': 'not-allowed-here'}):
            changed = {**self.config, **update}
            path.write_text(json.dumps(changed))
            with self.subTest(update=update), self.assertRaises(ValueError):
                service.load_config(path)
        path.write_text(json.dumps(self.config))
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'owner_only'):
            service.load_config(path)

    def test_health_requires_our_live_child_and_the_coursematch_identity(self):
        instance = self.supervisor()
        self.assertFalse(instance.healthy())
        instance.children['api'] = SimpleNamespace(poll=lambda: None)
        for content, expected in ((b'{"ok":true,"service":"OtherApp"}', False), (b'{"ok":false,"service":"CourseMatch"}', False), (b'not-json', False), (b'{"ok":true,"service":"CourseMatch"}', True)):
            response = io.BytesIO(content)
            response.status = 200
            with self.subTest(content=content), patch('scripts.service_supervisor.urllib.request.urlopen', return_value=response) as call:
                self.assertEqual(instance.healthy(), expected)
                request = call.call_args.args[0]
                self.assertEqual(request.full_url, 'http://127.0.0.1:8800/healthz')
                self.assertEqual(request.get_header('Host'), 'coursematch.example')


class ProcessTests(HostingFixture):
    def active_children(self, instance):
        for index, name in enumerate(('awake', 'api', 'worker', 'tunnel')):
            process = SimpleNamespace(pid=100 + index, returncode=None)
            process.poll = lambda p=process: p.returncode
            instance.children[name] = process
        instance.active_tunnel = 'https://active.trycloudflare.com'
        instance.last_deploy_id = '0123456789abcdef01234567'
        instance.api_healthy = True
        instance.healthy = Mock(return_value=True)

    def test_core_api_respawn_updates_status_pid_without_restarting_worker_or_tunnel(self):
        instance = self.supervisor()
        self.active_children(instance)
        instance.status('ready')
        old_api = instance.children['api']
        unchanged = {name: instance.children[name] for name in ('worker', 'tunnel', 'awake')}
        old_api.returncode = 1
        new_api = SimpleNamespace(pid=999, poll=lambda: None)
        instance.stop_child = Mock(side_effect=lambda name: instance.children.pop(name, None))
        def spawn(name, command):
            self.assertEqual(name, 'api')
            instance.children[name] = new_api
            return new_api
        instance.spawn = Mock(side_effect=spawn)
        instance.maintain_core()
        saved = json.loads((instance.runtime / 'status.json').read_text())
        self.assertEqual(saved['pids']['api'], 999)
        self.assertEqual(saved['state'], 'ready')
        self.assertEqual(saved['tunnel_url'], instance.active_tunnel)
        self.assertEqual(saved['deploy_id'], instance.last_deploy_id)
        for name, process in unchanged.items():
            self.assertIs(instance.children[name], process)
            self.assertEqual(saved['pids'][name], process.pid)
        instance.spawn.assert_called_once()

    def test_periodic_status_heartbeat_reports_recovery_and_ready_without_pid_changes(self):
        instance = self.supervisor()
        self.active_children(instance)
        with patch('scripts.service_supervisor.time.monotonic', return_value=100), patch('scripts.service_supervisor.time.time', return_value=1000):
            instance.status('ready')
        original_pids = json.loads((instance.runtime / 'status.json').read_text())['pids']
        instance.healthy.return_value = False
        with patch('scripts.service_supervisor.time.monotonic', return_value=116), patch('scripts.service_supervisor.time.time', return_value=1016):
            instance.maintain_core()
        saved = json.loads((instance.runtime / 'status.json').read_text())
        self.assertEqual(saved['state'], 'recovering')
        self.assertEqual(saved['at'], 1016)
        self.assertEqual(saved['pids'], original_pids)
        instance.healthy.return_value = True
        with patch('scripts.service_supervisor.time.monotonic', return_value=122), patch('scripts.service_supervisor.time.time', return_value=1022):
            instance.maintain_core()
        self.assertEqual(json.loads((instance.runtime / 'status.json').read_text())['state'], 'ready')
        with patch('scripts.service_supervisor.time.monotonic', return_value=138), patch('scripts.service_supervisor.time.time', return_value=1038):
            instance.maintain_core()
        saved = json.loads((instance.runtime / 'status.json').read_text())
        self.assertEqual(saved['at'], 1038)
        self.assertEqual(saved['state'], 'ready')
        self.assertEqual(saved['pids'], original_pids)

    def test_status_heartbeat_keeps_the_deploying_phase_while_routes_are_being_published(self):
        instance = self.supervisor()
        self.active_children(instance)
        instance.children['deploy'] = SimpleNamespace(pid=500, poll=lambda: None)
        with patch('scripts.service_supervisor.time.monotonic', return_value=100):
            instance.status('deploying_route', tunnel_url='https://replacement.trycloudflare.com')
        with patch('scripts.service_supervisor.time.monotonic', return_value=116):
            instance.maintain_core()
        saved = json.loads((instance.runtime / 'status.json').read_text())
        self.assertEqual(saved['state'], 'deploying_route')
        self.assertEqual(saved['tunnel_url'], 'https://replacement.trycloudflare.com')
        self.assertEqual(saved['pids']['deploy'], 500)

    def test_tunnel_reconnect_keeps_api_worker_and_awake_processes_intact(self):
        instance = self.supervisor()
        core = {name: SimpleNamespace(pid=100 + i, poll=lambda: None) for i, name in enumerate(('api', 'worker', 'awake'))}
        instance.children.update(core)
        old_tunnel = SimpleNamespace(pid=200, poll=lambda: None)
        instance.children['tunnel'] = old_tunnel
        instance.stop_child = Mock(side_effect=lambda name: instance.children.pop(name, None))
        current = SimpleNamespace(pid=201, stdout=io.BytesIO(b''), poll=lambda: None)
        def spawn(name, command, pipe=False):
            instance.children[name] = current
            return current
        instance.spawn = Mock(side_effect=spawn)
        instance.start_tunnel()
        self.assertEqual(instance.stop_child.call_args.args[0], 'tunnel')
        for name in core:
            self.assertIs(instance.children[name], core[name])
        command = instance.spawn.call_args.args[1]
        self.assertIn('http://127.0.0.1:8800', command)
        index = command.index('--http-host-header')
        self.assertEqual(command[index + 1], 'coursematch.example')

    def test_only_process_handles_started_by_the_supervisor_are_stopped(self):
        instance = self.supervisor()
        process = SimpleNamespace(pid=54321, poll=lambda: None, wait=Mock(), terminate=Mock())
        instance.children['api'] = process
        with patch('scripts.service_supervisor.os.getpgid', return_value=54321), patch('scripts.service_supervisor.os.killpg') as kill:
            instance.stop_child('api')
            kill.assert_called_once_with(54321, service.signal.SIGTERM)
        self.assertNotIn('api', instance.children)
        self.assertEqual(process.wait.call_count, 1)
        with patch('scripts.service_supervisor.os.killpg') as kill:
            instance.stop_child('unrelated-service')
            kill.assert_not_called()

    def test_deploy_uses_array_arguments_from_project_root_and_preserves_header_configuration(self):
        instance = self.supervisor()
        process = SimpleNamespace(pid=123, poll=lambda: 0, returncode=0)
        def spawn(name, command):
            (instance.runtime / 'deploy.log').write_text('{"deploy_id":"0123456789abcdef01234567"}')
            instance.logs['deploy'] = (instance.runtime / 'deploy.log').open('ab')
            return process
        instance.spawn = Mock(side_effect=spawn)
        instance.deploy('https://words.trycloudflare.com')
        command = instance.spawn.call_args.args[1]
        self.assertEqual(command[:2], self.config['netlify_command'])
        self.assertIn('--no-build', command)
        self.assertIn('--prod', command)
        self.assertNotIn('--auth', command)
        self.assertEqual(command[command.index('--dir') + 1], str(self.dist))
        self.assertEqual((instance.runtime / 'active-redirects').read_text(), (self.dist / '_redirects').read_text())
        self.assertEqual(json.loads((instance.runtime / 'status.json').read_text())['state'], 'ready')

    def test_stale_tunnel_stdout_cannot_republish_a_replaced_connection(self):
        instance = self.supervisor()
        instance.children['tunnel'] = SimpleNamespace(pid=202, poll=lambda: None)
        instance.maintain_core = Mock()
        instance.tunnel_events.put((201, 'https://old.trycloudflare.com'))
        instance.deploy = Mock()
        instance.cycle()
        instance.deploy.assert_not_called()


class InstallerTests(HostingFixture):
    def module(self):
        spec = importlib.util.spec_from_file_location('coursematch_service_installer', Path(__file__).resolve().parents[1] / 'scripts/install-service-macos.py')
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        return result

    def test_launchagent_has_only_paths_and_own_label_and_keeps_spaces_in_arguments(self):
        module = self.module()
        root = self.root / 'منصة مع مسافات'
        agent = module.launch_agent(root, root / '.venv/bin/python', root / 'runtime/service/config.json')
        self.assertEqual(agent['Label'], 'org.coursematch.service')
        self.assertTrue(agent['KeepAlive'])
        self.assertTrue(agent['RunAtLoad'])
        self.assertEqual(agent['WorkingDirectory'], str(root))
        self.assertIn(str(root / 'scripts/service_supervisor.py'), agent['ProgramArguments'])
        self.assertEqual(set(agent['EnvironmentVariables']), {'PATH', 'PYTHONUNBUFFERED'})
        runner = Mock(return_value=SimpleNamespace(returncode=1))
        destination = self.root / 'LaunchAgents/org.coursematch.service.plist'
        module.install(agent, destination, runner=runner)
        self.assertEqual(plistlib.loads(destination.read_bytes()), agent)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(runner.call_args.args[0][-1], str(destination))
        self.assertNotIn('shell', runner.call_args.kwargs)

    def test_installer_refuses_to_run_alongside_the_old_worker_launchagent(self):
        module = self.module()
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        agent = module.launch_agent(self.root, '/bin/echo', self.root / 'config.json')
        with self.assertRaisesRegex(RuntimeError, 'standalone_worker'):
            module.install(agent, self.root / 'LaunchAgents/org.coursematch.service.plist', runner=runner)
        self.assertEqual(runner.call_count, 1)

    def test_installer_recovers_the_observed_launchd_replacement_race(self):
        module = self.module()
        agent = module.launch_agent(self.root, '/bin/echo', self.root / 'config.json')
        runner = Mock(side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0),
                                   module.subprocess.CalledProcessError(5, ['launchctl', 'bootstrap']), SimpleNamespace(returncode=0)])
        with patch.object(module.time, 'sleep') as pause:
            module.install(agent, self.root / 'LaunchAgents/org.coursematch.service.plist', runner=runner)
        pause.assert_called_once_with(1)
        self.assertEqual(runner.call_count, 4)
        self.assertEqual(runner.call_args_list[-1].args[0], runner.call_args_list[-2].args[0])

    def test_installer_does_not_retry_permanent_bootstrap_errors(self):
        module = self.module()
        agent = module.launch_agent(self.root, '/bin/echo', self.root / 'config.json')
        runner = Mock(side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0),
                                   module.subprocess.CalledProcessError(13, ['launchctl', 'bootstrap'])])
        with patch.object(module.time, 'sleep') as pause, self.assertRaises(module.subprocess.CalledProcessError):
            module.install(agent, self.root / 'LaunchAgents/org.coursematch.service.plist', runner=runner)
        pause.assert_not_called()

    def test_installer_defaults_to_the_validated_host_interpreter(self):
        import io
        module = self.module()
        output = io.StringIO()
        with patch.object(module, 'load_config', return_value={'python_executable':'/bin/echo'}), patch.object(module, 'preflight'), patch('sys.stdout', output):
            self.assertEqual(module.main(['--dry-run']), 0)
        self.assertEqual(plistlib.loads(output.getvalue().encode())['ProgramArguments'][0], '/bin/echo')


if __name__ == '__main__':
    unittest.main()
