"""Build only the small ASR/Matcha bridges; large upstream libraries are downloaded."""
import hashlib
import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT.parent / '.cache/matcha-build'
CACHE = ROOT.parent / '.cache/voice-tts'
TARGET = ROOT / 'resources/native/linux_aarch64/bridge'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=('linux_aarch64', 'linux_x86_64'), default='linux_aarch64')
    parser.add_argument('--cc', default='gcc', help='x86 C compiler command, including optional target flags')
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--header-dir', type=Path)
    args = parser.parse_args()
    if args.arch == 'linux_x86_64':
        if args.runtime_dir is None or args.header_dir is None:
            parser.error('x86 builds require --runtime-dir and --header-dir')
        target = ROOT / 'resources/native/linux_x86_64/bridge'
        target.mkdir(parents=True, exist_ok=True)
        commands = []
        for worker in ('asr-worker', 'matcha-worker'):
            source = ROOT / 'resources/licenses' / (worker.replace('-', '_') + '.c')
            command = [*shlex.split(args.cc), '-O2', '-Wall', '-Wextra', '-I' + str(args.header_dir),
                '-L' + str(args.runtime_dir), '-Wl,--allow-shlib-undefined', str(source),
                '-lsherpa-onnx-c-api', '-o', str(target / worker)]
            subprocess.run(command, check=True)
            commands.append(command)
        report = {'runtime': 'sherpa-onnx 1.13.8 (downloaded separately)', 'architecture': args.arch,
            'commands': commands, 'header_sha256': hashlib.sha256((args.header_dir/'c-api.h').read_bytes()).hexdigest(),
            'files': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in target.iterdir() if p.is_file()}}
        (ROOT/'resources/licenses/speech-bridge-linux_x86_64.json').write_text(json.dumps(report, indent=2)+'\n')
        print('Built x86_64 speech bridges; models and runtime stay in userdata')
        return
    library = TARGET / 'lib'
    library.mkdir(parents=True, exist_ok=True)
    runtime = ROOT / '.cache/sensevoice/arm64-sysroot/usr/aarch64-linux-gnu/lib'
    for name in ('libc.so.6', 'libm.so.6', 'libdl.so.2', 'libpthread.so.0', 'librt.so.1',
                 'libgcc_s.so.1', 'libstdc++.so.6', 'libgomp.so.1', 'ld-linux-aarch64.so.1'):
        shutil.copyfile(runtime / name, library / name)
    sysroot = BUILD / 'toolchain'
    compiler = sysroot / 'usr/bin/aarch64-linux-gnu-gcc-13'
    onnx = CACHE / 'sherpa-onnx-v1.13.8-linux-aarch64-shared-cpu/lib'
    for worker, source in [('asr-worker', ROOT / 'resources/licenses/asr_worker.c'),
                           ('matcha-worker', ROOT / 'dev/matcha_worker.c')]:
        subprocess.run([str(compiler), '--sysroot=' + str(sysroot), '-B/usr/aarch64-linux-gnu/bin/',
            '-O2', '-Wall', '-Wextra', '-I' + str(BUILD), '-L' + str(onnx), '-L' + str(runtime),
            '-Wl,-rpath-link,' + str(onnx), str(source), '-lsherpa-onnx-c-api', '-o', str(TARGET / worker)], check=True)
        destination = ROOT / 'resources/licenses' / (worker.replace('-', '_') + '.c')
        if source != destination:
            shutil.copyfile(source, destination)
    (library / 'ld-linux-aarch64.so.1').chmod(0o755)
    files = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in TARGET.rglob('*') if path.is_file()}
    (ROOT / 'resources/licenses/speech-bridge-build.json').write_text(json.dumps({
        'runtime': 'sherpa-onnx 1.13.8 (downloaded separately)', 'compiler': compiler.name, 'files': files}, indent=2) + '\n')
    print('Built speech bridges and private ARM64 bootstrap libraries')


if __name__ == '__main__': main()
