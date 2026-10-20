import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
from resources.lib.ui.transcript import message_rows, message_page


def test_full_text_wrapping_preserves_chinese_english_and_blank_lines():
    text = '完整中文测试 English e\u0301 🎬 ' * 200 + '\n\n最后一句'
    rows = message_rows(text)
    assert ''.join(rows).replace('\n', '') == text.replace('\n', '')
    assert all(len(row.split('\n')) <= 2 for row in rows)
    assert rows[-1].endswith('最后一句')


@pytest.fixture
def conversation_module(monkeypatch):
    class Control:
        def __init__(self):self.items=[];self.selected=0
        def addItem(self,item):self.items.append(item)
        def addItems(self,items):self.items.extend(items)
        def size(self):return len(self.items)
        def reset(self):self.items=[]
        def selectItem(self,n):self.selected=n
        def getSelectedPosition(self):return self.selected
        def setLabel(self,*a):pass
        def setText(self,*a):pass
        def setEnabled(self,*a):pass
        def scroll(self,*a):pass
    class Window:
        def __init__(self,*a,**k):self.controls={i:Control() for i in (301,304,305,400,401,500)}
        def getControl(self,i):
            if getattr(self, 'missing', False):raise RuntimeError('Non-Existent Control')
            return self.controls[i]
        def show(self):
            self.visible=True
            self.onInit()
        def close(self):self.visible=False
        def setFocusId(self,i):self.focus=i
        def setProperty(self,k,v):pass
    class Item:
        def __init__(self,label):self.label=label;self.properties={}
        def setProperty(self,k,v):self.properties[k]=v
        def setArt(self,art):self.art=art
    monkeypatch.setitem(sys.modules,'xbmcgui',SimpleNamespace(WindowXMLDialog=Window,ListItem=Item,getCurrentWindowDialogId=lambda: 13000))
    monkeypatch.setitem(sys.modules,'xbmc',SimpleNamespace(getCondVisibility=lambda condition: True,getInfoLabel=lambda label: '1'))
    monkeypatch.setitem(sys.modules,'resources.lib.recording_ui',SimpleNamespace(BACK=(9,10,92)))
    path=Path(__file__).resolve().parents[1]/'resources/lib/ui/conversation.py'
    spec=importlib.util.spec_from_file_location('resources.lib.ui.conversation_under_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'current_codes', lambda: {'61588'})
    return module


def test_bound_voice_key_mapped_to_back_supplements_instead_of_closing(conversation_module):
    window = conversation_module.ConversationWindow()
    window.show()
    for busy in (False, True):
        window.busy = busy
        window.onAction(SimpleNamespace(getId=lambda: 92, getButtonCode=lambda: 61588))
        assert not window.cancelled.is_set()
        assert window.events.get_nowait() == ('voice', '')
    window.recording = True
    window.onAction(SimpleNamespace(getId=lambda: 92, getButtonCode=lambda: 61588 | 0x01000000))
    assert not window.cancelled.is_set() and window.events.empty()
    window.onAction(SimpleNamespace(getId=lambda: 92, getButtonCode=lambda: 61467))
    assert window.cancelled.is_set()


def test_juku_choice_uses_verified_media_selection_without_ai(conversation_module):
    window = conversation_module.ConversationWindow()
    window.show()
    item = {'kind': 'juku', 'id': 7, 'title': '疾速追杀', 'label': '疾速追杀', 'value': '播放',
            '_juku_source': 'online', '_juku_id': '20676'}
    window.present({'message': '请选择', 'choices': [item]})
    window.display_rows = [next(index for index, row in enumerate(window.rows) if row is not None)]
    window.onClick(500)
    event, selected = window.events.get_nowait()
    assert event == 'media' and selected['_juku_id'] == '20676'


def test_choices_stay_with_each_assistant_turn_and_old_choices_still_select(conversation_module):
    window=conversation_module.ConversationWindow()
    window.append_message('user','第一句')
    movie={'label':'电影 A','value':'play','title':'电影 A','kind':'movie','id':7}
    window.present({'message':'第一轮推荐','choices':[movie]})
    control=window.getControl(500)
    assert [i.label for i in window.items]==['我','AI','电影 A']
    assert control.selected==window.block_starts[2] and window.focus==500
    window.append_message('user','再推荐一部')
    window.present({'message':'第二轮推荐','choices':[{'label':'电影 B','value':'选 B'}]})
    assert [i.label for i in window.items]==['我','AI','电影 A','我','AI','电影 B']
    control.selectItem(window.block_starts[2]);conversation_module.xbmc.getInfoLabel=lambda label:str(window.block_starts[2]+1);window.onClick(500)
    event, selected = window.events.get_nowait()
    assert event == 'media' and selected['id'] == 7 and selected['kind'] == 'movie'
    control.selectItem(window.block_starts[1]);conversation_module.xbmc.getInfoLabel=lambda label:str(window.block_starts[1]+1);window.onClick(500)
    assert window.events.empty()
    window.present({'message':'没有新推荐','choices':[]})
    assert window.rows[2]['id']==7 and len(window.history)==5


def test_poster_only_choice_populates_both_logical_and_visible_cards(conversation_module):
    window = conversation_module.ConversationWindow()
    window.present({'message': '请选择影片', 'choices': [
        {'label': '伽马射线效应', 'value': '播放', 'title': '伽马射线效应', 'kind': 'movie', 'id': 7,
         'thumbnail': '', 'art': {'poster': 'image://real-poster/'}}]})
    assert window.items[-1].art['thumb'] == 'image://real-poster/'
    lines = [row for row in window.display_items if row.properties.get('role') == 'choice']
    assert lines and all(row.art['thumb'] == 'image://real-poster/' for row in lines)


def test_channel_choice_keeps_verified_id_in_followup(conversation_module):
    window = conversation_module.ConversationWindow()
    window.append_message('user', '我想看新闻')
    window.present({'message': '请选择频道', 'choices': [
        {'label': 'CCTV-13 新闻', 'value': '播放新闻', 'title': 'CCTV-13 新闻', 'kind': 'channel', 'id': 200}]})
    line = window.block_starts[-1]
    conversation_module.xbmc.getInfoLabel = lambda label: str(line + 1)
    window.onClick(500)
    event, selected = window.events.get_nowait()
    assert event == 'media' and selected['kind'] == 'channel' and selected['id'] == 200


@pytest.mark.parametrize('resume,lookup', [(True, False), (False, False), (False, True)])
def test_tvshow_resume_choice_uses_local_media_path_only_for_verified_intent(conversation_module, resume, lookup):
    window = conversation_module.ConversationWindow()
    choice = {'label': '某电视剧', 'value': '继续播放', 'title': '某电视剧', 'kind': 'tvshow', 'id': 3}
    if resume:
        choice['_command_resume'] = True
    if lookup:
        choice['_command_tvshow'] = True
    window.present({'message': '请选择', 'choices': [choice]})
    conversation_module.xbmc.getInfoLabel = lambda label: str(window.block_starts[-1] + 1)
    window.onClick(500)
    event, selected = window.events.get_nowait()
    assert event == ('media' if resume or lookup else 'text')
    if resume or lookup:
        assert selected['id'] == 3


def test_restore_replaces_old_session_rows_and_choices(conversation_module):
    window = conversation_module.ConversationWindow()
    window.show()
    answer = {'message':'旧回答','choices':[{'label':'旧推荐','value':'旧操作'}]}
    window.append_message('user','旧问题')
    window.present(answer)
    window.restore([])
    assert window.history == [] and window.rows == [] and window.choices == []
    assert window.getControl(500).size() == 0
    assert answer['choices']  # Reset must not mutate stored transcript choices.
    rows = [{'user':'新问题','answer':{'message':'新回答','choices':[]}}]
    window.restore(rows)
    window.restore(rows)
    assert window.history == [('user','新问题'),('assistant','新回答')]
    assert len(window.items) == 2 and window.getControl(500).size() == 6


def test_new_reply_reveals_last_body_line_without_leaving_voice_button(conversation_module):
    window = conversation_module.ConversationWindow()
    control = window.getControl(500)
    conversation_module.xbmc.getInfoLabel = lambda label: str(control.selected + 1)
    conversation_module.xbmc.getCondVisibility = lambda condition: condition != 'Control.HasFocus(500)'
    window.show()
    for count in (1, 6, 25):
        window.append_message('user', '问题')
        window.present({'message': '\n'.join('回复第%d行' % n for n in range(count)), 'choices': []})
        assert control.selected == len(window.display_items) - 2
        assert control.items[control.selected].properties['body'] == '回复第%d行' % (count - 1)
        assert window.focus == 400
    # Status polling and rebuilding controls must preserve the visible tail.
    window.status('完成')
    window.onInit()
    assert control.selected == len(window.display_items) - 2
    assert window.focus == 400


def test_skin_reload_restores_history_choices_and_inflight_status(conversation_module):
    window = conversation_module.ConversationWindow()
    window.show()
    window.append_message('user', '旧问题')
    window.present({'message': '旧回答', 'choices': [{'label': '推荐', 'value': '选择推荐'}]})
    window.append_message('user', '当前问题')
    progress = conversation_module.ConversationProgress(window)
    progress.create('正在执行', '当前任务')
    window.activity('重载前的步骤')
    saved = []
    window.activity_sink = saved.append
    window.missing = True
    window.activity('重载时的步骤')
    progress.update(0, '执行继续\n无需重做')
    assert not progress.iscanceled() and window.busy
    window.missing = False
    window.onInit()
    control = window.getControl(500)
    bodies = [i.properties.get('body', '') for i in control.items if i.properties.get('part') == 'body']
    assert bodies == ['旧问题', '旧回答', '当前问题', '重载前的步骤', '重载时的步骤']
    assert saved == ['重载时的步骤']
    assert window.last_status == ('执行继续', '无需重做')
    assert window.busy and not progress.iscanceled()
    progress.close()
    window.present({'message': '新回答', 'choices': []})
    control.selectItem(window.block_starts[2])
    conversation_module.xbmc.getInfoLabel=lambda label:str(window.block_starts[2]+1)
    window.onClick(500)
    assert window.events.get_nowait() == ('text', '选择推荐')
    assert len(window.history) == 4


def test_hidden_window_reopens_but_explicit_close_or_back_never_reopens(conversation_module, monkeypatch):
    window = conversation_module.ConversationWindow()
    window.show()
    window.append_message('user', '保留')
    monkeypatch.setattr(conversation_module.xbmc, 'getCondVisibility', lambda condition: window.visible)
    window.visible = False
    window.refresh()
    assert window.visible and len(window.items) == 1 and window.getControl(500).size() == 3
    window.onAction(SimpleNamespace(getId=lambda: 92))
    window.visible = False
    window.refresh()
    assert not window.visible
    window.cancelled.clear()
    window.close()
    window.refresh()
    assert not window.visible


def test_busy_window_accepts_supplements_but_does_not_activate_media(conversation_module):
    window = conversation_module.ConversationWindow()
    window.busy = window.accepting_input = True
    window.onClick(400)
    window.onClick(401)
    window.onClick(500)
    assert window.events.get_nowait() == ('voice', '')
    assert window.events.get_nowait() == ('keyboard', '')
    assert window.events.empty()
    window.recording = True
    window.onClick(400)
    assert window.finish_requested.is_set() and window.events.empty()


def test_restore_preserves_interleaved_supplements_without_duplicate_work(conversation_module):
    window = conversation_module.ConversationWindow()
    window.restore([{'user': '原问题', 'activity': ['步骤'], 'events': [
        {'role':'activity','text':'步骤'}, {'role':'user','text':'补充'},
        {'role':'assistant','text':'中间回答'}], 'answer': {'message':'最终回答','choices':[]}}])
    assert window.history == [('user','原问题'), ('user','补充'), ('assistant','中间回答'), ('assistant','最终回答')]
    assert len(window.items) == 5


def test_stop_task_is_distinct_from_exit_and_survives_reload(conversation_module):
    window = conversation_module.ConversationWindow()
    window.show()
    window.busy = window.task_active = True
    window.onClick(403)
    window.onInit()
    progress = conversation_module.ConversationProgress(window)
    assert window.task_stop.is_set() and not window.cancelled.is_set()
    assert progress.task_stopped() and progress.iscanceled()
    window.task_active = False
    window.task_stop.clear()
    window.onClick(403)
    assert not window.task_stop.is_set()
    window.onClick(402)
    assert window.cancelled.is_set() and not progress.task_stopped()


def test_question_choices_work_while_busy_but_old_choices_never_become_commands(conversation_module):
    window=conversation_module.ConversationWindow();window.busy=window.accepting_input=True
    window.show_question('q-token',{'options':[{'label':'蓝色','description':'冷色'}]})
    conversation_module.xbmc.getInfoLabel=lambda label:'1'
    window.onClick(500)
    assert window.events.get_nowait()==('answer',{'question':'q-token','text':'蓝色'})
    window.clear_question();window.busy=False;window.onClick(500)
    assert window.events.empty()


def test_whole_messages_grow_with_content_without_repeating_headers(conversation_module):
    window=conversation_module.ConversationWindow();window.show()
    short='一句话。';long='完整的长回复 English 内容。'*100+'\n\n结束。'
    window.append_message('assistant',short)
    window.append_message('assistant',long)
    assert len(window.items)==2 and window.history==[('assistant',short),('assistant',long)]
    visual=window.getControl(500).items
    assert [i.label for i in visual if i.label]==['AI','AI']
    first=[i for i in visual if i.properties['block']=='0']
    second=[i for i in visual if i.properties['block']=='1']
    assert len(first)==3 and len(second)>30
    body=''.join(i.properties['body'] for i in second if i.properties['part']=='body')
    assert body==long.replace('\n','')
    assert all('（续）' not in i.label for i in visual)
    line=window.block_starts[1]+20
    conversation_module.xbmc.getInfoLabel=lambda label:str(line+1)
    window.refresh();window.onInit()
    assert window.getControl(500).selected==line  # Skin reload preserves position within a tall reply.


def test_each_visible_line_of_recommendation_selects_the_same_item(conversation_module):
    window=conversation_module.ConversationWindow();window.show()
    window.present({'message':'详细介绍。'*100,'choices':[{'label':'电影','value':'看电影'}]})
    for offset in range(3):
        selected=window.block_starts[1]+offset
        conversation_module.xbmc.getInfoLabel=lambda label:str(selected+1)
        window.onClick(500)
        assert window.events.get_nowait()==('text','看电影')


def test_direction_keys_skip_spacers_and_leave_last_message(conversation_module):
    window=conversation_module.ConversationWindow();window.show()
    window.present({'message':'第一段','choices':[]})
    window.append_message('assistant','第二段')
    control=window.getControl(500)
    conversation_module.xbmc.getInfoLabel=lambda label:'0' if label.endswith('.Position') else '3'
    window.onAction(SimpleNamespace(getId=lambda:4))
    assert control.selected==4
    conversation_module.xbmc.getInfoLabel=lambda label:'0' if label.endswith('.Position') else '3'
    window.onAction(SimpleNamespace(getId=lambda:3))
    assert control.selected==0
    conversation_module.xbmc.getInfoLabel=lambda label:'0' if label.endswith('.Position') else '6'
    window.onAction(SimpleNamespace(getId=lambda:4))
    assert window.focus==400
    assert window.events.empty() and not window.cancelled.is_set()


def test_remote_focus_survives_skin_reload(conversation_module):
    window=conversation_module.ConversationWindow();window.show()
    window.present({'message':'回复','choices':[]})
    conversation_module.xbmc.getCondVisibility=lambda condition: condition == 'Control.HasFocus(401)' or condition.startswith('Window.IsVisible(')
    window.refresh()
    window.onInit()
    assert window.focus==401


def test_message_navigation_skips_visible_lines_and_pages_hidden_text():
    # Short, six-line, short: the middle reply takes one step, not six.
    starts=[0,3,11]
    assert message_page(starts,14,1,0,-1)==(0,1)
    assert message_page(starts,14,1,0,1)==(11,12)
    assert message_page(starts,14,2,3,1) is None
    # A 40-line answer remains readable in overlapping pages in either direction.
    starts=[0,3,44]
    assert message_page(starts,48,1,3,1)==(12,23)
    assert message_page(starts,48,1,12,-1)==(3,14)
    assert message_page(starts,48,1,31,1)==(44,46)
    assert message_page(starts,48,1,3,-1)==(0,1)
    assert message_page(starts,48,1,0,-1,entering=True)==(31,42)
