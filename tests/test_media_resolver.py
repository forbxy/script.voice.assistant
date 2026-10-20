"""Source priority is a pure decision, independent of Kodi, model and playback."""
import pytest

from resources.lib.intelligence.media_results import SearchResult
from resources.lib.intelligence.media_resolver import choose


def result(source, kind, complete=True):
    titles = {'empty': [], 'partial': ['希望之旅'], 'exact': ['希望'], 'versions': ['希望', '希望']}
    rows = [{'id': str(index), 'title': title, 'year': 2026-index} for index, title in enumerate(titles[kind])]
    if source == 'local':
        rows = [{'row': dict(row, id=index, kind='movie'), 'score': 1. if kind in ('exact','versions') else .7}
                for index, row in enumerate(rows)]
    return SearchResult(source=source, rows=rows, exact=kind in ('exact','versions'), complete=complete)


@pytest.mark.parametrize('local,private,online,expected,automatic', [
    ('exact','exact','exact','local',False),
    ('versions','exact','exact','local',False),
    ('partial','exact','exact','private',True),
    ('partial','versions','exact','private',False),
    ('partial','partial','exact','online',True),
    ('partial','partial','versions','online',False),
    ('partial','partial','partial','mixed',False),
    ('empty','partial','partial','mixed',False),
    ('empty','empty','partial','mixed',False),
])
def test_source_priority(local, private, online, expected, automatic):
    request = {'title':'希望'}
    selected = choose(result('local',local), result('private',private), result('online',online), request)
    assert selected.source == expected and selected.automatic is automatic
    if expected != 'local' and selected.exact:
        assert all(row['row']['title'] == '希望' for row in selected.rows)


@pytest.mark.parametrize('preceding_complete,online_complete', [(False,True),(True,False),(False,False)])
def test_unknown_or_truncated_catalogue_cannot_be_treated_as_a_unique_match(preceding_complete, online_complete):
    private = result('private','empty',preceding_complete)
    online = result('online','exact',online_complete)
    selected = choose(result('local','partial'), private, online, {'title':'希望'})
    assert selected.source == 'online' and not selected.automatic and not selected.complete


def test_sources_can_be_imported_without_commands_ui_or_agent_clients():
    import subprocess
    import sys
    code = '''import sys
from resources.lib.intelligence import command_media, juku, juku_playback
for name in ('resources.lib.intelligence.commands','resources.lib.ui.agent_turn',
             'resources.lib.codex_agent','resources.lib.codebuddy_agent'):
 assert name not in sys.modules,name
'''
    subprocess.run([sys.executable,'-c',code],check=True,capture_output=True,text=True)
