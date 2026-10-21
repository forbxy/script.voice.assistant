import hashlib
import io
import json
from pathlib import Path
import tarfile
import threading
import time
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from resources.lib.dependencies import catalog as api
from resources.lib.dependencies.download import fetch, install, unpack
from resources.lib.dependencies.service import Manager, request
from resources.lib.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    status = 200
    headers = {}


def asset(data=b'component'):
    return {'url': 'https://github.com/example/repo/releases/download/v1/file',
            'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
            'expanded_bytes': len(data), 'format': 'raw', 'target': 'binary'}


def manifest(root, row):
    path = root / 'resources/config/components.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'components': {'codex': {'version': '1', 'label': 'Test Codex',
        'assets': [row], 'required': ['binary']}}, 'asr_models': {}}))


def test_legacy_kodi_settings_cannot_enable_modules_or_supply_credentials():
    old = {'recognition_engine': 'ncnn', 'agent_backend': 'codex', 'tts_enabled': 'on',
           'agent_key_file': '/old/key', 'voice_input': 'cmcc', 'ncnn_model_quality': 'accurate'}
    addon = SimpleNamespace(getSetting=lambda key: old.get(key, ''),
        setSetting=lambda key, value: old.update({key: value}))
    config = Settings(addon)
    values = config.values()
    assert values['recognition_engine'] == values['agent_backend'] == values['tts_enabled'] == 'off'
    assert values['voice_input'] == 'usb' and values['agent_key_file'] == ''
    config.apply({'recognition_engine': 'onnx', 'onnx_model': 'nano-ctc'})
    assert config.values()['recognition_engine'] == 'onnx'
    assert old['recognition_engine'] == 'ncnn'  # Old settings are ignored, never migrated.


def test_pinned_catalog_has_six_variants_and_downloads_only_enabled_modules():
    data = api.catalog(str(ROOT))
    assert set(data['asr_models']) == {'zipformer-2025', 'zipformer-2025-fp16', 'zipformer-2025-fp32',
                                     'sensevoice', 'nano-ctc', 'firered-ctc'}
    assert sum(row['streaming'] for row in data['asr_models'].values()) == 3
    assert api.requirements(ROOT, {}) == []
    values = {'recognition_engine': 'onnx', 'onnx_model': 'nano-ctc', 'agent_backend': 'codex', 'tts_enabled': 'on'}
    assert api.requirements(ROOT, values) == ['onnx-runtime', 'asr-nano-ctc', 'codex', 'matcha-bilingual']
    assert api.requirements(ROOT, {'recognition_engine': 'codex'}) == ['codex']
    assert data['components']['codex']['version'] in ('0.163.0-ce.1', '0.163.0')
    for spec in data['components'].values():
        for row in spec['assets']:
            assert row['url'].startswith(('https://github.com/', 'https://acc-1258344699.cos.accelerate.myqcloud.com/@tencent-ai/codebuddy-code/releases/'))
            assert len(row['sha256']) == 64 and row['bytes'] > 0
            assert row['filename'] == row['url'].rsplit('/', 1)[-1] and len(row['md5']) == 32


def test_codex_ce_catalog_keeps_the_release_pin_and_required_companions():
    spec = api.catalog(str(ROOT), 'linux_aarch64')['components']['codex']
    pinned = json.loads((ROOT / 'tests/fixtures/codex_component.json').read_text())
    provenance = json.loads((ROOT / 'resources/licenses/codex-build.json').read_text())
    required = {'codex-app-server', 'codex-code-mode-host', 'codex-resources/bwrap'}
    assert spec == pinned
    assert set(spec['required']) == required
    assert set(provenance['installed_files']) == required
    assert spec['assets'] == provenance['assets']
    assert spec['version'] == provenance['version']
    assert provenance['build']['source_dirty'] is False
    for asset in spec['assets']:
        assert asset['url'].startswith('https://github.com/forbxy/codex-ce/releases/download/')
        assert required <= set(asset['files'].values())
        assert required <= set(asset['executables'])


@pytest.mark.parametrize('identifier,networks', [
    ('zipformer-2025-fp16', ['encoder.fp16.onnx', 'decoder.fp16.onnx', 'joiner.fp16.onnx']),
    ('zipformer-2025-fp32', ['encoder.onnx', 'decoder.onnx', 'joiner.onnx']),
])
def test_zipformer_variants_resolve_their_own_weights_and_keep_beam_search(identifier, networks, tmp_path, monkeypatch):
    from resources.lib.asr import native
    monkeypatch.setattr(native, 'require', lambda root, profile, component: tmp_path / component)
    monkeypatch.setattr(native, 'native_command', lambda *args: ['worker'])
    arguments = native.command(ROOT, tmp_path, identifier)
    assert arguments == ['worker', 'zipformer',
        *(str(tmp_path / ('asr-' + identifier) / name) for name in networks),
        str(tmp_path / ('asr-' + identifier) / 'tokens.txt'), '2', 'modified_beam_search', '']
    assert api.requirements(ROOT, {'recognition_engine': 'onnx', 'onnx_model': identifier}) == [
        'onnx-runtime', 'asr-' + identifier]


def test_proxy_range_resume_and_ignored_range_restart(tmp_path):
    data = b'abc12345'
    row = asset(data)
    part = tmp_path / (row['sha256'] + '.part')
    part.write_bytes(data[:3])
    calls = []
    def resumed(req, **kwargs):
        calls.append(req)
        response = Response(data[3:]); response.status = 206
        response.headers = {'Content-Range': 'bytes 3-7/8'}
        return response
    assert fetch(row, tmp_path, lambda: False, lambda *a, **k: None, resumed).read_bytes() == data
    assert calls[0].full_url.startswith('https://v6.gh-proxy.org/https://github.com/')
    assert calls[0].get_header('Range') == 'bytes=3-'
    part.write_bytes(data[:3])
    assert fetch(row, tmp_path, lambda: False, lambda *a, **k: None,
                 lambda *a, **k: Response(data)).read_bytes() == data


def test_cancel_leaves_partial_and_hash_mismatch_never_installs(tmp_path):
    data = b'a' * 300000
    row = asset(data)
    cancel = threading.Event()
    def progress(*args, **kwargs):
        if args[0] > 0: cancel.set()
    with pytest.raises(InterruptedError):
        fetch(row, tmp_path, cancel.is_set, progress, lambda *a, **k: Response(data))
    part = tmp_path / (row['sha256'] + '.part')
    assert 0 < part.stat().st_size < len(data)
    root, profile = tmp_path / 'addon', tmp_path / 'profile'
    manifest(root, asset(b'correct'))
    with pytest.raises(ValueError, match='SHA256'):
        install(root, profile, 'codex', lambda: False, lambda *a, **k: None,
                lambda *a, **k: Response(b'incorre'))
    assert not api.installed(root, profile, 'codex')
    assert not list(profile.glob('components/*/*.installing'))


def test_wrong_resume_range_is_rejected(tmp_path):
    row = asset(b'abcdefgh')
    (tmp_path / (row['sha256'] + '.part')).write_bytes(b'ab')
    def wrong(*args, **kwargs):
        response = Response(b'cdefgh'); response.status = 206
        response.headers = {'Content-Range': 'bytes 3-7/8'}
        return response
    with pytest.raises(ValueError, match='续传范围'):
        fetch(row, tmp_path, lambda: False, lambda *a, **k: None, wrong)


def test_cloud_and_disabled_modules_need_no_local_speech_model():
    assert api.requirements(ROOT, {'recognition_engine': 'doubao', 'agent_backend': 'off', 'tts_enabled': 'off'}) == []
    assert api.requirements(ROOT, {'recognition_engine': 'off', 'agent_backend': 'codex', 'tts_enabled': 'off'}) == ['codex']


@pytest.mark.parametrize('name,link', [('../escape', False), ('/escape', False), ('safe', True)])
def test_archive_traversal_and_links_are_rejected(tmp_path, name, link):
    source = tmp_path / 'archive.tar'
    with tarfile.open(source, 'w') as archive:
        row = tarfile.TarInfo(name); row.size = 1
        if link: row.type = tarfile.SYMTYPE; row.linkname = '/etc/passwd'
        archive.addfile(row, io.BytesIO(b'x'))
    spec = {'files': {name: name}, 'expanded_bytes': 100}
    with pytest.raises(ValueError):
        unpack(spec, source, tmp_path / 'output', lambda: False)
    assert not (tmp_path / 'escape').exists()


def test_install_receipt_reuse_and_missing_required_file(tmp_path):
    root, profile = tmp_path / 'addon', tmp_path / 'profile'
    manifest(root, asset())
    install(root, profile, 'codex', lambda: False, lambda *a, **k: None,
        lambda *a, **k: Response(b'component'))
    assert api.installed(root, profile, 'codex')
    path = api.require(root, profile, 'codex')
    assert path.is_relative_to(profile)
    assert (path / 'installed.json').stat().st_mode & 0o777 == 0o600
    (path / 'binary').unlink()
    assert not api.installed(root, profile, 'codex')


def test_web_and_kodi_attach_to_one_job_and_cancellation_is_shared(tmp_path, monkeypatch):
    root, profile = tmp_path / 'addon', tmp_path / 'profile'
    manifest(root, asset())
    import resources.lib.dependencies.service as module
    calls = []
    def blocked(root, profile, identifier, cancelled, progress):
        calls.append(identifier)
        while not cancelled(): time.sleep(.01)
        raise InterruptedError()
    monkeypatch.setattr(module, 'install', blocked)
    manager = Manager(root, profile)
    try:
        manager.update({'agent_backend': 'codex'})
        request(profile, 'start')
        manager.start({'agent_backend': 'codex'})
        assert request(profile)['job']['state'] == 'running'
        request(profile, 'cancel')
        manager.thread.join(1)
        assert manager.state(manager.values)['job']['state'] == 'cancelled'
        assert calls == ['codex']
    finally: manager.close()


def test_package_ships_complete_resources_without_models_or_test_fixtures():
    from build_package import build, packaged
    resources = ROOT / 'resources'
    names = [str(p.relative_to(ROOT)) for p in resources.rglob('*') if packaged(p)]
    expected = {str(p.relative_to(ROOT)) for p in resources.rglob('*')
                if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    assert set(names) == expected
    assert any(name.endswith('/bridge/asr-worker') for name in names)
    assert 'resources/lib/asr/recognizer.py' in names
    assert 'resources/lib/recognizer.py' not in names
    assert not any('/models/' in name or '/codex/codex-' in name for name in names)
    assert not any(name.endswith(('libonnxruntime.so', 'libsherpa-onnx-c-api.so', 'libncnn.so')) for name in names)
    assert sum((ROOT / name).stat().st_size for name in names) < 12 * 1024**2
    with ZipFile(build()) as archive:
        prefix = ROOT.name + '/'
        published = {name[len(prefix):] for name in archive.namelist() if name.startswith(prefix + 'resources/')}
        assert published == expected
        assert not any(name.startswith((prefix + 'tests/', prefix + 'dev/')) for name in archive.namelist())
