import json
import pytest
from resources.lib.speech.text import SpeechStream, encode, chunks, schema, commentary_allowed, phoneme_map
from resources.lib.intelligence.presentation import SCHEMA
from resources.lib.speech.client import Speech
from resources.lib.settings import validate, FIELDS


def test_stream_only_complete_rows_and_no_replay():
    rows = [{'text':'第一句话。','pronunciations':[]}, {'text':'第二句话。','pronunciations':[]}]
    value = json.dumps({'speech':rows,'message':'最终回复','interaction':'continue','choices':[]},ensure_ascii=False)
    received = []
    stream = SpeechStream(received.append)
    split = value.index('第二')
    for character in value[:split]: stream.feed(character)
    assert received == rows[:1]
    for character in value[split:]: stream.feed(character)
    stream.finish(value)
    assert received == rows
    assert schema(SCHEMA)['required'][0] == 'speech'
    assert 'speech' not in SCHEMA['properties']


def test_stream_ignores_message_quoting_speech():
    received = []
    stream = SpeechStream(received.append)
    stream.feed(json.dumps({'message':'示例 "speech": [{"text":"不朗读"}]'}))
    assert received == []


def test_context_specific_pronunciation_and_invalid_fallback():
    mapping = {'hang2':'\ue001','xing2':'\ue002'}
    row = {'text':'银行行长在行走。','pronunciations':[
        {'start':1,'text':'行','pinyin':['hang2']},
        {'start':5,'text':'行','pinyin':['xing2']},
        {'start':3,'text':'错','pinyin':['hang2']} ]}
    assert encode(row,mapping,True) == '银\ue001行长在\ue002走。'
    assert encode(row,mapping,False) == row['text']
    assert list(chunks('\ue001\ue002。')) == ['\ue001\ue002。']
    assert encode({'text':'正常\ue001文字'},mapping,True)=='正常文字'


def test_spoken_text_and_work_scope():
    assert list(chunks('**你好。**\n```bash\nrm -rf /example\n```')) == ['你好。']
    assert commentary_allowed('我正在查看片库，找一部适合你的科幻片。')
    for value in ['执行命令：ls', '编辑文件 /storage/settings.xml', '调用脚本 test.py', '```shell\necho hello\n```']:
        assert not commentary_allowed(value)
    assert FIELDS['tts_scope'].default == 'final'
    assert validate('tts_pause','0.5') == '0.5'
    assert FIELDS['tts_lead_in'].default == '1.0'
    assert validate('tts_lead_in','0') == '0'
    assert validate('tts_lead_in','2.0') == '2.0'
    assert FIELDS['tts_volume'].default == 50
    assert validate('tts_volume', 0) == 0
    assert validate('tts_volume', '100') == 100
    for value in (-1, 101, 50.5, True):
        with pytest.raises(ValueError): validate('tts_volume', value)


def test_english_words_survive_chunking_and_neutral_tone_uses_native_token():
    parts = list(chunks('现在打开 Kodi，Hello welcome to the voice assistant. Have a nice day!', maximum=20))
    assert any('assistant' in part for part in parts)
    assert any('Kodi' in part for part in parts)
    assert ' '.join(parts).count('Hello') == 1
    assert list(chunks('Supercalifragilisticexpialidocious', maximum=10)) == ['Supercalifragilisticexpialidocious']
    mapping = phoneme_map('le5 1\nhang2 2\nxing2 3\n')
    row = {'text':'好了，打开 Kodi。','pronunciations':[{'start':1,'text':'了','pinyin':['le']}]}
    assert encode(row, mapping, True) == '好' + mapping['le5'] + '，打开 Kodi。'


def test_disabled_speech_does_not_connect(tmp_path, monkeypatch):
    def forbidden(*args):raise AssertionError('Disabled speech connected')
    monkeypatch.setattr('socket.socket',forbidden)
    speaker = Speech(tmp_path,{'tts_enabled':'off'})
    speaker.say({'text':'不播出'});speaker.finish('不播出');speaker.stop()
    assert not speaker.available


def test_service_stream_cancel_and_media_handoff(tmp_path, monkeypatch):
    import time
    import resources.lib.speech.service as module
    class Engine:
        mapping = {}
        process = None
        rate = 16000
        def __init__(self,*args):pass
        def start(self):pass
        def synthesize(self,text,pause):return b'\x10\x00' * 2205
        def close(self):pass
    class Output:
        device = 'test-device'
        stream = None
        result = None
        def start(self,stream,lead_in=1.0,rate=22050,relative_volume=50):
            self.stream=stream;self.lead_in=lead_in;self.rate=rate;self.relative_volume=relative_volume
        def poll(self,relative_volume=None):
            if relative_volume is not None:self.relative_volume=relative_volume
            return self.result
        def stop(self):self.stream=None
    monkeypatch.setattr(module,'Engine',Engine)
    monkeypatch.setattr(module,'installed',lambda *args:True)
    output=Output();media=[]
    service=module.Service(tmp_path,tmp_path,output,lambda:bool(media))
    values={'tts_enabled':'on','tts_ai_pronunciation':'off','tts_pause':'1.0','tts_lead_in':'2.0'}
    try:
        service.tick(values)
        client=Speech(tmp_path,values)
        client.say({'text':'你好。','pronunciations':[]});client.finish()
        deadline=time.monotonic()+2
        while not output.stream and time.monotonic()<deadline:
            service.tick(values);time.sleep(.01)
        assert output.stream
        assert output.lead_in == 2.0
        assert output.rate == 16000
        assert output.relative_volume == 50
        assert output.stream.read(0)==b'\x10\x00'*2205
        playing = output.stream
        service.tick(dict(values,tts_volume=35))
        assert output.relative_volume == 35 and output.stream is playing
        output.result='ended';service.tick(values)
        assert service.current is None
        output.result=None
        # A stale session cannot requeue speech after cancellation.
        client.send('say',row={'text':'旧回复。'})
        service.tick(values)
        assert not service.jobs
        client=Speech(tmp_path,values);client.say({'text':'新回复。'})
        service.tick(values,recording=True)
        assert service.current is None
        client=Speech(tmp_path,values);service.tick(values)
        assert service.current
        media.append('movie.mkv');service.tick(values)
        assert service.current is None and output.stream is None
        client=Speech(tmp_path,values);client.say({'text':'不要打断电影。'})
        service.tick(values)
        assert service.current is None and media==['movie.mkv']
        service.tick(dict(values,tts_enabled='off'))
        assert not service.enabled
    finally:service.close()


def test_agent_streams_final_only_once_and_excludes_tools(tmp_path):
    from test_codex_agent import make_agent
    rows=[{'text':'银行行长来了。','pronunciations':[{'start':1,'text':'行','pinyin':['hang2']}]}]
    final=json.dumps({'speech':rows,'message':'银行行长来了。','interaction':'continue','choices':[]},ensure_ascii=False)
    events=[{'method':'item/started','params':{'item':{'type':'agentMessage','id':'reply','phase':'final_answer'}}},
        {'method':'item/agentMessage/delta','params':{'itemId':'reply','delta':final}},
        {'method':'item/completed','params':{'item':{'type':'commandExecution','command':'echo secret','exitCode':0}}},
        {'method':'item/completed','params':{'item':{'type':'agentMessage','id':'reply','phase':'final_answer','text':final}}},
        {'method':'turn/completed','params':{'turn':{'status':'completed','items':[]}}}]
    agent,protocol=make_agent(tmp_path,events);agent.structured=True
    heard=[]
    result=agent.run('测试',speech=lambda row,kind:heard.append((row,kind)),speech_options={'tts_ai_pronunciation':'on'})
    assert heard==[(rows[0],'final')]
    assert result['reply']==final
    assert protocol.requests[-1][1]['outputSchema']['required'][0]=='speech'


def test_commentary_is_cleaned_for_ui_and_only_natural_text_is_spoken(tmp_path):
    from test_codex_agent import make_agent,finished
    note=json.dumps({'message':'我正在帮你查找。','speech':[{'text':'我正在帮你查找。','pronunciations':[]}]},ensure_ascii=False)
    agent,_=make_agent(tmp_path,[{'method':'item/completed','params':{'item':{'type':'agentMessage','id':'note','phase':'commentary','text':note}}},finished()])
    heard=[];shown=[]
    agent.run('测试',progress=shown.append,speech=lambda row,kind:heard.append((row,kind)),speech_options={'tts_scope':'all'})
    assert heard[0][1]=='commentary'
    assert shown[-1]=='我正在帮你查找。'
