import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from resources.lib.intelligence.errors import AgentError
from resources.lib.agent_tools import KodiTools
from resources.lib.codex_agent import CodexAgent, Connection, launch_command, command_preview
from resources.lib.intelligence.permissions import Permissions


class Protocol:
    def __init__(self, events):
        self.events = iter(events)
        self.requests, self.sent = [], []
        self.closed = False

    def request(self, method, params):
        self.requests.append((method, params))
        return {'initialize': {'userAgent': 'official-codex'}, 'thread/start': {'thread': {'id': 'thread'}},
                'turn/start': {'turn': {'id': 'turn'}}}[method]

    def send(self, message):
        self.sent.append(message)

    def event(self):
        return next(self.events)

    def close(self, *ids):
        self.closed = True


def dynamic(arguments, call_id='call'):
    return {'id': 11, 'method': 'item/tool/call', 'params': {'threadId': 'thread', 'turnId': 'turn',
            'callId': call_id, 'tool': 'set_volume', 'arguments': arguments}}


def finished(status='completed'):
    return {'method': 'turn/completed', 'params': {'turn': {'status': status,
            'items': [{'type': 'agentMessage', 'id': 'answer', 'phase': 'final_answer', 'text': '已完成。'}]}}}


def make_agent(tmp_path, events, rpc=lambda *args: 20):
    binary = tmp_path / 'binary'
    binary.touch(mode=0o755)
    binary.with_name('codex-code-mode-host').touch(mode=0o755)
    protocol = Protocol(events)
    agent = CodexAgent(tmp_path, tmp_path, 'test-private-key', KodiTools(rpc),
                       binary=binary, connection_factory=lambda *args: protocol,
                       permissions=Permissions('full', 'full_access'))
    return agent, protocol


def test_real_kodi_tool_dispatch_deduplicates_id_and_uses_full_permissions(tmp_path):
    calls = []
    event = dynamic({'volume': 20})
    agent, protocol = make_agent(tmp_path, [event, event, finished()], lambda *args: calls.append(args) or 20)
    result = agent.run('音量20', [{'role': 'user', 'content': '近期内容'}])
    assert calls == [('Application.SetVolume', {'volume': 20})]
    assert result['permissions'] == 'full' and result['reply'] == '已完成。'
    start = protocol.requests[1][1]
    assert start['approvalPolicy'] == 'never' and start['sandbox'] == 'danger-full-access'
    assert all(tool['type'] == 'function' for tool in start['dynamicTools'])
    assert len([item for item in protocol.sent if item.get('id') == 11]) == 2
    assert '近期内容' in protocol.requests[2][1]['input'][0]['text']
    assert protocol.closed


def test_conflicting_repeated_tool_or_wrong_thread_cannot_execute(tmp_path):
    calls = []
    agent, protocol = make_agent(tmp_path, [dynamic({'volume': 20}), dynamic({'volume': 30})],
                                 lambda *args: calls.append(args) or 20)
    with pytest.raises(AgentError, match='冲突'):
        agent.run('音量20')
    assert len(calls) == 1 and protocol.closed
    event = dynamic({'volume': 30})
    event['params']['threadId'] = 'another-thread'
    agent, protocol = make_agent(tmp_path, [event], lambda *args: calls.append(args))
    with pytest.raises(AgentError, match='不匹配'):
        agent.run('音量30')
    assert len(calls) == 1 and protocol.closed


@pytest.mark.parametrize('status,exception', [('failed', AgentError), ('interrupted', InterruptedError)])
def test_incomplete_turn_does_not_report_a_successful_answer(tmp_path, status, exception):
    agent, protocol = make_agent(tmp_path, [finished(status)])
    with pytest.raises(exception):
        agent.run('你好')
    assert protocol.closed


def test_uploaded_log_url_is_shown_even_if_subsequent_model_reply_fails(tmp_path, monkeypatch):
    from resources.lib import log_upload
    url = 'https://paste.coreelec.org/TEST-NOT-UPLOADED'
    monkeypatch.setattr(log_upload, 'upload', lambda **kwargs: {'uploaded': True, 'url': url})
    event = dynamic({})
    event['params']['tool'] = 'upload_logs'
    agent, protocol = make_agent(tmp_path, [event, event, finished('failed')])
    agent.tools.confirm = lambda *args: True
    progress = []
    with pytest.raises(AgentError):
        agent.run('上传最新日志', progress=progress.append)
    assert progress.count('日志已上传：' + url) == 1
    assert protocol.closed


@pytest.mark.parametrize('approved', [False, True])
def test_tier_one_dynamic_upload_is_available_but_still_requires_confirmation(tmp_path, monkeypatch, approved):
    from resources.lib import log_upload
    uploads, confirmations = [], []
    monkeypatch.setattr(log_upload, 'upload', lambda **kwargs: uploads.append('upload') or
                        {'uploaded': True, 'url': 'https://paste.coreelec.org/TEST-NOT-UPLOADED'})
    event = dynamic({})
    event['params']['tool'] = 'upload_logs'
    agent, protocol = make_agent(tmp_path, [event, finished()])
    agent.root = Path(__file__).resolve().parents[1]
    agent.permissions = agent.tools.permissions = Permissions('media', 'request')
    agent.tools.confirm = lambda title, message: confirmations.append(title) or approved
    result = agent.run('上传日志')
    names = {row['name'] for row in protocol.requests[1][1]['dynamicTools']}
    assert {'upload_logs', 'system_action'} <= names
    assert result['tools'][-1]['ok'] is approved
    assert confirmations == ['上传日志'] and uploads == (['upload'] if approved else [])
    assert protocol.requests[1][1]['sandbox'] == 'read-only'


def test_native_execution_is_recorded_without_logging_commands_or_secrets(tmp_path):
    event = {'method': 'item/completed', 'params': {'item': {'type': 'commandExecution',
             'command': 'echo test-private-key', 'exitCode': 0}}}
    agent, protocol = make_agent(tmp_path, [event, finished()])
    result = agent.run('执行命令')
    assert result['tools'][0]['name'] == 'codex_command' and result['tools'][0]['ok']
    assert 'test-private-key' not in json.dumps(result)
    command = launch_command('/binary', 'https://api.deepseek.com', 'deepseek-flash', 30)
    assert 'features.shell_tool=true' in command
    assert 'features.unified_exec=true' in command
    assert 'wire_api="responses"' in ' '.join(command)
    assert 'test-private-key' not in ' '.join(command)


@pytest.mark.parametrize('provider', ['deepseek', 'chatgpt'])
@pytest.mark.parametrize('level', ['full', 'settings', 'media'])
@pytest.mark.parametrize('api_code_mode', [False, True])
def test_api_code_mode_switch_preserves_subscription_and_permission_ceiling(provider, level, api_code_mode):
    from resources.lib.intelligence.permissions import Permissions
    command = launch_command('/binary', 'https://api.deepseek.com', 'deepseek-flash', 30,
                             provider=provider, permissions=Permissions(level, 'full_access'),
                             catalog='/restricted-models.json', api_code_mode=api_code_mode)
    native = level == 'full'
    assert 'features.code_mode=' + str(native and (provider == 'chatgpt' or api_code_mode)).lower() in command
    assert 'features.unified_exec=' + str(native).lower() in command
    assert 'features.shell_tool=' + str(native).lower() in command
    assert 'features.apply_patch_freeform=' + str(native).lower() in command
    assert 'features.code_mode_only=false' in command


def test_cancellation_terminates_appserver_and_its_command_process_group(tmp_path):
    child_pid = tmp_path / 'child.pid'
    script = ('import pathlib,subprocess,time\n'
              'p=subprocess.Popen(["sleep","30"])\n'
              'pathlib.Path(' + repr(str(child_pid)) + ').write_text(str(p.pid))\n'
              'time.sleep(30)\n')
    began = time.monotonic()
    connection = Connection([sys.executable, '-c', script], 'test-private-key', tmp_path,
                             lambda: time.monotonic() - began > 0.3, 5)
    try:
        with pytest.raises(InterruptedError):
            connection.read()
    finally:
        connection.close()
    assert connection.process.poll() is not None and time.monotonic() - began < 3
    pid = int(child_pid.read_text())
    # A terminated child may briefly remain a zombie until init reaps it.
    status = Path('/proc') / str(pid) / 'stat'
    try:
        assert status.read_text().split()[2] == 'Z'
    except (FileNotFoundError, ProcessLookupError):
        pass  # The terminated process can be reaped between /proc reads.


def test_work_process_shows_command_at_start_and_finish_without_credentials(tmp_path):
    events = [
        {'method': 'item/completed', 'params': {'item': {'type': 'reasoning', 'summary': ['先核对设备状态。'],
            'content': ['PRIVATE REASONING']}}},
        {'method': 'turn/plan/updated', 'params': {'plan': [{'step': '查询系统', 'status': 'inProgress'}]}},
        {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'phase': 'commentary',
            'text': '我先检查系统。test-private-key'}}},
        {'method': 'item/started', 'params': {'item': {'type': 'commandExecution', 'command': 'uname -s; echo test-private-key'}}},
        {'method': 'item/completed', 'params': {'item': {'type': 'commandExecution', 'exitCode': 1}}},
        dynamic({'volume': 20}), finished()]
    agent, protocol = make_agent(tmp_path, events)
    messages = []
    result = agent.run('查询', progress=messages.append)
    assert '进行中：查询系统' in '\n'.join(messages)
    assert '我先检查系统。[private]' in messages
    assert '正在执行命令：\nuname -s; echo [private]' in messages
    assert '命令未成功（退出码 1）：\nuname -s; echo [private]' in messages
    assert any('已完成' in item for item in messages)
    assert 'test-private-key' not in '\n'.join(messages)
    assert '先核对设备状态。' in messages and 'PRIVATE REASONING' not in '\n'.join(messages)
    assert result['reply'] == '已完成。'


def test_command_preview_shows_short_commands_and_truncates_only_display():
    assert command_preview('uname -a\n df -h') == 'uname -a df -h'
    result = command_preview('find /storage ' + 'x' * 300)
    assert result.startswith('find /storage ') and result.endswith('…') and len(result) == 160


@pytest.mark.parametrize('command,secret', [
    ('API_KEY="secret value" curl example.test', 'secret value'),
    ('curl --api-key abcdef', 'abcdef'),
    ('env OPENAI_API_KEY=abcdef python test.py', 'abcdef'),
    ("curl -H 'Authorization: Bearer abcdef' example.test", 'abcdef'),
    ('curl https://someone:abcdef@example.test', 'abcdef'),
    ('tool --password abcdef', 'abcdef'),
    ("tool '{\"access_token\":\"abcdef\"}'", 'abcdef'),
    ('curl -u someone:abcdef example.test', 'abcdef'),
    ('sshpass -p abcdef ssh example.test', 'abcdef'),
])
def test_command_preview_removes_credentials_before_display_or_truncation(command, secret):
    result = command_preview(command)
    assert secret not in result and '[private]' in result
    assert 'KNOWN' not in command_preview('echo ' + 'KNOWN' * 60, key='KNOWN' * 60)


def test_file_activity_names_operations_paths_and_move_without_file_contents(tmp_path):
    changes = [
        {'path': '/storage/new.py', 'kind': {'type': 'add'}, 'diff': 'PRIVATE FILE CONTENT'},
        {'path': '/storage/settings.xml', 'kind': {'type': 'update'}, 'diff': 'PRIVATE FILE CONTENT'},
        {'path': '/storage/old.py', 'kind': {'type': 'delete'}, 'diff': 'PRIVATE FILE CONTENT'},
        {'path': '/storage/before.py', 'kind': {'type': 'update', 'move_path': '/storage/after.py'}, 'diff': ''}]
    events = [
        {'method': 'item/started', 'params': {'item': {'type': 'fileChange', 'id': 'files', 'changes': changes}}},
        {'method': 'item/completed', 'params': {'item': {'type': 'fileChange', 'id': 'files', 'status': 'failed'}}},
        finished()]
    agent, protocol = make_agent(tmp_path, events)
    messages = []
    agent.run('调整文件', progress=messages.append)
    start = next(m for m in messages if m.startswith('正在处理文件'))
    end = next(m for m in messages if m.startswith('文件操作失败'))
    for text in ('创建文件：/storage/new.py', '修改文件：/storage/settings.xml',
                 '删除文件：/storage/old.py', '移动并修改文件：/storage/before.py → /storage/after.py'):
        assert text in start and text in end
    assert 'PRIVATE FILE CONTENT' not in '\n'.join(messages)


def test_command_file_paths_survive_long_command_and_completion_without_repeating(tmp_path):
    actions = [{'type': 'read', 'path': 'config/settings.xml'},
               {'type': 'listFiles', 'path': 'resources'}, {'type': 'search', 'path': None}]
    command = 'python -c ' + 'x' * 300
    events = [
        {'method': 'item/started', 'params': {'item': {'type': 'commandExecution', 'id': 'read', 'command': command,
            'cwd': '/storage/addon', 'commandActions': actions}}},
        {'method': 'item/completed', 'params': {'item': {'type': 'commandExecution', 'id': 'read',
            'cwd': '/storage/addon', 'commandActions': actions, 'exitCode': 0}}}, finished()]
    agent, protocol = make_agent(tmp_path, events)
    messages = []
    agent.run('读取设置', progress=messages.append)
    for prefix in ('正在执行命令', '命令已完成'):
        text = next(m for m in messages if m.startswith(prefix))
        assert len(text.splitlines()[1]) == 160 and text.splitlines()[1].endswith('…')
        assert text.count('读取文件：/storage/addon/config/settings.xml') == 1
        assert '查看目录：/storage/addon/resources' in text
        assert '搜索文件：/storage/addon' in text


def test_long_file_path_keeps_filename():
    from resources.lib.codex_agent import path_preview
    result = path_preview('/storage/' + 'long-directory/' * 20 + 'settings.xml')
    assert len(result) == 120 and '…' in result and result.endswith('/settings.xml')


def test_steering_uses_active_turn_and_acknowledges_each_input_once(tmp_path):
    from resources.lib.intelligence.steering import Steering
    inbox = Steering()
    first = inbox.submit('只检查，不修改')
    second = inbox.submit('用中文回复')
    agent, protocol = make_agent(tmp_path, [
        {'id': first, 'result': {'turnId': 'turn'}},
        {'id': second, 'result': {'turnId': 'turn'}}, finished()])
    messages = []
    agent.run('检查系统', progress=messages.append, steering=inbox)
    sent = [m for m in protocol.sent if m.get('method') == 'turn/steer']
    assert [m['params']['input'][0]['text'] for m in sent] == ['只检查，不修改', '用中文回复']
    assert all(m['params']['expectedTurnId'] == 'turn' and m['params']['threadId'] == 'thread' for m in sent)
    assert inbox.followup() is None and inbox.take() is None
    assert messages.count('补充要求已发送给当前任务。') == 2


def test_completion_racing_steer_rejection_keeps_input_for_next_turn(tmp_path):
    from resources.lib.intelligence.steering import Steering
    inbox = Steering()
    identifier = inbox.submit('补充一句')
    agent, protocol = make_agent(tmp_path, [finished(), {'id': identifier, 'error': {'message': 'No active turn'}}])
    result = agent.run('原始任务', steering=inbox)
    assert result['reply'] == '已完成。'
    assert inbox.followup() == '补充一句' and inbox.followup() is None
    assert len([m for m in protocol.sent if m.get('method') == 'turn/steer']) == 1


def test_ambiguous_steer_ack_is_not_replayed(tmp_path):
    from resources.lib.intelligence.steering import Steering
    inbox = Steering()
    identifier = inbox.submit('需要修改')
    agent, protocol = make_agent(tmp_path, [{'id': identifier, 'result': {'turnId': 'wrong'}}])
    with pytest.raises(AgentError, match='接收状态不明确'):
        agent.run('原始任务', steering=inbox)
    assert inbox.followup() is None


def test_silent_appserver_still_receives_input_while_read_waits(tmp_path):
    script = 'import sys,json; line=sys.stdin.readline(); print(json.dumps({"echo":json.loads(line)}),flush=True)'
    connection = Connection([sys.executable, '-c', script], '', tmp_path, lambda: False, 5)
    calls = []
    def pump():
        if not calls:
            calls.append(True)
            connection.send({'method': 'turn/steer', 'params': {'input': 'new'}})
    connection.pump = pump
    try:
        message = connection.event()
        assert message['echo']['method'] == 'turn/steer'
    finally:
        connection.pump = None
        connection.close()


def test_native_questions_return_actual_answers_with_ids(tmp_path):
    params={'threadId':'thread','turnId':'turn','isBlocking':True,'questions':[
        {'id':'color','header':'颜色','question':'选颜色？','options':[{'label':'蓝色','description':'测试'}]},
        {'id':'size','header':'大小','question':'多大？','options':None}]}
    agent, protocol=make_agent(tmp_path,[{'id':55,'method':'item/tool/requestUserInput','params':params},finished()])
    def ask(request):
        request.answer('color','蓝色');request.answer('size','大一些')
    agent.request_user_input=ask
    agent.run('测试')
    responses=[m for m in protocol.sent if m.get('id')==55]
    assert responses==[{'id':55,'result':{'answers':{'color':{'answers':['蓝色']},'size':{'answers':['大一些']}}}}]


def test_question_cannot_answer_for_a_different_turn(tmp_path):
    agent,protocol=make_agent(tmp_path,[{'id':55,'method':'item/tool/requestUserInput','params':{
        'threadId':'thread','turnId':'wrong','questions':[]}}])
    agent.request_user_input=lambda request:pytest.fail('Wrong turn reached UI')
    with pytest.raises(AgentError,match='提问会话不匹配'):
        agent.run('测试')


def test_blocking_question_wait_pauses_transport_deadline_and_can_cancel(tmp_path):
    began=time.monotonic()
    connection=Connection([sys.executable,'-c','import time; time.sleep(0.6); print("{}",flush=True)'],
                          '',tmp_path,lambda:False,.2)
    connection.waiting_user=lambda:time.monotonic()-began<.55
    try:
        assert connection.read()=={}
    finally:
        connection.close()
    connection=Connection([sys.executable,'-c','import time; time.sleep(5)'],'',tmp_path,lambda:True,.2)
    connection.waiting_user=lambda:True
    try:
        with pytest.raises(InterruptedError):connection.read()
    finally:connection.close()
