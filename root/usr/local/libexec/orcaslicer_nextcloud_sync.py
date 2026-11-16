"""Run one persistent rclone bisync session for the selected Nextcloud folder."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import re
import unicodedata
from collections import deque
from urllib.parse import quote

PROJECTS = Path('/config/Projects')
CHECK = '.orcaslicer-sync-check'
EXCLUDES = ['*.part', '*.partial', '*.tmp', '*.new']


def error_summary(lines, secrets=()):
    messages, names = [], {}
    for line in lines:
        message = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
        for secret in secrets:
            if secret:
                message = message.replace(secret, '[redacted]')
        message = re.sub(r'https?://[^\s<>"\']+', '[server URL]', message)
        message = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:basic|bearer)\s+\S+', r'\1[redacted]', message)
        message = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', ' ', message).strip()
        match = re.search(r'Path[12] file not found in Path[12]\s+-\s+(.*)', message)
        if match:
            name = match[1].strip()
            canonical = unicodedata.normalize('NFC', name)
            if canonical in names and names[canonical] != name:
                return f'Filename encoding mismatch between local files and Nextcloud: {canonical[:300]}. Both sides could not be verified as matching.'
            names[canonical] = name
        if (message and not message.startswith(('Bisync aborted', 'Failed to bisync: bisync aborted'))
                and message not in messages):
            messages.append(message.replace('Path1', 'Local').replace('Path2', 'Nextcloud'))
    return '\n'.join(message[:600] for message in messages[:3]) or 'The sync process returned no detailed error.'


def identity(credentials):
    parts = [credentials.get('server'), credentials.get('userId'), credentials.get('folder'), EXCLUDES]
    if credentials.get('_managed_projects'):
        parts.append(credentials['_managed_projects'])
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]


def project_guard(allow_open=False):
    running = any(p.name.isdigit() and (p / 'comm').exists()
                  and 'orcaslicer' in (p / 'comm').read_text(errors='replace')
                  for p in Path('/proc').iterdir())
    if not running:
        return ''
    states = []
    for path in Path('/run/orcaslicer-project-storage').glob('*/state.json'):
        try:
            state = json.loads(path.read_text())
            if not isinstance(state, dict):
                continue
            if time.time() - state.get('time', 0) < 3:
                states.append(state)
        except (OSError, ValueError):
            continue
    if len(states) != 1 or states[0].get('error'):
        return 'Waiting for Orca project status. Enable the Project storage plugin.'
    if states[0].get('busy'):
        return 'Waiting for Orca to finish loading, saving or slicing.'
    if states[0].get('path') and not allow_open:
        return 'Waiting for the saved project to close. Open a new project in Orca to resume syncing.'
    return ''


class Sync:
    def __init__(self, login):
        self.login = login
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.process = None
        self.running = False
        self.requested = False
        self.manual_requested = False
        self.manual_running = False
        self.phase = 'disabled'
        self.progress = {}
        self.started_at = None
        self.finished_at = None
        self.message = 'Sync is off.'
        self.last_success = None
        self.last_identity = None
        self.last_error = None
        try:
            previous = json.loads((login.directory / 'last-success.json').read_text())
            self.last_success = previous['time']
            self.last_identity = previous['identity']
        except (OSError, ValueError, KeyError):
            pass
        self.thread = threading.Thread(target=self.loop, daemon=True)
        try:
            error = json.loads((login.directory / 'last-error.json').read_text())
            if (isinstance(error, dict) and isinstance(error.get('message'), str)
                    and isinstance(error.get('time'), (int, float))
                    and not (error.get('identity') == self.last_identity and self.last_success
                             and self.last_success >= error['time'])):
                self.last_error = error
        except (OSError, ValueError):
            pass

    def status(self):
        credentials = self.login.credentials or {}
        handoff = getattr(self.login, 'handoff', None)
        if handoff:
            credentials = dict(credentials, _managed_projects=handoff.project_names())
        enabled = bool(credentials.get('sync_enabled'))
        message = self.message if enabled else ('Stopping sync…' if self.running else 'Sync is off.')
        phase = ('requested' if self.requested else self.phase) if enabled else ('stopping' if self.running else 'disabled')
        return {'enabled': enabled, 'running': self.running,
                'editor_locked': self.manual_requested or self.manual_running, 'message': message,
                'phase': phase, 'progress': dict(self.progress),
                'started_at': self.started_at, 'finished_at': self.finished_at,
                'last_success': self.last_success if identity(credentials) == self.last_identity else None,
                'last_error': self.last_error if self.last_error and self.last_error.get('identity') == identity(credentials) else None}

    def record_error(self, message):
        credentials = dict(self.login.credentials or {})
        handoff = getattr(self.login, 'handoff', None)
        if handoff:
            credentials['_managed_projects'] = handoff.project_names()
        message = error_summary([message], [credentials.get('appPassword', '')])
        self.last_error = {'message': message, 'time': time.time(), 'identity': identity(credentials)}
        try:
            path = self.login.directory / 'last-error.new'
            with path.open('w') as stream:
                os.chmod(path, 0o600)
                json.dump(self.last_error, stream)
            path.replace(self.login.directory / 'last-error.json')
        except OSError:
            pass  # error reporting must not stop the worker

    def clear_error(self):
        self.last_error = None
        try:
            (self.login.directory / 'last-error.json').unlink(missing_ok=True)
        except OSError:
            pass

    def request_now(self):
        with self.login.lock:
            if not (self.login.credentials or {}).get('sync_enabled'):
                raise ValueError('Start syncing before requesting a run.')
            if not self.running:
                self.requested = True
                self.manual_requested = True
                self.message = 'Sync requested. Checking for changes shortly…'
                self.wake.set()

    def update_progress(self, line):
        try:
            stats = json.loads(line).get('stats')
        except (ValueError, AttributeError):
            return
        if not isinstance(stats, dict):
            return
        fields = ('bytes', 'totalBytes', 'transfers', 'totalTransfers', 'checks', 'totalChecks', 'speed', 'deletes', 'elapsedTime', 'eta')
        progress = {key: stats[key] for key in fields if isinstance(stats.get(key), (int, float))
                    and math.isfinite(stats[key]) and stats[key] >= 0}
        with self.login.lock:
            self.progress = progress
            if self.phase == 'checking' and (progress.get('bytes', 0) > 0 or stats.get('transferring')):
                self.phase = 'transferring'
                self.message = 'Syncing files…'

    def enable(self, value):
        with self.login.lock:
            credentials = self.login.credentials
            if not credentials or not credentials.get('folder') or credentials['folder'] == '/':
                raise ValueError('Choose a dedicated Nextcloud subfolder.')
            updated = dict(credentials, sync_enabled=value)
            self.login.save(updated)
            self.login.credentials = updated
            if not value:
                self.interrupt()
                self.requested = False
                self.manual_requested = False
                self.message = 'Sync is off.'
            else:
                self.requested = True
                self.message = 'Sync requested. Checking for changes shortly…'
        self.wake.set()

    def interrupt(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)

    def close(self):
        self.closed.set()
        self.wake.set()
        with self.login.lock:
            self.interrupt()
        self.thread.join(timeout=12)
        if self.process and self.process.poll() is None:
            self.process.kill()

    def command(self, arguments, environment, log):
        with self.login.lock:
            if self.closed.is_set() or not (self.login.credentials or {}).get('sync_enabled'):
                raise InterruptedError('Sync is off.')
            self.process = subprocess.Popen(['rclone', '--config', '/dev/null', '--use-json-log',
                                             '--stats', '1s', '--stats-log-level', 'NOTICE', *arguments],
                                            env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = self.process.stdout
        os.set_blocking(output.fileno(), False)
        pending = b''
        errors = deque(maxlen=32)

        def record(line):
            log.write(line + '\n')
            self.update_progress(line)
            try:
                item = json.loads(line)
                if str(item.get('level', '')).lower() in ('error', 'critical', 'fatal'):
                    errors.append(str(item.get('msg', item.get('message', ''))))
            except (ValueError, AttributeError):
                if re.search(r'(?i)\berror\b|\bfailed\b|\bdenied\b', line):
                    errors.append(line)

        def drain():
            nonlocal pending
            while True:
                try:
                    chunk = os.read(output.fileno(), 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    text = line.decode('utf-8', errors='replace')
                    record(text)
            log.flush()
        interruption = None
        start = time.monotonic()
        while self.process.poll() is None:
            drain()
            try:
                guard = project_guard(allow_open=self.manual_running)
            except OSError:
                guard = 'Orca project status is currently unavailable.'
            with self.login.lock:
                stop = self.closed.is_set() or not (self.login.credentials or {}).get('sync_enabled')
            if guard or stop or time.monotonic() - start > 900:
                if not interruption:
                    interruption = guard or ('Sync is off.' if stop else 'The transfer timed out.')
                    self.process.send_signal(signal.SIGINT)
                    interrupted_at = time.monotonic()
                elif time.monotonic() - interrupted_at > 10:
                    self.process.kill()
            time.sleep(.5)
        code = self.process.wait()
        drain()
        if pending:
            text = pending.decode('utf-8', errors='replace')
            record(text)
        output.close()
        with self.login.lock:
            self.process = None
        if interruption:
            if interruption == 'The transfer timed out.':
                raise TimeoutError(interruption)
            raise InterruptedError(interruption)
        if code:
            secrets = [(self.login.credentials or {}).get('appPassword', ''), environment.get('RCLONE_CONFIG_NEXTCLOUD_PASS', '')]
            raise RuntimeError(f'Sync failed (code {code}): {error_summary(errors, secrets)}')

    def run(self, credentials):
        with (self.login.directory / 'sync.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handoff = getattr(self.login, 'handoff', None)
            if handoff:
                from orcaslicer_handoff import Failure
                credentials = dict(credentials, _managed_projects=handoff.project_names())
                # Both engines hold the same lock; they can never write one folder concurrently.
                guard = project_guard(allow_open=self.manual_running)
                if guard:
                    raise InterruptedError(guard)
                try:
                    dedicated = handoff.credentials()
                    if dedicated:
                        handoff.sync_owned(dedicated)
                except Failure as error:
                    raise RuntimeError(error.message) from None
            return self.run_legacy(credentials)

    def run_legacy(self, credentials):
        guard = project_guard(allow_open=self.manual_running)
        if guard:
            raise InterruptedError(guard)
        if credentials.get('folder') in (None, '/') or not credentials.get('userId'):
            raise ValueError('Choose a Nextcloud project folder first.')
        key = identity(credentials)
        directory = self.login.directory / 'bisync' / key
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        initialized = directory / 'initialized'
        first = not initialized.exists()
        if first:
            PROJECTS.mkdir(parents=True, exist_ok=True)
        if not PROJECTS.is_dir():
            raise ValueError('The local Projects folder is missing. Sync is paused.')
        environment = dict(os.environ)
        obscured = subprocess.run(['rclone', 'obscure', '-'], input=credentials['appPassword'] + '\n',
                                  capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        environment.update(RCLONE_CONFIG_NEXTCLOUD_TYPE='webdav', RCLONE_CONFIG_NEXTCLOUD_VENDOR='nextcloud',
                           RCLONE_CONFIG_NEXTCLOUD_URL=credentials['server'] + '/remote.php/dav/files/' + quote(credentials['userId'], safe='') + '/',
                           RCLONE_CONFIG_NEXTCLOUD_USER=credentials['loginName'], RCLONE_CONFIG_NEXTCLOUD_PASS=obscured)
        remote = 'nextcloud:' + credentials['folder'].lstrip('/')
        with (directory / 'sync.log').open('w') as log:
            os.chmod(log.name, 0o600)
            if first:
                self.phase = 'preparing'
                self.message = 'Preparing the first sync…'
                (PROJECTS / CHECK).write_text(key)
                self.command(['copyto', str(PROJECTS / CHECK), remote + '/' + CHECK, '--ignore-existing'], environment, log)
            self.phase = 'checking'
            self.message = 'Checking for changes…'
            self.progress = {}
            arguments = ['bisync', str(PROJECTS), remote, '--workdir', str(directory),
                         '--check-access', '--check-filename', CHECK, '--resilient', '--recover', '--max-lock', '2m',
                         '--conflict-resolve', 'none', '--conflict-loser', 'num', '--conflict-suffix', 'local,cloud',
                         '--suffix-keep-extension', '--create-empty-src-dirs', '--max-delete', '100',
                         '--compare', 'size,modtime,checksum', '--checksum', '--download-hash',
                         '--retries', '1', '--low-level-retries', '1', '--contimeout', '10s', '--timeout', '60s', '-v']
            for pattern in EXCLUDES:
                arguments.extend(['--exclude', pattern])
            if credentials.get('_managed_projects'):
                from orcaslicer_handoff import legacy_exclusion
                for project in credentials['_managed_projects']:
                    arguments.extend(['--exclude', legacy_exclusion(project)])
            if first:
                # Preserve remote files overwritten by the initial merge, outside the selected folder.
                history = 'nextcloud:.orcaslicer-sync-history/' + key + '/' + time.strftime('%Y%m%d-%H%M%S')
                arguments.extend(['--resync', '--backup-dir2', history])
            self.command(arguments, environment, log)
            initialized.touch()
            self.last_success = time.time()
            self.last_identity = key
            temporary = self.login.directory / 'last-success.new'
            temporary.write_text(json.dumps({'time': self.last_success, 'identity': key}))
            temporary.replace(self.login.directory / 'last-success.json')
            self.message = 'Up to date.'
            self.phase = 'idle'

    def loop(self):
        while not self.closed.is_set():
            self.wake.clear()
            with self.login.lock:
                credentials = dict(self.login.credentials or {})
                enabled = credentials.get('sync_enabled', False)
                if enabled:
                    self.manual_running = self.manual_requested
                    self.manual_requested = False
                    self.requested = False
                    self.running = True
                    self.phase = 'checking'
                    self.progress = {}
                    self.started_at = time.time()
                    self.finished_at = None
                    self.message = 'Checking for changes…'
            if enabled:
                try:
                    self.run(credentials)
                    self.clear_error()
                except InterruptedError as error:
                    self.phase = 'waiting'
                    self.message = str(error)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    self.phase = 'error'
                    self.message = str(error) if not isinstance(error, subprocess.SubprocessError) else 'Could not start the sync process.'
                    self.record_error(self.message)
                    self.message = self.last_error['message']
                finally:
                    with self.login.lock:
                        self.running = False
                        self.manual_running = False
                        self.finished_at = time.time()
            self.wake.wait(30)
