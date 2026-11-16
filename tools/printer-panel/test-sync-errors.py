"""Check error capture and persistence without touching any real sync state."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'root/usr/local/libexec'))
import orcaslicer_nextcloud_sync as worker


class ErrorTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / '.test-tools'
        base.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.login = SimpleNamespace(directory=Path(temporary.name), lock=threading.RLock(),
                                     credentials={'sync_enabled': True, 'server': 'https://cloud.example',
                                                  'userId': 'user', 'folder': '/Slicer', 'appPassword': 'secret-password'})
        self.sync = worker.Sync(self.login)

    def test_real_error_survives_generic_abort(self):
        reason = worker.error_summary(['file.3mf: HTTP 503 Service Unavailable', 'Bisync aborted.', 'Failed to bisync: bisync aborted'])
        self.assertIn('HTTP 503', reason)
        self.assertNotIn('aborted', reason)

    def test_unicode_mismatch_is_explained(self):
        reason = worker.error_summary(['- Path1 file not found in Path2 - Lu\u0308fter/model.stl',
                                       '- Path2 file not found in Path1 - Lüfter/model.stl'])
        self.assertIn('Filename encoding mismatch', reason)
        self.assertIn('Lüfter/model.stl', reason)

    def test_credentials_and_terminal_codes_are_removed(self):
        reason = worker.error_summary(['\x1b[31mHTTP 401 https://user:password@cloud.example/?token=private Authorization: Bearer private secret-password\x1b[0m'], ['secret-password'])
        for secret in ('user:password', 'token=private', 'Bearer private', 'secret-password', '\x1b'):
            self.assertNotIn(secret, reason)

    def test_failure_survives_wait_stop_and_restart(self):
        self.sync.record_error('HTTP 503 Service Unavailable')
        self.sync.phase = 'waiting'
        self.sync.message = 'Waiting for the saved project to close.'
        self.assertEqual(self.sync.status()['phase'], 'waiting')
        self.assertIn('HTTP 503', self.sync.status()['last_error']['message'])
        self.login.credentials['sync_enabled'] = False
        self.assertIsNotNone(worker.Sync(self.login).status()['last_error'])
        self.assertEqual((self.login.directory / 'last-error.json').stat().st_mode & 0o777, 0o600)

    def test_other_account_does_not_inherit_error(self):
        self.sync.record_error('HTTP 503')
        self.login.credentials['folder'] = '/Other'
        self.assertIsNone(self.sync.status()['last_error'])

    def test_success_clears_persisted_failure(self):
        self.sync.record_error('HTTP 503')
        self.sync.clear_error()
        self.assertIsNone(worker.Sync(self.login).last_error)

    def test_worker_wait_preserves_error_and_success_clears_it(self):
        outcomes = [RuntimeError('HTTP 503'), InterruptedError('Project open'), None]
        for outcome in outcomes:
            self.sync.closed.clear()
            self.sync.wake = SimpleNamespace(clear=lambda: None, wait=lambda _: self.sync.closed.set())
            with patch.object(self.sync, 'run', side_effect=outcome):
                self.sync.loop()
            self.assertEqual(self.sync.last_error is None, outcome is None)

    def test_command_captures_rclone_failure_from_pipe(self):
        real_spawn = subprocess.Popen
        script = "import sys,json;print(json.dumps({'level':'error','msg':'file.3mf: HTTP 503 Service Unavailable'}));print(json.dumps({'level':'notice','msg':'Failed to bisync: bisync aborted'}));sys.exit(7)"
        def spawn(*args, **kwargs):
            return real_spawn([sys.executable, '-c', script], **kwargs)
        with patch.object(worker.subprocess, 'Popen', side_effect=spawn), patch.object(worker, 'project_guard', return_value=''):
            with self.assertRaisesRegex(RuntimeError, r'code 7.*HTTP 503'):
                self.sync.command(['bisync'], dict(os.environ), io.StringIO())


if __name__ == '__main__':
    unittest.main()
