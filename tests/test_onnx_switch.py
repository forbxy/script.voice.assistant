from pathlib import Path
import sys
import time

from resources.lib.asr import service as module
from test_onnx_resident import PROGRAM, wait_for


def test_switch_during_preload_cancels_old_load_without_ten_second_backoff(tmp_path, monkeypatch):
    slow=tmp_path/'slow.py';slow.write_text('import time\ntime.sleep(30)\n')
    fast=tmp_path/'fast.py';fast.write_text(PROGRAM)
    monkeypatch.setattr(module,'command',lambda root,profile,model:[sys.executable,'-u',str(slow if model=='slow' else fast)])
    monkeypatch.setattr(module.Service,'available',lambda *args:True)
    monkeypatch.setattr(module,'catalog',lambda root:{'asr_models':{}})
    owner=module.Service(tmp_path,tmp_path,delay=0)
    try:
        owner.tick({'recognition_engine':'onnx','onnx_model':'slow'})
        wait_for(lambda:owner.status['state']=='loading')
        owner.tick({'recognition_engine':'onnx','onnx_model':'fast'})
        wait_for(lambda:owner.loaded_model=='fast' and owner.status['state']=='ready')
        assert owner.status['state']=='ready'
    finally:owner.close()
