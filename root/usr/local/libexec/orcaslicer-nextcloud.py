#!/usr/bin/env python3
"""Nextcloud Login Flow v2; credentials stay behind the browser entrypoint."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
import xml.etree.ElementTree as ET


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def server_url(value):
    url = urlsplit(value.strip())
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(c.isspace() for c in value)
            or any(c in value for c in ('\\', '\r', '\n'))):
        raise ValueError('Enter an HTTPS address without embedded credentials.')
    url.port  # reject malformed ports
    return urlunsplit((url.scheme, url.netloc, url.path.rstrip('/'), '', ''))


def same_server(base, value):
    url = server_url(value)
    a, b = urlsplit(base), urlsplit(url)
    if (a.hostname, a.port or 443) != (b.hostname, b.port or 443):
        raise ValueError('Nextcloud returned a different server address.')
    if b.path != a.path and not b.path.startswith(a.path + '/'):
        raise ValueError('Nextcloud returned an unexpected server path.')
    return url


def request(url, data=None, method='POST', credentials=None, extra_headers=None, xml=False):
    headers = {'User-Agent': 'OrcaSlicer project storage', 'Accept': 'application/json'}
    if credentials:
        encoded = base64.b64encode(f"{credentials['loginName']}:{credentials['appPassword']}".encode()).decode()
        headers.update(Authorization='Basic ' + encoded, **{'OCS-APIREQUEST': 'true'})
    body = data if isinstance(data, bytes) else urlencode(data or {}).encode()
    if method == 'GET':
        body = None
    headers['Content-Type'] = 'application/x-www-form-urlencoded'
    headers.update(extra_headers or {})
    limit = 4 * 1024 * 1024 if xml else 65536
    with build_opener(NoRedirect()).open(Request(url, data=body, method=method, headers=headers), timeout=15) as response:
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('The Nextcloud response is too large.')
        if xml:
            return ET.fromstring(raw)
        return json.loads(raw) if raw and method != 'DELETE' else None


def folder_path(value):
    if not isinstance(value, str) or not value.startswith('/') or any(ord(c) < 32 or c == '\\' for c in value):
        raise ValueError('Invalid folder path.')
    parts = value.split('/')
    if any(part in ('.', '..') for part in parts):
        raise ValueError('Invalid folder path.')
    return '/' + '/'.join(part for part in parts if part)


class Login:
    def __init__(self, directory=None):
        self.directory = directory or Path('/data/orcaslicer/nextcloud')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.path = self.directory / 'credentials.json'
        self.credentials = None
        if self.path.exists():
            self.path.chmod(0o600)
            self.credentials = json.loads(self.path.read_text())
        self.lock = threading.RLock()
        self.pending = None
        self.message = ''
        self.sync = None

    def status(self):
        with self.lock:
            data = self.credentials or self.pending or {}
            return {'connected': bool(self.credentials), 'pending': bool(self.pending),
                    'server': data.get('server', ''), 'user': data.get('loginName', ''),
                    'folder': data.get('folder'),
                    'sync': self.sync.status() if self.sync else {'enabled': False, 'running': False},
                    'message': self.message}

    def save(self, credentials):
        temporary = self.path.with_suffix('.new')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(credentials, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def folders(self, value):
        path = folder_path(value)
        with self.lock:
            credentials = self.credentials
            if not credentials:
                raise ValueError('Connect to Nextcloud first.')
        # Login Flow may return an email login; WebDAV needs the account's UID.
        uid = credentials.get('userId')
        if not uid:
            user = request(credentials['server'] + '/ocs/v2.php/cloud/user?format=json', method='GET', credentials=credentials)
            uid = user['ocs']['data']['id']
            if not isinstance(uid, str) or not uid or '/' in uid:
                raise ValueError('Nextcloud did not return a valid account ID.')
            with self.lock:
                if self.credentials is not credentials:
                    raise ValueError('The connection has changed.')
                credentials['userId'] = uid
        url = credentials['server'] + '/remote.php/dav/files/' + quote(uid, safe='') + quote(path, safe='/')
        url = url.rstrip('/') + '/'
        document = request(url, data=b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
                           method='PROPFIND', credentials=credentials,
                           extra_headers={'Depth': '1', 'Content-Type': 'application/xml', 'Accept': 'application/xml'}, xml=True)
        current = unquote(urlsplit(url).path).rstrip('/')
        folders = {}
        found = False
        for response in document.findall('{DAV:}response'):
            href = urljoin(url, response.findtext('{DAV:}href', ''))
            same_server(credentials['server'], href)
            name = unquote(urlsplit(href).path).rstrip('/')
            collection = any(' 200 ' in prop.findtext('{DAV:}status', '')
                             and prop.find('{DAV:}prop/{DAV:}resourcetype/{DAV:}collection') is not None
                             for prop in response.findall('{DAV:}propstat'))
            if not collection:
                continue
            if name == current:
                found = True
            elif name.startswith(current + '/'):
                child = name[len(current) + 1:]
                if child and '/' not in child and child not in ('.', '..'):
                    folders[child] = {'name': child, 'path': folder_path(path.rstrip('/') + '/' + child)}
        if not found:
            raise ValueError('The folder does not exist or is not accessible.')
        return credentials, {'path': path, 'folders': sorted(folders.values(), key=lambda item: item['name'].casefold())}

    def select_folder(self, value):
        credentials, listing = self.folders(value)
        with self.lock:
            if self.sync and (self.sync.running or (self.credentials or {}).get('sync_enabled')):
                raise ValueError('Stop syncing before changing the folder.')
            if self.credentials is not credentials:
                raise ValueError('The connection has changed.')
            updated = dict(credentials, folder=listing['path'])
            self.save(updated)
            self.credentials = updated
        return self.status()

    def start(self, value):
        server = server_url(value)
        with self.lock:
            if self.credentials or self.pending:
                raise ValueError('Already connected or a sign-in is in progress.')
            generation = secrets.token_hex(16)
            self.pending = {'server': server, 'generation': generation}
            self.message = 'Approve the sign-in in Nextcloud.'
        try:
            result = request(server + '/index.php/login/v2')
            login = same_server(server, result['login'])
            endpoint = same_server(server, result['poll']['endpoint'])
            token = result['poll']['token']
            if not isinstance(token, str) or not token:
                raise ValueError('Nextcloud did not return a valid sign-in token.')
            with self.lock:
                if not self.pending or self.pending['generation'] != generation:
                    raise ValueError('Sign-in was cancelled.')
            threading.Thread(target=self.poll, args=(server, endpoint, token, generation), daemon=True).start()
            return {'login': login}
        except Exception:
            with self.lock:
                if self.pending and self.pending['generation'] == generation:
                    self.pending = None
                    self.message = 'Could not start sign-in. Check the server address and connection.'
            raise

    def poll(self, server, endpoint, token, generation):
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            with self.lock:
                if not self.pending or self.pending['generation'] != generation:
                    return
            try:
                result = request(endpoint, {'token': token})
            except HTTPError as error:
                if error.code not in (404, 429) and error.code < 500:
                    break
            except (OSError, ValueError):
                pass
            else:
                try:
                    credentials = {'server': same_server(server, result['server']),
                                   'loginName': result['loginName'], 'appPassword': result['appPassword']}
                    if not all(isinstance(credentials[k], str) and credentials[k] for k in ('loginName', 'appPassword')):
                        raise ValueError('Invalid credentials')
                    with self.lock:
                        if not self.pending or self.pending['generation'] != generation:
                            # The one-use response arrived after cancellation: revoke it.
                            abandoned = True
                        else:
                            abandoned = False
                            self.save(credentials)
                            self.credentials = credentials
                            self.pending = None
                            self.message = 'Connected to Nextcloud.'
                    if abandoned:
                        self.revoke(credentials)
                    return
                except (OSError, ValueError, KeyError, TypeError):
                    break
            time.sleep(2)
        with self.lock:
            if self.pending and self.pending['generation'] == generation:
                self.pending = None
                self.message = 'Sign-in expired or failed. Please connect again.'

    @staticmethod
    def revoke(credentials):
        try:
            request(credentials['server'] + '/ocs/v2.php/core/apppassword', method='DELETE', credentials=credentials)
            return True
        except HTTPError as error:
            return error.code == 401  # already revoked
        except (OSError, ValueError):
            return False

    def cancel(self):
        with self.lock:
            self.pending = None
            self.message = 'Sign-in cancelled.'

    def disconnect(self):
        with self.lock:
            if self.sync:
                self.sync.interrupt()
            self.pending = None
            credentials = self.credentials
            revoked = not credentials or self.revoke(credentials)
            self.path.unlink(missing_ok=True)
            self.credentials = None
            self.message = ('Disconnected.' if revoked else
                            'Disconnected locally. Revoke the app password in Nextcloud security settings; the server could not be reached.')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # do not log login URLs or tokens

    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed or reloaded after the operation completed

    def do_GET(self):
        if self.path == '/status':
            self.respond(200, self.server.login.status())
        else:
            self.respond(404, {'message': 'Unknown endpoint.'})

    def do_POST(self):
        # A custom header requires a CORS preflight for cross-origin browser requests.
        # We grant no CORS access.
        if self.headers.get('X-Orca-Nextcloud') != '1' or self.headers.get('Content-Type') != 'application/json':
            self.respond(403, {'message': 'Request not allowed.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                raise ValueError('Invalid request.')
            data = json.loads(self.rfile.read(size))
            if self.path == '/start':
                if not isinstance(data.get('server'), str):
                    raise ValueError('Enter a Nextcloud address.')
                self.respond(200, self.server.login.start(data['server']))
            elif self.path == '/cancel':
                self.server.login.cancel()
                self.respond(200, self.server.login.status())
            elif self.path == '/disconnect':
                self.server.login.disconnect()
                self.respond(200, self.server.login.status())
            elif self.path == '/folders':
                _, listing = self.server.login.folders(data.get('path', '/'))
                self.respond(200, listing)
            elif self.path == '/folder':
                self.respond(200, self.server.login.select_folder(data.get('path')))
            elif self.path == '/sync-start':
                self.server.login.sync.enable(True)
                self.respond(200, self.server.login.status())
            elif self.path == '/sync-stop':
                self.server.login.sync.enable(False)
                self.respond(200, self.server.login.status())
            elif self.path == '/sync-now':
                self.server.login.sync.request_now()
                self.respond(200, self.server.login.status())
            else:
                self.respond(404, {'message': 'Unknown endpoint.'})
        except (ValueError, TypeError, AttributeError):
            self.respond(400, {'message': 'Request failed. Check the server address, connection and folder path.'})
        except ET.ParseError:
            self.respond(502, {'message': 'Nextcloud did not return a valid folder listing.'})
        except (OSError, KeyError):
            self.respond(502, {'message': 'Nextcloud could not be reached or returned an invalid response.'})


if __name__ == '__main__':
    from orcaslicer_nextcloud_sync import Sync
    server = ThreadingHTTPServer(('127.0.0.1', 8093), Handler)
    server.login = Login()
    server.login.sync = Sync(server.login)
    server.login.sync.thread.start()
    def terminate(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    finally:
        server.login.sync.close()
