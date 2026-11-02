#!/usr/bin/env python3
"""Render or explicitly install the CourseMatch per-user service supervisor."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.service_supervisor import executable, load_config, preflight
from scripts.cloud_worker import private_directory

LABEL = 'org.coursematch.service'


def launch_agent(root, python, config_path):
    root, python, config_path = Path(root).resolve(), Path(python).absolute(), Path(config_path).absolute()
    logs = root / 'runtime/service/launchd'
    return {'Label': LABEL, 'ProgramArguments': [str(python), '-u', str(root / 'scripts/service_supervisor.py'), '--config', str(config_path)],
            'WorkingDirectory': str(root), 'KeepAlive': True, 'RunAtLoad': True, 'ThrottleInterval': 15, 'ProcessType': 'Background',
            'StandardOutPath': str(logs / 'supervisor.log'), 'StandardErrorPath': str(logs / 'supervisor.stderr.log'),
            'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'PYTHONUNBUFFERED': '1'}}


def install(agent, destination, runner=subprocess.run):
    destination = Path(destination)
    domain = f'gui/{os.getuid()}'
    old_worker = runner(['/bin/launchctl', 'print', domain + '/org.coursematch.worker'], check=False, capture_output=True)
    if old_worker.returncode == 0:
        raise RuntimeError('standalone_worker_launchagent_must_be_stopped_first')
    private_directory(Path(agent['StandardOutPath']).parent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        plistlib.dump(agent, stream)
    temporary.replace(destination)
    destination.chmod(0o600)
    runner(['/bin/launchctl', 'bootout', domain + '/' + LABEL], check=False, capture_output=True)
    runner(['/bin/launchctl', 'bootstrap', domain, str(destination)], check=True, capture_output=True)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description='CourseMatch Mac hosting; dry-run is the default')
    parser.add_argument('--python', type=Path, help='Defaults to python_executable in the validated service config')
    parser.add_argument('--config', type=Path, default=ROOT / 'runtime/service/config.json')
    parser.add_argument('--launchagents-dir', type=Path, default=Path.home() / 'Library/LaunchAgents')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        preflight(ROOT, config)
        python = args.python or Path(config['python_executable'])
        agent = launch_agent(ROOT, python, args.config)
        if not args.install:
            print(plistlib.dumps(agent).decode())
            return 0
        if sys.platform != 'darwin':
            raise RuntimeError('macos_required')
        executable(str(python.absolute()))
        install(agent, args.launchagents_dir.absolute() / (LABEL + '.plist'))
        print(json.dumps({'installed': True, 'label': LABEL}))
        return 0
    except Exception as error:
        print('service_install_failed type=' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
