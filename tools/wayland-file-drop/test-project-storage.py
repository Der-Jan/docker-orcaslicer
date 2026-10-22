#!/usr/bin/env python3
"""Check project association and preservation of uploaded originals."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import subprocess
import xml.etree.ElementTree as ET
import zipfile

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
        storage.LOG_ROOT = self.root / 'logs'

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

    def test_conversion_errors_drop_the_preserved_source_and_confirm_import(self):
        errors = [RuntimeError('Unknown file format'), subprocess.TimeoutExpired(['orca'], 120),
                  zipfile.BadZipFile('Invalid CLI output'), ET.ParseError('Malformed model'),
                  KeyError('Missing template metadata')]
        source = self.root / 'Original.STEP'
        source.write_bytes(b'original STEP data')
        for number, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                directory, state = self.session(str(number))
                dropped = []

                def import_source(helper, target):
                    pending = json.loads((storage.STATE_ROOT / 'pending.json').read_text())
                    self.assertEqual(pending['path'], '')
                    self.assertEqual(pending['source'], str(target))
                    self.assertEqual(pending['objects'], 0)
                    self.assertEqual(target.read_bytes(), source.read_bytes())
                    dropped.append(target)
                    current = dict(state, objects=1, dirty=True, sources=[str(target)], time=time.time())
                    (directory / 'state.json').write_text(json.dumps(current))

                with patch.object(storage, 'request', return_value={}) as request, \
                     patch.object(storage, 'create_project', side_effect=error), \
                     patch.object(storage, 'drop', side_effect=import_source):
                    folder, target = storage.import_new_model(source, directory, state, Path('/helper'), Path('/orca'))
                self.assertEqual(dropped, [folder / 'Sources' / source.name])
                self.assertEqual(target, dropped[0])
                self.assertFalse((folder / f'{folder.name}.3mf').exists())
                self.assertFalse((storage.STATE_ROOT / 'pending.json').exists())
                self.assertIn('Save Project As', request.call_args.kwargs['notice'])
                self.assertEqual(source.read_bytes(), b'original STEP data')
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in storage.LOG_ROOT.glob('*.log')))

    def test_failed_conversion_never_bypasses_a_changed_session(self):
        source = self.root / 'part.step'
        source.write_bytes(b'STEP')
        directory, state = self.session('current')
        with patch.object(storage, 'request', side_effect=[{}, RuntimeError('Project changed')]), \
             patch.object(storage, 'create_project', side_effect=RuntimeError('CLI failed')), \
             patch.object(storage, 'drop') as drop:
            with self.assertRaisesRegex(RuntimeError, 'Project changed'):
                storage.import_new_model(source, directory, state, Path('/helper'), Path('/orca'))
        drop.assert_not_called()
        self.assertFalse((storage.STATE_ROOT / 'pending.json').exists())

    def test_unconfirmed_fallback_is_never_dropped_twice(self):
        source = self.root / 'part.step'
        source.write_bytes(b'STEP')
        directory, state = self.session('current')
        with patch.object(storage, 'request', return_value={}), \
             patch.object(storage, 'create_project', side_effect=RuntimeError('CLI failed')), \
             patch.object(storage, 'drop') as drop, \
             patch.object(storage, 'wait_for_project', side_effect=RuntimeError('Import not confirmed')):
            with self.assertRaisesRegex(RuntimeError, 'Import not confirmed'):
                storage.import_new_model(source, directory, state, Path('/helper'), Path('/orca'))
        self.assertEqual(drop.call_count, 1)
        self.assertEqual(drop.call_args.args[1].suffix, '.step')
        with self.assertRaisesRegex(RuntimeError, 'import is still pending'):
            storage.active_session()

    def test_invalid_or_empty_cli_archives_never_become_project_files(self):
        directory, _ = self.session('current')
        template = directory / 'template.3mf'
        with zipfile.ZipFile(template, 'w') as archive:
            archive.writestr('Metadata/project_settings.config', '{}')
        reply = dict(template=str(template), printer_keys=[], printer_name='Test printer')
        source = self.root / 'part.stl'
        source.write_bytes(b'STL')
        target = self.root / 'part.3mf'
        for empty in (False, True):
            with self.subTest(empty=empty):
                def invalid_cli(*args, **kwargs):
                    geometry = directory / 'geometry.3mf'
                    if empty:
                        with zipfile.ZipFile(geometry, 'w') as archive:
                            archive.writestr('3D/3dmodel.model', '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources/></model>')
                    else:
                        geometry.write_bytes(b'Not a 3MF archive')
                    return 0
                with patch.object(storage, 'run_process', side_effect=invalid_cli):
                    with self.assertRaises((ValueError, zipfile.BadZipFile)):
                        storage.create_project(source, target, reply, directory, Path('/orca'))
                self.assertFalse(target.exists())
                self.assertFalse(target.with_suffix('.part').exists())

    def test_valid_cli_geometry_keeps_native_settings_and_source_description(self):
        directory, _ = self.session('current')
        template = directory / 'template.3mf'
        namespace = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
        with zipfile.ZipFile(template, 'w') as archive:
            archive.writestr('Metadata/project_settings.config', '{"layer_height":"0.2"}')
            archive.writestr('3D/3dmodel.model', f'<model xmlns="{namespace}"><resources/></model>')
        reply = dict(template=str(template), printer_keys=[], printer_name='Test printer')
        source = self.root / 'part.stl'
        source.write_bytes(b'STL')
        target = self.root / 'Named project.3mf'

        def export_geometry(*args, **kwargs):
            with zipfile.ZipFile(directory / 'geometry.3mf', 'w') as archive:
                archive.writestr('Metadata/project_settings.config', '{}')
                archive.writestr('3D/3dmodel.model', f'<model xmlns="{namespace}"><resources><object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
            return 0

        with patch.object(storage, 'run_process', side_effect=export_geometry):
            storage.create_project(source, target, reply, directory, Path('/orca'))
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(json.loads(archive.read('Metadata/project_settings.config')), {'layer_height': '0.2'})
            root = ET.fromstring(archive.read('3D/3dmodel.model'))
            metadata = {entry.get('name'): entry.text for entry in root.findall('{*}metadata')}
            self.assertEqual(metadata['Title'], 'Named project')
            self.assertEqual(metadata['Description'], 'Original model: Sources/part.stl')
            self.assertIsNotNone(root.find('.//{*}triangle'))
        self.assertFalse(target.with_suffix('.part').exists())


if __name__ == '__main__':
    unittest.main()
