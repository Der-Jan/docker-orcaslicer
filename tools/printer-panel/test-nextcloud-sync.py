#!/usr/bin/env python3
"""Integration test for the production worker against a disposable Nextcloud.

Run inside the Orca image, sharing the test Nextcloud's network namespace.
Mount an empty test directory at /config; the image supplies the production code.
The account sync-test / orca-sync-fixture-only must exist; never use real data.
"""
import base64
import importlib.util
from pathlib import Path
import secrets
import sys
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

sys.path.insert(0, '/usr/local/libexec')
import orcaslicer_nextcloud_sync as worker
from orcaslicer_nextcloud_sync import Sync, identity
TOKEN = secrets.token_hex(4)
PROJECTS = Path('/config') / ('sync-test-' + TOKEN) / 'Projects'
worker.PROJECTS = PROJECTS
FOLDER = '/Sync-test-' + TOKEN

spec = importlib.util.spec_from_file_location('login', '/usr/local/libexec/orcaslicer-nextcloud.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
BASE = 'http://localhost'
DAV = '/remote.php/dav/files/sync-test'
AUTH = 'Basic ' + base64.b64encode(b'sync-test:orca-sync-fixture-only').decode()


def remote(path, method='GET', body=None, headers=None):
    if path.startswith('/Projects'):
        path = FOLDER + path[len('/Projects'):]
    request = Request(BASE + DAV + quote(path, safe='/'), method=method, data=body,
                      headers={'Authorization': AUTH, **(headers or {})})
    with urlopen(request, timeout=20) as response:
        return response.read()


def missing(path):
    try:
        remote(path)
        return False
    except HTTPError as error:
        if error.code == 404:
            return True
        raise


def children(path):
    raw = remote(path.rstrip('/') + '/', 'PROPFIND', headers={'Depth': '1'})
    names = [unquote(urlsplit(x.findtext('{DAV:}href')).path)[len(DAV):].rstrip('/')
             for x in ET.fromstring(raw).findall('{DAV:}response')]
    return [name for name in names if name != path.rstrip('/')]


try:
    remote('/Projects', 'MKCOL')
except HTTPError as error:
    if error.code != 405 or children('/Projects'):
        raise
PROJECTS.mkdir(parents=True)
(PROJECTS / 'local-only.stl').write_bytes(b'local original')
(PROJECTS / 'same.stl').write_bytes(b'local first version')
(PROJECTS / 'upload.partial').write_bytes(b'local incomplete upload')
remote('/Projects/cloud-only.stl', 'PUT', b'cloud original')
remote('/Projects/same.stl', 'PUT', b'cloud first version')
remote('/Projects/download.partial', 'PUT', b'cloud incomplete upload')

login = module.Login(PROJECTS.parent / 'sync-state')
credentials = {'server': BASE, 'loginName': 'sync-test', 'appPassword': 'orca-sync-fixture-only',
               'userId': 'sync-test', 'folder': FOLDER, 'sync_enabled': True}
login.credentials = credentials
login.save(credentials)
sync = Sync(login)
login.sync = sync


def run(label):
    try:
        sync.run(credentials)
    except Exception:
        log = login.directory / 'bisync' / identity(credentials) / 'sync.log'
        print(log.read_text()[-12000:] if log.exists() else 'No sync log', flush=True)
        raise
    print(label + ' passed', flush=True)


run('Initial merge')
assert (PROJECTS / 'cloud-only.stl').read_bytes() == b'cloud original'
assert remote('/Projects/local-only.stl') == b'local original'
assert remote('/Projects/same.stl') == b'local first version'
assert missing('/Projects/upload.partial')
assert not (PROJECTS / 'download.partial').exists()
history = children('/.orcaslicer-sync-history/' + identity(credentials))
assert any(remote(directory + '/same.stl') == b'cloud first version' for directory in history)

(PROJECTS / 'local-only.stl').write_bytes(b'local updated content')
remote('/Projects/incoming.step', 'PUT', b'new cloud file')
run('Upload and download')
assert remote('/Projects/local-only.stl') == b'local updated content'
assert (PROJECTS / 'incoming.step').read_bytes() == b'new cloud file'

(PROJECTS / 'progress.bin').write_bytes(b'x' * 1024 * 1024)
observed = []
original_command = sync.command
original_progress = sync.update_progress
def sample_progress(line):
    original_progress(line)
    observed.append(sync.status())
def slow_transfer(arguments, environment, log):
    return original_command([*arguments, '--bwlimit', '256k'], environment, log)
with patch.object(sync, 'command', side_effect=slow_transfer), patch.object(sync, 'update_progress', side_effect=sample_progress):
    run('Live transfer statistics')
assert any(s['phase'] == 'transferring' and 0 < s['progress'].get('bytes', 0) < s['progress'].get('totalBytes', 0) for s in observed)
assert sync.progress.get('transfers', 0) > 0

(PROJECTS / 'local-only.stl').unlink()
remote('/Projects/cloud-only.stl', 'DELETE')
run('Deletion on both sides')
assert missing('/Projects/local-only.stl')
assert not (PROJECTS / 'cloud-only.stl').exists()

(PROJECTS / 'same.stl').write_bytes(b'LOCAL EDIT NUMBER TWO')
remote('/Projects/same.stl', 'PUT', b'CLOUD EDIT NUMBER TWO')
run('Simultaneous changes')
versions = {(x.name, x.read_bytes()) for x in PROJECTS.glob('same*.stl')}
assert any('.local' in name and data == b'LOCAL EDIT NUMBER TWO' for name, data in versions)
assert any('.cloud' in name and data == b'CLOUD EDIT NUMBER TWO' for name, data in versions)

before = {x.name: x.read_bytes() for x in PROJECTS.iterdir() if x.is_file()}
original = sync.command
def offline(arguments, environment, log):
    return original(arguments, dict(environment, RCLONE_CONFIG_NEXTCLOUD_URL='http://localhost:1/'), log)
with patch.object(sync, 'command', side_effect=offline):
    try:
        sync.run(credentials)
    except RuntimeError:
        pass
    else:
        raise AssertionError('Offline run must fail')
assert before == {x.name: x.read_bytes() for x in PROJECTS.iterdir() if x.is_file()}
run('Recovery after connection failure')

with patch('orcaslicer_nextcloud_sync.project_guard', return_value='project open'), patch('subprocess.Popen') as spawn:
    try:
        sync.run(credentials)
    except InterruptedError:
        pass
    else:
        raise AssertionError('Open project must pause sync')
    spawn.assert_not_called()
sync.enable(False)
restored = module.Login(login.directory)
assert restored.credentials['sync_enabled'] is False
assert Sync(restored).status()['last_success'] is not None
assert (login.directory / 'bisync' / identity(credentials) / 'initialized').exists()
sync.enable(True)
assert module.Login(login.directory).credentials['sync_enabled'] is True
sync.enable(False)
print('Offline safety, project guard, baseline persistence and stop persistence passed.', flush=True)
