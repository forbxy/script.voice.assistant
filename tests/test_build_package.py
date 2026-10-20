import shutil
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

import build_package


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / 'renamed-checkout'
    root.mkdir()
    source = build_package.ROOT
    for name in ('addon.xml', 'default.py', 'service.py', 'LICENSE.txt', 'README.md', 'command.md'):
        shutil.copyfile(source / name, root / name)
    (root / 'resources').symlink_to(source / 'resources', target_is_directory=True)
    monkeypatch.setattr(build_package, 'ROOT', root)
    return root


def test_package_is_installable_regardless_of_checkout_name(checkout):
    manifest = ET.parse(checkout / 'addon.xml').getroot()
    prefix = manifest.get('id') + '/'
    with ZipFile(build_package.build()) as archive:
        assert archive.testzip() is None
        assert all(name.startswith(prefix) for name in archive.namelist())
        for asset in manifest.findall("extension[@point='xbmc.addon.metadata']/assets/*"):
            assert archive.read(prefix + asset.text.strip())
        assert not any('/tests/' in name or '/.github/' in name for name in archive.namelist())


def test_package_rejects_a_missing_declared_icon(checkout):
    manifest = ET.parse(checkout / 'addon.xml')
    manifest.find("extension[@point='xbmc.addon.metadata']/assets/icon").text = 'resources/missing.png'
    manifest.write(checkout / 'addon.xml', encoding='utf-8')
    with pytest.raises(RuntimeError, match='Missing metadata asset'):
        build_package.build()
