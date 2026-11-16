#!/usr/bin/env python3
"""Exercise the real handoff transport and state machine against disposable files."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import unquote
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
LIB = HERE.parents[1] / 'root/usr/local/libexec'
sys.path.insert(0, str(LIB))
import orcaslicer_handoff as h
import orcaslicer_nextcloud_sync as sync

spec = importlib.util.spec_from_file_location('login', LIB / 'orcaslicer-nextcloud.py')
login_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login_module)


class DAV(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, body=b'', etag=None):
        self.send_response(status)
        if etag:
            self.send_header('ETag', etag)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def path_key(self):
        return unquote(self.path).split('/remote.php/dav/files/user/Slicer/', 1)[-1]

    def do_PROPFIND(self):
        path = self.path_key()
        if path not in self.server.files:
            return self.respond(404)
        body = self.server.files[path]
        etag = self.server.etag(path)
        kind = '<d:collection/>' if body is None else ''
        xml = f'<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop><d:getetag>{escape(etag)}</d:getetag><d:resourcetype>{kind}</d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'
        self.respond(207, xml.encode())

    def do_GET(self):
        path = self.path_key()
        if path not in self.server.files:
            return self.respond(404)
        if self.headers.get('If-Match') != self.server.etag(path):
            return self.respond(412)
        self.server.events.append(('download', path))
        self.respond(200, self.server.files[path], self.server.etag(path))

    def do_PUT(self):
        path = self.path_key()
        exists = path in self.server.files
        if (self.headers.get('If-None-Match') == '*' and exists or
                self.headers.get('If-Match') and (not exists or self.headers['If-Match'] != self.server.etag(path))):
            return self.respond(412)
        self.server.files[path] = self.rfile.read(int(self.headers['Content-Length']))
        self.server.events.append(('upload', path))
        self.respond(201, etag=self.server.etag(path))


class HandoffTests(unittest.TestCase):
    def setUp(self):
        work = HERE / '.test-tools'
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        h.PROJECTS = self.root / 'Projects'
        h.PROJECTS.mkdir()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), DAV)
        self.server.files = {'Part': None, 'Part/Sources/model.stl': b'model version one',
                             'Part/model.solidcraft': b'CAD bytes', 'Part/inputs/photo.jpg': b'photo'}
        self.server.events = []
        self.server.etag = lambda path: '"' + hashlib.sha256(self.server.files[path] or b'dir').hexdigest() + '"'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.login = login_module.Login(self.root / 'login')
        self.login.credentials = {'server': f'http://127.0.0.1:{self.server.server_port}',
                                  'userId': 'user', 'loginName': 'user', 'appPassword': 'private-password',
                                  'folder': '/Slicer'}
        self.service = h.Handoff(self.login, self.root / 'handoff')
        self.login.handoff = self.service
        self.service.drop.STATE_ROOT = self.root / 'state'
        self.service.drop.STATE_ROOT.mkdir()
        self.state = dict(session='session', mode='editor', model_id=1, objects=0, path='', dirty=False, busy=False)
        self.data = dict(version=1, request_id='request-1', design_id='stable-design',
                         account=h.account_ref(self.login.credentials), project='Part', intent='open',
                         print_project='Part/Part.3mf', target_source='Part/Sources/model.stl',
                         sources=[dict(path='Part/Sources/model.stl', etag=self.server.etag('Part/Sources/model.stl'))])
        self.drop_calls = []
        self.patches = [patch.object(self.service.drop, 'active_session', side_effect=self.session),
                        patch.object(self.service.drop, 'request', return_value={}),
                        patch.object(self.service.drop, 'create_project', side_effect=self.create),
                        patch.object(self.service.drop, 'drop', side_effect=self.drop),
                        patch.object(self.service.drop, 'wait_for_project')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def session(self):
        if self.state.get('busy'):
            raise RuntimeError('busy')
        return self.service.drop.STATE_ROOT, dict(self.state)

    def create(self, source, project, *args):
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_bytes(b'3MF with selected settings')

    def drop(self, helper, target):
        self.drop_calls.append(target)
        self.assertTrue(target.exists())
        self.assertIn(('download', 'Part/Sources/model.stl'), self.server.events)
        (self.service.drop.STATE_ROOT / 'state.json').write_text(json.dumps({'sources': [str(target)]}))

    def run_request(self, data=None):
        result = self.service.submit(data or self.data)
        self.service.worker.join(10)
        self.assertFalse(self.service.worker.is_alive())
        return self.service.status(result['request_id'])

    def test_first_open_versions_arrive_before_import_and_repeat_is_idempotent(self):
        result = self.run_request()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['synchronization'], 'complete')
        self.assertEqual(result['save'], 'manual_required')
        self.assertEqual(self.service.submit(self.data), result)
        self.assertEqual(len(self.drop_calls), 1)
        self.assertNotIn('request', result)
        changed = dict(self.data, intent='refresh')
        with self.assertRaises(h.Failure):
            self.service.submit(changed)

    def test_step_failure_opens_exact_source_without_second_folder(self):
        with patch.object(self.service.drop, 'create_project', side_effect=RuntimeError('unsupported STEP')):
            result = self.run_request()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.drop_calls, [h.PROJECTS / self.data['target_source']])
        self.assertEqual([p.name for p in h.PROJECTS.iterdir()], ['Part'])

    def test_existing_3mf_bytes_and_settings_are_not_rebuilt(self):
        self.server.files['Part/Part.3mf'] = b'existing 3mf print and plate settings'
        result = self.run_request()
        self.assertEqual(result['save'], 'unchanged')
        self.service.drop.create_project.assert_not_called()
        self.assertEqual(self.drop_calls[0].read_bytes(), self.server.files['Part/Part.3mf'])

    def test_unsaved_and_busy_never_download_or_drop(self):
        self.state.update(dirty=True, objects=1)
        result = self.run_request()
        self.assertEqual(result['code'], 'unsaved_work')
        self.assertEqual(self.server.events, [])
        self.assertEqual(self.drop_calls, [])
        self.state.update(dirty=False, busy=True)
        result = self.run_request(dict(self.data, request_id='busy'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.drop_calls, [])

    def test_stale_etag_failed_upload_and_concurrent_cloud_save_never_import(self):
        self.server.files['Part/Sources/model.stl'] = b'new concurrent CAD save'
        result = self.run_request()
        self.assertEqual(result['code'], 'version_mismatch')
        self.assertFalse(self.service.projects)
        self.assertEqual(self.drop_calls, [])
        missing = copy.deepcopy(self.data)
        missing.update(request_id='missing-upload')
        missing['sources'][0]['path'] = missing['target_source'] = 'Part/Sources/missing.stl'
        result = self.run_request(missing)
        self.assertEqual(result['code'], 'missing_file')

    def test_refresh_downloads_verified_sources_without_duplicating_geometry(self):
        self.run_request()
        self.state.update(path=str(h.PROJECTS / 'Part/Part.3mf'), objects=3)
        self.server.files['Part/Sources/model.stl'] = b'new cloud geometry'
        refresh = copy.deepcopy(self.data)
        refresh.update(request_id='refresh', intent='refresh')
        refresh['sources'][0]['etag'] = self.server.etag(refresh['target_source'])
        result = self.run_request(refresh)
        self.assertEqual(result['synchronization'], 'complete')
        self.assertEqual(result['code'], 'unsupported_refresh')
        self.assertEqual(len(self.drop_calls), 1)
        self.assertEqual((h.PROJECTS / refresh['target_source']).read_bytes(), b'new cloud geometry')
        self.assertEqual((h.PROJECTS / 'Part/Part.3mf').read_bytes(), b'3MF with selected settings')

    def test_rename_and_multiple_sources_retain_exact_links(self):
        self.server.files['Part/Sources/second.step'] = b'second model'
        self.data['sources'].append(dict(path='Part/Sources/second.step', etag=self.server.etag('Part/Sources/second.step')))
        self.run_request()
        renamed = copy.deepcopy(self.data)
        renamed.update(request_id='renamed', display_name='A new display name')
        # The linked 3MF does not exist in the cloud yet; preserve the local copy.
        result = self.run_request(renamed)
        self.assertEqual(result['code'], 'local_conflict')
        self.assertEqual(len(self.service.projects), 1)
        self.assertEqual(len(self.drop_calls), 1)

    def test_conditional_3mf_upload_preserves_cad_and_rejects_cloud_conflicts(self):
        self.run_request()
        cloud_before = dict(self.server.files)
        self.service.sync_owned(self.login.credentials)
        self.assertEqual(self.server.files['Part/Part.3mf'], b'3MF with selected settings')
        for path, value in cloud_before.items():
            self.assertEqual(self.server.files[path], value)
        (h.PROJECTS / 'Part/Part.3mf').write_bytes(b'local saved print settings')
        self.server.files['Part/Part.3mf'] = b'concurrent cloud print settings'
        with self.assertRaises(h.Failure) as failure:
            self.service.sync_owned(self.login.credentials)
        self.assertEqual(failure.exception.code, 'version_mismatch')
        self.assertEqual(self.server.files['Part/Part.3mf'], b'concurrent cloud print settings')

    def test_untracked_local_files_are_only_adopted_if_identical(self):
        target = h.local_path(self.data['target_source'])
        target.parent.mkdir(parents=True)
        target.write_bytes(b'different local edit')
        result = self.run_request()
        self.assertEqual(result['code'], 'local_conflict')
        self.assertEqual(target.read_bytes(), b'different local edit')
        target.write_bytes(self.server.files[self.data['target_source']])
        result = self.run_request(dict(self.data, request_id='identical'))
        self.assertEqual(result['status'], 'complete')

    def test_timeout_and_restart_cannot_replay_a_native_mutation(self):
        with patch.object(self.service.drop, 'wait_for_project', side_effect=RuntimeError('timeout')):
            result = self.run_request()
        self.assertEqual(result['status'], 'ambiguous')
        restored = h.Handoff(self.login, self.service.directory)
        self.assertEqual(restored.submit(self.data), result)
        self.assertEqual(len(self.drop_calls), 1)
        self.service.update(self.data, status='opening')
        restored = h.Handoff(self.login, self.service.directory)
        self.assertEqual(restored.status(self.data['request_id'])['status'], 'ambiguous')

    def test_paths_accounts_and_symlinks_are_rejected(self):
        for path in ('../outside', '/absolute', 'Part/../escape', 'Part//empty'):
            with self.assertRaises(h.Failure):
                self.service.validate(dict(self.data, project=path))
        with self.assertRaises(h.Failure):
            self.service.validate(dict(self.data, account='wrong-account'))
        (h.PROJECTS / 'Part').symlink_to(self.root)
        with self.assertRaises(h.Failure):
            self.service.validate(self.data)

    def test_multiple_designs_share_one_print_file_baseline_after_restart(self):
        self.run_request()
        self.service.sync_owned(self.login.credentials)
        other = dict(self.data, request_id='another-design', design_id='second-design')
        result = self.run_request(other)
        self.assertEqual(result['status'], 'complete')
        self.service.sync_owned(self.login.credentials)
        restored = h.Handoff(self.login, self.service.directory)
        (h.PROJECTS / 'Part/Part.3mf').write_bytes(b'new saved settings')
        restored.sync_owned(self.login.credentials)
        self.assertEqual(self.server.files['Part/Part.3mf'], b'new saved settings')

    def test_source_changes_again_before_import_are_rejected(self):
        original = self.service.drop.create_project.side_effect
        def change_source(*args):
            original(*args)
            self.server.files['Part/Sources/model.stl'] = b'changed while preparing'
        with patch.object(self.service.drop, 'create_project', side_effect=change_source):
            result = self.run_request()
        self.assertEqual(result['code'], 'version_mismatch')
        self.assertEqual(self.drop_calls, [])

    def test_provider_checksum_and_file_reference_are_accepted_without_etag(self):
        source = self.data['sources'][0]
        source.pop('etag')
        source['sha256'] = hashlib.sha256(self.server.files[source['path']]).hexdigest()
        result = self.run_request()
        self.assertEqual(result['status'], 'complete')
        self.assertIn(source['path'], result['source_versions'])

    def test_explicit_craft_account_reference_is_preserved(self):
        self.login.credentials['account_ref'] = 'craft-account-reference'
        self.data['account'] = 'craft-account-reference'
        result = self.run_request()
        self.assertEqual(result['status'], 'complete')

    def test_offline_preserves_files_and_never_imports(self):
        with patch.object(h.Cloud, 'stat', side_effect=h.Failure('offline', 'offline')):
            result = self.run_request()
        self.assertEqual(result['code'], 'offline')
        self.assertEqual(self.drop_calls, [])
        self.assertEqual(list(h.PROJECTS.iterdir()), [])

    def test_bearer_auth_and_durable_http_status_do_not_expose_credentials(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), login_module.Handler)
        server.login = self.login
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/handoff/'
            with self.assertRaises(HTTPError) as failure:
                urlopen(url + 'account')
            failure.exception.close()
            self.assertEqual(failure.exception.code, 401)
            headers = {'Authorization': 'Bearer ' + self.service.token, 'Content-Type': 'application/json'}
            with urlopen(Request(url + 'account', headers=headers)) as response:
                account = json.load(response)
            self.assertEqual(account['account'], self.data['account'])
            self.assertNotIn('private-password', json.dumps(account))
            with urlopen(Request(url + 'requests', data=json.dumps(self.data).encode(), headers=headers)) as response:
                self.assertEqual(response.status, 202)
            self.service.worker.join(10)
            with urlopen(Request(url + 'requests/request-1', headers=headers)) as response:
                result = json.load(response)
            self.assertEqual(result['operation'], 'opened')
            self.assertNotIn(self.service.token, json.dumps(result))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_managed_projects_are_excluded_from_legacy_sync(self):
        self.run_request()
        credentials = dict(self.login.credentials, sync_enabled=True)
        worker = sync.Sync(self.login)
        commands = []
        self.login.directory.joinpath('last-success.json').unlink(missing_ok=True)
        with patch.object(sync, 'PROJECTS', h.PROJECTS), patch.object(sync, 'project_guard', return_value=''), \
                patch.object(worker, 'command', side_effect=lambda args, *rest: commands.append(args)), \
                patch.object(sync.subprocess, 'run', return_value=SimpleNamespace(stdout='obscured')), \
                patch.object(self.service, 'sync_owned'):
            worker.run(credentials)
        bisync = next(args for args in commands if args[0] == 'bisync')
        self.assertIn('/Part/**', bisync)
        self.assertEqual(self.server.files['Part/model.solidcraft'], b'CAD bytes')
        self.assertIn('\\*', h.legacy_exclusion('Project*'))


if __name__ == '__main__':
    unittest.main()
