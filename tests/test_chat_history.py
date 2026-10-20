from resources.lib.data_paths import data_path
import json
from resources.lib.intelligence.transcript import Transcript


def test_fifty_rounds_keep_full_replies_work_and_choices_across_reopens(tmp_path):
    history = Transcript(tmp_path, 'session-a')
    for index in range(53):
        history.begin('问题 ' + str(index))
        history.activity('正在查询')
        history.finish({'message': ('完整回复 ' * 1000) + str(index),
                        'choices': [{'label': '电影', 'value': '播放', 'kind': 'movie', 'id': index}]})
    loaded = Transcript(tmp_path, 'session-a')
    assert len(loaded.rounds) == 50
    assert loaded.rounds[0]['user'] == '问题 3'
    assert loaded.rounds[-1]['answer']['message'].endswith('52')
    assert loaded.rounds[-1]['activity'] == ['正在查询']
    assert loaded.rounds[0]['answer']['choices'][0]['id'] == 3
    assert history.path.stat().st_mode & 0o777 == 0o600


def test_cancelled_and_crashed_rounds_survive_without_fabricated_answer(tmp_path):
    history = Transcript(tmp_path, 'session-a')
    history.begin('已取消的问题')
    history.activity('正在执行')
    history.interrupt()
    assert Transcript(tmp_path, 'session-a').rounds[0]['state'] == 'interrupted'
    history.begin('未完成的问题')
    assert Transcript(tmp_path, 'session-a').rounds[-1]['answer'] is None
    assert Transcript(tmp_path, 'session-a').rounds[-1]['state'] == 'working'
    history.path.write_text(json.dumps({'wrong': 'shape'}))
    assert Transcript(tmp_path, 'session-a').rounds == []


def test_old_unscoped_history_and_native_logs_are_not_imported(tmp_path):
    (data_path(tmp_path, 'chat-history.json')).write_text(json.dumps([{'user':'旧会话','answer':None}]))
    assert Transcript(tmp_path, 'new-session').rounds == []
    (data_path(tmp_path, 'chat-history.json')).unlink()
    directory = data_path(tmp_path, 'codex-home/sessions/2026')
    directory.mkdir(parents=True)
    (directory/'rollout.jsonl').write_text('old native history')
    assert Transcript(tmp_path, 'new-session').rounds == []


def test_different_session_never_loads_previous_rounds(tmp_path):
    history=Transcript(tmp_path,'first')
    history.begin('旧会话');history.finish({'message':'旧回复','choices':[]})
    assert Transcript(tmp_path,'first').rounds
    assert Transcript(tmp_path,'second').rounds == []
    # Merely reading a new scope does not delete the stored history.
    assert Transcript(tmp_path,'first').rounds


def test_supplemental_inputs_and_intermediate_answers_keep_timeline(tmp_path):
    history = Transcript(tmp_path, 'session-a')
    history.begin('第一句')
    history.activity('开始执行')
    history.message('user', '补充要求')
    history.activity('已发送')
    history.message('assistant', '上一轮的回答')
    history.finish({'message': '最后的回答', 'choices': []})
    row = Transcript(tmp_path, 'session-a').rounds[0]
    assert row['events'] == [
        {'role': 'activity', 'text': '开始执行'}, {'role': 'user', 'text': '补充要求'},
        {'role': 'activity', 'text': '已发送'}, {'role': 'assistant', 'text': '上一轮的回答'}]
    assert row['answer']['message'] == '最后的回答'
