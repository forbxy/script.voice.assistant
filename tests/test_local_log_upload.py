import pytest

from resources.lib import log_upload
from resources.lib.intelligence.command_rules import common_request
from resources.lib.intelligence.commands import Commands
from resources.lib.intelligence.presentation import parse
from resources.lib.intelligence.runtime import create_tools


@pytest.mark.parametrize('phrase', [
    '上传日志', '提交日志', '上传最新日志', '提交最新的日志', '上传最新 Kodi 日志',
    '请你帮我上传CoreELEC日志。', '麻烦你提交一下系统日志', '上传下CE日志',
    '把日志上传一下', '将最新的日志提交吧', '帮我上传日志和配置', '提交日志及配置',
])
def test_clear_latest_log_commands_use_the_existing_upload_tool(phrase):
    request = common_request(phrase)
    assert request['tool'] == 'upload_logs' and request['arguments'] == {}


@pytest.mark.parametrize('phrase', [
    '不要上传日志', '取消上传日志', '先不要提交日志', '上传日志了吗', '怎么上传日志',
    '能不能上传日志', '如果有错误就上传日志', '查看日志', '上传日志然后重启',
    '上传日志到其他网站', '上传昨天的日志', '上传旧日志', '上传Kodi.old.log',
])
def test_questions_negation_old_logs_and_multiple_actions_are_not_executed(phrase):
    assert common_request(phrase) is None


def tools(level='full', confirm=lambda *args: True):
    def forbidden(*args):
        raise AssertionError('Log command must not invoke Kodi RPC or an AI model')
    result = create_tools(None, {'agent_backend': 'off', 'permission_level': level,
                               'approval_strategy': 'request'}, forbidden, confirm=confirm)
    result._juku_available = False
    return result


@pytest.mark.parametrize('level', ['media', 'settings', 'full'])
def test_agent_off_upload_returns_the_actual_short_url_and_reuses_success(monkeypatch, level):
    calls, confirmations, progress = [], [], []
    url = 'https://paste.coreelec.org/2/TEST-NOT-UPLOADED'
    monkeypatch.setattr(log_upload, 'upload', lambda **kwargs:
                        calls.append('upload') or {'uploaded': True, 'url': url})
    target = tools(level, confirm=lambda title, message: confirmations.append(title) or True)
    for phrase in ('上传日志', '提交最新日志'):
        result = Commands(target).process(phrase, progress=progress.append)
        assert result['agent_backend'] == 'local' and result['model_invocations'] == 0
        assert result['tools'] == [{'name': 'upload_logs', 'arguments': {}, 'ok': True}]
        assert parse(result['reply'], target)['message'] == '日志已上传。\n' + url
    assert calls == ['upload'] and confirmations == ['上传日志']
    assert progress and all('上传' in message for message in progress)


@pytest.mark.parametrize('level', ['media', 'settings', 'full'])
def test_local_command_preserves_confirmation_at_every_level(monkeypatch, level):
    monkeypatch.setattr(log_upload, 'upload', lambda **kwargs: pytest.fail('Unexpected upload'))
    target = tools(level, confirm=lambda *args: False)
    result = Commands(target).process('上传日志')
    assert result['model_invocations'] == 0 and not result['tools'][-1]['ok']
    message = parse(result['reply'], target)['message']
    assert '已上传' not in message
    assert '用户取消' in message and '未提供该工具' not in message


def test_failed_upload_remains_a_local_error_without_a_fake_url(monkeypatch):
    def fail(**kwargs):
        raise ValueError('日志服务没有返回有效短网址。')
    monkeypatch.setattr(log_upload, 'upload', fail)
    target = tools()
    result = Commands(target).process('提交日志')
    message = parse(result['reply'], target)['message']
    assert result['model_invocations'] == 0 and not result['tools'][-1]['ok']
    assert '没有返回有效短网址' in message and 'http' not in message


def test_cancelled_local_upload_does_not_start_the_uploader(monkeypatch):
    monkeypatch.setattr(log_upload, 'upload', lambda **kwargs: pytest.fail('Unexpected upload'))
    with pytest.raises(InterruptedError):
        Commands(tools()).process('上传日志', cancelled=lambda: True)
