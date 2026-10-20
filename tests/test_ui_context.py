import json

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.ui_context import capture, LABELS
from test_codex_agent import finished, make_agent


def fixture_rpc(kind='movie', plugin='', title='密码疑云', actual_title=None):
    calls = []
    labels = dict.fromkeys(LABELS, '')
    labels.update({'ListItem.Label': '[COLOR white]' + title + '[/COLOR]', 'ListItem.Title': title,
                   'ListItem.DBTYPE': kind, 'ListItem.DBID': '42', 'ListItem.Year': '2007',
                   'Container.Content': kind + 's', 'Container.FolderName': '电影', 'Container.PluginName': plugin})
    def rpc(method, params):
        calls.append((method, params))
        if method == 'GUI.GetProperties':
            return {'currentwindow': {'id': 10025, 'label': '视频'}, 'currentcontrol': {'label': title}}
        if method == 'XBMC.GetInfoLabels':
            return dict(labels)
        fields = {'movie': 'moviedetails', 'tvshow': 'tvshowdetails', 'episode': 'episodedetails'}
        if method.startswith('VideoLibrary.Get'):
            return {fields[kind]: {kind + 'id': 42, 'title': actual_title if actual_title is not None else title}}
        return 'OK'
    return rpc, labels, calls


def test_selection_uses_real_library_id_without_screenshots_paths_or_directory_listing():
    rpc, labels, calls = fixture_rpc()
    value = capture(rpc)
    assert value['source'] == 'before_assistant'
    assert value['window'] == {'id': 10025, 'label': '视频'}
    assert value['selected']['label'] == value['selected']['title'] == '密码疑云'
    assert value['selected']['media'] == {'kind': 'movie', 'id': 42, 'title': '密码疑云'}
    assert value['selected']['year'] == 2007
    assert [method for method, _ in calls] == ['GUI.GetProperties', 'XBMC.GetInfoLabels', 'VideoLibrary.GetMovieDetails']
    assert calls[1][1]['labels'] == list(LABELS)
    assert not any('path' in field.lower() or 'filename' in field.lower() for field in LABELS)


@pytest.mark.parametrize('kind', ['tvshow', 'episode'])
def test_selected_show_and_episode_keep_available_metadata(kind):
    rpc, labels, calls = fixture_rpc(kind, title='测试剧')
    labels.update({'ListItem.TVShowTitle': '测试剧', 'ListItem.Season': '0', 'ListItem.Episode': '12'})
    selected = capture(rpc)['selected']
    assert selected['media']['kind'] == kind
    assert selected['season'] == 0 and selected['episode'] == 12 and selected['showtitle'] == '测试剧'


@pytest.mark.parametrize('plugin,actual_title', [('plugin.video.example', None), ('', '另一个片名')])
def test_plugin_supplied_or_mismatched_ids_never_become_playable_library_items(plugin, actual_title):
    rpc, labels, calls = fixture_rpc(plugin=plugin, actual_title=actual_title)
    value = capture(rpc)
    assert value['selected']['title'] == '密码疑云' and 'media' not in value['selected']
    assert not KodiTools(rpc, ui_context=value).media
    if plugin:
        assert len(calls) == 2 and value['container']['plugin'] == plugin


def test_home_button_and_empty_selection_are_reported_without_inventing_a_movie():
    rpc, labels, calls = fixture_rpc()
    labels.update(dict.fromkeys(LABELS, ''))
    value = capture(rpc)
    assert value['selected'] is None and len(calls) == 2
    def home(method, params):
        return ({'currentwindow': {'id': 10000, 'label': '主界面'}, 'currentcontrol': {'label': '设置'}}
                if method == 'GUI.GetProperties' else dict(labels))
    assert capture(home)['focus'] == {'label': '设置'}


def test_sensitive_control_values_tokens_and_urls_are_not_sent():
    def rpc(method, params):
        if method == 'GUI.GetProperties':
            return {'currentwindow': {'id': 10040, 'label': '插件设置'},
                    'currentcontrol': {'label': 'API Key (this-private-key)'}}
        return {'ListItem.Label': 'file https://user:password@example.test/movie?token=private',
                'ListItem.Title': 'sk-private123456789', 'Container.PluginName': 'plugin.example'}
    value = capture(rpc)
    assert value['focus']['label'] == '[敏感输入项]'
    encoded = json.dumps(value)
    for secret in ('this-private-key', 'user:password', 'token=private', 'sk-private123456789'):
        assert secret not in encoded


def test_failed_gui_or_library_lookup_keeps_voice_startup_available():
    def unavailable(*args):
        raise ValueError('Unavailable')
    assert capture(unavailable)['incomplete']
    rpc, labels, calls = fixture_rpc()
    def missing(method, params):
        if method.startswith('VideoLibrary.'):
            raise ValueError('Deleted item')
        return rpc(method, params)
    result = capture(missing)
    assert result['selected']['title'] == '密码疑云' and 'media' not in result['selected']


def test_every_ai_turn_uses_same_frozen_entry_selection_and_can_play_verified_item(tmp_path):
    rpc, labels, calls = fixture_rpc()
    value = capture(rpc)
    agent, protocol = make_agent(tmp_path, [finished(), finished()])
    agent.tools = KodiTools(rpc, ui_context=value)
    value['selected']['title'] = '助手自己的按钮'
    labels['ListItem.Label'] = '继续说话'
    for message in ('这个是什么电影？', '介绍一下这部电影'):
        agent.run(message)
    prompts = [params['input'][0]['text'] for method, params in protocol.requests if method == 'turn/start']
    assert len(prompts) == 2
    for prompt in prompts:
        data = json.loads(prompt.split('\n\n当前用户指令：\n', 1)[0].split('\n', 1)[1])
        assert data['kodi_ui_before_assistant']['selected']['title'] == '密码疑云'
        assert data['media_candidates'] == [{'kind': 'movie', 'id': 42, 'title': '密码疑云'}]
        assert '继续说话' not in prompt and '助手自己的按钮' not in prompt
    result = agent.tools.execute('play_media', {'kind': 'movie', 'id': 42})
    assert result['ok'] and ('Player.Open', {'item': {'movieid': 42}, 'options': {'resume': True}}) in calls
