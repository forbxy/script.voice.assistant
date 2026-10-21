import json

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.presentation import parse, structured_reply


@pytest.mark.parametrize('prefix,suffix', [
    ('', ''), ('我已经查到影片信息。\n\n', ''),
    ('```json\n', '\n```'), ('回复如下：\n```json\n', '\n```'),
    ('说明中的 {花括号} 不属于数据。\n', ''),
    ('前面的数据 {"other": 1} 只是说明。\n', ''),
])
def test_mixed_final_reply_displays_message_and_verified_choices(prefix, suffix):
    tools = KodiTools(lambda *args: None, media=[{'kind': 'movie', 'id': 1, 'title': '测试影片'}])
    value = {'speech': [{'text': '朗读内容。', 'pronunciations': []}],
             'message': '答复包含换行\n和括号 {"speech": "示例"}。', 'interaction': 'close',
             'choices': [{'label': '播放', 'value': '播放影片', 'kind': 'movie', 'id': 1},
                         {'label': '不存在', 'value': '播放', 'kind': 'movie', 'id': 999}]}
    reply = prefix + json.dumps(value, ensure_ascii=False) + suffix
    assert structured_reply(reply) == value
    shown = parse(reply, tools)
    assert shown['message'] == value['message']
    assert shown['interaction'] == 'continue'
    assert [row['id'] for row in shown['choices']] == [1]


@pytest.mark.parametrize('reply', [
    '普通回复，直接显示。', 'JSON 示例：{"volume": 20}',
    '字段示例：{"message": "不要截掉说明"}',
    '{"wrapper":{"message":"嵌套示例","interaction":"continue","choices":[]}}',
    '[{"message":"数组示例","interaction":"continue","choices":[]}]',
    '示例：{"message":"内层示例","interaction":"continue","choices":[]}\n后面还有解释。',
    '未完成：{"message":"部分文字","interaction":"continue","choices":[',
])
def test_ordinary_json_examples_and_incomplete_output_stay_text(reply):
    assert structured_reply(reply) is None
    assert parse(reply, KodiTools(lambda *args: None))['message'] == reply


def test_exact_message_only_reply_is_still_supported():
    assert parse('{"message":"你好"}', KodiTools(lambda *args: None))['message'] == '你好'


@pytest.mark.parametrize('metadata,expected', [
    ({'kind': 'movie'}, '同名作品（2020） · 电影 · 本地媒体库'),
    ({'kind': 'tvshow'}, '同名作品（2020） · 电视剧 · 本地媒体库'),
    ({'kind': 'emby', 'media_type': 'movie', 'server_name': 'Emby'},
     '同名作品（2020） · 电影 · Emby · Emby'),
    ({'kind': 'emby', 'media_type': 'tvshow', 'server_name': ' emby '},
     '同名作品（2020） · 电视剧 · Emby · emby'),
    ({'kind': 'emby', 'media_type': 'movie', 'server_name': ''},
     '同名作品（2020） · 电影 · Emby · Emby'),
    ({'kind': 'emby', 'media_type': 'tvshow', 'server_name': '测试服'},
     '同名作品（2020） · 电视剧 · Emby · 测试服'),
    ({'kind': 'juku', 'media_type': 'movie', '_juku_source': 'private'},
     '同名作品（2020） · 电影 · 巨库私有库'),
    ({'kind': 'juku', 'media_type': 'tvshow', '_juku_source': 'online'},
     '同名作品（2020） · 电视剧 · 巨库在线目录'),
    ({'kind': 'juku', 'media_type': '', '_juku_source': 'online'},
     '同名作品（2020） · 影视（类型未知） · 巨库在线目录'),
    ({'kind': 'episode', 'showtitle': '测试剧', 'season': 1, 'episode': 3},
     '测试剧 第1季第3集：同名作品 · 电视剧 · 本地媒体库'),
    ({'kind': 'emby', 'media_type': 'episode', 'showtitle': '测试剧', 'season': 2, 'episode': 4,
      'server_name': 'Emby'}, '测试剧 第2季第4集：同名作品 · 电视剧 · Emby · Emby'),
    ({'kind': 'channel'}, '同名作品（2020） · 直播频道'),
])
@pytest.mark.parametrize('explicit', [True, False])
def test_media_cards_identify_verified_type_and_source_for_model_and_local_results(metadata, expected, explicit):
    media = dict(metadata, id=1, title='同名作品', year=2020, thumbnail='poster.jpg')
    original = dict(media)
    tools = KodiTools(lambda *args: pytest.fail('Displaying choices must not perform I/O'), media=[media])
    tools.latest_matches = [media]
    # Labels and metadata supplied by a model or an older local formatter are
    # replaced using the remembered candidate; playback intent stays untouched.
    choices = [{'kind': media['kind'], 'id': 1, 'label': '错误标签 · Emby · Emby', 'value': '继续播放',
                'media_type': 'wrong', 'server_name': 'wrong'}] if explicit else []
    reply = json.dumps({'message': '请选择', 'choices': choices})
    shown = parse(reply, tools)['choices'][0]
    assert shown['label'] == expected
    assert shown['kind'] == media['kind'] and shown['id'] == 1 and shown['thumbnail'] == 'poster.jpg'
    if explicit:
        assert shown['value'] == '继续播放'
    assert media == original


def test_non_media_action_labels_remain_unchanged():
    reply = json.dumps({'message': '请选择', 'choices': [
        {'kind': 'none', 'id': 0, 'label': '换个关键词', 'value': '重新查询'}]})
    shown = parse(reply, KodiTools(lambda *args: None))['choices'][0]
    assert shown == {'label': '换个关键词', 'value': '重新查询'}
