"""Build a temporary Nanosic decoder; it does not install or enable a service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compiler-prefix', default=os.environ.get(
        'VOICE_CE_COMPILER_PREFIX',
        '/home/bxy/avdvplus/R10/CoreELEC/build.CoreELEC-Amlogic-ng.arm-21/'
        'toolchain/bin/armv8a-libreelec-linux-gnueabihf-'))
    parser.add_argument('--output', type=Path, default=ROOT / '.cache/xiaopai/native')
    args = parser.parse_args()
    args.output = args.output.resolve()
    source = ROOT / 'dev/remote/nanosic_wnvc_decoder.c'
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / 'libnanosic-wnvc-avutil60.so'
    temporary = target.with_suffix('.so.tmp')
    flags = ['-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
             '-mcpu=cortex-a53', '-mfpu=neon-fp-armv8', '-mfloat-abi=hard',
             '-Wl,-soname,' + target.name]
    subprocess.run([args.compiler_prefix + 'gcc', *flags, str(source),
                    '-o', str(temporary), '-lavutil', '-lm'], check=True)
    subprocess.run([args.compiler_prefix + 'strip', str(temporary)], check=True)
    needed = subprocess.check_output(
        [args.compiler_prefix + 'readelf', '-d', str(temporary)], text=True)
    if '[libavutil.so.60]' not in needed:
        temporary.unlink(missing_ok=True)
        raise RuntimeError('Build requires the CoreELEC libavutil 60 ABI.')
    target_name = target.name
    temporary.replace(target)
    report = {'source': source.relative_to(ROOT).as_posix(),
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'output': target_name,
              'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
              'compiler': args.compiler_prefix + 'gcc', 'flags': flags,
              'license': 'LGPL-2.1-or-later',
              'upstream': 'FFmpeg 8.1.3 libavcodec/siren.c',
              'codec': 'Nanosic WNVC-320, 16 regions, 24000 bit/s',
              'system_dependency': 'public libavutil.so.60 transform API',
              'bundles_android_binary': False,
              'production_addon_modified': target.is_relative_to(ROOT / 'resources')}
    (args.output / 'build.json').write_text(json.dumps(report, indent=2) + '\n')
    print(target)


if __name__ == '__main__':
    main()
