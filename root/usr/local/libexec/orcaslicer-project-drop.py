#!/usr/bin/env python3
"""Prepare uploaded models as named Orca projects, then use the native drop helper."""
import argparse
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

LOG = logging.getLogger('project-drop')
STATE_ROOT = Path('/run/orcaslicer-project-storage')
PROJECT_ROOT = Path.home() / 'Projects'


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def active_session():
    sessions = []
    for directory in STATE_ROOT.glob('*'):
        state = read_json(directory / 'state.json')
        if state and time.time() - state.get('time', 0) < 3:
            sessions.append((directory, state))
    if len(sessions) != 1:
        raise RuntimeError('Exactly one active Project storage plugin is required; keep Orca open and enable the plugin')
    directory, state = sessions[0]
    if state.get('error'):
        raise RuntimeError(state['error'])
    if state['mode'] != 'editor' or state['busy']:
        raise RuntimeError('Orca is busy or is not in the project editor')
    pending = read_json(STATE_ROOT / 'pending.json')
    if pending:
        if pending['session'] != state['session'] or pending_matches(state, pending):
            (STATE_ROOT / 'pending.json').unlink(missing_ok=True)
        else:
            raise RuntimeError(f"The previous project has not opened or its import is still pending. Complete Orca's dialogs, open {pending['path']}, or run Project storage > Cancel pending import; uploads remain on the Desktop")
    return directory, state


def pending_matches(state, pending):
    if state['path'] != pending['path'] or state.get('busy'):
        return False
    if pending.get('source'):
        return pending['source'] in state.get('sources', []) or state['objects'] > pending['objects']
    return True


def request(directory, state, action, timeout=30):
    current_directory, current = active_session()
    if current_directory != directory or current['session'] != state['session']:
        raise RuntimeError('The Orca session changed while preparing the upload')
    nonce = uuid.uuid4().hex
    temporary = directory / 'request.part'
    temporary.write_text(json.dumps({'nonce': nonce, 'action': action, 'expected': state}), encoding='utf-8')
    temporary.replace(directory / 'request.json')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = read_json(directory / 'reply.json')
        if reply and reply.get('nonce') == nonce:
            if reply.get('error'):
                raise RuntimeError(reply['error'])
            return reply
        time.sleep(0.2)
    raise RuntimeError('Orca did not answer; finish its dialogs and try the upload again')


def project_name(path):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', path.stem).strip(' .')[:120] or 'Project'


def new_directory(source):
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    name = project_name(source)
    for number in range(1, 10000):
        directory = PROJECT_ROOT / (name if number == 1 else f'{name}-{number}')
        try:
            directory.mkdir()
            return directory
        except FileExistsError:
            continue
    raise RuntimeError('Could not allocate a unique project folder')


def copy_source(source, directory):
    directory.mkdir(parents=True, exist_ok=True)
    for number in range(1, 10000):
        name = source.name if number == 1 else f'{source.stem}-{number}{source.suffix}'
        destination = directory / name
        try:
            output = destination.open('xb')
        except FileExistsError:
            continue
        try:
            with output, source.open('rb') as input_file:
                shutil.copyfileobj(input_file, output)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination
    raise RuntimeError('Could not allocate a unique source filename')


def run_process(command, timeout, cwd=None, log=None):
    with subprocess.Popen(command, cwd=cwd, stdout=log, stderr=log) as process:
        try:
            return process.wait(timeout=timeout)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def create_project(source, project, reply, directory, cli):
    template = Path(reply['template'])
    with zipfile.ZipFile(template) as archive:
        settings = json.loads(archive.read('Metadata/project_settings.config'))
        printer = {key: settings[key] for key in reply['printer_keys'] if key in settings}
        printer.update(type='machine', name=reply['printer_name'] or 'Project printer', **{'from': 'user', 'inherits': ''})
        machine = directory / 'machine.json'
        machine.write_text(json.dumps(printer), encoding='utf-8')
    geometry = directory / 'geometry.3mf'
    geometry.unlink(missing_ok=True)
    command = [str(cli), '--datadir', str(directory / 'cli-config'), '--load-settings', str(machine),
               '--arrange', '1', '--export-3mf', str(geometry), str(source)]
    with (directory / 'cli.log').open('w', encoding='utf-8') as log:
        result = run_process(command, 120, cwd=directory, log=log)
    if result or not geometry.is_file():
        raise RuntimeError(f'Orca CLI could not create the project (exit {result}); details: {directory / "cli.log"}')
    # Keep Orca's native config, profile differences and metadata; the CLI supplies the geometry.
    with zipfile.ZipFile(template) as original, zipfile.ZipFile(geometry) as model:
        replacements = {name: original.read(name) for name in original.namelist()
                        if name == 'Metadata/project_settings.config' or name.startswith('Metadata/') and name.endswith('.json')}
        root = ET.fromstring(model.read('3D/3dmodel.model'))
        namespace = root.tag.split('}')[0][1:]
        ET.register_namespace('', namespace)
        ET.register_namespace('p', 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06')
        old = ET.fromstring(original.read('3D/3dmodel.model'))
        for entry in list(root.findall('{*}metadata')):
            root.remove(entry)
        for index, entry in enumerate(old.findall('{*}metadata')):
            root.insert(index, entry)
        for name, value in [('Title', project.stem), ('Description', f'Original model: Sources/{source.name}')]:
            entries = [entry for entry in root.findall('{*}metadata') if entry.get('name') == name]
            if entries:
                entry = entries[0]
            else:
                entry = ET.Element(f'{{{namespace}}}metadata', name=name)
                root.insert(0, entry)
            entry.text = f'{entry.text}\n{value}' if name == 'Description' and entry.text else value
        replacements['3D/3dmodel.model'] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        staging = project.with_suffix('.part')
        with zipfile.ZipFile(staging, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            for name in model.namelist():
                output.writestr(name, replacements.pop(name, model.read(name)))
            for name, content in replacements.items():
                output.writestr(name, content)
        if project.exists():
            raise RuntimeError('Refusing to overwrite an existing project')
        staging.replace(project)


def mark_pending(state, project, source=None):
    pending = {'session': state['session'], 'path': str(project)}
    if source:
        pending.update(source=str(source), objects=state['objects'])
    (STATE_ROOT / 'pending.json').write_text(json.dumps(pending), encoding='utf-8')
    return pending


def drop(helper, target):
    try:
        run_process([str(helper), str(target)], 30)
    except subprocess.TimeoutExpired:
        LOG.warning('Drop helper timed out; checking the actual Orca state without retrying')


def wait_for_project(directory, state, pending, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = read_json(directory / 'state.json')
        if current and current.get('session') != state['session']:
            raise RuntimeError('The Orca session changed; prepared files are retained')
        if current and not current.get('error') and time.time() - current.get('time', 0) < 3 and pending_matches(current, pending):
            (STATE_ROOT / 'pending.json').unlink(missing_ok=True)
            return
        if not (STATE_ROOT / 'pending.json').exists():
            raise RuntimeError('Pending import cancelled; prepared files are retained')
        time.sleep(0.5)
    raise RuntimeError(f"Project opening/import was not confirmed. Complete Orca's dialogs, open {pending['path']}, or run Project storage > Cancel pending import; no drop is retried")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--helper', type=Path, required=True)
    parser.add_argument('--cli', type=Path, default=Path('/opt/orcaslicer/bin/orca-slicer'))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='[project-drop] %(message)s')
    signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(SystemExit(143)))
    try:
        source = args.source.absolute()
        if not stat.S_ISREG(source.lstat().st_mode) or not source.stat().st_size:
            raise RuntimeError('The upload must be a nonempty regular file')
        directory, state = active_session()
        if source.suffix.lower() == '.3mf':
            folder = new_directory(source)
            target = copy_source(source, folder)
            request(directory, state, 'check')
            pending = mark_pending(state, target)
            drop(args.helper, target)
            wait_for_project(directory, state, pending)
        elif state['path']:
            folder = Path(state['path']).parent
            target = copy_source(source, folder / 'Sources')
            request(directory, state, 'check')
            pending = mark_pending(state, Path(state['path']), source=target)
            drop(args.helper, target)
            wait_for_project(directory, state, pending)
        else:
            if state['objects']:
                raise RuntimeError('Save the existing unnamed project first; the uploaded file remains on the Desktop')
            reply = request(directory, state, 'template')
            folder = new_directory(source)
            copied = copy_source(source, folder / 'Sources')
            target = folder / f'{folder.name}.3mf'
            create_project(copied, target, reply, directory, args.cli)
            request(directory, state, 'check')
            pending = mark_pending(state, target)
            drop(args.helper, target)
            wait_for_project(directory, state, pending)
        LOG.info('Project: %s; file: %s', folder, target)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, zipfile.BadZipFile) as error:
        LOG.error('%s', error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
