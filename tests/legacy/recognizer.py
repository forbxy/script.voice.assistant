"""Small ctypes adapter for Vosk's public C API; no pip/cffi dependency."""
import ctypes
import json
from pathlib import Path
import re


def display_text(text):
    # Vosk inserts token spaces between Chinese words. Keep Latin word spacing.
    return re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', text.strip())


class VoskRecognizer:
    def __init__(self, addon_root, model_path=''):
        root = Path(addon_root)
        library = root / 'resources/native/linux_armv7l/libvosk.so'
        model = Path(model_path) if model_path else root / 'resources/models/vosk-model-small-cn-0.22'
        if ctypes.sizeof(ctypes.c_void_p) != 4:
            raise RuntimeError('当前离线识别库适用于 CoreELEC ARM32 用户态。此设备需要对应架构的 Vosk 库。')
        if not library.is_file() or not (model / 'am/final.mdl').is_file():
            raise RuntimeError('离线识别库或中文模型未安装，请安装包含模型的完整插件包。')
        try:
            self.lib = ctypes.CDLL(str(library))
        except OSError as exc:
            raise RuntimeError('无法加载 Vosk 离线识别库：{}'.format(exc)) from exc
        signatures = {
            'vosk_set_log_level': ([ctypes.c_int], None),
            'vosk_model_new': ([ctypes.c_char_p], ctypes.c_void_p),
            'vosk_model_free': ([ctypes.c_void_p], None),
            'vosk_recognizer_new': ([ctypes.c_void_p, ctypes.c_float], ctypes.c_void_p),
            'vosk_recognizer_free': ([ctypes.c_void_p], None),
            'vosk_recognizer_accept_waveform': ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
            'vosk_recognizer_result': ([ctypes.c_void_p], ctypes.c_char_p),
            'vosk_recognizer_partial_result': ([ctypes.c_void_p], ctypes.c_char_p),
            'vosk_recognizer_final_result': ([ctypes.c_void_p], ctypes.c_char_p),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result
        self.model = self.recognizer = None
        self.parts = []
        self.lib.vosk_set_log_level(-1)
        self.model = self.lib.vosk_model_new(str(model).encode())
        if not self.model:
            raise RuntimeError('Vosk 无法加载中文模型，请检查模型是否完整及可用内存。')
        self.recognizer = self.lib.vosk_recognizer_new(self.model, 16000.0)
        if not self.recognizer:
            self.close()
            raise RuntimeError('Vosk 无法创建中文识别器。')

    def accept(self, pcm):
        """Feed an audio block and return revisable text for immediate display."""
        if len(pcm) % 2:
            raise ValueError('PCM must contain complete S16_LE samples')
        accepted = self.lib.vosk_recognizer_accept_waveform(self.recognizer, pcm, len(pcm))
        if accepted < 0:
            raise RuntimeError('Vosk 解码音频失败。')
        if accepted:
            self.parts.append(json.loads(self.lib.vosk_recognizer_result(self.recognizer))['text'])
            partial = ''
        else:
            partial = json.loads(self.lib.vosk_recognizer_partial_result(self.recognizer))['partial']
        return display_text(' '.join(part for part in self.parts + [partial] if part))

    def finish(self):
        self.parts.append(json.loads(self.lib.vosk_recognizer_final_result(self.recognizer))['text'])
        return display_text(' '.join(part for part in self.parts if part))

    def transcribe(self, pcm, cancelled=lambda: False, update=lambda *args: None):
        for offset in range(0, len(pcm), 8000):
            if cancelled():
                return ''
            chunk = pcm[offset:offset + 8000]
            self.accept(chunk)
            update(min(100, int((offset + len(chunk)) * 100 / len(pcm))))
        if cancelled():
            return ''
        return self.finish()

    def close(self):
        if self.recognizer:
            self.lib.vosk_recognizer_free(self.recognizer)
            self.recognizer = None
        if self.model:
            self.lib.vosk_model_free(self.model)
            self.model = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
