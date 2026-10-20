import os
from pathlib import Path
import time

import pytest

from resources.lib import log_upload
from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.permissions import Permissions


def command(tmp_path, monkeypatch, source):
    script = tmp_path / 'pastekodi'
    script.write_text('#!/bin/sh\n' + source)
    script.chmod(0o700)
    monkeypatch.setattr(log_upload, 'COMMAND', str(script))
    return script


@pytest.mark.parametrize('url', ['https://paste.coreelec.org/abcd12', 'https://paste.coreelec.org/2/rkwl'])
def test_latest_log_command_returns_official_url_without_raw_output(tmp_path, monkeypatch, url):
    command(tmp_path, monkeypatch, 'test "$#" = 0 || exit 9\necho private-debug-text\necho ' + url + '\n')
    result = log_upload.upload()
    assert result['uploaded'] and result['url'] == url
    assert 'private-debug-text' not in str(result)


@pytest.mark.parametrize('output,exit_code', [
    ('private-log-text', 0),
    ('https://paste.coreelec.org.evil.example/abcd', 0),
    ('https://paste.coreelec.org/abcd?secret=private-log-text', 0),
    ('https://paste.coreelec.org/2/rkwl?secret=private-log-text', 0),
    ('https://paste.coreelec.org/2/../rkwl', 0),
    ('https://paste.coreelec.org/2//rkwl', 0),
    ('https://paste.coreelec.org/abcd', 1),
])
def test_failure_or_untrusted_response_does_not_report_success_or_echo_logs(tmp_path, monkeypatch, output, exit_code):
    command(tmp_path, monkeypatch, 'echo ' + repr(output) + '\nexit ' + str(exit_code) + '\n')
    with pytest.raises(ValueError) as error:
        log_upload.upload()
    assert 'private-log-text' not in str(error.value)


def test_missing_command_and_cancel_before_start_do_not_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(log_upload, 'COMMAND', str(tmp_path/'missing'))
    with pytest.raises(ValueError, match='pastekodi'): log_upload.upload()
    with pytest.raises(InterruptedError): log_upload.upload(cancelled=lambda: True)


@pytest.mark.parametrize('close_stdout', [False, True])
def test_cancellation_terminates_uploader_pipeline_even_after_stdout_closes(tmp_path, monkeypatch, close_stdout):
    pidfile = tmp_path/'child.pid'
    command(tmp_path, monkeypatch, ('exec 1>&-\n' if close_stdout else '') +
            'sleep 30 &\necho $! > ' + str(pidfile) + '\nwait\n')
    began = time.monotonic()
    with pytest.raises(InterruptedError):
        log_upload.upload(cancelled=lambda: pidfile.exists())
    assert time.monotonic() - began < 2
    pid = int(pidfile.read_text())
    for _ in range(20):
        path = Path('/proc')/str(pid)/'stat'
        if not path.exists() or path.read_text().split()[2] == 'Z': break
        time.sleep(.05)
    else: pytest.fail('Uploader child remained running')


def test_timeout_is_bounded(tmp_path, monkeypatch):
    command(tmp_path, monkeypatch, 'sleep 30\n')
    began = time.monotonic()
    with pytest.raises(ValueError, match='超时'): log_upload.upload(timeout=.15)
    assert time.monotonic() - began < 2


def test_upload_respects_permissions_approval_and_success_is_not_uploaded_twice(monkeypatch):
    calls = []
    monkeypatch.setattr(log_upload, 'upload', lambda **kw: calls.append('upload') or
                        {'uploaded': True, 'url': 'https://paste.coreelec.org/test'})
    for level in ('media', 'settings'):
        tools = KodiTools(lambda *a: None, permissions=Permissions(level, 'full_access'), confirm=lambda *a: True)
        assert not tools.execute('upload_logs', {})['ok']
    tools = KodiTools(lambda *a: None, permissions=Permissions('full'), confirm=lambda *a: False)
    assert not tools.execute('upload_logs', {})['ok'] and not calls
    tools = KodiTools(lambda *a: None, permissions=Permissions('full'), confirm=lambda *a: True)
    assert not tools.execute('upload_logs', {'command': 'other-command'})['ok']
    first = tools.execute('upload_logs', {})
    assert first['ok'] and tools.acted
    assert tools.execute('upload_logs', {}) == first and calls == ['upload']
    disabled = KodiTools(lambda *a: None, system_enabled=False, confirm=lambda *a: True)
    assert not disabled.execute('upload_logs', {})['ok']
