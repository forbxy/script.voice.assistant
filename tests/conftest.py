from pathlib import Path
import sys
from types import SimpleNamespace
import threading
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(scope='session')
def asr_tokens():
    directory = Path(__file__).resolve().parent / 'fixtures/asr-tokens'
    return {'fast': directory / 'chinese-14m.txt', 'balanced': directory / 'bilingual.txt',
            'accurate': directory / 'bilingual.txt'}


@pytest.fixture
def recording_progress(monkeypatch):
    """A ready recording dialog for input-route tests without the Kodi GUI."""
    class Progress:
        def __init__(self, root):
            self.window = SimpleNamespace(ready=threading.Event())
            self.window.ready.set()
        def create(self, *args): pass
        def iscanceled(self): return False
        def close(self): pass
    monkeypatch.setitem(sys.modules, 'resources.lib.recording_ui',
                        SimpleNamespace(RecordingProgress=Progress))
    return Progress
