import queue
from types import SimpleNamespace
import pytest
from resources.lib.intelligence.questions import UserQuestions
from resources.lib.ui.questions import QuestionFlow


def question(identifier='q1', **extra):
    return dict(id=identifier, header='测试', question='选择颜色？', options=[{'label':'蓝色','description':'冷色'}], **extra)


class Window:
    def __init__(self): self.active=''; self.messages=[]
    def show_question(self, token, q): self.active=token
    def clear_question(self): self.active=''
    def activity(self, text): self.messages.append(text)


def test_multiple_answers_keep_ids_expire_old_choices_and_mask_hidden_history():
    request=UserQuestions({'questions':[question(),question('q2',isSecret=True)],'isBlocking':True})
    requests=queue.Queue();requests.put(request);window=Window();history=[]
    flow=QuestionFlow(requests,window,lambda *args:history.append(args));flow.tick();first=flow.token
    assert not request.ready.is_set()
    assert flow.answer({'question':first,'text':'蓝色'},first)
    assert not request.ready.is_set() and flow.token!=first
    assert not flow.answer({'question':first,'text':'过期点击'},flow.token)
    flow.answer('private-test-value',flow.token)
    assert request.ready.is_set() and request.answers=={'q1':{'answers':['蓝色']},'q2':{'answers':['private-test-value']}}
    assert 'private-test-value' not in repr(history) and window.active==''


def test_optional_question_expires_without_fabricating_answer():
    request=UserQuestions({'questions':[question()],'isBlocking':False})
    requests=queue.Queue();requests.put(request);window=Window();history=[]
    flow=QuestionFlow(requests,window,lambda *args:history.append(args));flow.tick()
    token=flow.token;request.expired.set()
    assert flow.answer('迟到答案',token)
    assert not request.answers and all(row[0]!='user' for row in history)
    assert not flow.token


def test_duplicate_question_ids_rejected():
    with pytest.raises(ValueError,match='编号'):
        UserQuestions({'questions':[question(),question()]})
