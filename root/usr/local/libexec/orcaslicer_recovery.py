"""Restore Orca's window, or start it only when its process has exited."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

APP = Path('/opt/orcaslicer/bin/orca-slicer')
PROC = Path('/proc')
STATE = Path('/run/orcaslicer-project-storage')
SESSION = STATE / 'desktop-session.json'
STARTING = STATE / 'recovery-starting.json'
LOG = Path('/data/orcaslicer/log/recovery.log')
SESSION_KEYS = ('HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE',
                'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'XDG_SESSION_TYPE',
                'XDG_CURRENT_DESKTOP', 'DBUS_SESSION_BUS_ADDRESS', 'GDK_BACKEND',
                'GDK_SCALE', 'GDK_DPI_SCALE', 'GTK_THEME', 'GTK_CSD',
                'LIBGL_ALWAYS_SOFTWARE', 'GALLIUM_DRIVER', 'MESA_GL_VERSION_OVERRIDE',
                '__GLX_VENDOR_LIBRARY_NAME')


def running():
    pids = []
    executable = APP.resolve()
    for directory in PROC.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            if (directory / 'exe').resolve(strict=True) == executable:
                pids.append(int(directory.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # Never infer a crash from an inaccessible Orca process.
            try:
                if 'orcaslicer' in (directory / 'comm').read_text():
                    raise RuntimeError('Could not verify Orca process state. No new instance was started.') from None
            except (FileNotFoundError, ProcessLookupError):
                pass
    return pids


def remember_session(environment):
    selected = {key: environment[key] for key in SESSION_KEYS if key in environment}
    temporary = SESSION.with_suffix('.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(selected, stream)
    temporary.replace(SESSION)


def process_environment(pid):
    raw = (PROC / str(pid) / 'environ').read_bytes()
    return dict(item.decode().split('=', 1) for item in raw.split(b'\0') if b'=' in item)


def window_command(action, environment):
    control = dict(environment)
    if not control.get('XDG_RUNTIME_DIR'):
        control['XDG_RUNTIME_DIR'] = '/config/.XDG'
    if not control.get('WAYLAND_DISPLAY'):
        control['WAYLAND_DISPLAY'] = 'wayland-0'
    return subprocess.run(['wlrctl', 'toplevel', action, 'app_id:orca-slicer'],
                          env=control, capture_output=True, timeout=5)


def process_identity(pid):
    fields = (PROC / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    return fields[0], fields[19]


def remember_start(pid):
    _, started = process_identity(pid)
    temporary = STARTING.with_suffix('.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'pid': pid, 'started': started}, stream)
    temporary.replace(STARTING)


def launcher_pending():
    if STARTING.exists():
        marker = json.loads(STARTING.read_text())
        try:
            state, started = process_identity(marker['pid'])
            if state != 'Z' and started == marker['started']:
                return True
        except (FileNotFoundError, ProcessLookupError):
            pass
        STARTING.unlink(missing_ok=True)
    return False


def restore():
    fd = os.open(STATE / 'recovery.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pids = running()
        if pids:
            STARTING.unlink(missing_ok=True)
            environment = process_environment(pids[0])
            remember_session(environment)
            if window_command('find', environment).returncode:
                return {'action': 'running', 'message': 'Orca is running, but its window is not available yet. No new instance was started.'}
            if window_command('focus', environment).returncode:
                raise RuntimeError('Orca is running, but the window could not be restored. No new instance was started.')
            return {'action': 'restored', 'message': 'Orca is running. Its window has been brought forward.'}

        if launcher_pending():
            return {'action': 'starting', 'message': 'Orca is already starting. No additional instance was started.'}
        if not SESSION.exists():
            raise RuntimeError('The desktop session is not ready. No new instance was started.')
        # The wrapper records this at startup; it remains available after a crash.
        environment = dict(os.environ)
        environment.update(json.loads(SESSION.read_text()))
        LOG.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, 'ab') as output:
            child = subprocess.Popen(['/opt/orcaslicer/AppRun', '--datadir', '/data/orcaslicer'],
                                     env=environment, cwd='/config', stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        remember_start(child.pid)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if child.poll() is not None:
                STARTING.unlink(missing_ok=True)
                raise RuntimeError('Orca exited during startup. Check the Orca logs before trying again.')
            if running():
                STARTING.unlink(missing_ok=True)
                return {'action': 'started', 'message': 'Orca was not running and has been started again.'}
            time.sleep(.1)
        # The marker prevents another request from duplicating a slow launcher.
        return {'action': 'starting', 'message': 'Orca startup is taking longer than expected. Please wait before trying again.'}


if __name__ == '__main__':
    remember_start(os.getppid())
    remember_session(os.environ)
