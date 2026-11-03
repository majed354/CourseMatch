#!/usr/bin/env python3
"""Keep CourseMatch's own Mac processes alive and publish a static tunnel route."""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.cloud_worker import atomic_json, private_directory, singleton_lock
from scripts.public_catalog import matches_public_catalog

LOG = logging.getLogger('coursematch.service')
TUNNEL = re.compile(r'https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com')
CONFIG_KEYS = {'public_origin','site_id','node_executable','python_executable','cloudflared_executable','netlify_command','port'}
MODEL_CONFIG_KEYS = {'ollama_executable', 'ollama_port'}
PROTECTED_MODEL_PORTS = {11434, 11435, 11436, 8773, 8794, 8800}


def checked_origin(value):
    if not isinstance(value, str):
        raise ValueError('invalid_public_origin')
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('public_origin_requires_https')
    return value.rstrip('/')


def checked_tunnel(value):
    if not isinstance(value, str) or not TUNNEL.fullmatch(value):
        raise ValueError('invalid_quick_tunnel_origin')
    return value


def render_redirects(tunnel):
    tunnel = checked_tunnel(tunnel)
    return ('/api/worker/* /404.html 404!\n'
            f'/api/* {tunnel}/api/:splat 200\n'
            f'/pdf/* {tunnel}/pdf/:splat 200\n'
            f'/healthz {tunnel}/healthz 200\n')


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def executable(path):
    if not isinstance(path, str) or not Path(path).is_absolute() or not Path(path).is_file() or not os.access(path, os.X_OK):
        raise ValueError('absolute_executable_required')
    return path


def private_file(path):
    path = Path(path)
    details = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError('owner_only_service_configuration_required')


def load_config(path):
    private_file(path)
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or not CONFIG_KEYS <= set(value) or set(value) - CONFIG_KEYS - MODEL_CONFIG_KEYS:
        raise ValueError('invalid_service_config_keys')
    model_keys = set(value) & MODEL_CONFIG_KEYS
    if model_keys and model_keys != MODEL_CONFIG_KEYS:
        raise ValueError('ollama_executable_and_port_required_together')
    if model_keys:
        executable(value['ollama_executable'])
        model_port = value['ollama_port']
        if type(model_port) is not int or not 1024 <= model_port <= 65535 or model_port in PROTECTED_MODEL_PORTS:
            raise ValueError('invalid_private_ollama_port')
    value['public_origin'] = checked_origin(value['public_origin'])
    if value['port'] != 8800 or type(value['port']) is not int:
        raise ValueError('coursematch_requires_independent_port_8800')
    if not isinstance(value['site_id'], str) or not re.fullmatch(r'[a-f0-9-]{36}', value['site_id']):
        raise ValueError('invalid_netlify_site_id')
    for key in ('node_executable','python_executable','cloudflared_executable'):
        executable(value[key])
    command = value['netlify_command']
    if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part or any(ord(c) < 32 for c in part) for part in command):
        raise ValueError('invalid_netlify_command')
    executable(command[0])
    if any(re.match(r'--(?:auth|token|password|secret)(?:=|$)', part, re.I) for part in command):
        raise ValueError('secrets_must_not_be_command_arguments')
    return value


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_publish(root=ROOT, expected_redirects=None):
    """Exact public allowlist, projected catalog and asset verification before deploy."""
    root = Path(root).resolve()
    public = root / 'dist'
    config = tomllib.loads((root / 'netlify.toml').read_text())
    if config.get('build', {}).get('publish') != 'dist':
        raise ValueError('publish_directory_must_be_dist')
    function_directory = config.get('functions', {}).get('directory', 'netlify/functions')
    if not isinstance(function_directory, str):
        raise ValueError('functions_configuration_invalid')
    functions = root / function_directory
    if functions.exists() and any(path.is_file() for path in functions.rglob('*')):
        raise ValueError('serverless_functions_are_not_part_of_mac_hosting')
    catalog = json.loads((root / 'equivalency-pilot/catalog.json').read_text())
    assets = {c['source_pdf'] for c in catalog['courses'] + catalog.get('library_sources', []) if c.get('source_pdf')}
    for name in assets:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or not name.startswith(('assets/course-specifications/', 'assets/course-uploads/')) or path.suffix.lower() != '.pdf':
            raise ValueError('invalid_public_pdf_path')
    fonts = {'fonts/IBMPlexSansArabic-Regular.ttf','fonts/IBMPlexSansArabic-SemiBold.ttf','fonts/LICENSE.txt'}
    core = {'index.html','image-upload.js','app.js','style.css','catalog.json','build-info.json','404.html'} | fonts
    allowed = assets | core | {'_redirects'}
    actual = set()
    for path in public.rglob('*'):
        if path.is_symlink():
            raise ValueError('public_symlinks_forbidden')
        if path.is_file():
            name = path.relative_to(public).as_posix()
            if name not in allowed:
                raise ValueError('unexpected_public_file')
            actual.add(name)
    if not core.issubset(actual) or not assets.issubset(actual):
        raise ValueError('public_package_incomplete')
    for name in assets:
        source = root / name
        if source.is_symlink() or not source.resolve().is_relative_to(root / 'assets') or not source.is_file() or file_hash(source) != file_hash(public / name):
            raise ValueError('published_pdf_differs_from_local_source')
    for name in ('index.html','image-upload.js','app.js','style.css'):
        if file_hash(root / 'equivalency-pilot' / name) != file_hash(public / name):
            raise ValueError('published_interface_is_stale')
    for name in fonts:
        source = root / 'equivalency-pilot' / name
        if source.is_symlink() or not source.is_file() or file_hash(source) != file_hash(public / name):
            raise ValueError('published_font_differs_from_licensed_source')
    published_catalog = json.loads((public / 'catalog.json').read_text())
    if not matches_public_catalog(catalog, published_catalog):
        raise ValueError('published_catalog_is_stale')
    info = json.loads((public / 'build-info.json').read_text())
    if info.get('project') != 'CourseMatch' or info.get('catalog_id') != catalog['catalog_id'] or not isinstance(info.get('commit'), str):
        raise ValueError('invalid_build_identity')
    if (public / '404.html').stat().st_size > 64_000:
        raise ValueError('unexpected_not_found_document')
    if expected_redirects is not None:
        if not (public / '_redirects').is_file() or (public / '_redirects').read_text() != expected_redirects:
            raise ValueError('published_proxy_routes_mismatch')
    elif (public / '_redirects').exists():
        lines = (public / '_redirects').read_text().splitlines()
        match = re.fullmatch(r'/api/\* (https://[^/]+)/api/:splat 200', lines[1]) if len(lines) == 4 else None
        if not match or render_redirects(match.group(1)) != (public / '_redirects').read_text():
            raise ValueError('unsafe_proxy_routes')
    return {'public_files': len(actual), 'pdf_files': len(assets), 'catalog_id': catalog['catalog_id'], 'private_files_included': False, 'functions_included': False}


class ServiceSupervisor:
    def __init__(self, config, root=ROOT, popen=subprocess.Popen):
        self.config, self.root, self.popen = config, Path(root).resolve(), popen
        self.runtime = private_directory(self.root / 'runtime/service')
        self.children, self.logs, self.last_start = {}, {}, {}
        self.stop_event = threading.Event()
        self.tunnel_events = queue.Queue()
        self.active_tunnel = None
        self.last_deploy_id = None
        self.last_health_check, self.health_failures = 0, 0
        self.api_healthy = False
        self.phase, self.status_fields = 'starting', {}
        self.last_status_at, self.last_status_pids = 0, {}

    def live_pids(self):
        return {key: process.pid for key, process in self.children.items() if process.poll() is None}

    def status(self, phase, *, remember=True, **fields):
        if remember:
            self.phase, self.status_fields = phase, dict(fields)
        details = {}
        if self.active_tunnel:
            details['tunnel_url'] = self.active_tunnel
        if self.last_deploy_id:
            details['deploy_id'] = self.last_deploy_id
        details.update(fields)
        if phase == 'ready':
            details.pop('error_type', None)
        pids = self.live_pids()
        atomic_json(self.runtime / 'status.json', {'state': phase, 'at': time.time(), 'site_url': self.config['public_origin'], 'pids': pids, **details})
        self.last_status_at, self.last_status_pids = time.monotonic(), pids

    def refresh_status(self):
        pids = self.live_pids()
        required = ('api', 'worker', 'interactive', 'awake') + (('models',) if 'ollama_executable' in self.config else ())
        core_ready = self.api_healthy and all(name in pids for name in required)
        if core_ready and 'deploy' in pids:
            phase = 'deploying_route'
        elif core_ready and self.phase == 'connecting' and 'tunnel' in pids:
            phase = 'connecting'
        elif core_ready and self.active_tunnel and 'tunnel' in pids:
            phase = 'ready'
        else:
            phase = 'recovering'
        # A recovery heartbeat must not forget an in-progress connection or
        # deployment phase when an API restart briefly fails its health probe.
        self.status(phase, remember=False, **self.status_fields)

    def child_environment(self, name):
        environment = {**os.environ, 'PATH': str(Path(self.config['node_executable']).parent) + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'PYTHONUNBUFFERED': '1'}
        if 'ollama_executable' in self.config:
            # The private route belongs only to our workers, even if a parent
            # process happens to carry an unrelated value in its environment.
            environment.pop('COURSEMATCH_OLLAMA_URL', None)
            if name in ('worker', 'interactive'):
                environment['COURSEMATCH_OLLAMA_URL'] = f"http://127.0.0.1:{self.config['ollama_port']}"
            if name == 'models':
                environment.update(OLLAMA_HOST=f"127.0.0.1:{self.config['ollama_port']}",
                                   OLLAMA_NO_CLOUD='1', OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='3')
        return environment

    def spawn(self, name, command, *, pipe=False):
        if name in self.children and self.children[name].poll() is None:
            return self.children[name]
        log = (self.runtime / (name + '.log')).open('wb' if name == 'deploy' else 'ab')
        os.chmod(log.name, 0o600)
        self.logs[name] = log
        stderr = subprocess.STDOUT
        if name == 'deploy':
            stderr = (self.runtime / 'deploy.stderr.log').open('ab')
            os.chmod(stderr.name, 0o600)
            self.logs[name + '_stderr'] = stderr
        process = self.popen(command, cwd=self.root, env=self.child_environment(name),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if pipe else log, stderr=stderr, start_new_session=True)
        self.children[name] = process
        self.last_start[name] = time.monotonic()
        LOG.info('child_started component=%s', name)
        return process

    def stop_child(self, name):
        process = self.children.pop(name, None)
        if process is not None and process.poll() is None:
            # Every child was started in a new session; never signal a service
            # discovered by port, process name or PID file from another project.
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(timeout=10)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        for key in (name, name + '_stderr'):
            log = self.logs.pop(key, None)
            if log is not None:
                log.close()

    def maintain_core(self):
        commands = {}
        if 'ollama_executable' in self.config:
            commands['models'] = [self.config['ollama_executable'], 'serve']
        commands.update({'awake': ['/usr/bin/caffeinate', '-i'], 'api': [self.config['node_executable'], str(self.root / 'scripts/local-service.mjs')],
                    'worker': [self.config['python_executable'], str(self.root / 'scripts/cloud_worker.py'), '--env-file', str(self.root / '.env.worker'), '--lane', 'compare'],
                    'interactive': [self.config['python_executable'], str(self.root / 'scripts/cloud_worker.py'), '--env-file', str(self.root / '.env.worker'), '--lane', 'interactive']})
        changed = False
        for name, command in commands.items():
            process = self.children.get(name)
            if process is None or process.poll() is not None:
                if time.monotonic() - self.last_start.get(name, -100) < 5:
                    continue
                self.stop_child(name)
                self.spawn(name, command)
                changed = True
        previous_health = self.api_healthy
        if changed or time.monotonic() - self.last_health_check >= 5:
            self.last_health_check = time.monotonic()
            self.api_healthy = self.healthy()
            if self.api_healthy:
                self.health_failures = 0
            else:
                self.health_failures += 1
                if self.health_failures >= 6:
                    self.stop_child('api')
                    changed = True
                    self.health_failures = 0
        if changed or self.live_pids() != self.last_status_pids or self.api_healthy != previous_health or time.monotonic() - self.last_status_at >= 15:
            self.refresh_status()

    def healthy(self):
        process = self.children.get('api')
        if process is None or process.poll() is not None:
            return False
        request = urllib.request.Request(f"http://127.0.0.1:{self.config['port']}/healthz", headers={'Host': urllib.parse.urlsplit(self.config['public_origin']).netloc})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                body = json.loads(response.read(4097))
                return response.status == 200 and isinstance(body, dict) and body.get('service') == 'CourseMatch' and body.get('ok') is True and process.poll() is None
        except Exception:
            return False

    def start_tunnel(self):
        self.stop_child('tunnel')
        atomic_text(self.runtime / 'cloudflared.yml', 'no-autoupdate: true\n')
        command = [self.config['cloudflared_executable'], '--config', str(self.runtime / 'cloudflared.yml'), 'tunnel', '--url', f"http://127.0.0.1:{self.config['port']}", '--http-host-header', urllib.parse.urlsplit(self.config['public_origin']).netloc, '--no-autoupdate', '--protocol', 'http2']
        process = self.spawn('tunnel', command, pipe=True)
        def pump():
            for raw in iter(process.stdout.readline, b''):
                text = raw.decode('utf-8', errors='replace')
                # Retain only operational event codes and a public route; no raw
                # process output or environment contents enter supervisor logs.
                for candidate in re.findall(r"""https://[^\s"'<>|]+""", text):
                    if TUNNEL.fullmatch(candidate):
                        self.tunnel_events.put((process.pid, candidate))
            LOG.info('tunnel_stream_closed')
        threading.Thread(target=pump, name='coursematch-tunnel-reader', daemon=True).start()
        self.status('connecting')

    def deploy(self, tunnel):
        routes = render_redirects(tunnel)
        atomic_text(self.runtime / 'active-redirects', routes)
        atomic_text(self.root / 'dist/_redirects', routes)
        proof = validate_publish(self.root, routes)
        atomic_json(self.runtime / 'publish-proof.json', proof)
        command = [*self.config['netlify_command'], 'deploy', '--site', self.config['site_id'], '--dir', str(self.root / 'dist'), '--no-build', '--prod', '--json']
        process = self.spawn('deploy', command)
        self.status('deploying_route', tunnel_url=tunnel)
        # Netlify may still be processing after its required upload list empties.
        # Keep core workers healthy while allowing a bounded backend publish wait.
        deadline = time.monotonic() + 600
        while process.poll() is None and not self.stop_event.wait(0.25):
            self.maintain_core()
            if time.monotonic() >= deadline:
                self.stop_child('deploy')
                raise RuntimeError('route_deploy_timeout')
        if self.stop_event.is_set():
            self.stop_child('deploy')
            return
        self.logs['deploy'].flush()
        if process.returncode != 0:
            self.stop_child('deploy')
            raise RuntimeError('route_deploy_failed')
        text = (self.runtime / 'deploy.log').read_text(errors='replace')
        try:
            result = json.loads(text[text.rfind('\n{') + 1:] if '\n{' in text else text)
        except ValueError:
            self.stop_child('deploy')
            raise RuntimeError('route_deploy_result_invalid') from None
        self.last_deploy_id = result.get('deploy_id') or result.get('id')
        if not isinstance(self.last_deploy_id, str) or not re.fullmatch(r'[a-f0-9]{24}', self.last_deploy_id):
            self.stop_child('deploy')
            raise RuntimeError('route_deploy_id_missing')
        self.stop_child('deploy')
        self.active_tunnel = tunnel
        self.status('ready', tunnel_url=tunnel, deploy_id=self.last_deploy_id)
        LOG.info('route_deployed')

    def cycle(self):
        self.maintain_core()
        tunnel = self.children.get('tunnel')
        if tunnel is None or tunnel.poll() is not None:
            if self.healthy() and time.monotonic() - self.last_start.get('tunnel', -100) >= 5:
                self.start_tunnel()
        try:
            pid, url = self.tunnel_events.get_nowait()
        except queue.Empty:
            return
        current = self.children.get('tunnel')
        if current is None or current.pid != pid or current.poll() is not None:
            return
        url = checked_tunnel(url)
        if url != self.active_tunnel:
            self.deploy(url)

    def shutdown(self):
        self.stop_event.set()
        # Workers stop before their private model server. Nothing is discovered
        # or signalled by shared process name, port, or external PID file.
        order = [name for name in self.children if name != 'models'] + (['models'] if 'models' in self.children else [])
        for name in order:
            self.stop_child(name)
        self.status('stopped')

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.cycle()
                except Exception as error:
                    self.status('retrying', error_type=type(error).__name__)
                    LOG.warning('service_retry type=%s', type(error).__name__)
                    # Reconnect only the tunnel; model work on the local API
                    # remains alive while its public proxy is repaired.
                    self.stop_child('tunnel')
                self.stop_event.wait(3)
        finally:
            self.shutdown()


def preflight(root, config):
    root = Path(root)
    for name in ('.env.service', '.env.worker'):
        private_file(root / name)
    for name in ('scripts/local-service.mjs', 'scripts/cloud_worker.py'):
        if not (root / name).is_file():
            raise ValueError('service_program_missing')
    validate_publish(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description='CourseMatch Mac service supervisor')
    parser.add_argument('--config', type=Path, default=ROOT / 'runtime/service/config.json')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    supervisor = None
    try:
        config = load_config(args.config)
        preflight(ROOT, config)
        if args.check:
            print(json.dumps({'ok': True, 'port': config['port'], 'public_origin': config['public_origin'], 'publish': validate_publish(ROOT)}, ensure_ascii=False))
            return 0
        supervisor = ServiceSupervisor(config)
        for code in (signal.SIGTERM, signal.SIGINT):
            signal.signal(code, lambda *_: supervisor.stop_event.set())
        with singleton_lock(supervisor.runtime / 'supervisor.lock'):
            supervisor.run()
    except Exception as error:
        LOG.error('service_stopped type=%s', type(error).__name__)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
