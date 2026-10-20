"""Build remote decoders for a specified CE userspace/FFmpeg ABI (NG or NO)."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compiler-prefix', required=True)
    parser.add_argument('--arch', choices=('linux_armv7l', 'linux_aarch64'), required=True)
    parser.add_argument('--avcodec', type=int, required=True)
    parser.add_argument('--avutil', type=int, required=True)
    args = parser.parse_args()
    report = []
    for group, name, source, libraries in [
        ('cmcc', 'libcmcc-siren-ffmpeg' + str(args.avcodec), 'cmcc_siren_decoder.c', ['-lavcodec', '-lavutil']),
        ('r22', 'libr22-msbc-ffmpeg' + str(args.avcodec), 'r22_msbc_decoder.c', ['-lavcodec', '-lavutil']),
        ('nanosic', 'libnanosic-wnvc-avutil' + str(args.avutil), 'nanosic_wnvc_decoder.c', ['-lavutil']),
    ]:
        target = ROOT / 'resources/native' / args.arch / group / (name + '.so')
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = ['-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-mcpu=cortex-a53']
        if args.arch == 'linux_armv7l': flags += ['-mfpu=neon-fp-armv8', '-mfloat-abi=hard']
        subprocess.run([args.compiler_prefix + 'gcc', *flags, str(ROOT / 'dev/remote' / source),
            '-o', str(target), *libraries, '-lm'], check=True)
        subprocess.run([args.compiler_prefix + 'strip', str(target)], check=True)
        needed = subprocess.check_output([args.compiler_prefix + 'readelf', '-d', str(target)], text=True)
        if '[libavutil.so.' + str(args.avutil) + ']' not in needed:
            raise RuntimeError('Unexpected FFmpeg avutil ABI')
        if '-lavcodec' in libraries and '[libavcodec.so.' + str(args.avcodec) + ']' not in needed:
            raise RuntimeError('Unexpected FFmpeg avcodec ABI')
        report.append({'path': str(target.relative_to(ROOT)), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                       'compiler': Path(args.compiler_prefix + 'gcc').name, 'dependencies': needed})
    (ROOT / 'resources/licenses' / ('decoders-' + args.arch + '.json')).write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__': main()
