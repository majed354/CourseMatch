#!/usr/bin/env python3
"""Render or explicitly install a per-user CourseMatch LaunchAgent."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.cloud_worker import Config, private_directory


LABEL = 'org.coursematch.worker'


def launch_agent(root, python, env_file, label=LABEL):
    root, python, env_file = Path(root).resolve(), Path(python).absolute(), Path(env_file).absolute()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{2,100}', label):
        raise ValueError('invalid_launch_agent_label')
    logs = root / 'runtime/cloud-worker/logs'
    return {'Label': label, 'ProgramArguments': ['/usr/bin/caffeinate', '-i', str(python), '-u', str(root / 'scripts/cloud_worker.py'), '--env-file', str(env_file)],
            'WorkingDirectory': str(root), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15, 'ProcessType': 'Background',
            'StandardOutPath': str(logs / 'worker.stdout.log'), 'StandardErrorPath': str(logs / 'worker.stderr.log'),
            'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'PYTHONUNBUFFERED': '1'}}


def install(agent, destination, *, runner=subprocess.run):
    destination = Path(destination)
    private_directory(Path(agent['StandardOutPath']).parent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        plistlib.dump(agent, stream)
    temporary.replace(destination)
    destination.chmod(0o600)
    domain = f'gui/{os.getuid()}'
    # bootout may report "not loaded" on the first installation. No shell is used.
    runner(['/bin/launchctl', 'bootout', domain + '/' + agent['Label']], check=False, capture_output=True)
    runner(['/bin/launchctl', 'bootstrap', domain, str(destination)], check=True, capture_output=True)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description='CourseMatch user LaunchAgent; dry-run is the default')
    parser.add_argument('--python', type=Path, default=ROOT / '.venv/bin/python')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env.worker')
    parser.add_argument('--label', default=LABEL)
    parser.add_argument('--launchagents-dir', type=Path, default=Path.home() / 'Library/LaunchAgents')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--install', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        agent = launch_agent(ROOT, arguments.python, arguments.env_file, arguments.label)
        destination = arguments.launchagents_dir.absolute() / (arguments.label + '.plist')
        if not arguments.install:
            print(plistlib.dumps(agent).decode())
            return 0
        if sys.platform != 'darwin' or not arguments.python.is_file() or not os.access(arguments.python, os.X_OK):
            raise ValueError('macos_and_executable_python_required')
        Config.from_file(arguments.env_file)
        install(agent, destination)
        print(json.dumps({'installed': True, 'label': arguments.label}, ensure_ascii=False))
    except Exception as error:
        # launchctl errors can contain paths; environment content must never print.
        print('worker_install_failed type=' + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
