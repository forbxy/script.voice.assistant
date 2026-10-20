from pathlib import Path

import pytest

from dev.remove_unused_asr_models import candidates, remove


def test_cleanup_keeps_other_models_backups_and_symlink_targets(tmp_path):
    profile, cache = tmp_path / 'profile', tmp_path / 'cache'
    components = profile / 'components'
    removed = components / 'asr-zipformer-small-fp32'
    removed.mkdir(parents=True); (removed / 'model.onnx').write_bytes(b'old-model')
    retained = components / 'asr-nano-ctc'
    retained.mkdir(); (retained / 'model.onnx').write_bytes(b'current-model')
    external = tmp_path / 'external'; external.mkdir(); (external / 'keep').write_bytes(b'keep')
    (components / 'asr-moonshine').symlink_to(external, target_is_directory=True)
    model = 'sherpa-onnx-paraformer-zh-int8-2025-10-07'
    current = cache / 'evaluation' / model
    current.mkdir(parents=True); (current / 'model.onnx').write_bytes(b'test-model')
    backup = cache / 'before-upgrade' / model
    backup.mkdir(parents=True); (backup / 'model.onnx').write_bytes(b'backup')
    downloads = components / '.downloads'; downloads.mkdir()
    (downloads / ('a' * 64 + '.part')).write_bytes(b'removed-download')
    (downloads / ('b' * 64 + '.part')).write_bytes(b'keep-download')
    catalog = {'components': {'asr-paraformer-2025': {'assets': [{'sha256': 'a' * 64}]},
                               'asr-nano-ctc': {'assets': [{'sha256': 'b' * 64}]}}}
    targets = candidates(profile, cache, catalog)
    assert removed in targets and current in targets and backup not in targets
    assert (removed / 'model.onnx').is_file()  # Planning does not delete anything.
    remove(targets, profile, cache)
    assert not candidates(profile, cache, catalog)
    assert (retained / 'model.onnx').read_bytes() == b'current-model'
    assert (external / 'keep').read_bytes() == b'keep'
    assert (backup / 'model.onnx').read_bytes() == b'backup'
    assert (downloads / ('b' * 64 + '.part')).read_bytes() == b'keep-download'


def test_cleanup_rejects_other_directories_and_parent_symlinks_before_deleting(tmp_path):
    profile, cache = tmp_path / 'profile', tmp_path / 'cache'
    target = profile / 'components/asr-moonshine'
    target.mkdir(parents=True)
    other = profile / 'components/codex'; other.mkdir()
    with pytest.raises(ValueError, match='Unsafe'):
        remove([target, other], profile, cache)
    assert target.is_dir() and other.is_dir()
    outside = tmp_path / 'outside'; outside.mkdir()
    (cache / 'link').parent.mkdir(parents=True)
    (cache / 'link').symlink_to(outside, target_is_directory=True)
    redirected = cache / 'link/sherpa-onnx-paraformer-zh-int8-2025-10-07'
    redirected.mkdir()
    with pytest.raises(ValueError):
        remove([redirected], profile, cache)
    assert redirected.is_dir()
