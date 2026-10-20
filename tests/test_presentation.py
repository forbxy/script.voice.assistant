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
