"""Resolve SCREF1 return links without downloading or changing model geometry."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
import time
from urllib.parse import urlsplit

STATE = Path('/run/orcaslicer-project-storage')
PROJECTS = Path('/config/Projects')
DESKTOP = Path('/config/Desktop')
CACHE = Path('/data/orcaslicer/solidcraft/sources.json')
KEY = re.compile(r'[a-f0-9]{64}')
LOCK = threading.Lock()


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def text(value, empty=False):
    if (not isinstance(value, str) or (not value and not empty)
            or len(value.encode()) > 4096 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise ValueError('Invalid SolidCraft reference.')
    return value


def manifest(raw):
    if len(raw) > 65536:
        raise ValueError('SolidCraft source manifest is too large.')
    value = json.loads(raw)
    if value['version'] != 1 or not KEY.fullmatch(value['key']):
        raise ValueError('Unsupported SolidCraft source manifest.')
    service = text(value['service'])
    url = urlsplit(service)
    if (url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password
            or url.path or url.query or url.fragment or service.endswith('/')):
        raise ValueError('Invalid SolidCraft service origin.')
    origin = value['origin']
    if set(origin) != {'account', 'room', 'id', 'folder'}:
        raise ValueError('Invalid SolidCraft cloud origin.')
    origin = {field: text(origin[field], empty=field == 'folder') for field in ('account', 'room', 'id', 'folder')}
    bodies = value['selection']
    if not isinstance(bodies, list) or len(bodies) > 256:
        raise ValueError('Invalid SolidCraft selection.')
    canonical, ordering = [], []
    for body in bodies:
        if set(body) != {'feature', 'slot', 'component', 'occurrence'}:
            raise ValueError('Invalid SolidCraft body reference.')
        numbers = [body[k] for k in ('feature', 'slot', 'component')]
        occurrence = body['occurrence']
        if (not isinstance(occurrence, list) or len(occurrence) > 64
                or any(type(n) is not int or not 0 <= n < 2 ** 64 for n in numbers + occurrence)
                or not body['feature'] or body['slot'] > 1000000):
            raise ValueError('Invalid SolidCraft body reference.')
        canonical.append(dict(zip(('feature', 'slot', 'component', 'occurrence'), [*numbers, occurrence])))
        ordering.append((*numbers, occurrence))
    if any(a >= b for a, b in zip(ordering, ordering[1:])):
        raise ValueError('SolidCraft selection is not canonical.')
    expected = hashlib.sha256(compact(['SCREF1', service, origin['account'], origin['room'], canonical])).hexdigest()
    if expected != value['key']:
        raise ValueError('SolidCraft source identity does not match its manifest.')
    encoded = base64.urlsafe_b64encode(compact(origin)).decode().rstrip('=')
    if len(encoded) > 16384:
        raise ValueError('SolidCraft return link is too long.')
    # Construct the destination ourselves; never follow a manifest-supplied URL.
    return {'key': expected, 'name': text(value['design_name']), 'service': service,
            'url': service + '/?craft-source=' + encoded, 'snapshot': value.get('snapshot') is True}


def safe_source(value):
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in (PROJECTS, DESKTOP)):
        return None
    return resolved


def source_key(path):
    if path.suffix.lower() != '.stl':
        return None
    with path.open('rb') as stream:
        header = stream.read(84)
        size = os.fstat(stream.fileno()).st_size
    if len(header) != 84 or not header.startswith(b'SCREF1:') or header[71:80] != bytes(9):
        return None
    count = struct.unpack('<I', header[80:84])[0]
    key = header[7:71].decode('ascii')
    return key if count and size == 84 + 50 * count and KEY.fullmatch(key) else None


def read_json(path, limit=262144):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Source status is too large.')
    return json.loads(raw)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    part = path.with_suffix('.part')
    with part.open('w') as stream:
        os.chmod(part, 0o600)
        json.dump(value, stream)
    part.replace(path)


class Sources:
    def __init__(self):
        self.scanned = 0
        self.manifests = {}
        try:
            self.cache = read_json(CACHE, 4 * 1024 * 1024)
            if not isinstance(self.cache, dict):
                self.cache = {}
        except (OSError, ValueError):
            self.cache = {}

    def scan(self):
        if time.monotonic() - self.scanned < 5:
            return
        self.scanned = time.monotonic()
        found, ambiguous = {}, set()
        # Bound traversal, including directories that contain no source manifests.
        visited = 0
        for root in (PROJECTS, DESKTOP):
            for folder, directories, files in os.walk(root, followlinks=False):
                visited += 1
                directories[:] = sorted(d for d in directories if not d.startswith('.'))
                if visited > 2000:
                    break
                for name in files:
                    if not re.fullmatch(r'SCREF1-[a-f0-9]{64}\.scsource', name):
                        continue
                    path = safe_source(str(Path(folder) / name))
                    if path is None:
                        continue
                    try:
                        value = read_json(path, 65536)
                        link = manifest(compact(value))
                        if name != f'SCREF1-{link["key"]}.scsource':
                            continue
                        # Mutable origin hints may differ; immutable destination must agree.
                        old = found.get(link['key'])
                        if old and old['service'] != link['service']:
                            ambiguous.add(link['key'])
                        found[link['key']] = link
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        continue
        self.manifests = {key: link for key, link in found.items() if key not in ambiguous}

    def resolve(self, source, project=''):
        if isinstance(source, str) and source and not Path(source).is_absolute():
            saved = safe_source(project)
            if saved is None or '..' in Path(source).parts:
                return None
            matches = []
            for folder in (saved.parent, saved.parent / 'Sources', saved.parent / 'Quellen'):
                link = self.resolve(str(folder / source))
                if link:
                    matches.append(link)
            # Filenames only locate source bytes; SCREF1 establishes identity.
            return matches[0] if len({link['key'] for link in matches}) == 1 else None
        path = safe_source(source)
        if path is None:
            return None
        try:
            key = source_key(path)
            if not key:
                return None
        except FileNotFoundError:
            # Preserve a previously verified association when Orca reopens an
            # embedded mesh whose original source is temporarily unavailable.
            previous = self.cache.get(str(path))
            return previous if isinstance(previous, dict) else None
        link = self.manifests.get(key)
        if not link:
            previous = self.cache.get(str(path))
            link = previous if isinstance(previous, dict) and previous.get('key') == key else None
        if link:
            self.cache[str(path)] = link
        return link

    def status(self):
        with LOCK:
            self.scan()
            requested = None
            previous_cache = compact(self.cache)
            for directory in sorted(STATE.glob('[0-7]')):
                try:
                    state = read_json(directory / 'state.json')
                    if time.time() - state['time'] > 3 or state.get('error'):
                        continue
                    links = []
                    for obj in state.get('source_objects', []):
                        try:
                            link = self.resolve(obj['source'], state.get('path', ''))
                        except (OSError, ValueError, KeyError, TypeError):
                            link = None
                        if link:
                            links.append(dict(link, object_id=obj['id'], object_name=obj['name']))
                    data = {'session': state['session'], 'time': time.time(), 'links': links}
                    write_json(directory / 'solidcraft.json', data)
                    try:
                        action = read_json(directory / 'solidcraft-request.json')
                        if action['session'] == state['session'] and 0 <= time.time() - action['time'] < 120:
                            match = next((link for link in links if link['key'] == action['key']
                                          and link['object_id'] == action['object_id']), None)
                            if match:
                                requested = dict(match, nonce=action['nonce'])
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            if compact(self.cache) != previous_cache:
                write_json(CACHE, self.cache)
            return {'request': requested}
