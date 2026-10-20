from pathlib import Path

import pytest

from legacy import engines
from legacy.ncnn_recognizer import MODELS, model_directory


def test_default_uses_medium_model_and_custom_model_overrides_choice():
    assert model_directory('/addon').name == MODELS['balanced']
    assert model_directory('/addon', '/custom', 'accurate') == Path('/custom')
    assert model_directory('/addon', model_quality='fast').name == MODELS['fast']
    with pytest.raises(ValueError):
        model_directory('/addon', model_quality='unknown')


def test_engine_selection_forwards_quality_without_changing_vosk(monkeypatch):
    monkeypatch.setattr(engines, 'NcnnRecognizer', lambda *args, **kwargs: (args, kwargs))
    monkeypatch.setattr(engines, 'VoskRecognizer', lambda *args: args)
    args, kwargs = engines.create_recognizer('/addon', model_quality='accurate')
    assert args == ('/addon', '', 'modified_beam_search')
    assert kwargs['model_quality'] == 'accurate'
    assert engines.create_recognizer('/addon', '/vosk', 'vosk', model_quality='accurate') == ('/addon', '/vosk')


def test_hotwords_enable_beam_search_and_canonical_names(monkeypatch):
    monkeypatch.setattr(engines, 'NcnnRecognizer', lambda *args, **kwargs: (args, kwargs))
    args, kwargs = engines.create_recognizer('/addon', decoding_method='greedy_search',
        hotwords_file='/words.txt', hotwords_score=0.5, hotword_entries=[('KODI', 'Kodi')])
    assert args[-1] == 'modified_beam_search'
    assert kwargs['hotwords_file'] == '/words.txt'
    assert kwargs['hotwords_score'] == 0.5
    assert kwargs['format_text']('打开 KODI') == '打开 Kodi'


def test_doubao_selection_ignores_local_hotwords_and_model_options(monkeypatch):
    monkeypatch.setattr(engines, 'DoubaoRecognizer', lambda *args: args)
    cancel = lambda: False
    args = engines.create_recognizer('/addon', engine='doubao', profile='/private',
        hotwords_file='/must-not-read', hotword_entries=[('扣的', 'Kodi')],
        doubao_resource_id='volc.seedasr.sauc.duration', doubao_timeout=15, cancelled=cancel)
    assert args == ('/addon', '/private', 'volc.seedasr.sauc.duration', 15, cancel)
