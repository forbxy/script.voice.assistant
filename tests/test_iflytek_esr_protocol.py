import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('iflytek_esr_protocol',
    Path(__file__).parents[1]/'dev/iflytek_esr_protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


def item(key, text, kind='plain'):
    return {'key':key,'sub_key':kind,'value':json.dumps({'ws':[{'w':text}]})}


def test_revisions_replace_a_segment_and_punctuation_appends_once():
    result = protocol.Transcript()
    result.update({'elems':[item(1,'广州')]})
    result.update({'elems':[item(1,'广州市房地产中介协会分析')]})
    result.update({'elems':[item(2,'。'),item(2,'。','readable')]})
    result.update({'elems':[item(2,'。','readable')]})
    assert result.text == '广州市房地产中介协会分析。'


def test_plain_is_preferred_regardless_of_subkey_order():
    result = protocol.Transcript()
    result.update({'elems':[item(1,'9点','readable'),item(1,'九点')]})
    assert result.text == '九点'


def test_readable_only_events_and_independent_sessions():
    first, second = protocol.Transcript(), protocol.Transcript()
    first.update({'elems':[item(1,'奥兰多','readable')]})
    assert first.text == '奥兰多'
    assert second.text == ''


def test_engine_errors_and_malformed_text_are_reported():
    result = protocol.Transcript()
    with pytest.raises(RuntimeError,match='10004'):
        result.update({'error':10004})
    with pytest.raises(ValueError):
        result.update({'elems':[{'key':1,'sub_key':'plain','value':'{"ws":'}]})


def test_native_adjacent_objects_and_sentence_markers():
    value = '<s>'+json.dumps({'ws':[{'w':'<s>甚至出现交易'}]})+json.dumps({'ws':{'w':'几乎停滞的情况。'}})
    assert protocol.result_text(value) == '甚至出现交易几乎停滞的情况。'


def test_native_plain_string_fallback():
    assert protocol.result_text('奥兰多') == '奥兰多'


def test_vad_commits_append_even_when_the_native_key_is_reused():
    result = protocol.Transcript()
    result.update({'status': 1, 'elems': [item(1, '你好，这是遥控器')]})
    result.update({'status': 1, 'elems': [item(1, '，语音测试')]})
    result.update({'status': 2, 'elems': [item(2, '。')]})
    assert result.text == '你好，这是遥控器，语音测试。'


def test_final_progressive_snapshot_keeps_preceding_vad_commits():
    result = protocol.Transcript()
    result.update({'status': 1, 'elems': [item(1, '第一段')]})
    result.update({'status': 0, 'elems': [{'key': 0, 'sub_key': 'pgs', 'value': '第二{useCloud:0}'}]})
    assert result.text == '第一段第二'
    result.update({'status': 2, 'elems': [item(1, '第二段。'),
        {'key': 0, 'sub_key': 'pgs', 'value': '第二段{useCloud:0}'}]})
    assert result.text == '第一段第二段。'


def test_progressive_revisions_are_snapshots_and_controls_are_not_text():
    result = protocol.Transcript()
    result.update({'status':0,'elems':[{'key':0,'sub_key':'pgs','value':'奥兰多{useCloud:0}{nVadBegin:-1}'}]})
    assert result.text == '奥兰多'
    result.update({'status':0,'elems':[{'key':0,'sub_key':'pgs','value':'奥拉多{useCloud:1}'}]})
    assert result.text == '奥拉多'
    result.update({'status':2,'elems':[{'key':2,'sub_key':'pgs','value':'奥拉多{useCloud:1}'},item(2,'奥拉多。')]})
    assert result.text == '奥拉多。'


def test_framing_handles_utf8_multiline_and_fragmented_native_stdout():
    raw = json.dumps({'elems':[item(1,'惊天营救')]},ensure_ascii=False,indent=2).encode()
    frame = b'native initialization log\nIFLYTEK_EVENT result 0 '+str(len(raw)).encode()+b'\n'+raw+b'\n'
    chunks = iter([frame[:9],frame[9:47],frame[47:83],frame[83:]])
    worker = protocol.Worker.__new__(protocol.Worker)
    worker.buffer = bytearray()
    worker.native_messages = []
    worker._more = lambda deadline: worker.buffer.extend(next(chunks))
    response = worker.read()
    assert response['event'] == 'result'
    assert response['return_code'] == 0
    assert response['payload'] == json.loads(raw)
    assert worker.native_messages == ['native initialization log']


def test_corrupt_frame_terminator_is_rejected():
    worker = protocol.Worker.__new__(protocol.Worker)
    worker.buffer = bytearray(b'IFLYTEK_EVENT audio 0 0\nx')
    worker.native_messages = []
    with pytest.raises(ValueError,match='terminator'):
        worker.read()
