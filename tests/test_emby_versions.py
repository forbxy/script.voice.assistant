import copy
import json
from urllib.parse import urlsplit

import pytest

from resources.lib.intelligence import emby_local, emby_versions as versions


def source(id, width=None, size=None, **extra):
    return dict(Id=id, Name='版本：'+id, Path='https://media.invalid/'+id+'.mkv', Size=size,
                MediaStreams=[] if width is None else [dict(Type='Video', Width=width, Codec='hevc', Index=0)], **extra)


def rpc(version='12.4.24', vfs=True, nextgen=True):
    def call(method, params):
        assert method == 'Addons.GetAddonDetails'
        id = params['addonid']
        return {'addon': {'addonid': id, 'enabled': vfs if id == 'vfs.stream.fast' else nextgen, 'version': version}}
    return call


ITEM = dict(_emby_server='srv', _emby_id='32', media_type='movie')


@pytest.mark.parametrize('values,expected,reason', [
    ([source('a', 1920, 10000), source('b', 3840, 500)], 1, 'resolution'),
    ([source('a', None, 10000), source('b', 1920, 500)], 1, 'resolution'),
    ([source('a', 1920, 500), source('b', 1920, 10000)], 0, 'resolution'),
    ([source('a', None, 500), source('b', None, 10000)], 1, 'size'),
    ([source('a', 0, '500'), source('b', '0', '10000')], 1, 'size'),
    ([source('a'), source('b')], 0, 'first'),
    ([source('a', -1, -10), source('b', False, 'bad')], 0, 'first'),
])
def test_resolution_then_file_bytes_then_first_without_name_guesses(values, expected, reason):
    original = copy.deepcopy(values)
    index, detail = versions.choose(values)
    assert index == detail['index'] == expected and detail['reason'] == reason
    assert values == original


@pytest.mark.parametrize('version', ['12.4.0', '12.4.15.11', '12.4.24', '12.4.99', '12.4.24-beta.1'])
def test_all_12_4_patch_versions_use_one_source_when_vfs_is_enabled(version):
    actual = dict(MediaSources=[source('a'), source('b', size=100)])
    prepared = versions.prepare(ITEM, actual, rpc(version))
    assert prepared and prepared['path'] == actual['MediaSources'][1]['Path']
    payload = bytes.fromhex(urlsplit(prepared['url']).path.split('/')[-2].split('-')[4]).decode()
    assert '<>' not in payload and ':b:' in payload and ':a:' not in payload
    assert prepared['url'].startswith('http://127.0.0.1:57342/dynamic/http/movies/srv/0/0/32/')


@pytest.mark.parametrize('version', ['12.3.9', '12.5.0', '13.4.0', '12.40.1', '12.4', '12.4.foo', ''])
def test_other_versions_keep_the_normal_entry(version):
    assert versions.prepare(ITEM, dict(MediaSources=[source('a'), source('b')]), rpc(version)) is None


@pytest.mark.parametrize('vfs,nextgen', [(False, True), (True, False)])
def test_disabled_addons_never_trigger_the_compatibility_route(vfs, nextgen):
    assert versions.prepare(ITEM, dict(MediaSources=[source('a'), source('b')]), rpc(vfs=vfs, nextgen=nextgen)) is None


def test_single_source_makes_no_addon_queries_and_cancelled_probe_is_not_ignored():
    def forbidden(*args):raise AssertionError('unexpected addon query')
    assert versions.prepare(ITEM, dict(MediaSources=[source('a')]), forbidden) is None
    def aborted(*args):raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        versions.prepare(ITEM, dict(MediaSources=[source('a'), source('b')]), aborted)


def test_single_source_payload_keeps_stream_indexes_subtitles_markers_and_raw_http_path():
    chosen = source('b', size=100)
    chosen.update(Path='https://media.invalid:8443/play?id=1&token=private-value', IsRemote='true',
        MediaStreams=[dict(Type='Video', Width=3840, Codec='HEVC', Index=2),
            dict(Type='Audio', Codec='dts', Profile='DTS-HD MA', Index=5, DisplayTitle='中文:无损'),
            dict(Type='Subtitle', Codec='ass', Index=8, Language='chi', DisplayTitle='中文:字幕',
                 IsExternal=True, IsForced=True)],
        Chapters=[dict(MarkerType='IntroStart', StartPositionTicks=120000000),
                  dict(MarkerType='IntroEnd', StartPositionTicks=450000000),
                  dict(MarkerType='CreditsStart', StartPositionTicks=12000000000)])
    actual = dict(MediaSources=[source('a'), chosen], SeriesId='20')
    original = copy.deepcopy(actual)
    prepared = versions.prepare(dict(ITEM, media_type='episode'), actual, rpc())
    data = bytes.fromhex(urlsplit(prepared['url']).path.split('/')[-2].split('-')[4]).decode()
    base, video, audio, subtitle = data.split('<<')
    assert base.split(':')[3].replace('<;>', ':') == chosen['Path']
    assert base.split(':')[4:] == ['12', '45', '1200', '1']
    assert video == 'hevc:0:2:3840'
    assert audio == '中文<;>无损:dtshd_ma:0:5'
    assert subtitle == 'chi:中文<;>字幕:1:8:ass:1'
    assert '/dynamic/http/tvshows/srv/0/20/32/e-0-0--' in prepared['url']
    # The URL always enters Next Gen; followhttp is interpreted by that service.
    assert urlsplit(prepared['url']).hostname == '127.0.0.1'
    assert 'private-value' not in json.dumps(prepared['selection'])
    assert actual == original


@pytest.mark.parametrize('remote,expected', [(True, '0'), (False, '0'), ('true', '1'), ('false', '0'), (None, '0')])
def test_remote_wire_flag_matches_next_gen_12_4_serializer(remote, expected):
    row = source('a', IsRemote=remote)
    assert versions.metadata(dict(MediaSources=[row]), row).split('<<')[0].split(':')[-1] == expected


def test_global_chapters_apply_only_when_the_server_does_not_supply_per_source_chapters():
    actual = dict(MediaSources=[source('a'), source('b')],
                  Chapters=[dict(Name='Intro End', StartPositionTicks=310000000)])
    assert versions.markers(actual, actual['MediaSources'][1]) == [0, 31, 0]
    actual['MediaSources'][0]['Chapters'] = []
    assert versions.markers(actual, actual['MediaSources'][1]) == [0, 0, 0]


@pytest.mark.parametrize('path', ['http://media.invalid/<bad<>data', 'http://media.invalid/\r\ninvalid', 123])
def test_unrepresentable_paths_do_not_turn_into_another_source(path):
    actual = dict(MediaSources=[dict(source('a', size=100), Path=path), source('b')])
    with pytest.raises(versions.Unavailable):versions.prepare(ITEM, actual, rpc())


@pytest.mark.parametrize('container', ['iso', 'strm'])
def test_iso_preserves_disc_suffix_without_requiring_a_synced_kodi_database_id(container):
    actual = dict(MediaSources=[source('a'), dict(source('b', size=100), Container=container, Path='https://media.invalid/b.iso')])
    prepared = versions.prepare(ITEM, actual, rpc())
    assert '/m-0-0--' in prepared['url'] and prepared['url'].split('|')[0].endswith('unknown.iso')


def test_strm_source_is_a_video_not_a_second_playlist_lookup():
    actual = dict(Container='strm', MediaSources=[source('a'), source('b', size=100)])
    prepared = versions.prepare(ITEM, actual, rpc())
    assert prepared['mime_type'] == 'video/x-matroska'
    assert prepared['url'].split('|')[0].endswith('unknown.mkv')


def test_version_specific_startup_rejects_other_sources_of_the_same_title():
    actual = dict(MediaSources=[source('a'), source('b', size=100)])
    prepared = versions.prepare(ITEM, actual, rpc())
    assert emby_local.matches({'file': prepared['url']}, ITEM, actual, prepared)
    assert emby_local.matches({'file': prepared['path']}, ITEM, actual, prepared)
    assert not emby_local.matches({'file': actual['MediaSources'][0]['Path']}, ITEM, actual, prepared)
    other = prepared['url'].replace('m-0-0--', 'm-0-1--')
    assert not emby_local.matches({'file': other}, ITEM, actual, prepared)
