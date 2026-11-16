#!/usr/bin/env python3
"""Exercise producer compatibility, object scoping and the plugin return action."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

LIB = Path(__file__).resolve().parents[2] / 'root/usr/local/libexec'
sys.path.insert(0, str(LIB))
import orcaslicer_solidcraft as sources


class SourceTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-tools'
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ('STATE', 'PROJECTS', 'DESKTOP'):
            path = self.root / name
            path.mkdir()
            self.enterContext(patch.object(sources, name, path))
        self.enterContext(patch.object(sources, 'CACHE', self.root / 'cache.json'))
        self.reference = {'account': 'existing-service-user-id', 'room': 'existing-provider-room',
                          'id': 'handle', 'folder': '/Slicer'}
        self.selection = [{'feature': 12, 'slot': 0, 'component': 0, 'occurrence': []}]
        self.key = hashlib.sha256(sources.compact(['SCREF1', 'https://solidcraft.example',
                                                  self.reference['account'], self.reference['room'], self.selection])).hexdigest()
        self.value = {'version': 1, 'key': self.key, 'service': 'https://solidcraft.example',
                      'origin': self.reference, 'selection': self.selection,
                      'design_name': '<Bracket>.sc', 'snapshot': True, 'open_url': 'javascript:evil()'}
        self.mesh = sources.PROJECTS / 'Sources' / 'Bracket.stl'
        self.mesh.parent.mkdir()
        self.mesh.write_bytes(('SCREF1:' + self.key).encode().ljust(80, b'\0') + struct.pack('<I', 1) + bytes(50))
        self.receipt = self.mesh.parent / f'SCREF1-{self.key}.scsource'
        self.receipt.write_bytes(sources.compact(self.value))
        self.directory = sources.STATE / '2'
        self.directory.mkdir()
        self.state = {'session': 'session', 'pid': os.getpid(), 'time': time.time(),
                      'source_objects': [{'id': 17, 'name': 'Bracket', 'source': str(self.mesh)}]}
        self.store_state()

    def store_state(self):
        sources.write_json(self.directory / 'state.json', self.state)

    def request(self, **changes):
        value = {'session': 'session', 'time': time.time(), 'nonce': 'click1', 'key': self.key, 'object_id': 17}
        value.update(changes)
        sources.write_json(self.directory / 'solidcraft-request.json', value)

    def test_exact_producer_key_and_constructed_link(self):
        self.assertEqual(self.key, '669bcbbf6612976ae530742849ecefbaa6db2d5a1fc8d2cf6b4262e336e45ca5')
        result = sources.manifest(sources.compact(self.value))
        url = urlsplit(result['url'])
        self.assertEqual(url.scheme, 'https')
        encoded = parse_qs(url.query)['craft-source'][0]
        self.assertEqual(json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))), self.reference)

    def test_reject_wrong_identity_or_unsafe_origin(self):
        for changes in ({'key': '0' * 64}, {'service': 'javascript:alert(1)'},
                        {'service': 'https://user:password@solidcraft.example'},
                        {'service': 'https://solidcraft.example/path'}, {'version': 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                sources.manifest(sources.compact(dict(self.value, **changes)))

    def test_reject_noncanonical_body_selection(self):
        for selection in ([self.selection[0], self.selection[0]],
                          [dict(self.selection[0], feature=True)], [dict(self.selection[0], occurrence=[-1])]):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                sources.manifest(sources.compact(dict(self.value, selection=selection)))

    def test_binary_header_and_length_required(self):
        self.assertEqual(sources.source_key(self.mesh), self.key)
        self.mesh.write_bytes(self.mesh.read_bytes()[:-1])
        self.assertIsNone(sources.source_key(self.mesh))
        self.mesh.write_bytes(b'solid bracket\nendsolid bracket')
        self.assertIsNone(sources.source_key(self.mesh))

    def test_current_object_action_and_cache_survive_restart(self):
        self.request()
        service = sources.Sources()
        result = service.status()['request']
        self.assertEqual(result['object_id'], 17)
        self.assertEqual(result['object_name'], 'Bracket')
        self.assertEqual(json.loads((self.directory / 'solidcraft.json').read_text())['links'][0]['key'], self.key)
        self.mesh.unlink()
        self.receipt.unlink()
        self.assertEqual(sources.Sources().status()['request']['key'], self.key)

    def test_missing_manifest_does_not_link_by_name(self):
        self.receipt.unlink()
        self.request()
        self.assertIsNone(sources.Sources().status()['request'])

    def test_project_reopen_with_relative_source_reference(self):
        self.state['path'] = str(sources.PROJECTS / 'Bracket.3mf')
        self.state['source_objects'][0]['source'] = 'Bracket.stl'
        self.store_state()
        self.request()
        self.assertEqual(sources.Sources().status()['request']['key'], self.key)

    def test_relative_source_without_saved_project_is_unresolved(self):
        self.state['source_objects'][0]['source'] = 'Bracket.stl'
        self.store_state()
        self.request()
        self.assertIsNone(sources.Sources().status()['request'])

    def test_reject_stale_session_object_and_action(self):
        for changes in ({'session': 'old'}, {'object_id': 18}, {'key': '0' * 64}, {'time': time.time() - 121}):
            self.request(**changes)
            self.assertIsNone(sources.Sources().status()['request'])

    def test_changed_project_clears_action(self):
        self.request()
        service = sources.Sources()
        self.assertIsNotNone(service.status()['request'])
        self.state['source_objects'] = []
        self.store_state()
        self.assertIsNone(service.status()['request'])

    def test_unmarked_replaced_source_does_not_use_cached_link(self):
        self.request()
        service = sources.Sources()
        self.assertIsNotNone(service.status()['request'])
        self.mesh.write_bytes(b'new unlinked file')
        self.assertIsNone(service.status()['request'])

    def test_paths_and_symlinks_cannot_escape_source_roots(self):
        self.assertIsNone(sources.safe_source('/etc/passwd'))
        link = sources.PROJECTS / 'escape'
        link.symlink_to('..')
        self.assertIsNone(sources.safe_source(str(link / 'private.stl')))

    def test_plugin_reads_active_slot_and_writes_only_requested_object(self):
        panel = SimpleNamespace(post=lambda data: setattr(self, 'panel_data', data), hide=lambda: None)
        fake = SimpleNamespace(script=SimpleNamespace(ScriptPluginCapabilityBase=object),
                               plugin=lambda cls: cls, base=object)
        plugin_path = LIB.parent / 'share/orcaslicer/plugins/ProjectStorage/project_storage.py'
        spec = importlib.util.spec_from_file_location('storage_fixture', plugin_path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'orca': fake}):
            spec.loader.exec_module(module)
        module.ROOT = sources.STATE
        capability = module.SolidCraftSources()
        # Startup must leave the existing status dock alone. The fake host has
        # no UI, so an eager dock creation would fail this check.
        capability.on_load()
        self.assertIsNone(capability.panel)
        capability.panel = panel
        capability.hide_when_ready = False
        sources.Sources().status()
        capability.tick({'tick': True})
        self.assertEqual(self.panel_data['links'][0]['object_id'], 17)
        capability.tick({'key': self.key, 'object_id': 17})
        self.assertEqual(json.loads((self.directory / 'solidcraft-request.json').read_text())['object_id'], 17)
        self.assertEqual(sources.Sources().status()['request']['key'], self.key)


if __name__ == '__main__':
    unittest.main()
