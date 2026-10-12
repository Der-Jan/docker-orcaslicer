#!/usr/bin/env python3
"""Follow Orca's saved printer selection and update its same-origin proxy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

RUNTIME = Path('/run/orcaslicer-panel')
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', '/config/.config')) / 'OrcaSlicer'
PREFIX = '/' + os.environ.get('SUBFOLDER', '/').strip('/')
PREFIX = PREFIX.rstrip('/') + '/'
TEMPLATE = Path('/defaults/printer-panel/locations.conf.template')


def selected_printer():
    try:
        settings = json.loads((CONFIG / 'OrcaSlicer.conf').read_text())
        name = settings.get('presets', {}).get('machine', '')
        connections = set()
        for path in (CONFIG / 'user').glob('*/machine/*.json'):
            try:
                profile = json.loads(path.read_text())
                if profile.get('name') == name and profile.get('print_host'):
                    connections.add((profile['print_host'], profile.get('printhost_apikey', '')))
            except (OSError, ValueError):
                continue
        if len(connections) != 1:
            return name, None, 'Im ausgewählten Orca-Profil ist keine eindeutige Druckeradresse gespeichert.'
        address, api_key = connections.pop()
        if not isinstance(api_key, str) or (api_key and not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', api_key)):
            raise ValueError('unsupported API key')
        url = urlsplit(address if '://' in address else 'http://' + address)
        if (url.scheme not in ('http', 'https') or url.username or url.password
                or not re.fullmatch(r'[a-zA-Z0-9.-]+', url.hostname or '')
                or url.path not in ('', '/') or url.query or url.fragment):
            raise ValueError('unsupported address')
        port = url.port or (443 if url.scheme == 'https' else 80)
        return name, (url.scheme, url.hostname, port, api_key), ''
    except (OSError, ValueError, TypeError, AttributeError):
        return '', None, 'Noch kein nutzbares Orca-Druckerprofil gespeichert.'


def proxy_config(target):
    if target:
        scheme, host, port, api_key = target
        authentication = f'proxy_set_header X-Api-Key "{api_key}";\n    proxy_set_header Authorization "";' if api_key else 'proxy_set_header X-Api-Key "";'
        return TEMPLATE.read_text().format(prefix=PREFIX, host=host, authentication=authentication, web=f'{scheme}://{host}:{port}')
    return f'''
location = {PREFIX}printer-panel/printer.json {{
    alias /run/orcaslicer-panel/printer.json;
    default_type application/json;
    add_header Cache-Control "no-store" always;
}}
location {PREFIX}printer-panel/device/ {{ return 503; }}
location {PREFIX}printer-panel/moonraker/ {{ return 503; }}
location {PREFIX}printer-panel/camera/ {{ return 503; }}
location /webcam/ {{ return 503; }}
'''


def atomic_write(path, contents):
    staging = path.with_suffix('.new')
    staging.write_text(contents)
    staging.chmod(0o600 if path.name == 'locations.conf' else 0o644)
    staging.replace(path)


def update():
    name, target, message = selected_printer()
    desired = proxy_config(target)
    path = RUNTIME / 'locations.conf'
    previous = path.read_text() if path.exists() else ''
    ready = bool(target)
    if desired != previous:
        atomic_write(path, desired)
        if previous:
            # Disable printer routes if a new hostname cannot resolve.
            check = subprocess.run(['nginx', '-t'], capture_output=True)
            reload = subprocess.run(['nginx', '-s', 'reload'], capture_output=True) if check.returncode == 0 else None
            if reload is None or reload.returncode:
                offline = proxy_config(None)
                atomic_write(path, offline)
                if previous != offline:
                    subprocess.run(['nginx', '-s', 'reload'], capture_output=True)
                ready = False
                message = 'Die Druckeradresse konnte nicht als Proxy eingerichtet werden.'
    revision = hashlib.sha256(json.dumps([name, target, ready, message]).encode()).hexdigest()[:16]
    metadata = json.dumps({'name': name, 'ready': ready, 'message': message, 'revision': revision}, ensure_ascii=False)
    metadata_path = RUNTIME / 'printer.json'
    if not metadata_path.exists() or metadata_path.read_text() != metadata:
        atomic_write(metadata_path, metadata)
        print('[orcaslicer-panel] Printer selection updated.' if ready else '[orcaslicer-panel] Waiting for a usable printer profile.', flush=True)


def initialize():
    if not re.fullmatch(r'/[a-zA-Z0-9/_-]*', PREFIX):
        raise SystemExit('Unsupported SUBFOLDER for printer panel')
    RUNTIME.mkdir(parents=True, exist_ok=True)
    RUNTIME.chmod(0o755)
    # Start with a safe offline proxy; the supervised watcher resolves the printer.
    atomic_write(RUNTIME / 'locations.conf', proxy_config(None))
    atomic_write(RUNTIME / 'printer.json', json.dumps({'name': '', 'ready': False, 'message': 'Druckerprofil wird geladen.', 'revision': 'startup'}))
    nginx_path = Path('/etc/nginx/sites-available/default')
    include = '  include /run/orcaslicer-panel/locations.conf;\n'
    config = nginx_path.read_text()
    if include not in config:
        nginx_path.write_text(config.replace('server {\n', 'server {\n' + include))
    web = Path('/usr/share/selkies/web')
    original = web / 'desktop.html'
    if not original.exists():
        original.write_bytes((web / 'index.html').read_bytes())
    (web / 'index.html').write_bytes(Path('/defaults/printer-panel/index.html').read_bytes())
    assets = web / 'printer-panel'
    assets.mkdir(exist_ok=True)
    for asset in ('panel.css', 'panel.js'):
        (assets / asset).write_bytes((Path('/defaults/printer-panel') / asset).read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init', action='store_true')
    args = parser.parse_args()
    if args.init:
        initialize()
        return
    while not Path('/run/nginx.pid').exists():
        time.sleep(1)
    while True:
        try:
            update()
        except OSError:
            print('[orcaslicer-panel] Could not update printer proxy; retrying.', flush=True)
        time.sleep(2)


if __name__ == '__main__':
    main()
