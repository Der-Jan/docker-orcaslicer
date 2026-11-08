#!/usr/bin/env python3
"""Check restore/relaunch decisions against isolated process fixtures."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

LIB = Path(__file__).resolve().parents[2] / 'root/usr/local/libexec'
sys.path.insert(0, str(LIB))
import orcaslicer_recovery as recovery
REAL_RUNNING = recovery.running
spec = importlib.util.spec_from_file_location('login', LIB / 'orcaslicer-nextcloud.py')
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parent / '.test-tools'
        work.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for key, value in {'STATE': self.root, 'SESSION': self.root / 'session.json',
                           'STARTING': self.root / 'starting.json', 'PROC': self.root / 'proc',
                           'APP': self.root / 'orca', 'LOG': self.root / 'recovery.log'}.items():
            self.enterContext(patch.object(recovery, key, value))
        recovery.PROC.mkdir()
        recovery.APP.write_text('fixture executable')
        recovery.remember_session({'HOME': '/config', 'XDG_RUNTIME_DIR': '/config/.XDG', 'PRIVATE_TOKEN': 'secret'})
        self.alive = False
        self.enterContext(patch.object(recovery, 'running', side_effect=lambda: [123] if self.alive else []))
        self.window = self.enterContext(patch.object(recovery, 'window_command', return_value=SimpleNamespace(returncode=0)))
        self.spawn = self.enterContext(patch.object(recovery.subprocess, 'Popen', side_effect=self.launch))

    def process(self, pid=123, executable=None, state='S', started='12345'):
        directory = recovery.PROC / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / 'exe').write_text(str(executable or recovery.APP))
        self.alive = executable is None
        (directory / 'comm').write_text('orcaslicer_main')
        (directory / 'environ').write_bytes(b'HOME=/config\0XDG_RUNTIME_DIR=/config/.XDG\0PRIVATE_TOKEN=secret\0')
        (directory / 'stat').write_text(f'{pid} (orca-slicer) {state} ' + ' '.join(['0'] * 18 + [started]))

    def launch(self, *args, **kwargs):
        self.process()
        return SimpleNamespace(pid=123, poll=lambda: None)

    def test_running_orca_is_restored_without_launch(self):
        self.process()
        self.assertEqual(recovery.restore()['action'], 'restored')
        self.spawn.assert_not_called()
        self.assertEqual([c.args[0] for c in self.window.call_args_list], ['find', 'focus'])

    def test_real_process_detection_matches_executable(self):
        own_process = SimpleNamespace(iterdir=lambda: [Path('/proc') / str(os.getpid())])
        with patch.object(recovery, 'PROC', own_process), patch.object(recovery, 'APP', Path(sys.executable)):
            self.assertIn(os.getpid(), REAL_RUNNING())
        with patch.object(recovery, 'PROC', own_process):
            self.assertNotIn(os.getpid(), REAL_RUNNING())

    def test_running_without_window_is_never_restarted(self):
        self.process()
        self.window.return_value.returncode = 1
        self.assertEqual(recovery.restore()['action'], 'running')
        self.spawn.assert_not_called()

    def test_exited_process_starts_once_in_recorded_session(self):
        self.assertEqual(recovery.restore()['action'], 'started')
        self.assertEqual(recovery.restore()['action'], 'restored')
        self.spawn.assert_called_once()
        self.assertEqual(self.spawn.call_args.kwargs['env']['XDG_RUNTIME_DIR'], '/config/.XDG')
        self.assertTrue(self.spawn.call_args.kwargs['start_new_session'])

    def test_concurrent_requests_cannot_duplicate_launch(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            actions = list(pool.map(lambda _: recovery.restore()['action'], range(8)))
        self.spawn.assert_called_once()
        self.assertEqual(actions.count('started'), 1)
        self.assertEqual(actions.count('restored'), 7)

    def test_existing_slow_launcher_is_not_duplicated(self):
        self.process(executable=Path('/usr/bin/bash'))
        recovery.remember_start(123)
        self.assertEqual(recovery.restore()['action'], 'starting')
        self.spawn.assert_not_called()

    def test_reused_pid_does_not_block_relaunch(self):
        self.process(executable=Path('/usr/bin/bash'), started='22222')
        recovery.STARTING.write_text(json.dumps({'pid': 123, 'started': '11111'}))
        with patch.object(recovery, 'running', side_effect=[[], [456]]), patch.object(recovery, 'remember_start'):
            self.spawn.side_effect = None
            self.spawn.return_value = SimpleNamespace(pid=456, poll=lambda: None)
            self.assertEqual(recovery.restore()['action'], 'started')
        self.spawn.assert_called_once()

    def test_missing_session_fails_without_launch(self):
        recovery.SESSION.unlink()
        with self.assertRaisesRegex(RuntimeError, 'session is not ready'):
            recovery.restore()
        self.spawn.assert_not_called()

    def test_failed_launch_reports_error(self):
        self.process(executable=Path('/usr/bin/bash'))
        self.spawn.side_effect = None
        self.spawn.return_value = SimpleNamespace(pid=123, poll=lambda: 1)
        with self.assertRaisesRegex(RuntimeError, 'exited during startup'):
            recovery.restore()
        self.assertFalse(recovery.STARTING.exists())

    def test_session_cache_is_private_and_excludes_secrets(self):
        self.assertEqual(recovery.SESSION.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('secret', recovery.SESSION.read_text())

    def test_http_restore_requires_explicit_mutation_header_and_no_login(self):
        server = login.ThreadingHTTPServer(('127.0.0.1', 0), login.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.process()
        url = f'http://127.0.0.1:{server.server_port}/restore-orca'
        try:
            for headers, body in [({'Content-Type': 'application/json'}, b'{}'),
                                  ({'Content-Type': 'application/json', 'X-Orca-Action': 'restore'}, b'{"pid":1}')]:
                with self.assertRaises(HTTPError) as result:
                    urlopen(Request(url, data=body, headers=headers))
                self.assertEqual(result.exception.code, 403)
            response = urlopen(Request(url, data=b'{}', headers={'Content-Type': 'application/json', 'X-Orca-Action': 'restore'}))
            self.assertEqual(json.load(response)['action'], 'restored')
            self.spawn.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
