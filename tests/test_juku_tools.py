import json
from pathlib import Path

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence import juku
from resources.lib.intelligence.permissions import Permissions
from resources.lib.intelligence.presentation import parse
from resources.lib.intelligence.sessions import Sessions


class API:
    def __init__(self, cancelled=lambda: False):
        self.cancelled = cancelled
        self.calls = []
        self.private = []
        self.online = [{'id': '20676', 'title': '疾速追杀', 'year': 2014, 'typeId': 1,
                        'url': 'private-share', 'password': 'private-password'}]

    def check(self):
        if self.cancelled(): raise InterruptedError('cancelled')

    def get(self, path, params=None, raw=False):
        self.calls.append((path, params))
        if path == '/api/private/libraries': return [{'id': 'private-library'}]
        if path.startswith('/api/private/libraries/'):
            return {'items': self.private, 'total': len(self.private)}
        if path == '/api/mv/videos/search':
            return {'items': self.online, 'pagination': {'total': len(self.online)}}
        if path.startswith('/api/mv/videos/'):
            return next((row for row in self.online if str(row['id']) == path.rsplit('/', 1)[1]), {})
        if path.endswith('/links'): return [{'id': 'link', 'link_type': 'direct', 'url': 'https://media.invalid/movie.mp4'}]
        if path == '/api/playback/status': return {'resume_seconds': 35}
        raise AssertionError(path)

    pages = juku.Client.pages


@pytest.fixture
def setup(monkeypatch):
    api = API()
    monkeypatch.setattr(juku, 'available', lambda tools: True)
    monkeypatch.setattr(juku, 'Client', lambda cancelled=lambda: False: api)
    tools = KodiTools(lambda *args: pytest.fail('Unexpected Kodi read'), defer_ui=True)
    return tools, api


@pytest.mark.parametrize('present', [False, True])
def test_optional_tools_are_only_advertised_when_available(monkeypatch, present):
    calls = []
    monkeypatch.setattr(juku, 'available', lambda tools: calls.append(True) or present)
    tools = KodiTools(lambda *args: None)
    for _ in range(3):
        names = {row['function']['name'] for row in tools.definitions()}
        assert ('search_juku' in names) is present and ('play_juku' in names) is present
    assert len(calls) == 1  # Repeated tool execution must not re-query Kodi's registry.
    if not present:
        assert not tools.execute('search_juku', {'query': '疾速追杀'})['ok']


def test_search_does_not_play_and_excludes_private_api_fields(setup):
    tools, api = setup
    result = tools.execute('search_juku', {'query': '疾速追杀'})
    assert result['ok'] and result['matches'][0]['kind'] == 'juku'
    assert result['matches'][0]['title'] == '疾速追杀' and result['complete']
    assert not tools.acted and tools.ui_action is None
    public = json.dumps(result)
    assert 'private-share' not in public and 'private-password' not in public
    assert 'private-library' not in public and '_juku_' not in public
    remembered = tools.media[('juku', result['matches'][0]['id'])]
    assert remembered['_juku_id'] == '20676'


def test_search_reorders_fuzzy_results_and_reports_our_score_without_server_aliases(setup):
    from resources.lib.intelligence.media_match import rank
    tools, api = setup
    api.online = [{'id': 'wrong', 'title': '楚门的世界', 'year': 1998, 'score': 100,
                   'matchedText': '疾速追杀', 'matchField': 'alias'},
                  {'id': 'sequel', 'title': '疾速追杀2', 'year': 2017},
                  {'id': 'first', 'title': '疾速追杀', 'year': 2014}]
    result = tools.execute('search_juku', {'query': '疾速追沙'})
    assert [row['title'] for row in result['matches']] == ['疾速追杀', '疾速追杀2']
    score = rank('疾速追沙', [{'kind':'movie', 'id':1, 'title':'疾速追杀'}])[0][0]['score']
    assert result['matches'][0]['score'] == score and not tools.acted
    assert 'matchedText' not in json.dumps(result)


def test_search_reports_truncation_beyond_twelve_ranked_results(setup):
    tools, api = setup
    api.online = [{'id': str(i), 'title': '疾速追杀', 'year': 2000 + i} for i in range(15)]
    result = tools.execute('search_juku', {'query': '疾速追杀', 'limit': 12})
    assert len(result['matches']) == 12 and result['truncated'] and not result['complete']


def test_fuzzy_results_with_type_do_not_require_extra_detail_calls(setup):
    tools, api = setup
    result = tools.execute('search_juku', {'query': '疾速追沙', 'kind': 'movie'})
    assert result['matches'][0]['title'] == '疾速追杀'
    assert not any(path.startswith('/api/mv/videos/') and not path.endswith('/search') for path, _ in api.calls)


def test_search_results_become_verified_selectable_cards(setup):
    tools, api = setup
    result = tools.execute('search_juku', {'query': '疾速追杀'})
    shown = parse('{"message":"请选择","interaction":"continue","choices":[]}', tools)
    assert shown['choices'][0]['kind'] == 'juku'
    assert shown['choices'][0]['id'] == result['matches'][0]['id']


def test_play_uses_only_a_previously_queried_id_and_is_deferred(setup):
    tools, api = setup
    assert not tools.execute('play_juku', {'id': 20676})['ok']
    assert tools.ui_action is None
    row = tools.execute('search_juku', {'query': '疾速追杀'})['matches'][0]
    result = tools.execute('play_juku', {'id': row['id']})
    assert result['ok'] and result['deferred'] == 'ui_navigation'
    assert result['status'] == 'pending' and result['destination'] == 'resource_picker'
    assert tools.ui_action.startswith('ActivateWindow(Videos,') and 'id=20676' in tools.ui_action
    assert 'plugin://' not in json.dumps(result) and 'media.invalid' not in json.dumps(result)


@pytest.mark.parametrize('resume', [True, False])
def test_private_play_preserves_explicit_resume_choice(setup, resume):
    tools, api = setup
    api.private = [{'id': 'private-movie', 'title': '疾速追杀', 'year': 2014, 'type_id': 1}]
    row = tools.execute('search_juku', {'query': '疾速追杀'})['matches'][0]
    result = tools.execute('play_juku', {'id': row['id'], 'resume': resume})
    assert result['ok'] and result['destination'] == 'playback'
    assert tools.ui_action.endswith(',resume)' if resume else ',noresume)')
    assert ('resume_time=35' in tools.ui_action) is resume
    assert tools.media[('juku', row['id'])]['_juku_request']['resume'] is True


def test_source_selection_can_search_online_after_private_results(setup):
    tools, api = setup
    api.private = [{'id': 'p1', 'title': '疾速追杀', 'year': 2014, 'type_id': 1}]
    assert tools.execute('search_juku', {'query': '疾速追杀'})['source'] == 'private'
    assert not any(path.startswith('/api/mv') for path, _ in api.calls)
    result = tools.execute('search_juku', {'query': '疾速追杀', 'source': 'online'})
    assert result['source'] == 'online' and result['matches']


def test_private_only_search_never_accesses_online_catalogue(setup):
    tools, api = setup
    result = tools.execute('search_juku', {'query': '疾速追杀', 'source': 'private'})
    assert result['ok'] and not result['matches'] and result['complete']
    assert not any(path.startswith('/api/mv') for path, _ in api.calls)


def test_type_year_and_limit_are_honoured_without_fuzzy_correction(setup):
    tools, api = setup
    api.online += [{'id': '2', 'title': '疾速追杀2', 'year': 2017, 'typeId': 1}]
    result = tools.execute('search_juku', {'query': '疾速追杀', 'limit': 1})
    assert result['truncated'] and not result['complete'] and len(result['matches']) == 1
    result = tools.execute('search_juku', {'query': '疾速追杀', 'kind': 'movie', 'year': 2017})
    assert [row['title'] for row in result['matches']] == ['疾速追杀2']
    assert tools.execute('search_juku', {'query': '疾速追杀', 'kind': 'tvshow'})['matches'] == []
    tools.execute('search_juku', {'query': '激素追杀'})
    assert [params['q'] for path, params in api.calls if path.endswith('/search')][-1] == '激素追杀'


def test_stale_or_removed_addon_never_plays(setup, monkeypatch):
    tools, api = setup
    row = tools.execute('search_juku', {'query': '疾速追杀'})['matches'][0]
    api.online[0]['title'] = '其他片名'
    result = tools.execute('play_juku', {'id': row['id']})
    assert not result['ok'] and '已变化' in result['error'] and not tools.acted
    monkeypatch.setattr(juku, 'available', lambda tools: False)
    assert not tools.execute('play_juku', {'id': row['id']})['ok']
    assert not tools.acted


def test_cancel_and_permission_ceiling_prevent_handoff(setup):
    tools, api = setup
    row = tools.execute('search_juku', {'query': '疾速追杀'})['matches'][0]
    with pytest.raises(InterruptedError):
        tools.execute('play_juku', {'id': row['id']}, cancelled=lambda: True)
    class DenyPlay(Permissions):
        def allows(self, name): return name != 'play_media'
    tools.permissions = DenyPlay('media')
    assert not tools.execute('play_juku', {'id': row['id']})['ok']
    assert not tools.acted


def test_followup_can_use_persisted_juku_choice(setup, tmp_path):
    tools, api = setup
    row = tools.execute('search_juku', {'query': '疾速追杀'})['matches'][0]
    sessions = Sessions(tmp_path, 'forever', signature='test')
    sessions.completed(tools.media.values())
    following = KodiTools(tools.rpc, media=Sessions(tmp_path, 'forever', signature='test').media, defer_ui=True)
    assert following.execute('play_juku', {'id': row['id']})['ok']


@pytest.mark.parametrize('arguments', [{'id': True}, {'id': -1}, {'id': 1, 'url': 'http://evil'},
                                     {'id': 1, 'resume': 'yes'}])
def test_play_schema_rejects_urls_or_invalid_ids_before_accessing_server(setup, arguments):
    tools, api = setup
    assert not tools.execute('play_juku', arguments)['ok']
    assert not api.calls and not tools.acted


def test_codex_dynamic_tool_dispatch_includes_optional_juku_tools(setup, tmp_path):
    from resources.lib.codex_agent import CodexAgent
    tools, api = setup
    class Protocol:
        def __init__(self): self.requests = []; self.sent = []
        def request(self, method, params):
            self.requests.append((method, params))
            return {'initialize': {}, 'thread/start': {'thread': {'id': 'thread'}},
                    'turn/start': {'turn': {'id': 'turn'}}}[method]
        def send(self, value): self.sent.append(value)
        def close(self, *args): pass
        def event(self): return next(events)
    events = iter([
        {'id': 1, 'method': 'item/tool/call', 'params': {'threadId': 'thread', 'turnId': 'turn',
            'callId': 'search', 'tool': 'search_juku', 'arguments': {'query': '疾速追杀'}}},
        {'id': 2, 'method': 'item/tool/call', 'params': {'threadId': 'thread', 'turnId': 'turn',
            'callId': 'play', 'tool': 'play_juku', 'arguments': {'id': 1}}},
        {'method': 'turn/completed', 'params': {'turn': {'status': 'completed', 'items': [
            {'type': 'agentMessage', 'id': 'answer', 'phase': 'final_answer', 'text': '准备打开巨库资源页。'}]}}}])
    protocol = Protocol()
    binary = tmp_path / 'binary'; binary.touch(mode=0o755)
    binary.with_name('codex-code-mode-host').touch(mode=0o755)
    agent = CodexAgent(tmp_path, tmp_path, 'test-key', tools, binary=binary,
        permissions=Permissions('full', 'full_access'), connection_factory=lambda *args: protocol)
    outcome = agent.run('通过巨库播放疾速追杀')
    assert [row['name'] for row in outcome['tools']] == ['search_juku', 'play_juku']
    definitions = next(params['dynamicTools'] for name, params in protocol.requests if name == 'thread/start')
    assert {'search_juku', 'play_juku'} <= {row['name'] for row in definitions}
    assert 'private-password' not in json.dumps(protocol.sent)


def test_codebuddy_mcp_dispatch_uses_same_verified_search_and_play(setup, tmp_path):
    from resources.lib.codebuddy_agent import CodeBuddyAgent
    tools, api = setup
    agent = CodeBuddyAgent(Path(__file__).resolve().parents[1], tmp_path, tools)
    allowed = {row['function']['name'] for row in tools.definitions()}
    completed, trace = {}, []
    def request(method, params, id):
        return {'server_name': 'kodi', 'tool_use_id': 'call-' + str(id),
                'message': {'id': id, 'method': method, 'params': params}}
    advertised = agent._mcp(request('tools/list', {}, 0), allowed, completed, trace, lambda: False, lambda x: None)
    assert {'search_juku', 'play_juku'} <= {row['name'] for row in advertised['result']['tools']}
    for id, name, args in [(1, 'search_juku', {'query': '疾速追杀'}), (2, 'play_juku', {'id': 1})]:
        response = agent._mcp(request('tools/call', {'name': name, 'arguments': args}, id),
            allowed, completed, trace, lambda: False, lambda x: None)
        assert not response['result']['isError']
        assert 'private-password' not in json.dumps(response)
    assert [row['name'] for row in trace] == ['search_juku', 'play_juku'] and tools.acted


def test_verified_play_can_handoff_immediately_without_leaving_a_duplicate_action(setup):
    tools, api = setup
    calls = []
    tools.playback_handoff = lambda command, target: calls.append((command, target)) or {
        'status': 'dispatched', 'target': target}
    row = tools.execute('search_juku', {'query': '疾速追杀'})['matches'][0]
    tools._queue_ui('obsolete-destination', 'obsolete-addon')
    result = tools.execute('play_juku', {'id': row['id']})
    assert result['ok'] and result['status'] == 'dispatched' and 'deferred' not in result
    assert result['destination'] == 'resource_picker'
    assert len(calls) == 1 and calls[0][1] == 'plugin.video.juku'
    assert calls[0][0].startswith('ActivateWindow(Videos,')
    assert tools.ui_handed_off and tools.ui_action is None
    assert 'plugin://' not in json.dumps(result)
    # Availability and identity checks still precede foreground handoff.
    api.online[0]['title'] = 'changed'
    assert not tools.execute('play_juku', {'id': row['id']})['ok']
    assert len(calls) == 1
