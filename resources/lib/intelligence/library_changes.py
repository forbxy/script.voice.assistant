"""Cross-script revisions of the title catalogue, owned by the Kodi service."""
import json
import os
from pathlib import Path
import threading
import time
import uuid

from ..data_paths import data_path
from ..storage import read_json, write_json

REVISION_FILE = 'cache/media-library-revision.json'
KINDS = ('movie', 'tvshow')


def process_start(pid):
    """Reject stale owners after Kodi exits, PID reuse, or a system restart."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        stat = Path('/proc/{}/stat'.format(pid)).read_text()
        start = stat.rsplit(')', 1)[1].split()[19]
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return boot + ':' + start
    except (OSError, IndexError):
        return None


def revisions(profile):
    state = read_json(data_path(profile, REVISION_FILE), {})
    if not isinstance(state, dict) or state.get('version') != 1:
        return None
    owner = state.get('owner_start')
    if not owner or owner != process_start(state.get('owner_pid')):
        return None
    value = state.get('revisions')
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k] for k in KINDS):
        return None
    return {k: value[k] for k in KINDS}


class LibraryChanges:
    def __init__(self, profile):
        self.path = data_path(profile, REVISION_FILE)
        self.owner = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.busy = set()
        self.closed = False
        self.state = {'version': 1, 'owner': self.owner, 'owner_pid': os.getpid(),
                      'owner_start': process_start(os.getpid()),
                      'revisions': {kind: uuid.uuid4().hex for kind in KINDS}}
        # A new service epoch covers notifications missed while it was stopped.
        self._save()

    def _save(self):
        self.state['updated_at'] = time.time()
        write_json(self.path, self.state)

    def notify(self, sender, method, data):
        if sender != 'xbmc' or not method.startswith('VideoLibrary.'):
            return False
        event = method.partition('.')[2]
        if event not in ('OnUpdate', 'OnRemove', 'OnScanStarted', 'OnScanFinished',
                         'OnCleanStarted', 'OnCleanFinished'):
            return False
        try:
            value = json.loads(data) if isinstance(data, str) else data
        except (ValueError, TypeError):
            value = None
        with self.lock:
            if self.closed:
                return False
            if event in ('OnScanStarted', 'OnCleanStarted'):
                operation = 'scan' if event == 'OnScanStarted' else 'clean'
                if operation in self.busy:
                    return False
                self.busy.add(operation)
                kinds = KINDS
            elif event in ('OnScanFinished', 'OnCleanFinished'):
                self.busy.discard('scan' if event == 'OnScanFinished' else 'clean')
                kinds = KINDS
            else:
                if self.busy:
                    return False  # One revision at either end of a bulk operation.
                value = value if isinstance(value, dict) else {}
                item = value.get('item') if isinstance(value.get('item'), dict) else value
                kind = item.get('type')
                if kind in ('episode', 'musicvideo'):
                    return False  # This index only stores movie/show titles and years.
                if event == 'OnUpdate' and type(value.get('playcount')) is int and value['playcount'] >= 0:
                    if not value.get('added') and not item.get('added'):
                        return False
                kinds = (kind,) if kind in KINDS else KINDS
            for kind in kinds:
                self.state['revisions'][kind] = uuid.uuid4().hex
            self._save()
            return True

    def close(self):
        with self.lock:
            self.closed = True
            saved = read_json(self.path, {})
            if isinstance(saved, dict) and saved.get('owner') == self.owner:
                self.path.unlink(missing_ok=True)
