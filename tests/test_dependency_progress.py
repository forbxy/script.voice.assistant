import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest

from resources.lib.dependencies import catalog
from resources.lib.dependencies.download import digest, install, unpack
from resources.lib.dependencies.service import Manager, request


def archive_fixture(path, names):
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in names.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def test_unpack_reports_incremental_bytes_inside_a_large_file_and_tree(tmp_path):
    files = {'bundle/model': b'm' * (3 * 1024**2), 'bundle/data/phones': b'phones'}
    source = archive_fixture(tmp_path / 'archive.tgz', files)
    size = sum(map(len, files.values()))
    asset = {'files': {'bundle/model': 'model'}, 'trees': {'bundle/data/': 'data/'},
             'expanded_bytes': size}
    updates = []
    written = unpack(asset, source, tmp_path / 'output', lambda: False,
                     lambda *args: updates.append(args))
    assert written == size and updates[0] == (0, size, 0)
    assert [row[0] for row in updates] == sorted(row[0] for row in updates)
    assert any(0 < done < len(files['bundle/model']) and speed > 0 for done, total, speed in updates)
    assert updates[-1] == (size, size, 0)
    assert (tmp_path / 'output/data/phones').read_bytes() == b'phones'


def test_progress_does_not_claim_unused_expanded_allowance_was_written(tmp_path):
    source = archive_fixture(tmp_path / 'archive.tgz', {'model': b'abc', 'unused': b'x' * 100})
    updates = []
    unpack({'files': {'model': 'model'}, 'expanded_bytes': 1000}, source, tmp_path / 'output',
           lambda: False, lambda *args: updates.append(args))
    assert updates[0] == (0, 1000, 0)
    assert updates[-1] == (3, 3, 0)
    assert max(done for done, total, speed in updates) == 3
    assert not (tmp_path / 'output/unused').exists()


def test_unpack_can_cancel_from_an_intermediate_progress_update(tmp_path):
    source = archive_fixture(tmp_path / 'archive.tgz', {'model': b'x' * (3 * 1024**2)})
    stopped = threading.Event()
    def progress(done, total, speed):
        if done:
            stopped.set()
    with pytest.raises(InterruptedError):
        unpack({'files': {'model': 'model'}, 'expanded_bytes': 3 * 1024**2}, source,
               tmp_path / 'output', stopped.is_set, progress)
    assert (tmp_path / 'output/model').stat().st_size == 1024**2


def test_raw_component_copy_and_receipt_hash_report_real_progress(tmp_path):
    source = tmp_path / 'raw'
    data = b'x' * (2 * 1024**2 + 1)
    source.write_bytes(data)
    copied, checked = [], []
    unpack({'format': 'raw', 'target': 'model'}, source, tmp_path / 'output',
           lambda: False, lambda *args: copied.append(args))
    assert digest(tmp_path / 'output/model', progress=lambda *args: checked.append(args)) == hashlib.sha256(data).hexdigest()
    assert len(copied) > 3 and len(checked) > 3
    assert copied[0][:2] == checked[0][:2] == (0, len(data))
    assert copied[-1][:2] == checked[-1][:2] == (len(data), len(data))


@pytest.fixture
def component(tmp_path):
    root, profile = tmp_path / 'addon', tmp_path / 'profile'
    data = b'x' * (2 * 1024**2 + 7)
    source = archive_fixture(tmp_path / 'archive.tgz', {'bundle/model': data})
    compressed = source.read_bytes()
    assets = [dict(format='tar', files={'bundle/model': 'model'}, expanded_bytes=len(data),
                   bytes=len(compressed), sha256=hashlib.sha256(compressed).hexdigest(),
                   url='https://example.com/model.tgz'),
              dict(format='raw', target='tokens', expanded_bytes=6, bytes=6,
                   sha256=hashlib.sha256(b'tokens').hexdigest(), url='https://example.com/tokens')]
    path = root / 'resources/config/components.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'components': {'codex': {'version': '1', 'label': 'fixture',
        'assets': assets, 'required': ['model', 'tokens']}}, 'asr_models': {}}))
    downloads = profile / 'components/.downloads'
    downloads.mkdir(parents=True)
    for asset, payload in zip(assets, [compressed, b'tokens']):
        (downloads / (asset['sha256'] + '.part')).write_bytes(payload)
    return root, profile, len(data) + 6


def no_network(*args, **kwargs):
    pytest.fail('Progress checks must use only their isolated cached fixtures')


def test_cached_multi_asset_install_exposes_extract_and_receipt_progress(component):
    root, profile, total_expanded = component
    updates = []
    install(root, profile, 'codex', lambda: False,
            lambda done, total, speed, phase: updates.append((phase, done, total, speed)), opener=no_network)
    extracted = [row[1:] for row in updates if row[0] == 'extracting']
    assert extracted[0][:2] == (0, total_expanded)
    assert any(0 < done < total for done, total, speed in extracted)
    assert [done for done, total, speed in extracted] == sorted(done for done, total, speed in extracted)
    assert extracted[-1][:2] == (total_expanded, total_expanded)
    assert updates[-1][:3] == ('verifying', total_expanded, total_expanded)
    assert catalog.installed(root, profile, 'codex')


def test_cancel_during_extract_never_commits_receipt_and_can_retry(component):
    root, profile, total = component
    stop = threading.Event()
    def progress(done, total, speed, phase):
        if phase == 'extracting' and done:
            stop.set()
    with pytest.raises(InterruptedError):
        install(root, profile, 'codex', stop.is_set, progress, opener=no_network)
    assert not catalog.installed(root, profile, 'codex')
    assert not list(profile.glob('components/*/*.installing'))
    install(root, profile, 'codex', lambda: False, lambda *args, **kwargs: None, opener=no_network)
    assert catalog.installed(root, profile, 'codex')


def test_web_and_kodi_receive_extraction_phase_progress_before_completion(component, monkeypatch):
    root, profile, total = component
    import resources.lib.dependencies.service as module
    entered, release = threading.Event(), threading.Event()
    original = module.install
    def blocked(root, profile, identifier, cancelled, progress, **kwargs):
        def report(done, total, speed, phase='downloading'):
            progress(done, total, speed, phase=phase)
            if phase == 'extracting' and 0 < done < total:
                entered.set()
                assert release.wait(5)
        return original(root, profile, identifier, cancelled, report, opener=no_network)
    monkeypatch.setattr(module, 'install', blocked)
    manager = Manager(root, profile)
    try:
        manager.update({'agent_backend': 'codex'})
        manager.start(manager.values)
        assert entered.wait(5)
        kodi = request(profile)['job']
        web = manager.state(manager.values)['job']
        assert kodi == web
        assert kodi['phase'] == 'extracting' and 0 < kodi['phase_percent'] < 100
        assert 0 < kodi['downloaded'] < kodi['total'] == total
        release.set()
        manager.thread.join(5)
        assert manager.job['state'] == 'done' and manager.job['phase_percent'] == 100
    finally:
        release.set()
        manager.close()


def test_kodi_progress_uses_current_phase_not_already_finished_download(monkeypatch):
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace())
    from resources.lib.dependencies import ui
    updates = []
    class Progress:
        def create(self, *args): pass
        def update(self, *args): updates.append(args)
        def iscanceled(self): return False
        def close(self): pass
    monkeypatch.setattr(ui, 'xbmc', SimpleNamespace(Monitor=lambda: SimpleNamespace(
        abortRequested=lambda: False, waitForAbort=lambda delay: None)))
    monkeypatch.setattr(ui, 'xbmcgui', SimpleNamespace(Dialog=lambda: SimpleNamespace(
        notification=lambda *args, **kwargs: None), DialogProgress=Progress))
    states = iter([{'job': {'state': 'running', 'phase': 'extracting', 'label': 'fixture',
                           'percent': 100, 'phase_percent': 42.5, 'downloaded': 42, 'total': 100}},
                   {'job': {'state': 'done'}}])
    monkeypatch.setattr(ui, 'request', lambda *args: next(states))
    ui.show('/profile')
    assert updates[0][0] == 42
    assert '正在解压 fixture · 42.5%' in updates[0][1]
