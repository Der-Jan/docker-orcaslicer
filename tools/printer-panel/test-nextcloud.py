#!/usr/bin/env python3
"""Exercise login state transitions without using a real account."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location('nextcloud', Path(__file__).resolve().parents[2] / 'root/usr/local/libexec/orcaslicer-nextcloud.py')
nextcloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nextcloud)
sync_spec = importlib.util.spec_from_file_location('sync_worker', Path(__file__).resolve().parents[2] / 'root/usr/local/libexec/orcaslicer_nextcloud_sync.py')
sync_worker = importlib.util.module_from_spec(sync_spec)
sync_spec.loader.exec_module(sync_worker)
TEST_ROOT = Path(__file__).resolve().parent / '.test-tools'
TEST_ROOT.mkdir(exist_ok=True)
SERVER = 'https://cloud.example.com/nextcloud'
CREDENTIALS = {'server': SERVER, 'loginName': 'test-user', 'appPassword': 'test-app-password'}


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.login = nextcloud.Login(Path(self.directory.name))

    def pending(self):
        self.login.pending = {'server': SERVER, 'generation': 'test'}

    def test_server_validation(self):
        self.assertEqual(nextcloud.server_url(SERVER + '/'), SERVER)
        for value in ('http://cloud.example.com', 'https://user:password@cloud.example.com', SERVER + '?token=x'):
            with self.assertRaises(ValueError):
                nextcloud.server_url(value)
        for value in ('https://other.example.com/poll', 'https://cloud.example.com/outside'):
            with self.assertRaises(ValueError):
                nextcloud.same_server(SERVER, value)

    def test_pending_404_then_credentials_persist_privately(self):
        self.pending()
        with patch.object(nextcloud, 'request', side_effect=[HTTPError(SERVER, 404, 'pending', {}, None), CREDENTIALS]), patch.object(nextcloud.time, 'sleep'):
            self.login.poll(SERVER, SERVER + '/poll', 'poll-secret', 'test')
        self.assertEqual(json.loads(self.login.path.read_text()), CREDENTIALS)
        self.assertEqual(self.login.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.login.directory.stat().st_mode & 0o777, 0o700)
        status = nextcloud.Login(self.login.directory).status()
        self.assertTrue(status['connected'])
        self.assertFalse(status['pending'])
        self.assertNotIn('appPassword', status)
        self.assertNotIn('test-app-password', json.dumps(status))
        self.assertNotIn('poll-secret', json.dumps(status))

    def test_cancel_during_poll_revokes_late_credentials(self):
        self.pending()
        def result(*args, **kwargs):
            self.login.cancel()
            return CREDENTIALS
        with patch.object(nextcloud, 'request', side_effect=result), patch.object(self.login, 'revoke', return_value=True) as revoke:
            self.login.poll(SERVER, SERVER + '/poll', 'token', 'test')
        revoke.assert_called_once_with(CREDENTIALS)
        self.assertFalse(self.login.path.exists())
        self.assertFalse(self.login.status()['connected'])

    def test_start_never_accepts_foreign_poll_server(self):
        with patch.object(nextcloud, 'request', return_value={'login': SERVER + '/login', 'poll': {'token': 'secret', 'endpoint': 'https://evil.example/poll'}}):
            with self.assertRaises(ValueError):
                self.login.start(SERVER)
        self.assertIsNone(self.login.pending)

    def test_disconnect_offline_removes_local_credentials(self):
        self.pending()
        with patch.object(nextcloud, 'request', return_value=CREDENTIALS):
            self.login.poll(SERVER, SERVER + '/poll', 'token', 'test')
        with patch.object(self.login, 'revoke', return_value=False):
            self.login.disconnect()
        self.assertFalse(self.login.path.exists())
        self.assertFalse(self.login.status()['connected'])
        self.assertIn('Revoke', self.login.status()['message'])

    def test_http_mutations_require_custom_header(self):
        from urllib.request import Request, urlopen
        server = nextcloud.ThreadingHTTPServer(('127.0.0.1', 0), nextcloud.Handler)
        server.login = self.login
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            self.assertFalse(json.load(urlopen(url + '/status'))['connected'])
            with self.assertRaises(HTTPError) as failure:
                urlopen(Request(url + '/disconnect', data=b'{}', headers={'Content-Type': 'application/json'}))
            self.assertEqual(failure.exception.code, 403)
            response = urlopen(Request(url + '/cancel', data=b'{}', headers={'Content-Type': 'application/json', 'X-Orca-Nextcloud': '1'}))
            self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_folder_listing_uses_uid_and_only_direct_child_folders(self):
        self.login.credentials = dict(CREDENTIALS, loginName='login@example.com')
        xml = '''<d:multistatus xmlns:d="DAV:">
          <d:response><d:href>/nextcloud/remote.php/dav/files/account-id/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
          <d:response><d:href>/nextcloud/remote.php/dav/files/account-id/3D%20Models/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
          <d:response><d:href>/nextcloud/remote.php/dav/files/account-id/readme.txt</d:href><d:propstat><d:prop><d:resourcetype/></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
          <d:response><d:href>/nextcloud/remote.php/dav/files/account-id/deep/child/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
        </d:multistatus>'''
        with patch.object(nextcloud, 'request', side_effect=[{'ocs': {'data': {'id': 'account-id'}}}, ET.fromstring(xml)]) as fetch:
            _, listing = self.login.folders('/')
        self.assertEqual(listing['folders'], [{'name': '3D Models', 'path': '/3D Models'}])
        self.assertEqual(fetch.call_args_list[1].args[0], SERVER + '/remote.php/dav/files/account-id/')
        self.assertEqual(fetch.call_args_list[1].kwargs['extra_headers']['Depth'], '1')

    def test_selection_persists_and_disconnect_removes_it(self):
        self.login.credentials = dict(CREDENTIALS, userId='uid')
        credentials = self.login.credentials
        with patch.object(self.login, 'folders', return_value=(credentials, {'path': '/Projects/3D Models'})):
            status = self.login.select_folder('/Projects/3D Models')
        self.assertEqual(status['folder'], '/Projects/3D Models')
        self.assertEqual(nextcloud.Login(self.login.directory).status()['folder'], '/Projects/3D Models')
        self.assertNotIn('appPassword', status)
        with patch.object(self.login, 'revoke', return_value=True):
            self.login.disconnect()
        self.assertIsNone(self.login.status()['folder'])

    def test_invalid_and_missing_folders_are_not_saved(self):
        self.login.credentials = dict(CREDENTIALS, userId='uid')
        with patch.object(nextcloud, 'request') as fetch:
            for value in ('/../outside', '/a/./b', 'https://evil.example/', '/bad\x00path'):
                with self.assertRaises(ValueError):
                    self.login.select_folder(value)
            fetch.assert_not_called()
        with patch.object(nextcloud, 'request', return_value=ET.fromstring('<d:multistatus xmlns:d="DAV:"/>')):
            with self.assertRaises(ValueError):
                self.login.select_folder('/Missing')
        self.assertFalse(self.login.path.exists())

    def test_sync_request_acknowledges_without_enabling_stopped_sync(self):
        self.login.credentials = dict(CREDENTIALS, folder='/Projects', sync_enabled=False)
        sync = sync_worker.Sync(self.login)
        with self.assertRaises(ValueError):
            sync.request_now()
        self.assertFalse(self.login.credentials['sync_enabled'])
        self.login.credentials['sync_enabled'] = True
        sync.request_now()
        self.assertEqual(sync.status()['phase'], 'requested')
        self.assertTrue(sync.wake.is_set())

    def test_progress_exposes_counters_without_raw_log_messages(self):
        self.login.credentials = dict(CREDENTIALS, folder='/Projects', sync_enabled=True)
        sync = sync_worker.Sync(self.login)
        sync.phase = 'checking'
        sync.update_progress(json.dumps({'stats': {'bytes': 512, 'totalBytes': 1024, 'transfers': 1,
                                                  'totalTransfers': 2, 'speed': 256,
                                                  'lastError': 'private log detail', 'eta': None}}))
        status = sync.status()
        self.assertEqual(status['phase'], 'transferring')
        self.assertEqual(status['progress']['bytes'], 512)
        self.assertEqual(status['progress']['totalBytes'], 1024)
        self.assertNotIn('private log detail', json.dumps(status))

    def test_waiting_is_distinct_from_sync_errors(self):
        self.login.credentials = dict(CREDENTIALS, folder='/Projects', sync_enabled=True)
        for error, phase in [(InterruptedError('Close the project first.'), 'waiting'), (RuntimeError('Cloud unavailable.'), 'error')]:
            with self.subTest(phase=phase):
                sync = sync_worker.Sync(self.login)
                with patch.object(sync, 'run', side_effect=error):
                    sync.thread.start()
                    try:
                        deadline = time.monotonic() + 2
                        while sync.status()['phase'] != phase and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertEqual(sync.status()['phase'], phase)
                        self.assertFalse(sync.status()['running'])
                    finally:
                        sync.close()


if __name__ == '__main__':
    unittest.main()
