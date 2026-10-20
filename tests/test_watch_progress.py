from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.watch_progress import continuation


def episode(number, season=1, position=0, played=0, date=''):
    return {'episodeid': season * 1000 + number, 'title': '第%d集' % number,
            'season': season, 'episode': number, 'playcount': played, 'lastplayed': date,
            'resume': {'position': position, 'total': 2876 if position else 0}}


def test_resume_uses_latest_played_episode_not_first_unwatched_or_highest_number():
    rows = [episode(1), episode(2, position=1403, date='2026-10-15 22:03:55'),
            episode(8, position=900, date='2026-10-01 12:00:00')]
    result = continuation(rows)
    assert result['status'] == 'resume'
    assert result['play'] == {'kind': 'episode', 'id': 1002, 'resume': True}
    assert result['recommended']['resume']['position'] == 1403


def test_finished_episode_advances_across_seasons_not_to_old_unwatched_or_specials():
    rows = [episode(1), episode(20, played=1, date='2026-10-15 12:00:00'),
            episode(3, season=0), episode(1, season=2)]
    result = continuation(rows)
    assert result['status'] == 'next_episode'
    assert result['play'] == {'kind': 'episode', 'id': 2001, 'resume': True}


def test_last_played_without_bookmark_restarts_that_episode_and_explains():
    result = continuation([episode(2, date='2026-10-15 12:00:00')])
    assert result['status'] == 'restart_episode' and result['play']['resume']


def test_missing_ambiguous_and_end_of_library_have_no_automatic_play():
    cases = [([episode(1)], 'no_history'),
             ([episode(1, position=10), episode(2, position=20)], 'ambiguous'),
             ([episode(1, played=1)], 'no_next_episode'),
             ([episode(1, played=1), episode(2), dict(episode(2), episodeid=999)], 'ambiguous')]
    for rows, status in cases:
        result = continuation(rows)
        assert result['status'] == status and 'play' not in result


def test_progress_reads_all_pages_and_registers_only_real_continuation_ids():
    rows = [episode(n) for n in range(1, 252)]
    rows[240] = episode(241, position=80, date='2026-10-15 12:00:00')
    requests = []
    def rpc(method, params):
        requests.append((method, params))
        if method == 'Player.Open': return 'OK'
        start, end = params['limits']['start'], params['limits']['end']
        assert {'resume', 'lastplayed', 'playcount'} <= set(params['properties'])
        return {'episodes': rows[start:end], 'limits': {'total': len(rows)}}
    tools = KodiTools(rpc)
    assert not tools.execute('get_tvshow_progress', {'tvshowid': 3})['ok']
    assert not requests
    tools.media[('tvshow', 3)] = {'kind': 'tvshow', 'id': 3, 'title': '测试剧'}
    result = tools.execute('get_tvshow_progress', {'tvshowid': 3})
    assert result['total_episodes'] == 251 and result['recommended']['episode'] == 241
    assert len(requests) == 2
    assert result['recommended']['resume']['position'] == 80
    assert tools.execute('play_media', result['play'])['ok']
    assert requests[-1] == ('Player.Open', {'item': {'episodeid': 1241}, 'options': {'resume': True}})


def test_regular_queries_keep_progress_fields_but_not_private_media_paths():
    def rpc(method, params):
        assert {'resume', 'playcount', 'lastplayed'} <= set(params['properties'])
        if method.endswith('GetMovies'):
            return {'movies': [{'movieid': 5, 'title': '影片', 'resume': {'position': 123, 'total': 456},
                                'lastplayed': '2026-10-15 12:00:00', 'playcount': 0, 'file': 'secret-path'}]}
        return {'episodes': [dict(episode(2, position=1403), file='secret-path')]}
    tools = KodiTools(rpc, media=[{'kind': 'tvshow', 'id': 3, 'title': '剧'}])
    movie = tools.execute('search_media', {'query': '影片', 'kind': 'movie'})['matches'][0]
    row = tools.execute('get_episodes', {'tvshowid': 3})['matches'][0]
    assert movie['resume']['position'] == 123 and row['resume']['position'] == 1403
    assert 'file' not in movie and 'file' not in row


def test_undated_bookmark_is_not_hidden_by_another_episodes_timestamp():
    result = continuation([episode(2, position=1403), episode(3, played=1, date='2026-10-15 12:00:00'), episode(4)])
    assert result['status'] == 'ambiguous' and 'play' not in result
    assert {row['episodeid'] for row in result['candidates']} == {1002, 1003}


def test_equal_timestamps_do_not_guess_which_incomplete_episode_was_last():
    result = continuation([episode(2, position=1403, date='2026-10-15 12:00:00'),
                           episode(3, played=1, date='2026-10-15 12:00:00')])
    assert result['status'] == 'ambiguous' and 'play' not in result


def test_bookmark_at_end_does_not_restart_finished_episode():
    result = continuation([episode(2, position=2876), episode(3)])
    assert result['status'] == 'next_episode' and result['play']['id'] == 1003


def test_unknown_episode_number_is_not_automatically_played():
    result = continuation([episode(0, position=1403)])
    assert result['status'] == 'ambiguous' and 'play' not in result


def test_ambiguous_versions_are_returned_as_verified_choices_without_private_paths():
    rows = [episode(1, played=1), episode(2), dict(episode(2), episodeid=999, file='secret-path')]
    tools = KodiTools(lambda *args: {'episodes': rows}, media=[{'kind': 'tvshow', 'id': 3, 'title': '测试剧'}])
    result = tools.execute('get_tvshow_progress', {'tvshowid': 3})
    assert result['candidate_count'] == 2
    assert {row['id'] for row in result['candidates']} == {1002, 999}
    assert tools.latest_matches == result['candidates']
    assert all('file' not in row for row in result['candidates'])
    assert ('episode', 999) in tools.media


def test_pagination_uses_actual_returned_length_when_server_caps_page_size():
    rows = [episode(n) for n in range(1, 252)]
    rows[240] = episode(241, position=80)
    starts = []
    def rpc(method, params):
        start = params['limits']['start']; starts.append(start)
        return {'episodes': rows[start:start+100], 'limits': {'start': start, 'total': len(rows)}}
    tools = KodiTools(rpc, media=[{'kind': 'tvshow', 'id': 3, 'title': '测试剧'}])
    result = tools.execute('get_tvshow_progress', {'tvshowid': 3})
    assert result['ok'] and result['play']['id'] == 1241
    assert starts == [0, 100, 200]


def test_incomplete_repeated_or_changed_pages_never_produce_play_recommendation():
    for second in ({'episodes': [], 'limits': {'total': 201}},
                   {'episodes': [episode(1)], 'limits': {'total': 201}},
                   {'episodes': [episode(201)], 'limits': {'total': 202}},
                   {'episodes': [episode(201)], 'limits': {'start': 199, 'total': 201}}):
        pages = iter([{'episodes': [episode(n) for n in range(1, 201)], 'limits': {'total': 201}}, second])
        tools = KodiTools(lambda *args: next(pages), media=[{'kind': 'tvshow', 'id': 3, 'title': '测试剧'}])
        result = tools.execute('get_tvshow_progress', {'tvshowid': 3})
        assert not result['ok'] and 'play' not in result
        assert not any(kind == 'episode' for kind, id in tools.media)


def test_progress_can_be_cancelled_between_pages():
    import pytest
    requests = []
    def rpc(method, params):
        requests.append(params)
        return {'episodes': [episode(n) for n in range(1, 201)], 'limits': {'total': 201}}
    tools = KodiTools(rpc, media=[{'kind': 'tvshow', 'id': 3, 'title': '测试剧'}])
    with pytest.raises(InterruptedError):
        tools.execute('get_tvshow_progress', {'tvshowid': 3}, cancelled=lambda: bool(requests))
    assert len(requests) == 1


def test_episode_listing_can_reach_beyond_first_thirty():
    requests = []
    def rpc(method, params):
        requests.append(params)
        return {'episodes': [episode(31), episode(32)], 'limits': {'total': 80}}
    tools = KodiTools(rpc, media=[{'kind': 'tvshow', 'id': 3, 'title': '测试剧'}])
    result = tools.execute('get_episodes', {'tvshowid': 3, 'season': 1, 'offset': 30, 'limit': 2})
    assert requests[0]['limits'] == {'start': 30, 'end': 32}
    assert result['total'] == 80 and result['next_offset'] == 32
    assert [row['episode'] for row in result['matches']] == [31, 32]
