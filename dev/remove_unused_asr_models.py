"""One-off CE maintenance script; never included in or run by the addon.

Preview by default. Stop the assistant service before --apply. An optional old
catalog identifies exact download fragments after the new catalog is deployed.
"""
import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import re
import shutil

REMOVED = ('zipformer-small-int8', 'zipformer-small-fp32', 'moonshine', 'paraformer-2025', 'zipformer-ctc-2025')
ARCHIVES = (
    'sherpa-onnx-streaming-zipformer-ctc-zh-int8-2025-06-30',
    'sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16',
    'sherpa-onnx-moonshine-base-zh-quantized-2026-02-27',
    'sherpa-onnx-paraformer-zh-int8-2025-10-07',
)


def candidates(profile, cache, catalog):
    """Exact component/archive names only; never descend into symlinks/backups."""
    profile, cache = Path(profile), Path(cache)
    paths = [profile / 'components' / ('asr-' + name) for name in REMOVED]
    components = catalog.get('components', {})
    removed_hashes, retained_hashes = set(), set()
    for name, spec in components.items():
        target = removed_hashes if name in {'asr-' + value for value in REMOVED} else retained_hashes
        target.update(asset['sha256'] for asset in spec.get('assets', []))
    for digest in removed_hashes - retained_hashes:
        if not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('Invalid archive digest')
        paths.append(profile / 'components/.downloads' / (digest + '.part'))
    names = {name + suffix for name in ARCHIVES for suffix in ('', '.tar.bz2')}
    if cache.is_dir():
        for directory, dirs, files in os.walk(cache, followlinks=False):
            for name in dirs + files:
                if name in names:
                    paths.append(Path(directory) / name)
            depth = len(Path(directory).relative_to(cache).parts)
            dirs[:] = [name for name in dirs if name not in names and depth < 4
                and not name.startswith(('before-', 'backup', 'addon-before'))
                and not (Path(directory) / name).is_symlink()]
    return sorted({path for path in paths if path.exists() or path.is_symlink()})


def check_path(path, profile, cache):
    if path.is_relative_to(profile / 'components'):
        base = profile / 'components'
        relative = path.relative_to(base)
        valid = (len(relative.parts) == 1 and relative.name in {'asr-' + n for n in REMOVED}
            or len(relative.parts) == 2 and relative.parts[0] == '.downloads'
            and re.fullmatch('[a-f0-9]{64}\\.part', relative.name))
    elif path.is_relative_to(cache):
        base = cache
        valid = path.name in {name + suffix for name in ARCHIVES for suffix in ('', '.tar.bz2')}
    else:
        raise ValueError('Path outside model storage: ' + str(path))
    if not valid or not path.parent.resolve().is_relative_to(base.resolve()):
        raise ValueError('Unsafe model path: ' + str(path))
    # Do not allow an intermediate symlink to redirect an authorized directory.
    if base.resolve() != base or any(p.is_symlink() for p in path.parents if p.is_relative_to(base)):
        raise ValueError('Symlink in model storage: ' + str(path))


def size(path):
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())


def remove(paths, profile, cache):
    for path in paths:
        check_path(path, profile, cache)
    # All paths are validated before deleting the first file.
    for path in paths:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=Path('/storage/.kodi/userdata/addon_data/script.voice.assistant'))
    parser.add_argument('--cache', type=Path, default=Path('/storage/.cache/voice-assistant'))
    parser.add_argument('--catalog', type=Path, default=Path('/storage/.kodi/addons/script.voice.assistant/resources/config/components.json'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.profile.is_absolute() or not args.cache.is_absolute():
        parser.error('Storage paths must be absolute')
    catalog = json.loads(args.catalog.read_text())
    paths = candidates(args.profile, args.cache, catalog)
    rows = [{'path': str(path), 'bytes': size(path)} for path in paths]
    report = {'applied': False, 'removed_model_ids': list(REMOVED), 'targets': rows,
              'total_bytes': sum(row['bytes'] for row in rows)}
    if args.apply:
        with ExitStack() as stack:
            for name in ('cmcc-service.lock', 'invocation.lock', 'capture.lock', 'conversation.lock'):
                lock = stack.enter_context((args.profile / name).open('a'))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError('Assistant is active; stop its service before cleanup') from None
            for proc in Path('/proc').iterdir():
                if not proc.name.isdigit():
                    continue
                try:
                    arguments = (proc / 'cmdline').read_bytes().split(b'\0')
                except OSError:
                    continue
                if any(arg.startswith(str(path).encode() + b'/') for arg in arguments for path in paths):
                    raise RuntimeError('A model is still in use by PID ' + proc.name)
            remove(paths, args.profile, args.cache)
            report['applied'] = True
            report['remaining_targets'] = [str(p) for p in candidates(args.profile, args.cache, catalog)]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
