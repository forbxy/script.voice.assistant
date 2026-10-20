"""Download the pinned Codex component into the development userdata cache."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from resources.lib.dependencies.catalog import specification, component_path, installed
from resources.lib.dependencies.download import install

def main():
    profile = ROOT / '.cache/optional-install'
    if not installed(ROOT, profile, 'codex'):
        install(ROOT, profile, 'codex', lambda: False, lambda *args, **kwargs: None)
    print(specification(ROOT, 'codex')['label'], component_path(ROOT, profile, 'codex'))

if __name__ == '__main__': main()
