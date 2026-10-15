# /// script
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "Project storage"
# description = "Associate browser uploads with the open OrcaSlicer project."
# version = "1.0.0"
# ///
import json
import fcntl
import os
from pathlib import Path
import time
import uuid

import orca

ROOT = Path('/run/orcaslicer-project-storage')
PAGE = '''<p>Project storage is active. New projects are stored in /config/Projects.</p>
<button onclick="orca.postMessage({action:'clear_pending'})">Cancel pending import</button>
<p>Cancel releases the upload queue. Files already prepared are kept.</p>
<pre id="status"></pre><script>
orca.onMessage(x => document.getElementById('status').textContent = x);
setInterval(() => orca.postMessage({tick:true}), 500);
orca.postMessage({tick:true});
</script>'''


def write_json(path, data):
    temporary = path.with_suffix('.part')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    temporary.replace(path)


class Storage(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return 'Project storage'

    def on_load(self):
        self.session = uuid.uuid4().hex
        for slot in range(8):
            directory = ROOT / str(slot)
            lock = (directory / 'plugin.lock').open('a+')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                continue
            self.directory = directory
            self.lock = lock
            break
        else:
            raise RuntimeError('No free project storage session')
        self.last_request = None
        self.hide_when_ready = True
        self.panel = orca.host.ui.create_dock_panel(
            html=PAGE, title='Project storage', width=320, height=120,
            dock='bottom', on_message=self.tick)

    def on_unload(self):
        try:
            if hasattr(self, 'directory'):
                (self.directory / 'state.json').unlink(missing_ok=True)
        except OSError:
            pass
        if hasattr(self, 'lock'):
            self.lock.close()

    def execute(self):
        self.panel.show()
        return orca.ExecutionResult.success()

    def state(self):
        plater = orca.host.plater()
        model = plater.model()
        return {
            'session': self.session, 'pid': os.getpid(), 'time': time.time(),
            'mode': orca.host.app_info()['mode'], 'model_id': model.id(),
            'path': plater.project_path(), 'dirty': plater.is_project_dirty(),
            'busy': plater.inside_snapshot_capture(),
            'objects': model.object_count(),
            'sources': [obj.input_file for obj in model.objects()],
        }

    def tick(self, message):
        try:
            if message.get('action') == 'clear_pending':
                pending_path = ROOT / 'pending.json'
                if pending_path.exists():
                    pending = json.loads(pending_path.read_text(encoding='utf-8'))
                    if pending.get('session') == self.session:
                        pending_path.unlink()
            state = self.state()
            request_path = self.directory / 'request.json'
            if request_path.exists():
                request = json.loads(request_path.read_text(encoding='utf-8'))
                nonce = request.get('nonce')
                if nonce != self.last_request:
                    self.last_request = nonce
                    reply = {'nonce': nonce, 'state': state}
                    try:
                        expected = request['expected']
                        if any(state[key] != expected[key] for key in ('session', 'model_id', 'path', 'objects', 'dirty')) or state['busy']:
                            raise RuntimeError('The active project changed while preparing the upload')
                        if request['action'] == 'template':
                            if state['path'] or state['objects'] or state['mode'] != 'editor':
                                raise RuntimeError('A new project requires an empty, unnamed editor')
                            snapshot = self.directory / 'template.3mf'
                            orca.host.plater().export_3mf_copy(str(snapshot))
                            printer = orca.host.preset_bundle().current_printer_preset()
                            reply.update(template=str(snapshot), printer_keys=printer.config_keys(), printer_name=printer.name)
                        elif request['action'] != 'check':
                            raise RuntimeError('Unknown project storage request')
                    except Exception as error:
                        reply['error'] = str(error)
                    write_json(self.directory / 'reply.json', reply)
            write_json(self.directory / 'state.json', state)
            self.panel.post(state['path'] or ('Empty project' if not state['objects'] else 'Unsaved project'))
            if self.hide_when_ready:
                self.hide_when_ready = False
                self.panel.hide()
        except Exception as error:
            write_json(self.directory / 'state.json', {'session': self.session, 'time': time.time(), 'error': str(error)})
            self.panel.post(str(error))


@orca.plugin
class Package(orca.base):
    def register_capabilities(self):
        orca.register_capability(Storage)
