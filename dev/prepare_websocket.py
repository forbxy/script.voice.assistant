"""Restore the pinned pure-Python WebSocket dependency and its license."""
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    metadata = json.loads((ROOT / 'resources/licenses/websocket-client-build.json').read_text())
    cache = ROOT / '.cache/websocket-client'
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / metadata['url'].rsplit('/', 1)[-1]
    if not path.exists():
        path.write_bytes(urllib.request.urlopen(metadata['url'], timeout=30).read())
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata['sha256']:
        raise RuntimeError('WebSocket dependency checksum mismatch')
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.startswith('websocket/') and '/tests/' not in name and name.endswith('.py'):
                output = ROOT / 'resources/vendor' / name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.read(name))
        license_name = next(name for name in archive.namelist() if name.endswith('/LICENSE'))
        (ROOT / 'resources/licenses/WEBSOCKET-CLIENT-LICENSE').write_bytes(archive.read(license_name))
    print('Prepared websocket-client ' + metadata['version'])


if __name__ == '__main__':
    main()
