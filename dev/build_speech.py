"""Build only the small ASR/Matcha bridges; large upstream libraries are downloaded."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT.parent / '.cache/matcha-build'
CACHE = ROOT.parent / '.cache/voice-tts'
TARGET = ROOT / 'resources/native/linux_aarch64/bridge'


def main():
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
