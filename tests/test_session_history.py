from resources.lib.intelligence.sessions import Sessions
from resources.lib.intelligence.transcript import Transcript


def save_turn(profile, session, text):
    history = Transcript(profile, session.history_id)
    history.begin(text)
    history.finish({'message': '回复', 'choices': []})
    return history


def test_conversation_keeps_followups_but_next_window_starts_empty(tmp_path):
    session = Sessions(tmp_path, 'conversation', 'model', today='2026-10-15')
    session.started('native-a')
    save_turn(tmp_path, session, '第一句')
    assert not session.refresh(today='2026-10-16')
    assert session.thread_id == 'native-a'
    assert len(save_turn(tmp_path, session, '继续').rounds) == 2
    reopened = Sessions(tmp_path, 'conversation', 'model', today='2026-10-16')
    assert reopened.thread_id is None and reopened.history_id != session.history_id
    assert Transcript(tmp_path, reopened.history_id).rounds == []


def test_daily_reopens_same_day_and_rotates_in_open_window_after_midnight(tmp_path):
    first = Sessions(tmp_path, 'daily', 'model', today='2026-10-15')
    first.started('native-day1')
    save_turn(tmp_path, first, '今天')
    reopened = Sessions(tmp_path, 'daily', 'model', today='2026-10-15')
    assert reopened.history_id == first.history_id and reopened.thread_id == first.thread_id
    assert Transcript(tmp_path, reopened.history_id).rounds[0]['user'] == '今天'
    assert reopened.refresh(today='2026-10-16')
    assert reopened.thread_id is None and reopened.media == []
    assert Transcript(tmp_path, reopened.history_id).rounds == []
    reopened.started('native-day2')
    save_turn(tmp_path, reopened, '明天')
    again = Sessions(tmp_path, 'daily', 'model', today='2026-10-16')
    assert [r['user'] for r in Transcript(tmp_path, again.history_id).rounds] == ['明天']


def test_forever_keeps_thread_and_last_fifty_rounds_across_days(tmp_path):
    first = Sessions(tmp_path, 'forever', 'model', today='2026-10-15')
    first.started('native-forever')
    for index in range(53): save_turn(tmp_path, first, str(index))
    reopened = Sessions(tmp_path, 'forever', 'model', today='2027-01-01')
    assert not reopened.refresh(today='2027-02-01')
    assert reopened.history_id == first.history_id and reopened.thread_id == first.thread_id
    rows = Transcript(tmp_path, reopened.history_id).rounds
    assert len(rows) == 50 and rows[0]['user'] == '3' and rows[-1]['user'] == '52'


def test_policy_configuration_and_explicit_clear_rotate_both(tmp_path):
    session = Sessions(tmp_path, 'forever', 'model')
    for change in ('policy', 'model', 'clear_file', 'clear_method'):
        session.started('native-old')
        save_turn(tmp_path, session, '旧记录')
        before = session.history_id
        if change == 'policy': assert session.refresh(policy='daily')
        elif change == 'model': assert session.refresh(signature='other-model')
        elif change == 'clear_file':
            session.path.unlink()
            assert session.refresh()
        else: session.clear()
        assert session.history_id != before and session.thread_id is None
        assert Transcript(tmp_path, session.history_id).rounds == []


def test_identity_survives_failed_first_request_without_native_thread(tmp_path):
    session = Sessions(tmp_path, 'forever', 'model')
    history = Transcript(tmp_path, session.history_id)
    history.begin('连接失败的输入');history.interrupt()
    reopened = Sessions(tmp_path, 'forever', 'model')
    assert reopened.history_id == session.history_id
    assert Transcript(tmp_path, reopened.history_id).rounds[0]['state'] == 'interrupted'
