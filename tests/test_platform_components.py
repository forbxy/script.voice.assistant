"""CPU selection must not turn x86 into ARM or invalidate existing CE installs."""
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

import pytest

from resources.lib import native_platform as native
from resources.lib.dependencies import catalog as components
from resources.lib.dependencies.uploads import locate

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('machine,bits,expected', [('aarch64', 4, 'linux_aarch64'),
    ('armv7l', 4, 'linux_aarch64'), ('aarch64', 8, 'linux_aarch64'),
    ('x86_64', 8, 'linux_x86_64'), ('AMD64', 8, 'linux_x86_64')])
def test_workers_follow_cpu_without_loading_64_bit_libraries_into_arm32_python(monkeypatch, machine, bits, expected):
    monkeypatch.setattr(native.platform, 'machine', lambda: machine)
    monkeypatch.setattr(native.struct, 'calcsize', lambda value: bits)
    assert native.worker_architecture() == expected


@pytest.mark.parametrize('machine,bits', [('x86_64', 4), ('i686', 4), ('riscv64', 8)])
def test_unknown_userlands_are_rejected_instead_of_launching_an_arm_binary(monkeypatch, machine, bits):
    monkeypatch.setattr(native.platform, 'machine', lambda: machine)
    monkeypatch.setattr(native.struct, 'calcsize', lambda value: bits)
    with pytest.raises(ValueError, match='架构'):native.worker_architecture()


def test_models_are_shared_but_executables_and_receipts_are_architecture_specific(tmp_path, monkeypatch):
    arm = components.catalog(str(ROOT), 'linux_aarch64')['components']
    x86 = components.catalog(str(ROOT), 'linux_x86_64')['components']
    for identifier in ('asr-zipformer-2025', 'asr-sensevoice', 'matcha-bilingual'):
        assert arm[identifier] == x86[identifier]
    for identifier in ('onnx-runtime', 'codebuddy', 'codex'):
        assert components.revision(arm[identifier]) != components.revision(x86[identifier])
        assert all('arm64' not in row['filename'] and 'aarch64' not in row['filename'] for row in x86[identifier]['assets'])
    monkeypatch.setattr(components, 'worker_architecture', lambda: 'linux_aarch64')
    old = components.component_path(ROOT, tmp_path, 'codex')
    expected = json.loads((ROOT/'tests/fixtures/codex_component.json').read_text())
    assert components.specification(ROOT, 'codex') == expected
    assert old.name == expected['version'] + '-' + components.revision(expected)
    monkeypatch.setattr(components, 'worker_architecture', lambda: 'linux_x86_64')
    assert components.component_path(ROOT, tmp_path, 'codex') != old


def test_x86_official_codex_includes_code_mode_and_matching_sandbox():
    spec = components.catalog(str(ROOT), 'linux_x86_64')['components']['codex']
    assert spec['version'] == '0.163.0'
    assert set(spec['required']) == {'codex-app-server', 'codex-code-mode-host', 'codex-resources/bwrap'}
    mapped = {destination for row in spec['assets'] for destination in row['files'].values()}
    assert mapped == set(spec['required'])
    assert all('/openai/codex/releases/download/rust-v0.163.0/' in row['url'] for row in spec['assets'])


def test_uploaded_dependency_names_are_checked_against_current_cpu(monkeypatch):
    # uploads imports the catalogue function directly; use an explicit effective
    # catalogue to model a page served by x86 without touching the host CPU.
    from resources.lib.dependencies import uploads
    data = json.loads((ROOT/'resources/config/components.json').read_text())
    data['components'].update(data['platform_components']['linux_x86_64'])
    monkeypatch.setattr(uploads, 'catalog', lambda root: data)
    name = data['components']['codebuddy']['assets'][0]['filename']
    assert locate(ROOT, name)[0] == 'codebuddy'
    with pytest.raises(ValueError):locate(ROOT, 'codebuddy-code-headless_Linux_arm64.tar.gz')


def test_x86_codebuddy_launches_without_arm_loader(tmp_path, monkeypatch):
    from resources.lib import codebuddy_protocol
    monkeypatch.setattr(native, 'worker_architecture', lambda: 'linux_x86_64')
    binary = tmp_path/'codebuddy-headless';binary.write_bytes(b'fixture')
    monkeypatch.setattr(components, 'require', lambda *args: tmp_path)
    args = codebuddy_protocol.command(ROOT, tmp_path)
    assert args[0] == str(binary) and '--library-path' not in args
    assert '--input-format=stream-json' in args


def test_x86_speech_uses_downloaded_runtime_and_an_x86_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(components, 'worker_architecture', lambda: 'linux_x86_64')
    monkeypatch.setattr(native, 'worker_architecture', lambda: 'linux_x86_64')
    monkeypatch.setattr(components, 'require', lambda *args: tmp_path)
    monkeypatch.setattr(native.Path, 'is_file', lambda path: True)
    command = components.native_command(ROOT, tmp_path, 'asr-worker')
    assert str(tmp_path/'lib') == command[command.index('--library-path')+1]
    assert command[-1] == str(ROOT/'resources/native/linux_x86_64/bridge/asr-worker')
    assert not any('aarch64' in arg for arg in command)


def test_packaged_x86_bridges_and_decoders_are_real_x86_elf_files():
    files = list((ROOT/'resources/native/linux_x86_64').rglob('*'))
    files = [p for p in files if p.is_file()]
    assert len(files) == 8
    for path in files:
        header = path.read_bytes()[:20]
        assert header[:6] == b'\x7fELF\x02\x01' and struct.unpack('<H', header[18:20])[0] == 62


def test_addon_dependencies_use_python_abi_without_a_kodi_patch_version_gate():
    addon = ET.parse(ROOT/'addon.xml').getroot()
    assert [(row.get('addon'), row.get('version')) for row in addon.findall('requires/import')] == [('xbmc.python', '3.0.0')]
    assert addon.find("extension[@point='xbmc.addon.metadata']/platform").text == 'linux'
