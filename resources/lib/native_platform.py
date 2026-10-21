"""Select native code by CPU/userland ABI, independently of Kodi's version."""
from pathlib import Path
import platform
import re
import struct


def architecture():
    machine, size = platform.machine().lower(), struct.calcsize('P')
    if machine in ('x86_64', 'amd64') and size == 8:
        return 'linux_x86_64'
    if machine in ('aarch64', 'arm64') or machine.startswith('armv'):
        return 'linux_aarch64' if size == 8 else 'linux_armv7l'
    raise ValueError('当前 CPU/用户态架构不受支持：' + machine)


def worker_architecture():
    selected = architecture()
    # CE's ARM32 Kodi runs on an ARM64 kernel; speech/agents stay out of Python.
    return 'linux_aarch64' if selected == 'linux_armv7l' else selected


def launch_command(root, binary, libraries=()):
    binary = Path(binary)
    if not binary.is_file():
        raise ValueError('缺少适用于当前架构的适配程序，请安装完整插件。')
    binary.chmod(0o755)
    if worker_architecture() == 'linux_aarch64':
        bridge = Path(root) / 'resources/native/linux_aarch64/bridge/lib'
        loader = bridge / 'ld-linux-aarch64.so.1'
        libraries = [*libraries, bridge]
    elif not libraries:
        return [str(binary)]
    else:
        loader = next((Path(name) for name in ('/lib64/ld-linux-x86-64.so.2',
            '/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2', '/usr/lib/ld-linux-x86-64.so.2')
            if Path(name).is_file()), None)
    if loader is None or not loader.is_file():
        raise ValueError('缺少当前架构的 Linux 动态加载器。')
    if worker_architecture() == 'linux_aarch64':
        loader.chmod(0o755)
    return [str(loader), '--library-path', ':'.join(map(str, libraries)), str(binary)]


def decoder_path(root, group, stem, dependency='avcodec'):
    import ctypes.util
    selected = architecture()
    name = ctypes.util.find_library(dependency) or ''
    match = re.search(r'\.so\.(\d+)', name)
    if match:
        major = int(match[1])
    else:
        found = []
        for base in ('/usr/lib', '/lib'):
            for path in Path(base).glob('lib' + dependency + '.so.*'):
                version = re.search(r'\.so\.(\d+)', path.name)
                if version: found.append(int(version[1]))
        major = max(found) if found else (60 if dependency == 'avutil' else 62)
    return Path(root) / 'resources/native' / selected / group / (stem + str(major) + '.so')
