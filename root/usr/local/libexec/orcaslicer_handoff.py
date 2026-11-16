"""Versioned, authenticated cloud-to-Orca handoffs and conditional 3MF writes."""
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener
import xml.etree.ElementTree as ET

PROJECTS = Path('/config/Projects')
DROP_MODULE = Path(__file__).with_name('orcaslicer-project-drop.py')


class Failure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    part = path.with_suffix('.new')
    with part.open('w') as stream:
        os.chmod(part, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    part.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def relative(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or value.startswith('/') or any(ord(c) < 32 or c == '\\' for c in value)
            or any(p in ('', '.', '..') for p in value.split('/'))):
        raise Failure('invalid_path', 'Use exact paths relative to the configured project root.')
    return value


def local_path(value):
    path = PROJECTS / relative(value)
    for parent in (PROJECTS, *reversed(path.parents), path):
        if parent.is_symlink():
            raise Failure('invalid_path', 'Symbolic links are not allowed in cloud project paths.')
    return path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def account_ref(credentials):
    if not credentials or not credentials.get('userId'):
        return None
    if credentials.get('account_ref'):
        return credentials['account_ref']
    return hashlib.sha256(json.dumps([credentials['server'].rstrip('/'), credentials['userId'], credentials.get('folder')],
                                    ensure_ascii=True).encode()).hexdigest()


def legacy_exclusion(project):
    # Escape literal project names before passing them to rclone's glob filters.
    escaped = ''.join('\\' + c if c in '*?[]{}\\' else c for c in project)
    return '/' + escaped + '/**'


class Cloud:
    def __init__(self, credentials):
        self.credentials = credentials
        self.base = (credentials['server'].rstrip('/') + '/remote.php/dav/files/'
                     + quote(credentials['userId'], safe='') + '/'
                     + quote(credentials['folder'].strip('/'), safe='/') + '/')

    def open(self, path, method, data=None, headers=None):
        from urllib.request import HTTPRedirectHandler

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None

        credentials = self.credentials
        auth = base64.b64encode((credentials['loginName'] + ':' + credentials['appPassword']).encode()).decode()
        request = Request(self.base + quote(relative(path), safe='/'), data=data, method=method,
                          headers={'Authorization': 'Basic ' + auth, 'User-Agent': 'Orca project handoff',
                                   **(headers or {})})
        try:
            return build_opener(NoRedirect()).open(request, timeout=30)
        except HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403):
                raise Failure('authentication', 'Nextcloud rejected the configured account.') from None
            if code == 404:
                raise Failure('missing_file', 'A requested cloud file or folder does not exist.') from None
            if code == 412:
                raise Failure('version_mismatch', 'The cloud file changed. Save or reload in SolidCraft and send a new request.') from None
            raise Failure('cloud_error', f'Nextcloud returned HTTP {code}. No import was started.') from None
        except OSError:
            raise Failure('offline', 'Nextcloud could not be reached. No import was started.') from None

    def stat(self, path, missing=False):
        try:
            with self.open(path, 'PROPFIND',
                           b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop><d:getetag/><d:resourcetype/><oc:fileid/></d:prop></d:propfind>',
                           {'Depth': '0', 'Content-Type': 'application/xml'}) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise Failure('cloud_error', 'The cloud metadata response is too large.')
                root = ET.fromstring(raw)
            for item in root.findall('{DAV:}response/{DAV:}propstat'):
                if ' 200 ' in item.findtext('{DAV:}status', ''):
                    prop = item.find('{DAV:}prop')
                    return {'etag': prop.findtext('{DAV:}getetag'),
                            'file_id': prop.findtext('{http://owncloud.org/ns}fileid'),
                            'folder': prop.find('{DAV:}resourcetype/{DAV:}collection') is not None}
            raise Failure('cloud_error', 'Nextcloud did not return file metadata.')
        except Failure as error:
            if missing and error.code == 'missing_file':
                return None
            raise
        except ET.ParseError:
            raise Failure('cloud_error', 'Nextcloud did not return valid file metadata.') from None

    def download(self, path, etag, target, checksum=None):
        if not isinstance(etag, str) or not etag or any(ord(c) < 32 for c in etag):
            raise Failure('invalid_request', 'Every source requires its post-upload ETag.')
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + '.' + uuid.uuid4().hex + '.handoff.part')
        try:
            with self.open(path, 'GET', headers={'If-Match': etag}) as response, part.open('xb') as output:
                if response.headers.get('ETag') != etag:
                    raise Failure('version_mismatch', 'The downloaded ETag does not match the requested source version.')
                h = hashlib.sha256()
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > 2 * 1024 ** 3:
                        raise Failure('file_too_large', 'A handoff file exceeds the 2 GiB limit.')
                    output.write(chunk)
                    h.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if checksum and checksum != h.hexdigest():
                raise Failure('version_mismatch', 'The downloaded source checksum does not match.')
            if self.stat(path)['etag'] != etag:
                raise Failure('version_mismatch', 'The source changed during download. Send a new request.')
            part.replace(target)
            return h.hexdigest()
        finally:
            part.unlink(missing_ok=True)

    def upload(self, path, target, etag):
        # Upload only Orca-owned bytes, conditionally; never mirror or purge CAD files.
        with target.open('rb') as stream:
            with self.open(path, 'PUT', data=stream,
                           headers={'Content-Length': str(target.stat().st_size),
                                    'Content-Type': 'application/octet-stream',
                                    **({'If-Match': etag} if etag else {'If-None-Match': '*'})}) as response:
                accepted = response.headers.get('ETag')
        return accepted or self.stat(path)['etag']


class Handoff:
    def __init__(self, login, directory=None):
        self.login = login
        self.directory = directory or Path('/data/orcaslicer/handoff')
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.token_path = self.directory / 'token'
        if not self.token_path.exists():
            with self.token_path.open('x') as stream:
                os.chmod(self.token_path, 0o600)
                stream.write(secrets.token_urlsafe(48) + '\n')
        self.token = self.token_path.read_text().strip()
        if len(self.token) < 32:
            raise ValueError('The handoff token must contain at least 32 characters.')
        self.lock = threading.RLock()
        self.worker = None
        self.projects_path = self.directory / 'projects.json'
        self.projects = json.loads(self.projects_path.read_text()) if self.projects_path.exists() else {}
        shared = {}
        for registry in self.projects.values():
            key = (registry['account'], registry['project'])
            shared.setdefault(key, {}).update(registry['files'])
        for registry in self.projects.values():
            registry['files'] = shared[(registry['account'], registry['project'])]
        spec = importlib.util.spec_from_file_location('orca_project_drop', DROP_MODULE)
        self.drop = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.drop)
        # Never replay a mutation on restart. Its outcome must be inspected first.
        for path in self.directory.glob('requests/*.json'):
            record = json.loads(path.read_text())
            if record['status'] not in ('complete', 'failed', 'ambiguous'):
                record.update(status='ambiguous', code='restart',
                              message='Orca restarted during this request. Inspect the project before sending another request.')
                atomic(path, record)

    def authorized(self, header):
        return secrets.compare_digest(header or '', 'Bearer ' + self.token)

    def credentials(self):
        # Optional independent account provisioning; never expose these fields over HTTP.
        path = self.directory / 'account.json'
        if path.exists():
            path.chmod(0o600)
            credentials = json.loads(path.read_text())
            from urllib.parse import urlsplit
            parsed = urlsplit(credentials['server'])
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not credentials.get('userId')
                    or '/' in credentials['userId']
                    or not all(isinstance(credentials.get(k), str) and credentials[k]
                               for k in ('loginName', 'appPassword', 'folder'))
                    or not credentials['folder'].startswith('/')
                    or any(p in ('.', '..') for p in credentials['folder'].split('/'))):
                raise Failure('account_mismatch', 'The dedicated handoff account configuration is invalid.')
            return credentials
        return self.login.credentials

    def project_names(self):
        with self.lock:
            # Protect linked folders from legacy mirroring, even after account changes.
            return sorted({p['project'] for p in self.projects.values()})

    def validate(self, data):
        if not isinstance(data, dict) or data.get('version') != 1:
            raise Failure('invalid_request', 'Handoff version 1 is required.')
        if set(data) - {'version', 'request_id', 'design_id', 'account', 'project', 'intent',
                        'sources', 'target_source', 'print_project', 'display_name', 'session_ref'}:
            raise Failure('invalid_request', 'Unknown request fields. Credentials must never be included in a handoff.')
        for field in ('request_id', 'design_id'):
            if not isinstance(data.get(field), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', data[field]):
                raise Failure('invalid_request', f'Invalid {field}.')
        credentials = self.credentials()
        if not credentials or data.get('account') != account_ref(credentials):
            raise Failure('account_mismatch', 'The requested Nextcloud account is not configured in Orca.')
        if not credentials.get('folder') or credentials['folder'] == '/':
            raise Failure('invalid_root', 'Choose a dedicated Nextcloud project root in Orca first.')
        project = relative(data.get('project'))
        local_path(project)
        if data.get('intent') not in ('open', 'refresh'):
            raise Failure('invalid_request', 'Choose open or refresh.')
        sources = data.get('sources')
        if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
            raise Failure('invalid_request', 'Supply between 1 and 32 uploaded sources.')
        paths = []
        for source in sources:
            if not isinstance(source, dict):
                raise Failure('invalid_request', 'Invalid source descriptor.')
            path = relative(source.get('path'))
            if not path.startswith(project + '/') or PurePosixPath(path).suffix.lower() not in ('.stl', '.step', '.stp', '.obj', '.amf'):
                raise Failure('invalid_path', 'Model sources must belong to the selected project folder.')
            if set(source) - {'path', 'etag', 'sha256', 'file_id'}:
                raise Failure('invalid_request', 'Unknown source reference fields.')
            if source.get('etag') is not None and (not isinstance(source['etag'], str) or not source['etag']
                    or len(source['etag']) > 256 or any(ord(c) < 32 for c in source['etag'])):
                raise Failure('invalid_request', 'Invalid provider ETag.')
            if not source.get('etag') and not source.get('sha256'):
                raise Failure('version_required', 'Supply the provider ETag or exported source SHA-256 after a successful cloud export.')
            if source.get('sha256') and not re.fullmatch('[0-9a-f]{64}', source['sha256']):
                raise Failure('invalid_request', 'Invalid source SHA-256.')
            local_path(path)
            paths.append(path)
        if len(paths) != len(set(paths)) or data.get('target_source') not in paths:
            raise Failure('invalid_request', 'Choose one exact target_source from the source list.')
        print_path = data.get('print_project')
        if print_path:
            if not relative(print_path).startswith(project + '/') or not print_path.lower().endswith('.3mf'):
                raise Failure('invalid_path', 'The linked 3MF must belong to the selected project.')
            local_path(print_path)
        return json.loads(json.dumps(data))

    def record_path(self, request_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
            raise Failure('invalid_request', 'Invalid request ID.')
        return self.directory / 'requests' / (request_id + '.json')

    def status(self, request_id):
        path = self.record_path(request_id)
        with self.lock:
            if not path.exists():
                raise Failure('unknown_request', 'No such handoff request.')
            result = json.loads(path.read_text())
            return {key: value for key, value in result.items() if key != 'request'}

    def submit(self, data):
        with self.lock:
            # Deduplicate before checking current state/account, including after restart.
            path = self.record_path(data.get('request_id', '') if isinstance(data, dict) else '')
            if path.exists():
                previous = json.loads(path.read_text())
                if previous['request'] != data:
                    raise Failure('request_id_conflict', 'This request ID already identifies a different immutable request.')
                return self.status(data['request_id'])
            data = self.validate(data)
            if self.worker and self.worker.is_alive():
                raise Failure('busy', 'Another handoff is running. Query its status before sending another.')
            result = dict(request_id=data['request_id'], design_id=data['design_id'], project=data['project'],
                          request=data, status='accepted',
                          synchronization='pending', operation='pending', save='not_requested',
                          message='Handoff accepted. No import has started.', updated_at=time.time())
            atomic(path, result)
            self.worker = threading.Thread(target=self.execute, args=(data,), daemon=True)
            self.worker.start()
            return self.status(data['request_id'])

    def update(self, data, **changes):
        with self.lock:
            path = self.record_path(data['request_id'])
            result = json.loads(path.read_text())
            result.update(changes, updated_at=time.time())
            atomic(path, result)

    def registry_key(self, data):
        return hashlib.sha256(json.dumps([data['account'], data['design_id']], sort_keys=True).encode()).hexdigest()

    def adopt(self, data, cloud):
        key = self.registry_key(data)
        previous = self.projects.get(key)
        paths = [s['path'] for s in data['sources']]
        if previous and (previous['project'] != data['project'] or previous['sources'] != paths
                         or previous.get('print_project') not in (None, data.get('print_project'))):
            raise Failure('link_changed', 'The stable design already has different links. Relinking or moving requires an explicit migration.')
        if not cloud.stat(data['project'])['folder']:
            raise Failure('invalid_path', 'Choose an existing Nextcloud project folder.')
        # A handoff is an explicit association. Do not infer it from a display name.
        with self.lock:
            if not previous:
                siblings = [p for p in self.projects.values() if p['account'] == data['account']]
                for item in siblings:
                    if (item['project'].startswith(data['project'] + '/')
                            or data['project'].startswith(item['project'] + '/')):
                        raise Failure('link_changed', 'Choose the existing project folder; nested managed projects are not supported.')
                files = next((p['files'] for p in siblings if p['project'] == data['project']), {})
                self.projects[key] = {'account': data['account'], 'project': data['project'],
                                      'sources': paths, 'print_project': data.get('print_project'), 'files': files}
                atomic(self.projects_path, self.projects)
            elif not previous.get('print_project') and data.get('print_project'):
                previous['print_project'] = data['print_project']
                atomic(self.projects_path, self.projects)
        return self.projects[key]

    def pull(self, cloud, registry, path, metadata, checksum=None):
        target = local_path(path)
        old = registry['files'].get(path)
        if target.exists():
            current = digest(target)
            if old and current != old['sha256']:
                raise Failure('local_conflict', 'A local file changed independently. Preserve it and resolve the conflict before importing.')
            if old and old['etag'] == metadata['etag']:
                if checksum and checksum != current:
                    raise Failure('version_mismatch', 'The source checksum does not match the requested version.')
                return
            if not old:
                # Enrol byte-identical existing files without overwriting local data.
                comparison = target.with_name(target.name + '.' + uuid.uuid4().hex + '.compare.part')
                try:
                    value = cloud.download(path, metadata['etag'], comparison, checksum)
                    if value != current:
                        raise Failure('local_conflict', 'The local and cloud file differ. Explicitly reconcile them before linking.')
                    registry['files'][path] = dict(metadata, sha256=value)
                    atomic(self.projects_path, self.projects)
                    return
                finally:
                    comparison.unlink(missing_ok=True)
        value = cloud.download(path, metadata['etag'], target, checksum)
        registry['files'][path] = dict(metadata, sha256=value)
        atomic(self.projects_path, self.projects)

    def sync_owned(self, credentials):
        cloud = Cloud(credentials)
        seen = set()
        for registry in self.projects.values():
            if registry['account'] != account_ref(credentials) or registry['project'] in seen:
                continue
            seen.add(registry['project'])
            folder = local_path(registry['project'])
            if not folder.exists():
                continue
            for target in folder.rglob('*'):
                if target.is_symlink():
                    raise Failure('invalid_path', 'Symbolic links are not allowed in linked projects.')
                if target.suffix.lower() != '.3mf' or not target.is_file():
                    continue
                path = str(target.relative_to(PROJECTS))
                old = registry['files'].get(path)
                value = digest(target)
                if old and old['sha256'] == value:
                    continue
                before = target.stat()
                accepted = cloud.upload(path, target, old['etag'] if old else None)
                after = target.stat()
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size) or digest(target) != value:
                    raise Failure('local_conflict', 'The 3MF changed during upload. Inspect its cloud version before retrying.')
                registry['files'][path] = {'etag': accepted, 'sha256': value}
                atomic(self.projects_path, self.projects)

    def execute(self, data):
        mutated = False
        try:
            # Same lock as periodic sync: an in-flight bisync must finish first.
            with (self.login.directory / 'sync.lock').open('a') as lock, \
                    (self.drop.STATE_ROOT / 'operation.lock').open('a') as operation:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.validate(data)
                directory, state = self.drop.active_session()
                if state['dirty'] or state['objects'] and not state['path']:
                    self.drop.request(directory, state, 'check', notice=
                                      'A cloud project is waiting. Save your work, then open a new empty project in Orca. '
                                      'Send a new handoff request when ready; nothing has been discarded.')
                    raise Failure('unsaved_work', 'Save or close your current work in Orca, then send a new request.')
                if data['intent'] == 'refresh':
                    expected = data.get('print_project')
                    if not expected or state['path'] != str(local_path(expected)):
                        raise Failure('project_mismatch', 'Open the explicitly linked print project before refreshing its sources.')
                elif state['objects'] or state['path']:
                    self.drop.request(directory, state, 'check', notice=
                                      'A cloud project is waiting. Open a new empty project, then send a new handoff request.')
                    raise Failure('project_open', 'Open a new empty project in Orca before opening the cloud project.')
                credentials = dict(self.credentials())
                cloud = Cloud(credentials)
                # Validate every ETag before changing any local source or adopting a folder.
                metadata = {}
                for source in data['sources']:
                    info = cloud.stat(source['path'])
                    if (info['folder'] or source.get('etag') and info['etag'] != source['etag']
                            or source.get('file_id') and str(info['file_id']) != str(source['file_id'])):
                        raise Failure('version_mismatch', 'A requested source changed after upload. Send its current ETag.')
                    metadata[source['path']] = info
                registry = self.adopt(data, cloud)
                self.update(data, status='synchronizing', message='Downloading the requested cloud versions.')
                for source in data['sources']:
                    self.pull(cloud, registry, source['path'], metadata[source['path']], source.get('sha256'))
                if data['intent'] == 'refresh':
                    self.update(data, synchronization='complete')
                    self.drop.request(directory, state, 'check', notice=
                                      'Cloud sources are downloaded and verified. Use Reload from disk for the linked model in Orca. '
                                      'Automatic geometry replacement is not supported by this Orca API; no objects were added.')
                    raise Failure('unsupported_refresh', 'Sources verified. Use Reload from disk in Orca to preserve placements and settings; no objects were added.')
                print_path = data.get('print_project')
                info = cloud.stat(print_path, missing=True) if print_path else None
                if info:
                    self.pull(cloud, registry, print_path, info)
                    target = local_path(print_path)
                    pending_source = None
                    pending_project = target
                else:
                    target = local_path(data['target_source'])
                    project = local_path(print_path or (data['project'] + '/' + PurePosixPath(data['project']).name + '.3mf'))
                    if project.exists():
                        raise Failure('local_conflict', 'A local print project already exists. Link its exact path explicitly.')
                    try:
                        reply = self.drop.request(directory, state, 'template')
                        self.drop.create_project(target, project, reply, directory, Path('/opt/orcaslicer/bin/orca-slicer'))
                        target, pending_project, pending_source = project, project, None
                    except self.drop.CONVERSION_ERRORS:
                        pending_project, pending_source = state['path'], target
                    # .3mf is not uploaded until an actual Orca save; export is a copy.
                for source in data['sources']:
                    if cloud.stat(source['path'])['etag'] != metadata[source['path']]['etag']:
                        raise Failure('version_mismatch', 'A source changed before import. No import was started.')
                self.validate(data)
                self.drop.request(directory, state, 'check', notice=
                                  f'Cloud sources verified. Opening {target}. Save the print project in '
                                  f'{local_path(data["project"])} using Save Project As. STEP import dialogs remain under your control.')
                self.update(data, synchronization='complete', status='opening',
                            source_versions=metadata,
                            operation='ambiguous', message='Import started. Do not retry; query this request status.')
                pending = self.drop.mark_pending(state, pending_project, source=pending_source)
                mutated = True  # persisted before native mutation; timeout must never trigger a replay
                self.drop.drop(Path('/usr/local/libexec/wayland-file-drop'), target)
                self.drop.wait_for_project(directory, state, pending)
                if pending_source:
                    current = self.drop.read_json(directory / 'state.json')
                    if not current or str(pending_source) not in current.get('sources', []):
                        raise Failure('operation_unconfirmed', 'The imported source association could not be confirmed. Inspect Orca; do not retry automatically.')
                self.update(data, status='complete', operation='opened', save='manual_required' if not info else 'unchanged',
                            message='Project opened. Save changes in Orca; opening is not a cloud save acknowledgement.')
        except BlockingIOError:
            self.fail(data, mutated, 'busy', 'A sync or desktop import is running. Wait for it to finish and send a new request.')
        except Failure as error:
            self.fail(data, mutated, error.code, error.message)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, AttributeError):
            self.fail(data, mutated, 'operation_unconfirmed' if mutated else 'unavailable',
                      'Inspect Orca and finish its dialogs. This request will not be replayed.' if mutated else
                      'Orca or the cloud project is unavailable. Check its state before sending another request.')

    def fail(self, data, mutated, code, message):
        state = self.status(data['request_id'])
        self.update(data, status='ambiguous' if mutated else 'failed', code=code, message=message,
                    operation='ambiguous' if mutated else 'not_started',
                    synchronization='complete' if state['synchronization'] == 'complete' else 'failed')
