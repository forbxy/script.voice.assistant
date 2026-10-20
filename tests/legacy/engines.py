"""Keep backend selection separate from capture, streaming and the Kodi UI."""
from .ncnn_recognizer import NcnnRecognizer
from .recognizer import VoskRecognizer
from .hotwords import text_formatter
from resources.lib.doubao_config import DEFAULT_RESOURCE
from resources.lib.doubao_recognizer import DoubaoRecognizer
from resources.lib.codex_dictation_recognizer import CodexDictationRecognizer


def create_recognizer(root, model_path='', engine='ncnn', decoding_method='modified_beam_search',
                      model_quality='balanced', hotwords_file='', hotwords_score=1.0, hotword_entries=(),
                      endpoint_silence=0.0, profile='', doubao_resource_id=DEFAULT_RESOURCE,
                      doubao_timeout=20, cancelled=lambda: False, codex_dictation_timeout=30, codex_proxy=''):
    if engine == 'codex':
        return CodexDictationRecognizer(root, profile, codex_dictation_timeout, cancelled, codex_proxy)
    if engine == 'doubao':
        return DoubaoRecognizer(root, profile, doubao_resource_id, doubao_timeout, cancelled)
    if engine == 'vosk':
        return VoskRecognizer(root, model_path)
    if engine == 'ncnn':
        if hotwords_file:
            decoding_method = 'modified_beam_search'
        return NcnnRecognizer(root, model_path, decoding_method, model_quality=model_quality,
                              hotwords_file=hotwords_file, hotwords_score=hotwords_score,
                              format_text=text_formatter(hotword_entries), endpoint_silence=endpoint_silence)
    raise ValueError('未知语音识别引擎：' + str(engine))
