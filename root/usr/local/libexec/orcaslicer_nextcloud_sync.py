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
from urllib.parse import quote

PROJECTS = Path('/config/Projects')
CHECK = '.orcaslicer-sync-check'
EXCLUDES = ['*.part', '*.partial', '*.tmp', '*.new']


def identity(credentials):
    return hashlib.sha256(json.dumps([credentials.get('server'), credentials.get('userId'), credentials.get('folder'), EXCLUDES]).encode()).hexdigest()[:24]


def project_guard():
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
    if states[0].get('path') or states[0].get('busy'):
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
        self.phase = 'disabled'
        self.progress = {}
        self.started_at = None
        self.finished_at = None
        self.message = 'Sync is off.'
        self.last_success = None
        self.last_identity = None
        try:
            previous = json.loads((login.directory / 'last-success.json').read_text())
            self.last_success = previous['time']
            self.last_identity = previous['identity']
        except (OSError, ValueError, KeyError):
            pass
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def status(self):
        credentials = self.login.credentials or {}
        enabled = bool(credentials.get('sync_enabled'))
        message = self.message if enabled else ('Stopping sync…' if self.running else 'Sync is off.')
        phase = ('requested' if self.requested else self.phase) if enabled else ('stopping' if self.running else 'disabled')
        return {'enabled': enabled, 'running': self.running, 'message': message,
                'phase': phase, 'progress': dict(self.progress),
                'started_at': self.started_at, 'finished_at': self.finished_at,
                'last_success': self.last_success if identity(credentials) == self.last_identity else None}

    def request_now(self):
        with self.login.lock:
            if not (self.login.credentials or {}).get('sync_enabled'):
                raise ValueError('Start syncing before requesting a run.')
            if not self.running:
                self.requested = True
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
                    log.write(text + '\n')
                    self.update_progress(text)
            log.flush()
        interruption = None
        start = time.monotonic()
        while self.process.poll() is None:
            drain()
            try:
                guard = project_guard()
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
            log.write(text)
            self.update_progress(text)
        output.close()
        with self.login.lock:
            self.process = None
        if interruption:
            if interruption == 'The transfer timed out.':
                raise TimeoutError(interruption)
            raise InterruptedError(interruption)
        if code:
            raise RuntimeError(f'Sync failed (code {code}). See the private sync log. Retrying in 30 seconds.')

    def run(self, credentials):
        guard = project_guard()
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
        with (self.login.directory / 'sync.lock').open('a') as lock, (directory / 'sync.log').open('w') as log:
            os.chmod(log.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                except InterruptedError as error:
                    self.phase = 'waiting'
                    self.message = str(error)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    self.phase = 'error'
                    self.message = str(error) if not isinstance(error, subprocess.SubprocessError) else 'Could not start the sync process.'
                finally:
                    with self.login.lock:
                        self.running = False
                        self.finished_at = time.time()
            self.wake.wait(30)
