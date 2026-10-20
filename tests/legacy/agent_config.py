"""Legacy dev-only conversation helper; production uses native Codex sessions."""
import json
from pathlib import Path
import time
from resources.lib.storage import atomic_write
from resources.lib.configuration.credentials import DEFAULT_URL, DEFAULT_MODEL, validate_url, read_key_file, save_key, load_key


class Conversation:
    def __init__(self, profile):
        self.path = Path(profile) / 'agent_context.json'
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if time.time() - value['updated_at'] > 900:
                raise ValueError()
            self.turns = value['turns'][-4:]
            self.media = value.get('media', [])[-50:]
            if not isinstance(self.turns, list) or not isinstance(self.media, list):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            self.turns, self.media = [], []

    def history(self):
        return [message for turn in self.turns for message in turn]

    def save(self, turn, media):
        turns = (self.turns + [turn])[-4:]
        while len(turns) > 1 and len(json.dumps(turns, ensure_ascii=False)) > 16000:
            turns.pop(0)
        atomic_write(self.path, json.dumps({'updated_at': time.time(), 'turns': turns,
                                           'media': list(media)[-50:]}, ensure_ascii=False))

    def clear(self):
        self.path.unlink(missing_ok=True)
