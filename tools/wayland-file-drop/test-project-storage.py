#!/usr/bin/env python3
"""Check project association and preservation of uploaded originals."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
MODULE = HERE.parents[1] / 'root/usr/local/libexec/orcaslicer-project-drop.py'
spec = importlib.util.spec_from_file_location('project_drop', MODULE)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        work = HERE / '.project-storage-tests'
        work.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=work)
        self.root = Path(self.temporary.name)
        storage.PROJECT_ROOT = self.root / 'projects'
        storage.STATE_ROOT = self.root / 'state'
        storage.STATE_ROOT.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def session(self, name, path='', age=0):
        directory = storage.STATE_ROOT / name
        directory.mkdir()
        state = dict(session=name, time=time.time()-age, mode='editor', busy=False,
                     path=path, objects=0, dirty=False, model_id=7)
        (directory / 'state.json').write_text(json.dumps(state))
        return directory, state

    def test_project_name_and_folder_collisions(self):
        source = self.root / 'Gehäuse.stl'
        first = storage.new_directory(source)
        (first / 'existing.3mf').write_bytes(b'keep this project')
        second = storage.new_directory(source)
        self.assertEqual(first.name, 'Gehäuse')
        self.assertEqual(second.name, 'Gehäuse-2')
        self.assertEqual((first / 'existing.3mf').read_bytes(), b'keep this project')

    def test_reupload_preserves_both_originals(self):
        source = self.root / 'part.stl'
        source.write_bytes(b'first revision')
        first = storage.copy_source(source, self.root / 'Sources')
        source.write_bytes(b'second revision')
        second = storage.copy_source(source, self.root / 'Sources')
        self.assertEqual(first.read_bytes(), b'first revision')
        self.assertEqual(second.name, 'part-2.stl')
        self.assertEqual(second.read_bytes(), b'second revision')
        self.assertEqual(source.read_bytes(), b'second revision')

    def test_stale_sessions_are_ignored_and_multiple_live_sessions_are_rejected(self):
        self.session('old', age=60)
        directory, _ = self.session('current')
        self.assertEqual(storage.active_session()[0], directory)
        self.session('other')
        with self.assertRaisesRegex(RuntimeError, 'Exactly one'):
            storage.active_session()

    def test_pending_project_blocks_imports_into_the_previous_project(self):
        directory, state = self.session('current', path='/previous.3mf')
        pending = storage.STATE_ROOT / 'pending.json'
        pending.write_text(json.dumps(dict(session='current', path='/prepared.3mf')))
        with self.assertRaisesRegex(RuntimeError, 'previous project has not opened'):
            storage.active_session()
        state['path'] = '/prepared.3mf'
        (directory / 'state.json').write_text(json.dumps(state))
        self.assertEqual(storage.active_session()[1]['path'], '/prepared.3mf')
        self.assertFalse(pending.exists())

    def test_pending_source_is_not_confirmed_by_an_unchanged_project_path(self):
        directory, state = self.session('current', path='/current.3mf')
        pending = storage.mark_pending(state, Path('/current.3mf'), source=Path('/Sources/part.stl'))
        with self.assertRaisesRegex(RuntimeError, 'import is still pending'):
            storage.active_session()
        state['sources'] = [pending['source']]
        (directory / 'state.json').write_text(json.dumps(state))
        self.assertEqual(storage.active_session()[1]['sources'], [pending['source']])
        self.assertFalse((storage.STATE_ROOT / 'pending.json').exists())


if __name__ == '__main__':
    unittest.main()
