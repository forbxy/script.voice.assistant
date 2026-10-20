"""Build the CMCC decoder against the test CoreELEC FFmpeg ABI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compiler-prefix', default=os.environ.get('VOICE_CE_COMPILER_PREFIX',
        '/home/bxy/avdvplus/R10/CoreELEC/build.CoreELEC-Amlogic-ng.arm-21/toolchain/bin/armv8a-libreelec-linux-gnueabihf-'))
    args = parser.parse_args()
    source = ROOT / 'dev/remote/cmcc_siren_decoder.c'
    target = ROOT / 'resources/native/linux_armv7l/cmcc/libcmcc-siren-ffmpeg62.so'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.so.tmp')
    flags = ['-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-mcpu=cortex-a53',
             '-mfpu=neon-fp-armv8', '-mfloat-abi=hard', '-Wl,-soname,' + target.name]
    subprocess.run([args.compiler_prefix + 'gcc', *flags, str(source), '-o', str(temporary),
                    '-lavcodec', '-lavutil', '-lm'], check=True)
    subprocess.run([args.compiler_prefix + 'strip', str(temporary)], check=True)
    needed = subprocess.check_output([args.compiler_prefix + 'readelf', '-d', str(temporary)], text=True)
    if '[libavcodec.so.62]' not in needed or '[libavutil.so.60]' not in needed:
        temporary.unlink(missing_ok=True)
        raise RuntimeError('This build requires CoreELEC FFmpeg 62/60 headers and libraries.')
    temporary.replace(target)
    report = {'source': 'dev/remote/cmcc_siren_decoder.c', 'wrapper_license': 'MIT',
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'output': target.relative_to(ROOT).as_posix(), 'compiler': args.compiler_prefix + 'gcc',
              'flags': flags, 'system_dependencies': ['libavcodec.so.62', 'libavutil.so.60'],
              'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
              'bundles_android_binary': False,
              'codec_implementation': 'CoreELEC system FFmpeg Siren; no FFmpeg libraries bundled',
              'codec_source': 'https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/siren.c'}
    (ROOT / 'resources/licenses/cmcc-build.json').write_text(json.dumps(report, indent=2) + '\n')
    print(target)


if __name__ == '__main__':
    main()
