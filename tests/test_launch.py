from resources.lib.data_paths import data_path
import fcntl
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace


def test_direct_launch_is_guidance_and_only_voice_argument_starts_capture(tmp_path, monkeypatch):
    messages=[];captures=[];properties={};removals=[]
    old_home=tmp_path/'codex-home';old_home.mkdir()
    (old_home/'auth.json').write_text('retained-upgrade-login')
    addon=SimpleNamespace(getAddonInfo=lambda name:str(tmp_path),getSetting=lambda name:'usb' if name=='voice_input' else '')
    home=SimpleNamespace(getProperty=lambda name:properties.get(name,''),setProperty=lambda name,value:properties.update({name:value}))
    for name,module in {
        'xbmc':SimpleNamespace(log=lambda *a:None,LOGERROR=3,
                              Monitor=lambda:SimpleNamespace(abortRequested=lambda:False)),
        'xbmcgui':SimpleNamespace(Dialog=lambda:SimpleNamespace(ok=lambda *args:messages.append(args)),Window=lambda *a:home),
        'xbmcaddon':SimpleNamespace(Addon=lambda *a:addon),
        'xbmcvfs':SimpleNamespace(translatePath=lambda path:path),
    }.items():monkeypatch.setitem(sys.modules,name,module)
    path=Path(__file__).resolve().parents[1]/'resources/lib/application.py'
    spec=importlib.util.spec_from_file_location('resources.lib.application_under_test',path)
    app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
    app.conversation=lambda *a:captures.append(True)
    monkeypatch.setitem(sys.modules,'resources.lib.ui.key_binding',SimpleNamespace(remove=lambda addon:removals.append(addon)))
    monkeypatch.setitem(sys.modules,'resources.lib.application',app)
    spec=importlib.util.spec_from_file_location('entry_under_test',path.parents[2]/'default.py')
    entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
    for launch in (app.main,entry.guarded_main):
        monkeypatch.setattr(sys,'argv',['default.py'])
        launch();assert not captures and '绑定' in messages[-1][1]
        monkeypatch.setattr(sys,'argv',['default.py','--unknown'])
        launch();assert not captures
        monkeypatch.setattr(sys,'argv',['default.py','--unbind-voice-key'])
        launch();assert removals==[addon];removals.clear()
        assert data_path(tmp_path,'codex-home/auth.json').read_text()=='retained-upgrade-login'
        assert not old_home.exists()
        monkeypatch.setattr(sys,'argv',['default.py','--voice'])
        launch();assert captures==[True];captures.clear()
        properties['VoiceAssistant.State']='conversation'
        with (data_path(tmp_path, 'invocation.lock')).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            monkeypatch.setattr(sys,'argv',['default.py'])
            launch();assert 'VoiceAssistant.USBRecordRequested' not in properties
            monkeypatch.setattr(sys,'argv',['default.py','--voice'])
            launch();assert properties.pop('VoiceAssistant.USBRecordRequested')
            monkeypatch.setattr(sys,'argv',['default.py','--unbind-voice-key'])
            launch();assert not removals
            if launch==entry.guarded_main:assert '请先关闭当前弹窗' in messages[-1][1]
