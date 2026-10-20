import json
from pathlib import Path
import sys

DEV = Path(__file__).parents[1]/'dev'
sys.path.insert(0, str(DEV))
from iflytek_offline_flow import OfflineFlow, candidate_branch_enabled, normalize_identifiers


def partial(text):
    return {'status': 0, 'elems': [{'key': 0, 'sub_key': 'pgs', 'value': text+'{useCloud:0}'}]}


def final(text):
    return {'status': 2, 'elems': [{'key': 2, 'sub_key': 'plain',
                                  'value': json.dumps({'ws': [{'w': text}]})}]}


def test_partial_revisions_and_final_do_not_duplicate_text():
    flow = OfflineFlow()
    flow.on_native(partial('今天'))
    flow.on_native(partial('今天营救'))
    flow.on_native(final('今天营救。'))
    assert flow.editor.committed == '今天营救'
    assert [x['callback']['pgs'] for x in flow.trace] == ['apd', 'rpl', 'rpl']
    assert flow.editor.actions[-1]['operation'] == 'endProgressiveComposing'


def test_offline_origin_metadata_does_not_enable_cloud_candidate_stage():
    assert not candidate_branch_enabled({'en': 'esr', 'origin': {'ws': []}})
    assert candidate_branch_enabled({'en': 'ws', 'cloud': '{}'})


def test_keyword_policy_removes_punctuation_but_preserves_decimal_points():
    flow = OfflineFlow('keyword')
    flow.on_native(final('价格3.5元，今天营救。'))
    assert flow.editor.committed == '价格3.5元今天营救'


def test_readable_and_controls_are_not_appended_to_formatted_plain():
    flow = OfflineFlow()
    event = final('9点。')
    event['elems'] += [{'sub_key': 'readable', 'key': 2, 'value': json.dumps({'ws': [{'w': '九点。'}]})},
                      {'sub_key': 'pgs', 'key': 0, 'value': '九点{useCloud:1}{nVadBegin:2}'}]
    flow.on_native(event)
    assert flow.editor.committed == '9点'
    assert flow.trace[0]['callback']['control'] == [{'useCloud': 1}, {'nVadBegin': 2}]


def test_plain_final_without_any_partial_commits_once():
    flow = OfflineFlow()
    flow.on_native(final('奥兰多。'))
    assert flow.editor.committed == '奥兰多'
    assert len(flow.editor.actions) == 2


def test_empty_final_completes_the_existing_composition():
    flow = OfflineFlow()
    flow.on_native(partial('测试'))
    flow.on_native({'status': 2, 'elems': []})
    assert flow.editor.committed == '测试'


def test_vad_segments_commit_without_losing_the_previous_segment():
    flow = OfflineFlow()
    first = final('第一段')
    first['status'] = 1
    flow.on_native(first)
    second = final('，第二段')
    second['status'] = 1
    flow.on_native(second)
    flow.on_native(final('。'))
    assert flow.editor.committed == '第一段，第二段'


def test_identifier_rules_preserve_words_and_normalize_identifiers():
    assert normalize_identifiers('1 2 3 4') == '1234'
    assert normalize_identifiers('京 a 1 2 3 4 5') == '京A12345'
    assert normalize_identifiers('电影 2025') == '电影 2025'
